"""
ca_enrich.py
────────────
Everything between "a CA was spotted" and "the alert reads properly".

Three jobs, in one place because the Telegram and Discord scrapers had grown
their own near-identical copy of the first two:

  1. `fetch_token_quick` / `fetch_ath` — the Dexscreener + GeckoTerminal
     lookup. Unchanged behaviour, one copy.
  2. `build_ca_message` — the alert body, enriched or bare. The bare form now
     carries whatever `ca_probe` established on-chain instead of the flat
     "Ξ EVM Contract".
  3. `schedule_recheck` / `run_recheck_loop` — the part that was simply
     missing. A token that has no Dexscreener pool at 13:45 very often has one
     at 13:50, and the old flow never looked again: the ping stayed bare
     forever. Now the address goes on a queue, is re-fetched on a widening
     schedule, and the moment Dexscreener answers, the original Telegram
     message is edited in place into the full alert.

Editing beats re-posting: the channel keeps one message per call, the mcap
lands in the message people already scrolled past, and nobody gets pinged
twice for the same coin.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp

from .ca_probe import describe
from .mention_store import store
from .send_ping import edit_ping
from .utils import dex_wait

logger = logging.getLogger(__name__)

# Widening re-check schedule, in seconds after the bare ping. The first is
# short because a pump.fun/Uniswap pool usually indexes within a minute; the
# last is long because by then it is a genuine pre-launch contract and we are
# waiting on the deployer, not on Dexscreener.
RECHECK_DELAYS = [float(s) for s in
                  os.getenv("CA_RECHECK_DELAYS", "45,180,600,1800").split(",") if s.strip()]
RECHECK_TICK = float(os.getenv("CA_RECHECK_TICK", "10"))
RECHECK_MAX_PENDING = int(os.getenv("CA_RECHECK_MAX_PENDING", "300"))


# ── Dexscreener / GeckoTerminal lookup ────────────────────────────────────
async def fetch_token_quick(address: str, chain: str) -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
            await dex_wait()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    # A 429 and "this token does not exist" used to look
                    # identical in the log as well as in the alert. Say which.
                    logger.info("dex lookup for %s returned HTTP %s", address, resp.status)
                    return {}
                data = await resp.json()

            pairs = data.get("pairs") or []
            if not pairs:
                return {}

            # If the scraper detected the address as SOL (base58), restrict to
            # solana pairs. If it was detected as ETH (0x...), let Dexscreener
            # return whichever EVM chain actually has the token — could be
            # ethereum, bsc, base, robinhood, arbitrum, etc. We pick the pair
            # with the most liquidity and use ITS chainId as the source of truth.
            if chain == "SOL":
                filtered = [p for p in pairs if p.get("chainId", "").lower() == "solana"] or pairs
            else:
                filtered = pairs
            best = max(filtered, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
            actual_chain_id = (best.get("chainId") or "").lower()
            dex_id          = (best.get("dexId") or "").lower()

            base = best.get("baseToken", {})
            vol  = best.get("volume", {})
            chg  = best.get("priceChange", {})

            image_url = ""
            info = best.get("info", {})
            if info.get("imageUrl"):
                image_url = info["imageUrl"]

            # Calculate age
            created_at = best.get("pairCreatedAt", 0) or 0
            if created_at:
                age_secs = time.time() - created_at / 1000
                if age_secs < 3600:
                    age_str = f"{int(age_secs/60)} minutes"
                elif age_secs < 86400:
                    age_str = f"{int(age_secs/3600)} hours"
                elif age_secs < 2592000:
                    age_str = f"{int(age_secs/86400)} days"
                else:
                    age_str = f"{int(age_secs/2592000)} months"
            else:
                age_str = "?"

            price_usd = float(best.get("priceUsd", 0) or 0)
            fdv_usd   = float(best.get("marketCap", 0) or 0)
            ath_mc, ath_time = await fetch_ath(address, chain, price_usd, fdv_usd, session)

            return {
                "name":       base.get("name", "Unknown"),
                "symbol":     base.get("symbol", "???"),
                "price":      price_usd,
                "volume_24h": float(vol.get("h24", 0) or 0),
                "change_24h": float(chg.get("h24", 0) or 0),
                "market_cap": fdv_usd,
                "url":        best.get("url", ""),
                "image_url":  image_url,
                "age":        age_str,
                "ath_mc":     ath_mc,
                "ath_time":   ath_time,
                "chain_id":   actual_chain_id,   # "ethereum" / "bsc" / "base" / "solana" / ...
                "dex_id":     dex_id,            # "uniswap" / "pancakeswap" / "raydium" / ...
            }
    except Exception as e:
        logger.warning(f"Quick fetch failed for {address}: {e}")
        return {}


async def fetch_ath(address: str, chain: str, current_price: float, current_fdv: float,
                    session: aiohttp.ClientSession) -> tuple:
    """Fetch ATH market cap and time from GeckoTerminal. Returns (ath_mc, ath_time)."""
    try:
        network = "solana" if chain == "SOL" else "eth"
        # Get pools for this token
        url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}/pools?page=1"
        headers = {"Accept": "application/json;version=20230302"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return 0, 0
            data = await resp.json()
            pools = data.get("data", [])
            if not pools:
                return 0, 0
            pool_id = pools[0].get("id", "").replace(f"{network}_", "")

        ohlcv_url = (f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool_id}"
                     f"/ohlcv/hour?limit=1000&currency=usd&token=base")
        async with session.get(ohlcv_url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return 0, 0
            data = await resp.json()
            candles = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            if not candles:
                return 0, 0

            ath_candle = max(candles, key=lambda c: c[2])
            ath_price = ath_candle[2]
            ath_time  = ath_candle[0]

            if current_price > 0 and current_fdv > 0:
                ath_mc = (ath_price / current_price) * current_fdv
            else:
                ath_mc = 0
            return ath_mc, ath_time
    except Exception as e:
        logger.warning(f"ATH fetch failed for {address}: {e}")
        return 0, 0


# ── message building ──────────────────────────────────────────────────────
def _fmt2(n) -> str:
    n = n or 0
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}K"
    return str(n)


def _fmt_supply(n) -> str:
    """Token supplies run past what the market-cap formatter covers — it stops
    at M, and widening it would move the FDV line in every existing alert."""
    n = n or 0
    if n >= 1_000_000_000_000: return f"{n/1_000_000_000_000:.1f}T"
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.1f}B"
    return _fmt2(n)


def _ago(secs: float) -> str:
    if secs < 3600:
        return f"{int(secs/60)}m"
    if secs < 86400:
        return f"{int(secs/3600)}h"
    return f"{int(secs/86400)}d"


def _ath_suffix(token: dict, mc: float) -> str:
    ath_mc   = (token or {}).get("ath_mc", 0)
    ath_time = (token or {}).get("ath_time", 0)
    if ath_mc > mc * 1.05 and ath_time:
        return f" ⇨ {_fmt2(ath_mc)} ATH[{_ago(time.time() - ath_time)}]"
    return " ATH" if ath_mc > 0 else ""


def _scan_line(address: str) -> str:
    scan_total, scan_groups = store.get_scan_stats(address)
    if scan_total == 0:
        scan_total, scan_groups = 1, 1
    if scan_total <= 1:
        return "👥 *First scan!*\n"
    grp_word = "groups" if scan_groups != 1 else "group"
    return f"👥 Scanned *{scan_total}x* in *{scan_groups}* {grp_word}\n"


def _history_block(address: str, current_mc: float) -> str:
    """Top-3 earlier scanners, with the multiplier off the stored peak."""
    history = store.get_ca_history(address, limit=3)
    if not history:
        return ""

    stored_entries = store._ca_history.get(address, [])
    peak_mc_stored = max((e.get("peak_mc", 0) for e in stored_entries), default=0)
    best_mc = peak_mc_stored if peak_mc_stored > 0 else current_mc

    medals = ["🥇", "🥈", "🥉"]
    block = "\n\n━━━━━━━━━━━━━━━"
    for i, mention in enumerate(history):
        ago_mins = int((time.time() - mention.timestamp) / 60)
        if ago_mins < 60:
            ts = f"{ago_mins}m ago"
        elif ago_mins < 1440:
            ts = f"{ago_mins // 60}h ago"
        else:
            ts = f"{ago_mins // 1440}d ago"
        grp = mention.group_name or mention.source
        who = mention.sender_name or "Unknown"
        mc  = mention.market_cap
        if mc >= 1_000_000:
            mc_str = f"${mc/1_000_000:.1f}M"
        elif mc > 0:
            mc_str = f"${mc/1_000:.0f}K"
        else:
            mc_str = "N/A"
        if mc > 0 and best_mc > 0:
            mult = best_mc / mc
            mult_str = f"({mult:.1f}x)" if mult >= 1.1 else ""
        else:
            mult_str = ""
        medal = medals[i] if i < len(medals) else "•"
        # Skip entries with no useful data
        if mc_str == "N/A" and who == "Unknown" and grp in ("discord", "telegram"):
            continue
        block += f"\n{medal} *{grp}* — *{who}* — *{mc_str}{mult_str}* — *{ts}*"
    return block


def build_ca_message(header: str, address: str, chain: str, token: dict,
                     trading_links: str, probe: Optional[dict] = None,
                     pending_note: str = "") -> tuple:
    """The alert body and its image URL.

    `header` is the already-built "👤 who in where" line, because only the
    caller knows whether the group name can be hyperlinked to its mirror topic.
    """
    from .utils import chain_display_name

    context_block = _history_block(address, (token or {}).get("market_cap", 0))
    scan_line = _scan_line(address)

    if token:
        mc     = token["market_cap"]
        price  = token["price"]
        ticker = f"${token['symbol']}" if token.get("symbol") else ""
        name   = token["name"]
        dex_id = token.get("dex_id") or ""
        actual_chain = token.get("chain_id") or ("solana" if chain == "SOL" else "ethereum")
        platform_label = dex_id.title() if dex_id else chain_display_name(actual_chain)

        msg = (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🪙 *{name}*  | *{_fmt2(mc)}* | *{ticker}*\n"
            f"💊 {chain_display_name(actual_chain)} @ {platform_label}\n"
            f"🕐 Age: {token.get('age', '?')}\n"
            f"💵 USD: `{price:.8f}`\n"
            f"💎 FDV: *{_fmt2(mc)}{_ath_suffix(token, mc)}*\n"
            f"{scan_line}"
            f"\n`{address}`\n"
            f"\n🔗 {trading_links}"
            f"{context_block}"
        )
        return msg, token.get("image_url", "")

    # No market data. Say what the address actually is, when the chain told us.
    what = describe(probe or {}) or ("◎ SOL Contract" if chain == "SOL" else "Ξ EVM Contract")
    supply = (probe or {}).get("supply") or 0
    supply_line = f"🧾 Supply: *{_fmt_supply(supply)}*\n" if supply else ""
    msg = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{what}\n"
        f"{supply_line}"
        f"{scan_line}"
        f"\n`{address}`\n"
        f"\n🔗 {trading_links}"
        f"{context_block}"
        f"{pending_note}"          # last, where the re-check's own note lands
    )
    return msg, ""


# ── deferred re-check ─────────────────────────────────────────────────────
# address -> {"chain", "attempt", "due", "first_seen", "targets": [...]}
# target  -> {"chat_id", "message_id", "header", "source", "group_name"}
_pending: dict[str, dict] = {}


# Footer that tells the reader the message is not final.
PENDING_NOTE = "\n\n⏳ _No market data yet — re-checking._"


def schedule_recheck(address: str, chain: str, header: str, sent: Optional[dict],
                     source: str = "", group_name: str = "") -> bool:
    """Queue a bare ping for re-enrichment. Returns True if it was queued.

    `sent` is what `send_ping` returned — without a message_id there is
    nothing to edit later, so there is no point queuing.
    """
    if not sent or not sent.get("message_id"):
        return False
    target = {"chat_id": sent.get("chat_id"), "message_id": sent["message_id"],
              "header": header, "source": source, "group_name": group_name}

    job = _pending.get(address)
    if job:
        # Same CA, bare in a second group: one lookup, both messages fixed.
        if not any(t["message_id"] == target["message_id"] and t["chat_id"] == target["chat_id"]
                   for t in job["targets"]):
            job["targets"].append(target)
        return True

    if len(_pending) >= RECHECK_MAX_PENDING:
        logger.warning("ca_recheck: queue full (%s), dropping %s", RECHECK_MAX_PENDING, address)
        return False

    _pending[address] = {"chain": chain, "attempt": 0, "first_seen": time.time(),
                         "due": time.time() + RECHECK_DELAYS[0], "targets": [target]}
    return True


async def _attempt(address: str, job: dict) -> None:
    """One re-fetch. Edits every message waiting on this address, or reschedules."""
    token = await fetch_token_quick(address, job["chain"])
    if not token:
        job["attempt"] += 1
        if job["attempt"] >= len(RECHECK_DELAYS):
            logger.info("ca_recheck: %s still has no market data after %d tries — giving up",
                        address, job["attempt"])
            _pending.pop(address, None)
        else:
            job["due"] = time.time() + RECHECK_DELAYS[job["attempt"]]
        return

    from .utils import build_trading_links

    mc = token.get("market_cap", 0)
    actual_chain = token.get("chain_id") or ("solana" if job["chain"] == "SOL" else "ethereum")
    trading_links = build_trading_links(actual_chain, address)

    # The history rows written at ping time carry market_cap 0, which is a
    # permanent "N/A" in every later alert and an un-scoreable call on the
    # leaderboard. Now that there is a price, backfill them.
    for target in job["targets"]:
        store.backfill_market_cap(address, target.get("group_name", ""), mc,
                                  ticker=token.get("symbol", ""), chain_id=actual_chain)

    waited = int(time.time() - job["first_seen"])
    note = f"\n\n✅ _Market data arrived {_ago(waited)} after the call._"
    edited = 0
    for target in job["targets"]:
        msg, _ = build_ca_message(
            header=target["header"], address=address, chain=job["chain"], token=token,
            trading_links=trading_links, pending_note="",
        )
        if await edit_ping(target["chat_id"], target["message_id"], msg + note):
            edited += 1

    logger.info("ca_recheck: %s enriched after %ss (%s/%s messages updated)",
                address, waited, edited, len(job["targets"]))
    _pending.pop(address, None)


async def run_recheck_loop():
    """Re-enrich bare pings until Dexscreener has them or the schedule runs out."""
    logger.info("🔁 CA re-check loop started — retries at %s s",
                ",".join(str(int(d)) for d in RECHECK_DELAYS))
    while True:
        try:
            await asyncio.sleep(RECHECK_TICK)
            now = time.time()
            due = [(addr, job) for addr, job in list(_pending.items()) if job["due"] <= now]
            for address, job in due:
                if address not in _pending:
                    continue
                try:
                    await _attempt(address, job)
                except Exception as e:
                    logger.warning("ca_recheck: %s failed: %r", address, e)
                    _pending.pop(address, None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("ca_recheck loop error: %r", e)
            await asyncio.sleep(30)
