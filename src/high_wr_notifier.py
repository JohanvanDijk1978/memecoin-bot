"""
high_wr_notifier.py
───────────────────
DM alert when a HIGH WIN RATE caller scans a token for the FIRST time in a
specific Telegram group.

Rules (all must hold):
  1. Caller win rate strictly > HIGH_WR_THRESHOLD (default 22.5%).
  2. First scan of this token by this caller in this group — tracked per
     (caller, group, token), NOT globally. Same token in another group still
     counts as a first scan here.
  3. Only live scan events. Historical entries never trigger; on first run the
     seen-set is seeded from ca_history.json so pre-existing calls don't fire.
  4. Market cap strictly below HIGH_WR_MAX_MCAP (default $100K), measured at
     scan time. Tokens with no usable mcap data are skipped, since we can't
     confirm they're under the cap.

Semantics: "first scan only" — every first scan is recorded in the persistent
seen-set regardless of win rate. If the caller's WR was too low at first-scan
time, later re-scans of the same combo never notify.

Win rate source: memedash's SQLite read model (dashboard/data/dash.db),
opened READ-ONLY. Replicates the dashboard's win_rate_score():
  mult = MAX(first_mc, peak_mc_bot, peak_mc_live) / first_mc
  WR   = share of completed calls (first_mc > 0) with mult >= 2.0
Legacy rows without sender_id are merged by display name, matching the
dashboard's alias behaviour for callers with exactly one known ID.

Modular by design: add extra filters in _passes_filters().

Env (all optional):
  HIGH_WR_THRESHOLD  — win-rate %% threshold, strictly greater-than (22.5)
  HIGH_WR_MAX_MCAP   — max market cap in USD, strictly less-than (100000)
  HIGH_WR_CHAT_ID    — destination chat; falls back to YOUR_TELEGRAM_USER_ID
  DASH_DB_PATH       — path to memedash SQLite (dashboard/data/dash.db)
"""

import os
import json
import time
import asyncio
import logging
import sqlite3

from dotenv import load_dotenv
from .send_ping import send_ping
from .utils import escape_md, chain_display_name, build_trading_links
from .mention_store import store

load_dotenv()
logger = logging.getLogger(__name__)

WR_THRESHOLD = float(os.getenv("HIGH_WR_THRESHOLD", "22.5"))
MAX_MCAP     = float(os.getenv("HIGH_WR_MAX_MCAP", "100000"))
CHAT_ID      = os.getenv("HIGH_WR_CHAT_ID", "") or os.getenv("YOUR_TELEGRAM_USER_ID", "")
DASH_DB      = os.getenv("DASH_DB_PATH", "dashboard/data/dash.db")
WIN_X        = 2.0  # keep in sync with WIN_X in dashboard/main.py

SEEN_FILE     = "data/high_wr_seen.json"
SEEN_MAX_AGE  = 30 * 24 * 3600  # prune seen entries older than 30 days


# ── Persistent (caller, group, token) seen-set ────────────────────────────

def _key(caller_key: str, group_name: str, address: str) -> str:
    return f"{caller_key}|{group_name}|{address}"


def _load_seen() -> dict:
    """Load seen-set from disk; on very first run, seed from ca_history so
    historical scans (rule 3) never trigger a notification."""
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r") as f:
                seen = json.load(f)
            cutoff = time.time() - SEEN_MAX_AGE
            seen = {k: ts for k, ts in seen.items() if ts > cutoff}
            logger.info(f"✅ Loaded high-WR seen-set: {len(seen)} combos")
            return seen
        except Exception as e:
            logger.warning(f"Could not load {SEEN_FILE}: {e}")
            return {}

    # First run: seed from persistent CA history. Note: ca_history keeps only
    # the FIRST caller per (address, group), so non-first historical scanners
    # can't be seeded — acceptable one-time limitation.
    seen = {}
    try:
        for address, entries in store._ca_history.items():
            for e in entries:
                caller_key = e.get("sender_id") or e.get("sender_name") or ""
                if caller_key:
                    seen[_key(caller_key, e.get("group_name", ""), address)] = e.get("timestamp", time.time())
        logger.info(f"🌱 Seeded high-WR seen-set from ca_history: {len(seen)} combos")
    except Exception as e:
        logger.warning(f"Seen-set seeding failed: {e}")
    _save_seen(seen)
    return seen


def _save_seen(seen: dict):
    try:
        os.makedirs("data", exist_ok=True)
        with open(SEEN_FILE, "w") as f:
            json.dump(seen, f)
    except Exception as e:
        logger.warning(f"Could not save {SEEN_FILE}: {e}")


_seen: dict = _load_seen()


# ── Win rate from memedash's read model ───────────────────────────────────

def _query_win_rate(caller_key: str, sender_name: str):
    """Blocking sqlite query — call via asyncio.to_thread.
    Returns (win_rate_pct, wins, completed_calls) or None if db unavailable."""
    if not os.path.exists(DASH_DB):
        return None
    try:
        conn = sqlite3.connect(f"file:{DASH_DB}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """SELECT first_mc, peak_mc_bot, IFNULL(peak_mc_live, 0) AS peak_mc_live
                   FROM calls
                   WHERE sender_id = ? OR (sender_id = '' AND sender_name = ?)""",
                (caller_key, sender_name),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        # e.g. old schema without peak_mc_live, or db mid-migration
        logger.warning(f"dash.db win-rate query failed: {e}")
        return None

    mults = []
    for first_mc, peak_bot, peak_live in rows:
        if first_mc and first_mc > 0:
            mults.append(max(first_mc, peak_bot or 0, peak_live or 0) / first_mc)
    if not mults:
        return (0.0, 0, 0)
    wins = sum(1 for m in mults if m >= WIN_X)
    return (round(100 * wins / len(mults), 1), wins, len(mults))


# ── Formatting ─────────────────────────────────────────────────────────────

def _fmt_mc_compact(n) -> str:
    """Compact mcap for the header line: 31.3K / 1.2M / 870. No $ prefix
    (a $ already appears next to the symbol in the header)."""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "n/a"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:.0f}"


# ── Filters (extend here) ─────────────────────────────────────────────────

def _passes_filters(win_rate: float, wins: int, completed: int) -> bool:
    """All extra gating beyond first-scan dedup lives here so future filters
    (min calls, chain whitelist, ...) are one-line additions.
    Note: the mcap gate lives in _passes_mcap() since it needs token metadata
    that is only fetched after this cheap check passes."""
    return win_rate > WR_THRESHOLD


def _passes_mcap(market_cap) -> bool:
    """True only when we have a positive mcap strictly below MAX_MCAP.
    Unknown/zero mcap fails — we can't confirm it's under the cap."""
    try:
        mc = float(market_cap or 0)
    except (TypeError, ValueError):
        return False
    return 0 < mc < MAX_MCAP


# ── Entry point ───────────────────────────────────────────────────────────

async def notify_high_wr_scan(address: str, chain: str, sender_name: str,
                              sender_id: str, group_name: str):
    """Fire-and-forget from the scraper on EVERY scan event (must run before
    any ping cooldown so no first scan is missed). Never raises."""
    try:
        caller_key = sender_id or sender_name
        if not caller_key or not address:
            return

        key = _key(caller_key, group_name, address)
        if key in _seen:
            return  # not a first scan for this (caller, group, token)
        # Mark seen immediately (before any await) — first-scan-only semantics,
        # and no double-fire if the same combo arrives twice in quick succession.
        _seen[key] = time.time()
        _save_seen(_seen)

        wr = await asyncio.to_thread(_query_win_rate, caller_key, sender_name)
        if wr is None:
            logger.warning("High-WR check skipped: dash.db unavailable")
            return
        win_rate, wins, completed = wr
        if not _passes_filters(win_rate, wins, completed):
            return

        # Qualified — fetch token metadata for the message. Lazy import to
        # avoid a circular import (telegram_scraper imports this module).
        from .telegram_scraper import fetch_token_quick
        token = await fetch_token_quick(address, chain)

        market_cap = (token or {}).get("market_cap", 0)
        if not _passes_mcap(market_cap):
            logger.info(
                f"High-WR skip (mcap): {sender_name} ({win_rate}%) → {address} "
                f"mcap={_fmt_mc_compact(market_cap)} (max {_fmt_mc_compact(MAX_MCAP)})"
            )
            return

        actual_chain = (token or {}).get("chain_id") or ("solana" if chain == "SOL" else "ethereum")

        name   = escape_md(token.get("name", "Unknown")) if token else "Unknown"
        symbol = escape_md(token.get("symbol", "???")) if token else "???"
        mcap   = _fmt_mc_compact(market_cap)
        caller = escape_md(" ".join(sender_name.split()))
        group  = escape_md(" ".join(group_name.split()))
        ts     = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

        msg = (
            f"🚨 *High Win Rate Caller*\n\n"
            f"👤 Caller: *{caller}*\n"
            f"📈 Win Rate: *{win_rate}%* ({wins}/{completed} ≥2x)\n\n"
            f"🪙 {name} | {mcap} | ${symbol}\n"
            f"🌐 Chain: {chain_display_name(actual_chain)}\n"
            f"`{address}`\n\n"
            f"💬 Group: {group}\n"
            f"🕐 {ts}\n\n"
            f"🔗 {build_trading_links(actual_chain, address)}\n\n"
            f"First scan of this token by {caller} in this group."
        )
        await send_ping(msg, chat_id=CHAT_ID)
        logger.info(f"🚨 High-WR alert: {sender_name} ({win_rate}%) → {address} in {group_name}")
    except Exception as e:
        logger.warning(f"High-WR notifier error: {e}")
