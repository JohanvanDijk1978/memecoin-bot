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
