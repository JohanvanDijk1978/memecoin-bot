"""
utils.py
────────
Shared helpers used across the bot: Markdown escaping for Telegram legacy
Markdown, USD/age formatting, and a global rate limiter for Dexscreener
so every module hits the free-tier ceiling from a single shared budget
instead of racing each other into 429s.
"""

import time
import asyncio
from typing import Optional


# ── Markdown escaping ─────────────────────────────────────────────────────
def escape_md(s) -> str:
    """Escape Telegram legacy-Markdown special chars in dynamic strings.
    Used everywhere we drop user/API-supplied text into a caption or embed
    with parse_mode='Markdown'."""
    if not s:
        return ""
    s = str(s)
    for ch in ("\\", "_", "*", "`", "[", "]"):
        s = s.replace(ch, "\\" + ch)
    return s


# ── Number formatting ─────────────────────────────────────────────────────
def fmt_usd(n) -> str:
    """USD-with-suffix format: $1.2M / $340K / $87. Robust to None/nan/bad types."""
    if n is None or n == 0:
        return "n/a"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "n/a"
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n/1_000:.0f}K"
    return f"${n:.0f}"


# ── Age formatting ────────────────────────────────────────────────────────
def fmt_age(pair_created_ms: Optional[int]) -> str:
    """Human-readable age from a ms-epoch pair-created timestamp. Returns
    'unknown' if the timestamp is missing or in the future."""
    if not pair_created_ms:
        return "unknown"
    delta_secs = time.time() - (pair_created_ms / 1000)
    if delta_secs < 0:
        return "unknown"
    days, rem = divmod(int(delta_secs), 86400)
    hours = rem // 3600
    if days:
        return f"{days}d {hours}h"
    minutes = (rem % 3600) // 60
    return f"{hours}h {minutes}m"


def age_hours(pair_created_ms: Optional[int]) -> Optional[float]:
    """Fractional hours since pair creation. None if missing."""
    if not pair_created_ms:
        return None
    return (time.time() - pair_created_ms / 1000) / 3600.0


# ── Shared Dexscreener rate limiter ───────────────────────────────────────
class _RateLimiter:
    """Sliding-window limiter: at most `calls_per_minute` awaits pass through
    in any 60-second window. Serializes all callers via a single asyncio.Lock
    so bursts across modules don't cascade into 429s."""

    def __init__(self, calls_per_minute: int):
        self.interval = 60.0 / max(calls_per_minute, 1)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.time()
            elapsed = now - self._last
            if elapsed < self.interval:
                await asyncio.sleep(self.interval - elapsed)
            self._last = time.time()


# Dexscreener free-tier limits (per IP):
#   - latest/dex/tokens/*        : 300 req/min
#   - token-profiles / CTOs      :  60 req/min
# We use a single shared limiter at 250/min: leaves headroom under 300 for
# the high-volume token endpoint, and the low-volume feed calls (~4/min from
# the two watchers combined) consume a trivial share.
_dex_limiter = _RateLimiter(calls_per_minute=250)


async def dex_wait() -> None:
    """Call `await dex_wait()` immediately before any HTTP request to
    api.dexscreener.com. Safe to call from any module — it's a shared
    singleton that serializes all callers."""
    await _dex_limiter.wait()


# ── Chain-aware link + label helpers ──────────────────────────────────────
# Dexscreener's `chainId` string is the source of truth for what chain a token
# is on. Any `0x...` address might be Ethereum, BSC, Base, Robinhood, Arbitrum,
# etc. — indistinguishable by address alone. The CA scrapers should call
# `chain_display_name(token["chain_id"])` etc. to render correctly.

_CHAIN_DISPLAY = {
    "solana":    "Solana",
    "ethereum":  "Ethereum",
    "bsc":       "BNB Chain",
    "base":      "Base",
    "robinhood": "Robinhood",
    "arbitrum":  "Arbitrum",
    "polygon":   "Polygon",
    "avalanche": "Avalanche",
    "unichain":  "Unichain",
    "hyperevm":  "HyperEVM",
    "abstract":  "Abstract",
    "ink":       "Ink",
    "story":     "Story",
    "xlayer":    "X Layer",
    "plasma":    "Plasma",
    "monad":     "Monad",
    "megaeth":   "MegaETH",
    "tempo":     "Tempo",
}


def chain_display_name(chain_id: str) -> str:
    if not chain_id:
        return "Unknown"
    return _CHAIN_DISPLAY.get(chain_id.lower(), chain_id.title())


def basedbot_url(chain_id: str, address: str) -> str:
    """BasedBot web-app URL for the chain, or empty string if we don't know
    BasedBot's slug for the chain. Add new slugs here as they're confirmed."""
    slug = {
        "solana":    "sol",
        "robinhood": "robinhood",
        "ethereum":  "eth",
        "bsc":       "bnb",
        "base":      "base",
    }.get((chain_id or "").lower())
    if not slug:
        return ""
    return f"https://basedbot.app/token/{slug}/{address}"


def padre_url(chain_id: str, address: str) -> str:
    slug = {
        "solana":    "solana",
        "ethereum":  "eth",
        "bsc":       "bnb",
        "base":      "base",
        "robinhood": "robinhood",
    }.get((chain_id or "").lower())
    if not slug:
        return ""
    return f"https://trade.padre.gg/trade/{slug}/{address}"


def gmgn_url(chain_id: str, address: str) -> str:
    slug = {
        "solana":   "sol",
        "ethereum": "eth",
        "bsc":      "bsc",
        "base":     "base",
    }.get((chain_id or "").lower())
    if not slug:
        return ""
    return f"https://gmgn.ai/{slug}/token/{address}"


def dexscreener_url(chain_id: str, address: str) -> str:
    """Universal fallback — Dexscreener supports every chain in its feed."""
    return f"https://dexscreener.com/{(chain_id or 'solana').lower()}/{address}"


def build_trading_links(chain_id: str, address: str) -> str:
    """Return a ' | '-joined markdown string of trading-tool links appropriate
    for the chain. Always includes DexScreener as universal fallback. Silently
    omits tools that don't support the chain."""
    links = []
    if u := basedbot_url(chain_id, address):
        links.append(f"[BasedBot]({u})")
    if u := padre_url(chain_id, address):
        links.append(f"[Padre]({u})")
    if u := gmgn_url(chain_id, address):
        links.append(f"[GMGN]({u})")
    links.append(f"[DexScreener]({dexscreener_url(chain_id, address)})")
    return " | ".join(links)
