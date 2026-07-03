"""
dex_watcher.py
──────────────
Polls two Dexscreener paid-product feeds and alerts a dedicated Telegram
channel whenever a project pays for either:

  1. Token profile update (icon, banner, description, links)
     → https://api.dexscreener.com/token-profiles/latest/v1
  2. Community Takeover (CTO) claim
     → https://api.dexscreener.com/community-takeovers/latest/v1

Only Solana tokens older than DEX_WATCHER_MIN_AGE_HOURS (default 24) are alerted.
Every qualifying token is alerted — no filter by whether the token was seen
by the group scrapers.

Design notes:
- Async loop, sits in main.py's asyncio.gather alongside run_cleanup_loop.
- Silent no-op when DEX_UPDATES_CHANNEL_ID isn't set — lets the module load
  before the .env is updated.
- Persistent dedup at data/dex_watcher_seen.json with 30-day TTL prune on load.
- Uses shared send_ping / send_media_group helpers — no Bot API URL logic here.
"""

import os
import json
import time
import asyncio
import logging
from typing import Optional

import aiohttp
from dotenv import load_dotenv

from .send_ping import send_ping, send_media_group

load_dotenv()
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
CHANNEL_ID          = os.getenv("DEX_UPDATES_CHANNEL_ID", "")
POLL_SECONDS        = int(os.getenv("DEX_WATCHER_POLL_SECONDS", "45"))
MIN_AGE_HOURS       = float(os.getenv("DEX_WATCHER_MIN_AGE_HOURS", "24"))
INITIAL_DELAY_SECS  = 60  # warmup before first poll

# ── Endpoints ─────────────────────────────────────────────────────────────
PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
CTO_URL      = "https://api.dexscreener.com/community-takeovers/latest/v1"
TOKENS_URL   = "https://api.dexscreener.com/latest/dex/tokens/{address}"

# ── Persistence (mirrors filtered_forward.py pattern) ─────────────────────
SEEN_FILE     = "data/dex_watcher_seen.json"
SEEN_TTL_SECS = 30 * 86400


def _load_seen() -> dict:
    try:
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        return {}
    cutoff = time.time() - SEEN_TTL_SECS
    return {k: ts for k, ts in data.items()
            if isinstance(ts, (int, float)) and ts >= cutoff}


def _save_seen(seen: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
        with open(SEEN_FILE, "w") as f:
            json.dump(seen, f)
    except Exception as e:
        logger.warning(f"dex_watcher: failed to persist seen file: {e}")


_seen: dict = _load_seen()
_save_lock = asyncio.Lock()


def _escape_md(s) -> str:
    """Legacy-Markdown escape for dynamic content. Matches bot.py's escape_md."""
    if not s:
        return ""
    s = str(s)
    for ch in ("\\", "_", "*", "`", "[", "]"):
        s = s.replace(ch, "\\" + ch)
    return s


def _fmt_usd(n) -> str:
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


def _fmt_age(pair_created_ms: Optional[int]) -> str:
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


def _age_hours(pair_created_ms: Optional[int]) -> Optional[float]:
    if not pair_created_ms:
        return None
    return (time.time() - pair_created_ms / 1000) / 3600.0


# ── Dexscreener client ────────────────────────────────────────────────────
async def _fetch_feed(session: aiohttp.ClientSession, url: str) -> list:
    """GET a Dexscreener paid feed. Returns [] on any error (logged)."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.warning(f"dex_watcher: feed {url} returned {resp.status}")
                return []
            data = await resp.json()
            if isinstance(data, list):
                return data
            # Some endpoints return {"data": [...]}
            return data.get("data", []) or []
    except Exception as e:
        logger.warning(f"dex_watcher: feed fetch failed for {url}: {e}")
        return []


async def _fetch_pair_data(session: aiohttp.ClientSession, address: str) -> Optional[dict]:
    """Grab market data for a token (best pair by liquidity). Returns None on failure."""
    try:
        async with session.get(
            TOKENS_URL.format(address=address),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None
        best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
        return {
            "symbol":         (best.get("baseToken") or {}).get("symbol") or "?",
            "market_cap":     float(best.get("marketCap") or 0),
            "fdv":            float(best.get("fdv") or 0),
            "liquidity_usd":  float((best.get("liquidity") or {}).get("usd") or 0),
            "pair_created_ms": int(best.get("pairCreatedAt") or 0),
        }
    except Exception as e:
        logger.warning(f"dex_watcher: pair fetch failed for {address}: {e}")
        return None


# ── Alert formatting + sending ────────────────────────────────────────────
def _format_alert(profile: dict, market: Optional[dict], event_type: str) -> str:
    address = profile.get("tokenAddress", "")
    symbol  = _escape_md((market or {}).get("symbol") or "?")

    mcap  = (market or {}).get("market_cap") or (market or {}).get("fdv") or 0
    liq   = (market or {}).get("liquidity_usd") or 0
    created_ms = (market or {}).get("pair_created_ms") or 0

    header = ("🔁 *Community Takeover Claimed*"
              if event_type == "cto"
              else "🆕 *Paid Dexscreener Update Detected*")

    body = (
        f"{header}\n\n"
        f"*Token:* ${symbol}\n"
        f"*CA:* `{address}`\n"
        f"*Market Cap:* {_fmt_usd(mcap)}\n"
        f"*Liquidity:* {_fmt_usd(liq)}\n"
        f"*Token Age:* {_fmt_age(created_ms)}\n"
    )

    if event_type == "cto" and profile.get("claimDate"):
        body += f"*Claimed:* {_escape_md(profile['claimDate'])}\n"

    description = (profile.get("description") or "").strip()
    if description:
        body += f"\n_{_escape_md(description[:300])}_\n"

    links = profile.get("links") or []
    link_lines = []
    for l in links:
        url = l.get("url")
        if not url:
            continue
        label = _escape_md(l.get("label") or l.get("type") or "link")
        link_lines.append(f"• [{label}]({url})")
    if link_lines:
        body += "\n" + "\n".join(link_lines)

    body += f"\n\n[View on Dexscreener](https://dexscreener.com/solana/{address})"

    return body


async def _send_alert(profile: dict, market: Optional[dict], event_type: str) -> bool:
    """Returns True on successful send, False otherwise."""
    caption = _format_alert(profile, market, event_type)
    header_url = profile.get("header")
    icon_url   = profile.get("icon")

    photos = [u for u in (header_url, icon_url) if u]
    try:
        if len(photos) >= 2:
            await send_media_group(CHANNEL_ID, photos, caption=caption)
        elif len(photos) == 1:
            await send_ping(caption, image_url=photos[0], chat_id=CHANNEL_ID)
        else:
            await send_ping(caption, chat_id=CHANNEL_ID)
        return True
    except Exception as e:
        logger.warning(f"dex_watcher: alert send failed for {profile.get('tokenAddress')}: {e}")
        return False


# ── Per-poll processing ───────────────────────────────────────────────────
async def _process_feed(session: aiohttp.ClientSession, feed: list, event_type: str) -> int:
    """Handle one feed's contents, sending alerts for qualifying new entries.
    Returns count of alerts sent this batch."""
    sent = 0
    for profile in feed:
        if profile.get("chainId") != "solana":
            continue
        address = profile.get("tokenAddress")
        if not address:
            continue

        seen_key = f"{event_type}:{address}"
        if seen_key in _seen:
            continue

        market = await _fetch_pair_data(session, address)
        age_h = _age_hours((market or {}).get("pair_created_ms") if market else None)

        if age_h is None:
            logger.info(f"dex_watcher: skip {address} ({event_type}) — no age data")
            _seen[seen_key] = time.time()  # don't retry every 45s
            continue
        if age_h < MIN_AGE_HOURS:
            logger.info(f"dex_watcher: skip {address} ({event_type}) — {age_h:.1f}h < {MIN_AGE_HOURS}h")
            _seen[seen_key] = time.time()
            continue

        ok = await _send_alert(profile, market, event_type)
        if ok:
            _seen[seen_key] = time.time()
            sent += 1
            logger.info(f"dex_watcher: alerted {address} ({event_type}) age={age_h:.1f}h")

        # Polite pacing between per-token enrichment + send cycles
        await asyncio.sleep(1)

    return sent


# ── Public entry point ────────────────────────────────────────────────────
async def run_dex_watcher() -> None:
    """Async loop for main.py's asyncio.gather. Silent no-op when
    DEX_UPDATES_CHANNEL_ID isn't configured."""
    if not CHANNEL_ID:
        logger.info("dex_watcher: DEX_UPDATES_CHANNEL_ID not set — feature disabled")
        return

    logger.info(
        f"📡 dex_watcher armed — poll={POLL_SECONDS}s, "
        f"min_age={MIN_AGE_HOURS}h, seen_size={len(_seen)}"
    )

    await asyncio.sleep(INITIAL_DELAY_SECS)

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                profiles = await _fetch_feed(session, PROFILES_URL)
                p_sent   = await _process_feed(session, profiles, "profile_update")
                ctos     = await _fetch_feed(session, CTO_URL)
                c_sent   = await _process_feed(session, ctos, "cto")

            if p_sent or c_sent:
                async with _save_lock:
                    _save_seen(_seen)
                logger.info(f"dex_watcher: sent {p_sent} profile + {c_sent} cto alerts")
        except Exception as e:
            logger.warning(f"dex_watcher: iteration failed: {e}")

        await asyncio.sleep(POLL_SECONDS)
