"""
dex_milestone_tracker.py
────────────────────────
Watches every coin alerted by dex_watcher and posts a milestone reply when
its market cap hits 2×, 3×, 5×, 10×, then every +5× thereafter, relative to
the initial market cap at the moment of the original dex_watcher alert.

Design:
- dex_watcher calls `register_token(...)` right after sending an alert.
- We persist state at `data/dex_milestones.json` so restarts don't lose it.
- Every MILESTONE_POLL_SECONDS (default 180 = 3 min) we poll Dexscreener for
  each tracked token, compare the current mcap to the initial, and post ONE
  reply for the HIGHEST new milestone that was crossed since the last poll.
  Lower milestones we jumped over get marked as hit silently — prevents spam
  when a coin 10x's between two polls.
- Replies stack under the original alert: Telegram via `reply_parameters`,
  Discord via `message_reference`. Neither pings anyone.
- Entries older than MILESTONE_TTL_DAYS (default 30) are dropped.
"""

import os
import json
import time
import asyncio
import logging
from typing import Optional, List

import aiohttp
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
POLL_SECONDS       = int(os.getenv("MILESTONE_POLL_SECONDS", "60"))
TTL_DAYS           = int(os.getenv("MILESTONE_TTL_DAYS", "30"))
INITIAL_DELAY_SECS = 90  # let dex_watcher's first burst settle before we poll

BOT_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API    = f"https://api.telegram.org/bot{BOT_TOKEN}"

MILESTONES_FILE = "data/dex_milestones.json"


def _channel_for_chain(chain: str) -> str:
    """Route milestone replies to the same channel that the original alert
    used, based on the chain."""
    if chain == "solana":
        return os.getenv("DEX_UPDATES_CHANNEL_ID", "")
    # ethereum / bsc / robinhood / any other EVM chain
    return os.getenv("DEX_UPDATES_EVM_CHANNEL_ID", "")


def _webhook_for_chain(chain: str) -> str:
    if chain == "solana":
        return os.getenv("DEX_UPDATES_DISCORD_WEBHOOK", "")
    return os.getenv("DEX_UPDATES_EVM_DISCORD_WEBHOOK", "")


def _milestones_ordered() -> List[int]:
    """Ordered milestone multipliers: 2, 3, 5, 10, 15, 20, 25, ... up to 10000."""
    fixed = [2, 3, 5]
    tail = list(range(10, 10001, 5))
    return fixed + tail


_MILESTONES: List[int] = _milestones_ordered()


# ── Persistence ───────────────────────────────────────────────────────────
def _load() -> dict:
    try:
        with open(MILESTONES_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(MILESTONES_FILE), exist_ok=True)
        with open(MILESTONES_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logger.warning(f"milestone_tracker: save failed: {e}")


_state: dict = _load()
_save_lock = asyncio.Lock()


# ── Public registration API (called by dex_watcher) ───────────────────────
def register_token(
    address: str,
    initial_mc: float,
    ticker: str = "",
    name: str = "",
    tg_message_id: Optional[int] = None,
    dc_message_id: Optional[str] = None,
    chain: str = "solana",
) -> None:
    """Register a token for milestone tracking. Called by dex_watcher /
    dex_watcher_evm after successfully sending its initial alert. No-op if:
      - address is falsy
      - initial_mc is missing / invalid / <= 0
      - the token is already being tracked (first alert wins)
      - both message IDs are missing (nothing to reply to)

    `chain` is used to route milestone replies to the correct Telegram
    channel / Discord webhook (Solana vs EVM).
    """
    if not address:
        return
    try:
        initial_mc = float(initial_mc or 0)
    except (TypeError, ValueError):
        return
    if initial_mc <= 0:
        logger.info(f"milestone_tracker: skip {address} — initial_mc missing/invalid")
        return
    if not (tg_message_id or dc_message_id):
        return
    if address in _state:
        return

    _state[address] = {
        "initial_mc":  initial_mc,
        "ticker":      ticker or "",
        "name":        name or "",
        "tg_msg_id":   tg_message_id,
        "dc_msg_id":   dc_message_id,
        "chain":       chain or "solana",
        "posted_at":   time.time(),
        "hit":         [],  # milestones already announced
    }
    _save(_state)
    logger.info(
        f"milestone_tracker: registered {address} (${initial_mc:,.0f}, "
        f"chain={chain}, tg={tg_message_id}, dc={dc_message_id})"
    )


# ── Dexscreener client ────────────────────────────────────────────────────
async def _fetch_current_mc(session: aiohttp.ClientSession, address: str) -> Optional[float]:
    """Fetch current market cap for the token's top-liquidity pair. None on failure."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None
        best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
        mcap = float(best.get("marketCap") or 0)
        return mcap if mcap > 0 else None
    except Exception as e:
        logger.warning(f"milestone_tracker: fetch failed for {address}: {e}")
        return None


# ── Milestone math ────────────────────────────────────────────────────────
def _highest_new_milestone(initial_mc: float, current_mc: float, already_hit: list) -> Optional[int]:
    """Return the highest milestone that (a) has been reached given initial→current
    and (b) wasn't already announced. None if nothing new."""
    if initial_mc <= 0 or current_mc <= 0:
        return None
    mult = current_mc / initial_mc
    hit_set = set(already_hit)
    highest = None
    for m in _MILESTONES:
        if m > mult:
            break
        if m not in hit_set:
            highest = m
    return highest


def _milestones_up_to(threshold: int) -> List[int]:
    return [m for m in _MILESTONES if m <= threshold]


# ── Formatting ────────────────────────────────────────────────────────────
def _fmt_usd(n: float) -> str:
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n/1_000:.0f}K"
    return f"${n:.0f}"


def _format_update(ticker: str, name: str, multiplier: int,
                   initial_mc: float, current_mc: float) -> str:
    percent_gain = ((current_mc / initial_mc) - 1.0) * 100 if initial_mc > 0 else 0
    tkr = f"${ticker}" if ticker else (name or "This coin")
    return (
        f"🚀 Coin pumped! {tkr} has pumped {multiplier}×.\n"
        f"📈 Initial MC: {_fmt_usd(initial_mc)}    "
        f"💰 Current MC: {_fmt_usd(current_mc)}    "
        f"📊 Gain: +{percent_gain:.0f}%"
    )


# ── Reply senders ─────────────────────────────────────────────────────────
async def _reply_telegram(
    session: aiohttp.ClientSession,
    text: str,
    reply_to: Optional[int],
    channel_id: str,
) -> None:
    if not (BOT_TOKEN and channel_id and reply_to):
        return
    try:
        resp = await session.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": channel_id,
                "text": text,
                "reply_parameters": {"message_id": reply_to, "allow_sending_without_reply": True},
                "disable_web_page_preview": True,
            },
            timeout=aiohttp.ClientTimeout(total=10),
        )
        data = await resp.json()
        if not data.get("ok"):
            logger.warning(f"milestone_tracker: TG reply not ok: {data}")
    except Exception as e:
        logger.warning(f"milestone_tracker: TG reply failed: {e}")


async def _reply_discord(
    session: aiohttp.ClientSession,
    text: str,
    reply_to: Optional[str],
    webhook_url: str,
) -> None:
    if not (webhook_url and reply_to):
        return
    try:
        resp = await session.post(
            webhook_url,
            json={
                "content": text,
                "username": "Dex Updates",
                "message_reference": {"message_id": str(reply_to)},
                "allowed_mentions": {"parse": []},  # no pings, ever
            },
            timeout=aiohttp.ClientTimeout(total=10),
        )
        if resp.status not in (200, 204):
            body = await resp.text()
            logger.warning(f"milestone_tracker: Discord reply {resp.status}: {body[:200]}")
    except Exception as e:
        logger.warning(f"milestone_tracker: Discord reply failed: {e}")


# ── Per-cycle processing ──────────────────────────────────────────────────
async def _process(session: aiohttp.ClientSession) -> int:
    """One sweep. Returns count of milestone replies posted."""
    now = time.time()
    ttl_cutoff = now - TTL_DAYS * 86400
    dirty = False
    replies_sent = 0

    for address in list(_state.keys()):
        entry = _state[address]
        posted_at = entry.get("posted_at", 0)
        if posted_at < ttl_cutoff:
            del _state[address]
            dirty = True
            continue

        current_mc = await _fetch_current_mc(session, address)
        if current_mc is None:
            continue

        milestone = _highest_new_milestone(entry.get("initial_mc", 0), current_mc, entry.get("hit", []))
        if milestone is None:
            continue

        text = _format_update(
            entry.get("ticker", ""),
            entry.get("name", ""),
            milestone,
            entry["initial_mc"],
            current_mc,
        )
        chain = entry.get("chain", "solana")  # legacy entries default to solana
        channel_id = _channel_for_chain(chain)
        webhook    = _webhook_for_chain(chain)
        await asyncio.gather(
            _reply_telegram(session, text, entry.get("tg_msg_id"), channel_id),
            _reply_discord(session, text, entry.get("dc_msg_id"), webhook),
        )

        # Mark ALL milestones up to and including this one as hit — so if we
        # jumped from 1x to 10x we don't retroactively announce 2/3/5.
        entry["hit"] = sorted(set(entry.get("hit", [])) | set(_milestones_up_to(milestone)))
        replies_sent += 1
        dirty = True

        # Polite pacing between Dexscreener calls
        await asyncio.sleep(0.5)

    if dirty:
        async with _save_lock:
            _save(_state)

    return replies_sent


# ── Public entry point ────────────────────────────────────────────────────
async def run_milestone_tracker() -> None:
    sol_channel = _channel_for_chain("solana")
    evm_channel = _channel_for_chain("ethereum")
    if not BOT_TOKEN or not (sol_channel or evm_channel):
        logger.info(
            "milestone_tracker: TELEGRAM_BOT_TOKEN and at least one of "
            "DEX_UPDATES_CHANNEL_ID / DEX_UPDATES_EVM_CHANNEL_ID must be set — feature disabled"
        )
        return

    sources = []
    if sol_channel:
        sources.append("solana")
    if evm_channel:
        sources.append("evm")
    logger.info(
        f"📈 milestone_tracker armed — poll={POLL_SECONDS}s, ttl={TTL_DAYS}d, "
        f"tracked={len(_state)}, sources={sources}"
    )
    await asyncio.sleep(INITIAL_DELAY_SECS)

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                sent = await _process(session)
            if sent:
                logger.info(f"milestone_tracker: sent {sent} milestone reply(ies)")
        except Exception as e:
            logger.warning(f"milestone_tracker: iteration failed: {e}")
        await asyncio.sleep(POLL_SECONDS)
