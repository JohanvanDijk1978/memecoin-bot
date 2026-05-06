"""
filtered_forward.py
───────────────────
Side-channel forwarder. When a scraped CA matches BOTH the watched-group
list AND the market-cap ceiling, copy the alert into a separate Telegram
channel. First-detection-only per CA — deduped persistently across restarts
via data/filtered_seen.json.

Designed for near-zero added latency: the call is fire-and-forget from the
scraper's perspective (use asyncio.create_task at the call site).
"""

import os
import json
import time
import asyncio
import logging
from typing import Optional

from dotenv import load_dotenv

from .send_ping import send_ping

load_dotenv()
logger = logging.getLogger(__name__)

FILTERED_CHANNEL_ID = os.getenv("FILTERED_CHANNEL_ID", "")
MC_CEILING = 100_000  # only forward CAs first detected under this market cap (USD)

# Substring matchers, lowercased. Group names from scrapers can include topic
# suffixes like "the village #trenches" — we match if any watched name is a
# substring of the incoming group_name.
WATCHED_GROUPS = {
    "fantom troupe",
    "the village",
    "alphadao",
    "versus",
    "the great locked in penis",
    "tiktokfnf",
}

SEEN_FILE = "data/filtered_seen.json"
SEEN_TTL_SECS = 30 * 86400  # prune entries older than 30 days


def _load_seen() -> dict:
    try:
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        return {}
    cutoff = time.time() - SEEN_TTL_SECS
    return {addr: ts for addr, ts in data.items() if isinstance(ts, (int, float)) and ts >= cutoff}


def _save_seen(seen: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
        with open(SEEN_FILE, "w") as f:
            json.dump(seen, f)
    except Exception as e:
        logger.warning(f"Failed to persist filtered_seen: {e}")


_seen: dict = _load_seen()
_save_lock = asyncio.Lock()


def is_watched_group(group_name: str) -> bool:
    if not group_name:
        return False
    g = group_name.lower().strip()
    return any(w in g for w in WATCHED_GROUPS)


async def maybe_forward(
    text: str,
    image_url: str,
    group_name: str,
    market_cap: Optional[float],
    address: str,
) -> None:
    """Forward to the filtered channel iff group is watched, mc < ceiling,
    and this CA hasn't been forwarded before. Safe to call from any scraper —
    silently no-ops when the filter doesn't match or FILTERED_CHANNEL_ID is unset."""
    if not FILTERED_CHANNEL_ID or not address:
        return
    if address in _seen:
        return
    if not is_watched_group(group_name):
        return
    try:
        mc = float(market_cap or 0)
    except (TypeError, ValueError):
        return
    if mc <= 0 or mc >= MC_CEILING:
        return

    # Mark seen BEFORE the network call so concurrent detections don't double-fire.
    _seen[address] = time.time()
    async with _save_lock:
        _save_seen(_seen)

    try:
        await send_ping(text, image_url, chat_id=FILTERED_CHANNEL_ID)
        logger.info(f"Filtered forward: {address} from '{group_name}' mc=${mc:,.0f}")
    except Exception as e:
        logger.warning(f"Filtered forward failed for {address}: {e}")
