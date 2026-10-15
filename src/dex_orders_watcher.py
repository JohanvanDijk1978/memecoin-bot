"""
dex_orders_watcher.py
─────────────────────
Alerts a Dexscreener profile payment the minute it is paid, 1-2 minutes before the profile goes live, which is
when dex_watcher (and every bot reading the token-profiles feed) sees it. Measured 2026-10-15: profiles went
live 62 s and 117 s after payment, with the order still "processing"; 3 of 5 live profiles had no order on the
endpoint at all, so dex_watcher stays the complete source and this is the early one.

The orders endpoint answers per coin only, 60 a minute, so this cycles through the Solana coins the groups
called most recently (data/ca_history.json), about one a second: the HOT newest calls every pass (~20 s),
the rest of the WATCH newest in rotation, COLD_PER of them per pass (each every ~3 min). 0x addresses need
a chain id the history does not hold: not watched.

Env: DEX_ORDERS_WATCH (recent called coins watched, 60), DEX_ORDERS_HOT (checked every pass, 15). Delivery
reuses dex_watcher's Telegram channel and Discord webhook, and the Padre feed.
"""

import asyncio
import json
import logging
import os
import time

import aiohttp

from . import dex_watcher as dw
from .utils import dex_wait, escape_md, fmt_usd

logger = logging.getLogger(__name__)

WATCH = int(os.getenv("DEX_ORDERS_WATCH") or 60)
HOT = int(os.getenv("DEX_ORDERS_HOT") or 15)
COLD_PER = 5
ORDERS_URL = "https://api.dexscreener.com/orders/v1/solana/{address}"
CA_HISTORY = "data/ca_history.json"
PADRE_FEED = "data/padre_feed.jsonl"
FRESH_S = 600  # a payment older than this was alerted by dex_watcher long ago


def candidates(n: int = WATCH, path: str = CA_HISTORY) -> list:
    """The n Solana coins the groups called last, newest call first. The Dex watchers record their own alerts in
    the same history (source dex_watcher*); those are not calls, and counting them made every Dex-paid alert
    spawn a second, late one here (CHUNKEE, SAFEGUARD, 2026-10-15 22:49)."""
    try:
        with open(path) as f:
            hist = json.load(f)
    except Exception:
        return []
    last = {}
    for a, es in hist.items():
        calls = [e.get("timestamp") or 0 for e in es if not str(e.get("source") or "").startswith("dex_watcher")]
        if calls and not a.startswith("0x"):
            last[a] = max(calls)
    return sorted(last, key=last.get, reverse=True)[:n]


def batch(todo: list, i: int, hot: int = HOT, step: int = COLD_PER) -> tuple:
    """One pass: the hot newest coins plus the next `step` of the rest, and where the rotation continues."""
    cold = todo[hot:]
    i = i if i < len(cold) else 0
    return todo[:hot] + cold[i:i + step], i + step


def profile_orders(o) -> list:
    return [x for x in (o.get("orders") if isinstance(o, dict) else o) or [] if x.get("type") == "tokenProfile"]


async def orders(http, address: str):
    await dex_wait()
    async with http.get(ORDERS_URL.format(address=address), timeout=aiohttp.ClientTimeout(total=15)) as r:
        return profile_orders(await r.json(content_type=None)) if r.status == 200 else None


async def alert(http, address: str, order: dict, paid: float) -> None:
    market = await dw._fetch_pair_data(http, address) or {}
    sym = market.get("symbol") or address[:6]
    mc = float(market.get("market_cap") or market.get("fdv") or 0)
    ago, pending = int(time.time() - paid), order.get("status") == "processing"
    label = "Dex paid (pending)" if pending else f"Dex paid ({order.get('status')})"
    state = "profile not live yet" if pending else "approved, not in the feed yet"
    try:
        with open(PADRE_FEED, "a") as f:
            f.write(json.dumps({"ts": time.time(), "kind": "dexpaid", "address": address, "chain": "solana",
                                "ticker": sym, "mc": mc, "paid_at": paid, "status": order.get("status")}) + "\n")
    except Exception as e:
        logger.warning("dex_orders: padre feed write failed: %s", e)
    logger.info("dex_orders: %s $%s %s paid %ds ago", label, sym, address, ago)
    caption = (f"💸 *{label}*: ${escape_md(sym)} — {fmt_usd(mc)} MC\n"
               f"paid {ago}s ago, {state}\n`{address}`")
    embed = {"title": f"💸 {label}: ${sym}", "url": f"https://dexscreener.com/solana/{address}", "color": 0xE74C3C,
             "description": f"Profile order paid {ago}s ago, {state}.\nMC {fmt_usd(mc)}\n`{address}`"}
    await asyncio.gather(dw._send_telegram_alert(caption, "") if dw.CHANNEL_ID else asyncio.sleep(0),
                         dw._notify_discord(embed))


async def run_dex_orders_watcher() -> None:
    if not (dw.CHANNEL_ID or dw.DISCORD_WEBHOOK):
        logger.info("dex_orders: no DEX_UPDATES channel — not started")
        return
    started, seen, cold_i = time.time(), {}, 0  # seen: address -> paymentTimestamp alerted
    logger.info("dex_orders: armed — %d most recent called coins, the newest %d every pass", WATCH, HOT)
    async with aiohttp.ClientSession() as http:
        while True:
            todo = candidates()
            if not todo:
                await asyncio.sleep(10)
                continue
            todo, cold_i = batch(todo, cold_i)
            for address in todo:
                if time.time() - (dw._seen.get(f"profile_update:{address}") or 0) < dw.REALERT_TTL_SECS:
                    continue  # the profile is live and dex_watcher alerted it: nothing early left to say
                try:
                    prof = await orders(http, address)
                except Exception as e:
                    logger.debug("dex_orders: %s: %r", address, e)
                    prof = None
                for x in prof or []:
                    paid = (x.get("paymentTimestamp") or 0) / 1000
                    # fresh: paid within FRESH_S, and after this watcher started (minus a little), so a restart
                    # does not replay what was alerted before it
                    if time.time() - paid < FRESH_S and paid > started - 120 and seen.get(address) != paid:
                        seen[address] = paid
                        try:
                            await alert(http, address, x, paid)
                        except Exception as e:
                            logger.warning("dex_orders: alert for %s failed: %r", address, e)
                await asyncio.sleep(1.1)  # the orders endpoint allows 60 a minute


if __name__ == "__main__":  # python -m src.dex_orders_watcher
    import tempfile
    assert profile_orders({"orders": [{"type": "tokenAd", "status": "approved"},
                                      {"type": "tokenProfile", "status": "processing", "paymentTimestamp": 1000}]}) == \
        [{"type": "tokenProfile", "status": "processing", "paymentTimestamp": 1000}]
    assert profile_orders([]) == [] and profile_orders({}) == []
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"A": [{"timestamp": 1}], "B": [{"timestamp": 5}, {"timestamp": 2}], "0xC": [{"timestamp": 9}], "D": [],
                   "E": [{"timestamp": 8, "source": "dex_watcher"}],  # a scanner find, not a call
                   "F": [{"timestamp": 7, "source": "dex_watcher_evm"}, {"timestamp": 3, "source": "telegram"}]}, f)
    assert candidates(path=f.name) == ["B", "F", "A"], candidates(path=f.name)
    assert candidates(n=1, path=f.name) == ["B"]
    assert candidates(path="/nonexistent") == []
    os.unlink(f.name)
    todo = list(range(12))
    assert batch(todo, 0, hot=3, step=4) == ([0, 1, 2, 3, 4, 5, 6], 4)
    assert batch(todo, 4, hot=3, step=4) == ([0, 1, 2, 7, 8, 9, 10], 8)
    assert batch(todo, 8, hot=3, step=4) == ([0, 1, 2, 11], 12)
    assert batch(todo, 12, hot=3, step=4) == ([0, 1, 2, 3, 4, 5, 6], 4)  # wrapped
    assert batch([1, 2], 7, hot=3, step=4) == ([1, 2], 4)  # fewer coins than hot: all of them, every pass
    print("ok")
