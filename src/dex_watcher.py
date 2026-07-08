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
DISCORD_WEBHOOK     = os.getenv("DEX_UPDATES_DISCORD_WEBHOOK", "")
POLL_SECONDS        = int(os.getenv("DEX_WATCHER_POLL_SECONDS", "30"))
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


# pump.fun API — used as a fallback when Dexscreener doesn't populate
# `pairCreatedAt` (common for bonding-curve tokens). Cache successful lookups
# for the process lifetime — token creation times don't change.
PUMPFUN_URL = "https://frontend-api-v3.pump.fun/coins/{address}"
_pumpfun_cache: dict = {}


async def _fetch_pumpfun_created(session: aiohttp.ClientSession, address: str) -> int:
    """Return the pump.fun creation timestamp in ms, or 0 if unavailable.
    Silent on 404 (not a pump.fun mint) — those are expected for non-pumpfun tokens."""
    cached = _pumpfun_cache.get(address)
    if cached:
        return cached
    try:
        async with session.get(
            PUMPFUN_URL.format(address=address),
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                return 0
            data = await resp.json()
        ts = int(data.get("created_timestamp") or 0)
        if ts:
            _pumpfun_cache[address] = ts
        return ts
    except Exception as e:
        logger.warning(f"dex_watcher: pump.fun fetch failed for {address}: {e}")
        return 0


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
        # For age, use the oldest pool across all pools — not the highest-liquidity one.
        # A 289-day-old token can have a fresh pool spun up (migration, CTO revival)
        # that would make the token look artificially young if we used its timestamp.
        created_times = [int(p.get("pairCreatedAt") or 0) for p in pairs if p.get("pairCreatedAt")]
        oldest_pair_ms = min(created_times) if created_times else int(best.get("pairCreatedAt") or 0)

        # Fallback: bonding-curve pump.fun pairs often have no pairCreatedAt.
        # Ask pump.fun directly. This covers pre-graduation tokens and post-
        # graduation tokens whose Dex metadata is incomplete.
        if oldest_pair_ms == 0:
            oldest_pair_ms = await _fetch_pumpfun_created(session, address)

        return {
            "symbol":         (best.get("baseToken") or {}).get("symbol") or "?",
            "market_cap":     float(best.get("marketCap") or 0),
            "fdv":            float(best.get("fdv") or 0),
            "liquidity_usd":  float((best.get("liquidity") or {}).get("usd") or 0),
            "pair_created_ms": oldest_pair_ms,
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


def _format_discord_embed(profile: dict, market: Optional[dict], event_type: str) -> dict:
    """Build a Discord embed dict for the same event that Telegram gets. Uses
    Discord's native embed formatting — no Markdown escaping here since embeds
    aren't parsed as Telegram legacy-Markdown."""
    address = profile.get("tokenAddress", "")
    symbol  = (market or {}).get("symbol") or "?"
    mcap    = (market or {}).get("market_cap") or (market or {}).get("fdv") or 0
    liq     = (market or {}).get("liquidity_usd") or 0
    created = (market or {}).get("pair_created_ms") or 0

    is_cto = event_type == "cto"
    title  = "🔁 Community Takeover Claimed" if is_cto else "🆕 Paid Dexscreener Update Detected"
    color  = 0x8B5CF6 if is_cto else 0x22C55E  # purple for CTO, green for profile

    lines = [
        f"**Token:** ${symbol}",
        f"**CA:** `{address}`",
        f"**Market Cap:** {_fmt_usd(mcap)}",
        f"**Liquidity:** {_fmt_usd(liq)}",
        f"**Token Age:** {_fmt_age(created)}",
    ]
    if is_cto and profile.get("claimDate"):
        lines.append(f"**Claimed:** {profile['claimDate']}")

    description = (profile.get("description") or "").strip()
    if description:
        lines.append("")
        lines.append(f"*{description[:300]}*")

    links = profile.get("links") or []
    link_lines = []
    for l in links:
        url = l.get("url")
        if not url:
            continue
        label = l.get("label") or l.get("type") or "link"
        link_lines.append(f"• [{label}]({url})")
    if link_lines:
        lines.append("")
        lines.extend(link_lines)

    embed = {
        "title": title,
        "description": "\n".join(lines)[:4096],
        "url": f"https://dexscreener.com/solana/{address}",
        "color": color,
    }
    header_url = profile.get("header")
    if header_url:
        embed["image"] = {"url": header_url}
    return embed


def _webhook_wait_url(url: str) -> str:
    """Append wait=true so Discord returns the created message JSON (needed to
    capture the message_id for milestone replies)."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}wait=true"


async def _notify_discord(embed: dict) -> Optional[str]:
    """Post to the Discord webhook. Returns the created message_id on success, or
    None. Silent no-op if DEX_UPDATES_DISCORD_WEBHOOK isn't set. Logs on failure
    but never raises."""
    if not DISCORD_WEBHOOK:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                _webhook_wait_url(DISCORD_WEBHOOK),
                json={"username": "Dex Updates", "embeds": [embed]},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if resp.status not in (200, 204):
                body = await resp.text()
                logger.warning(f"dex_watcher: Discord webhook returned {resp.status}: {body[:200]}")
                return None
            if resp.status == 204:
                return None  # shouldn't happen with wait=true, but guard anyway
            data = await resp.json()
            return str(data.get("id")) if data.get("id") else None
    except Exception as e:
        logger.warning(f"dex_watcher: Discord webhook failed: {e}")
        return None


async def _send_telegram_alert(caption: str, image_url: str) -> Optional[int]:
    """Send the alert to the Telegram dex updates channel. Returns the created
    message_id on success, or None. Uses sendPhoto when a banner URL is present,
    falls back to sendMessage if the photo request fails or no banner exists."""
    if not (os.getenv("TELEGRAM_BOT_TOKEN") and CHANNEL_ID):
        return None
    telegram_api = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN')}"
    try:
        async with aiohttp.ClientSession() as session:
            if image_url:
                resp = await session.post(
                    f"{telegram_api}/sendPhoto",
                    json={
                        "chat_id": CHANNEL_ID,
                        "photo": image_url,
                        "caption": caption[:1024],
                        "parse_mode": "Markdown",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                )
                data = await resp.json()
                if data.get("ok"):
                    return int(data["result"]["message_id"])
                # Photo failed (bad URL, unreachable CDN). Fall back to text.
                logger.info(f"dex_watcher: sendPhoto failed ({data.get('description')}), falling back to text")

            resp = await session.post(
                f"{telegram_api}/sendMessage",
                json={
                    "chat_id": CHANNEL_ID,
                    "text": caption[:4096],
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            )
            data = await resp.json()
            if data.get("ok"):
                return int(data["result"]["message_id"])
            logger.warning(f"dex_watcher: sendMessage not ok: {data}")
            return None
    except Exception as e:
        logger.warning(f"dex_watcher: Telegram send failed: {e}")
        return None


async def _send_alert(profile: dict, market: Optional[dict], event_type: str) -> bool:
    """Send the alert to Telegram (and Discord if configured), capture message IDs,
    and register the token with the milestone tracker. Returns True if the
    Telegram send succeeded (used to decide whether to mark seen)."""
    caption    = _format_alert(profile, market, event_type)
    header_url = profile.get("header") or ""
    address    = profile.get("tokenAddress", "")

    market_dict = market or {}
    initial_mc = float(market_dict.get("market_cap") or market_dict.get("fdv") or 0)
    symbol     = market_dict.get("symbol") or ""

    # Fire Telegram + Discord in parallel — matched to their existing sync semantics.
    tg_task = asyncio.create_task(_send_telegram_alert(caption, header_url))
    dc_task = asyncio.create_task(_notify_discord(_format_discord_embed(profile, market, event_type)))
    tg_msg_id, dc_msg_id = await asyncio.gather(tg_task, dc_task)

    if tg_msg_id is None and dc_msg_id is None:
        # Nothing landed anywhere — let the caller retry on the next poll.
        return False

    # Register with milestone tracker if we have valid initial state + at least
    # one message to reply to.
    try:
        from .dex_milestone_tracker import register_token
        register_token(
            address=address,
            initial_mc=initial_mc,
            ticker=symbol,
            name=(market_dict.get("name") or ""),
            tg_message_id=tg_msg_id,
            dc_message_id=dc_msg_id,
        )
    except Exception as e:
        logger.warning(f"dex_watcher: milestone register failed for {address}: {e}")

    return tg_msg_id is not None


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
            # Don't mark seen — pair data may become available on a later poll.
            logger.info(f"dex_watcher: skip {address} ({event_type}) — no age data")
            continue
        if age_h < MIN_AGE_HOURS:
            # Don't mark seen — the token will age past the threshold eventually
            # and we want it to fire when it does (e.g. later paid update at 24h+).
            logger.info(f"dex_watcher: skip {address} ({event_type}) — {age_h:.1f}h < {MIN_AGE_HOURS}h")
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
        f"min_age={MIN_AGE_HOURS}h, discord={'on' if DISCORD_WEBHOOK else 'off'}, "
        f"seen_size={len(_seen)}"
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
