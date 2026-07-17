"""
dex_watcher_evm.py
──────────────────
EVM-chain sibling of dex_watcher.py. Polls the same Dexscreener paid-product
feeds (token-profiles + community-takeovers) but filters to EVM chains
(Ethereum, BSC, Robinhood by default) and alerts a separate Telegram channel
(and Discord webhook, if configured).

Differences from dex_watcher.py:
- Chain filter: `DEX_WATCHER_EVM_CHAINS` env var, comma-separated Dexscreener
  chainId strings. Default: "ethereum,bsc,robinhood".
- No age filter by default (`DEX_WATCHER_EVM_MIN_AGE_HOURS=0`). EVM projects
  paying for boosts are usually established; the >24h Solana filter that
  screens out fresh pump.fun spam doesn't apply.
- No pump.fun fallback — bonding-curve tokens are Solana-only.
- No milestone_tracker integration in v1 — can be wired later if useful.
- Independent seen set at `data/dex_watcher_evm_seen.json`.

Silent no-op when DEX_UPDATES_EVM_CHANNEL_ID isn't set — the feature stays
dormant until the env is populated.
"""

import os
import json
import time
import asyncio
import logging
from typing import Optional

import aiohttp
from dotenv import load_dotenv

# NOTE: EVM watcher talks to the Bot API directly (see _send_telegram_alert
# below) so it can capture message_id for milestone_tracker replies. No
# send_ping import needed.
from .utils import escape_md as _escape_md, fmt_usd as _fmt_usd, fmt_age as _fmt_age, age_hours as _age_hours, dex_wait

load_dotenv()
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
CHANNEL_ID          = os.getenv("DEX_UPDATES_EVM_CHANNEL_ID", "")
DISCORD_WEBHOOK     = os.getenv("DEX_UPDATES_EVM_DISCORD_WEBHOOK", "")
POLL_SECONDS        = int(os.getenv("DEX_WATCHER_EVM_POLL_SECONDS", "30"))
MIN_AGE_HOURS       = float(os.getenv("DEX_WATCHER_EVM_MIN_AGE_HOURS", "12"))
INITIAL_DELAY_SECS  = 60

_CHAINS_RAW = os.getenv("DEX_WATCHER_EVM_CHAINS", "ethereum,bsc,robinhood")
CHAINS = {c.strip().lower() for c in _CHAINS_RAW.split(",") if c.strip()}

# ── Endpoints ─────────────────────────────────────────────────────────────
PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
CTO_URL      = "https://api.dexscreener.com/community-takeovers/latest/v1"
TOKENS_URL   = "https://api.dexscreener.com/latest/dex/tokens/{address}"

# ── Persistence ───────────────────────────────────────────────────────────
SEEN_FILE     = "data/dex_watcher_evm_seen.json"
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
        logger.warning(f"dex_watcher_evm: failed to persist seen file: {e}")


_seen: dict = _load_seen()
_save_lock = asyncio.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────
# escape_md / fmt_usd / fmt_age / age_hours all live in src/utils.py now and
# are imported at the top of this module.


def _chain_pretty(chain: str) -> str:
    """Human-readable label for the chain badge in alerts."""
    return {
        "ethereum": "Ethereum",
        "bsc":      "BNB Chain",
        "robinhood": "Robinhood",
    }.get(chain, chain.title())


# ── Dexscreener client ────────────────────────────────────────────────────
async def _fetch_feed(session: aiohttp.ClientSession, url: str) -> list:
    try:
        await dex_wait()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                logger.warning(f"dex_watcher_evm: feed {url} returned {resp.status}")
                return []
            data = await resp.json()
            if isinstance(data, list):
                return data
            return data.get("data", []) or []
    except Exception as e:
        logger.warning(f"dex_watcher_evm: feed fetch failed for {url}: {e}")
        return []


async def _fetch_pair_data(session: aiohttp.ClientSession, address: str) -> Optional[dict]:
    """Best-liquidity pair market data. Returns None on failure."""
    try:
        await dex_wait()
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
        # Age: use oldest pool across all pools so a fresh listing on an old
        # token doesn't reset apparent age.
        created_times = [int(p.get("pairCreatedAt") or 0) for p in pairs if p.get("pairCreatedAt")]
        oldest_pair_ms = min(created_times) if created_times else int(best.get("pairCreatedAt") or 0)
        return {
            "symbol":          (best.get("baseToken") or {}).get("symbol") or "?",
            "name":            (best.get("baseToken") or {}).get("name") or "",
            "market_cap":      float(best.get("marketCap") or 0),
            "fdv":             float(best.get("fdv") or 0),
            "liquidity_usd":   float((best.get("liquidity") or {}).get("usd") or 0),
            "pair_created_ms": oldest_pair_ms,
        }
    except Exception as e:
        logger.warning(f"dex_watcher_evm: pair fetch failed for {address}: {e}")
        return None


# ── Alert formatting ──────────────────────────────────────────────────────
def _format_alert_tg(profile: dict, market: Optional[dict], event_type: str, chain: str) -> str:
    address = profile.get("tokenAddress", "")
    symbol  = _escape_md((market or {}).get("symbol") or "?")
    mcap    = (market or {}).get("market_cap") or (market or {}).get("fdv") or 0
    liq     = (market or {}).get("liquidity_usd") or 0
    created = (market or {}).get("pair_created_ms") or 0

    is_cto = event_type == "cto"
    header = ("🔁 *Community Takeover Claimed*"
              if is_cto
              else "🆕 *Paid Dexscreener Update Detected*")

    body = (
        f"{header}\n\n"
        f"*Chain:* {_escape_md(_chain_pretty(chain))}\n"
        f"*Token:* ${symbol}\n"
        f"*CA:* `{address}`\n"
        f"*Market Cap:* {_fmt_usd(mcap)}\n"
        f"*Liquidity:* {_fmt_usd(liq)}\n"
        f"*Token Age:* {_fmt_age(created)}\n"
    )

    if is_cto and profile.get("claimDate"):
        body += f"*Claimed:* {_escape_md(profile['claimDate'])}\n"

    description = (profile.get("description") or "").strip()
    if description:
        # See dex_watcher.py note: no italic wrap on user-supplied text because
        # Telegram legacy Markdown doesn't honor `\_` escapes inside entities.
        desc_flat = " ".join(description.split())
        body += f"\n{_escape_md(desc_flat[:300])}\n"

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

    body += f"\n\n[View on Dexscreener](https://dexscreener.com/{chain}/{address})"
    return body


def _format_discord_embed(profile: dict, market: Optional[dict], event_type: str, chain: str) -> dict:
    address = profile.get("tokenAddress", "")
    symbol  = (market or {}).get("symbol") or "?"
    mcap    = (market or {}).get("market_cap") or (market or {}).get("fdv") or 0
    liq     = (market or {}).get("liquidity_usd") or 0
    created = (market or {}).get("pair_created_ms") or 0

    is_cto = event_type == "cto"
    title  = "🔁 Community Takeover Claimed" if is_cto else "🆕 Paid Dexscreener Update Detected"
    color  = 0x8B5CF6 if is_cto else 0x22C55E

    lines = [
        f"**Chain:** {_chain_pretty(chain)}",
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
        "title":       title,
        "description": "\n".join(lines)[:4096],
        "url":         f"https://dexscreener.com/{chain}/{address}",
        "color":       color,
    }
    header_url = profile.get("header")
    if header_url:
        embed["image"] = {"url": header_url}
    return embed


# ── Discord posting ───────────────────────────────────────────────────────
def _webhook_wait_url(url: str) -> str:
    """Append wait=true so Discord returns the created message JSON (needed to
    capture the message_id for milestone replies)."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}wait=true"


async def _notify_discord(embed: dict) -> Optional[str]:
    """Post to the Discord webhook. Returns the created message_id on success,
    or None. Silent no-op if DEX_UPDATES_EVM_DISCORD_WEBHOOK isn't set."""
    if not DISCORD_WEBHOOK:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                _webhook_wait_url(DISCORD_WEBHOOK),
                json={"username": "Dex Updates (EVM)", "embeds": [embed]},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if resp.status not in (200, 204):
                body = await resp.text()
                logger.warning(f"dex_watcher_evm: Discord webhook {resp.status}: {body[:200]}")
                return None
            if resp.status == 204:
                return None
            data = await resp.json()
            return str(data.get("id")) if data.get("id") else None
    except Exception as e:
        logger.warning(f"dex_watcher_evm: Discord webhook failed: {e}")
        return None


async def _send_telegram_alert(caption: str, image_url: str) -> Optional[int]:
    """Send the alert to the EVM Telegram channel and return the created
    message_id, or None on failure. sendPhoto with caption when a banner exists,
    plain sendMessage fallback otherwise."""
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
                logger.info(f"dex_watcher_evm: sendPhoto failed ({data.get('description')}), falling back to text")

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
            logger.warning(f"dex_watcher_evm: sendMessage not ok: {data}")
            return None
    except Exception as e:
        logger.warning(f"dex_watcher_evm: Telegram send failed: {e}")
        return None


# ── Alert send ────────────────────────────────────────────────────────────
async def _send_alert(profile: dict, market: Optional[dict], event_type: str, chain: str) -> bool:
    caption    = _format_alert_tg(profile, market, event_type, chain)
    header_url = profile.get("header") or ""
    address    = profile.get("tokenAddress", "")

    market_dict = market or {}
    initial_mc = float(market_dict.get("market_cap") or market_dict.get("fdv") or 0)
    symbol     = market_dict.get("symbol") or ""

    # Fire Telegram + Discord in parallel; we need message IDs from both for
    # milestone reply routing later.
    tg_task = asyncio.create_task(_send_telegram_alert(caption, header_url))
    dc_task = asyncio.create_task(_notify_discord(_format_discord_embed(profile, market, event_type, chain)))
    tg_msg_id, dc_msg_id = await asyncio.gather(tg_task, dc_task)

    if tg_msg_id is None and dc_msg_id is None:
        return False

    # Register for milestone tracking on the EVM channel/webhook.
    try:
        from .dex_milestone_tracker import register_token
        register_token(
            address=address,
            initial_mc=initial_mc,
            ticker=symbol,
            name=(market_dict.get("name") or ""),
            tg_message_id=tg_msg_id,
            dc_message_id=dc_msg_id,
            chain=chain,
        )
    except Exception as e:
        logger.warning(f"dex_watcher_evm: milestone register failed for {address}: {e}")

    # Same rationale as Solana watcher: mark seen when ANY platform got the
    # alert. Otherwise a per-content failure on one side loops forever.
    return True


# ── Per-feed processing ───────────────────────────────────────────────────
async def _process_feed(session: aiohttp.ClientSession, feed: list, event_type: str) -> int:
    sent = 0
    for profile in feed:
        chain = (profile.get("chainId") or "").lower()
        if chain not in CHAINS:
            continue
        address = profile.get("tokenAddress")
        if not address:
            continue

        seen_key = f"{event_type}:{address}"
        if seen_key in _seen:
            continue

        market = await _fetch_pair_data(session, address)

        # Age filter only if configured. When MIN_AGE_HOURS=0 (default for EVM)
        # missing age data is not a blocker.
        if MIN_AGE_HOURS > 0:
            age_h = _age_hours((market or {}).get("pair_created_ms") if market else None)
            if age_h is None:
                logger.info(f"dex_watcher_evm: skip {address} ({event_type}, {chain}) — no age data")
                continue
            if age_h < MIN_AGE_HOURS:
                logger.info(
                    f"dex_watcher_evm: skip {address} ({event_type}, {chain}) "
                    f"— {age_h:.1f}h < {MIN_AGE_HOURS}h"
                )
                continue

        ok = await _send_alert(profile, market, event_type, chain)
        if ok:
            _seen[seen_key] = time.time()
            sent += 1
            logger.info(f"dex_watcher_evm: alerted {address} ({event_type}, {chain})")

        await asyncio.sleep(1)

    return sent


# ── Public entry point ────────────────────────────────────────────────────
async def run_dex_watcher_evm() -> None:
    if not CHANNEL_ID:
        logger.info("dex_watcher_evm: DEX_UPDATES_EVM_CHANNEL_ID not set — feature disabled")
        return

    logger.info(
        f"📡 dex_watcher_evm armed — poll={POLL_SECONDS}s, "
        f"chains={sorted(CHAINS)}, min_age={MIN_AGE_HOURS}h, "
        f"discord={'on' if DISCORD_WEBHOOK else 'off'}, seen_size={len(_seen)}"
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
                logger.info(f"dex_watcher_evm: sent {p_sent} profile + {c_sent} cto alerts")
        except Exception as e:
            logger.warning(f"dex_watcher_evm: iteration failed: {e}")

        await asyncio.sleep(POLL_SECONDS)
