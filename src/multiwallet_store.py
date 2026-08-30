"""
multiwallet_store.py
────────────────────
SQLite storage for the multi-wallet buy watcher.

Why a database when the rest of the bot keeps JSON files: every write here is a
single row in a hot path (a buy can land at any second, from five chains at
once), and every read is a range query — "which unique wallets bought this CA
in the last 120 minutes". A JSON blob rewritten on every event loses both, and
loses the one property the feature depends on: after a restart the bot must
know which transactions it already handled and which alert it already sent, or
the channel gets a duplicate 3-wallets post for a convergence it announced
before the restart.

sqlite3 is stdlib and the file lives in `data/`, which is gitignored — so this
adds no dependency, no service, and nothing to deploy. Same shape as memedash's
dash.db (WAL, short busy timeout) so the two are operationally familiar.

Every table is keyed so that replaying the same transaction is a no-op:
  mw_buys    PRIMARY KEY (chain, tx, wallet, token)  — dedup by construction
  mw_alerts  PRIMARY KEY (list, chain, token)        — remembers the highest
             wallet count already announced, which is what makes a restart (or
             a reconcile sweep re-reading old signatures) silent.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

DB_FILE = os.getenv("MULTIWALLET_DB", "data/multiwallet.db")

DEFAULT_LIST = "ALL"

# The rule lives in the DB so /multirule can change it from a phone without a
# redeploy. These are only the values a fresh database starts with.
DEFAULT_RULE: dict[str, int] = {
    "min_wallets": 3,     # unique wallets needed before the first alert
    "window_min": 120,    # how far back buys count toward that number
    "max_wallets": 6,     # last milestone that still posts; above this, silence
    "cooldown_h": 24,     # after the last milestone, mute this token this long
}

_initialised = False


# ── connection ────────────────────────────────────────────────────────────
def db() -> sqlite3.Connection:
    """A fresh connection. Callers use it as a context manager, which commits.

    WAL plus a real busy timeout: the watcher writes from its own task while a
    /list or /buys command reads from the bot's task, and both share the loop.
    """
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema() -> None:
    """Create the tables. Cheap and idempotent; safe to call on every start."""
    global _initialised
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS mw_wallets (
          key        TEXT NOT NULL,          -- normalised address (0x lowered)
          list       TEXT NOT NULL DEFAULT 'ALL',
          address    TEXT NOT NULL,          -- as the chain writes it
          name       TEXT NOT NULL DEFAULT '',
          kind       TEXT NOT NULL DEFAULT '',   -- 'sol' | 'evm'
          added_at   REAL NOT NULL,
          PRIMARY KEY (key, list)
        );
        CREATE TABLE IF NOT EXISTS mw_buys (
          chain      TEXT NOT NULL,          -- dexscreener chainId: solana, ethereum, base, bsc, robinhood
          tx         TEXT NOT NULL,          -- signature or tx hash
          wallet     TEXT NOT NULL,          -- normalised address
          token      TEXT NOT NULL,          -- mint / contract, normalised
          ts         REAL NOT NULL,          -- block time, epoch seconds
          amount     REAL NOT NULL DEFAULT 0,    -- tokens received
          spent_usd  REAL NOT NULL DEFAULT 0,    -- quote leg in USD, 0 when unknown
          price      REAL NOT NULL DEFAULT 0,    -- USD per token, from this tx
          mcap       REAL NOT NULL DEFAULT 0,    -- market cap at buy time, 0 until known
          quote_sym  TEXT NOT NULL DEFAULT '',   -- what was paid with: SOL, ETH, USDC…
          quote_amt  REAL NOT NULL DEFAULT 0,    -- how much of it, in whole units
          symbol     TEXT NOT NULL DEFAULT '',
          seen_at    REAL NOT NULL,
          PRIMARY KEY (chain, tx, wallet, token)
        );
        CREATE INDEX IF NOT EXISTS idx_mw_buys_token ON mw_buys(chain, token, ts);
        CREATE INDEX IF NOT EXISTS idx_mw_buys_ts    ON mw_buys(ts);
        CREATE TABLE IF NOT EXISTS mw_alerts (
          list       TEXT NOT NULL,
          chain      TEXT NOT NULL,
          token      TEXT NOT NULL,
          max_count  INTEGER NOT NULL DEFAULT 0,  -- highest wallet count posted
          first_at   REAL NOT NULL DEFAULT 0,
          last_at    REAL NOT NULL DEFAULT 0,
          message_id INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (list, chain, token)
        );
        CREATE TABLE IF NOT EXISTS mw_cursors (
          key        TEXT PRIMARY KEY,       -- 'sol:<wallet>' | 'evm:<chain>'
          value      TEXT NOT NULL DEFAULT '',
          updated_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS mw_tokens (
          chain      TEXT NOT NULL,
          address    TEXT NOT NULL,
          symbol     TEXT DEFAULT '',
          name       TEXT DEFAULT '',
          image      TEXT DEFAULT '',
          price      REAL DEFAULT 0,
          mcap       REAL DEFAULT 0,
          supply     REAL DEFAULT 0,
          liq        REAL DEFAULT 0,
          decimals   INTEGER DEFAULT -1,
          links_json TEXT DEFAULT '{}',
          updated_at REAL DEFAULT 0,
          PRIMARY KEY (chain, address)
        );
        CREATE TABLE IF NOT EXISTS mw_config (
          key   TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """)
    _initialised = True


def ensure_schema() -> None:
    if not _initialised:
        init_schema()


# ── small helpers ─────────────────────────────────────────────────────────
def normalize(address: str) -> str:
    """Case-fold EVM addresses only. Base58 is case-sensitive — lowering a
    Solana address turns it into a different (invalid) address."""
    a = (address or "").strip()
    return a.lower() if a.startswith("0x") else a


def wallet_kind(address: str) -> str:
    """'evm', 'sol' or '' when the string is not an address at all."""
    a = (address or "").strip()
    if a.startswith("0x") and len(a) == 42:
        try:
            int(a[2:], 16)
            return "evm"
        except ValueError:
            return ""
    if 32 <= len(a) <= 44 and all(ch in _B58 for ch in a):
        return "sol"
    return ""


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _rows(sql: str, args: Iterable = ()) -> list[dict]:
    ensure_schema()
    with db() as c:
        return [dict(r) for r in c.execute(sql, tuple(args)).fetchall()]


# ── rule ──────────────────────────────────────────────────────────────────
def get_rule() -> dict[str, int]:
    """The active rule, defaults filled in for anything never set."""
    rule = dict(DEFAULT_RULE)
    for row in _rows("SELECT key, value FROM mw_config WHERE key LIKE 'rule.%'"):
        name = row["key"].split(".", 1)[1]
        if name in rule:
            try:
                rule[name] = int(float(row["value"]))
            except (TypeError, ValueError):
                pass
    return rule


def set_rule(**changes: Any) -> dict[str, int]:
    """Persist the parts of the rule that were passed. Returns the new rule."""
    ensure_schema()
    with db() as c:
        for name, value in changes.items():
            if name not in DEFAULT_RULE or value is None:
                continue
            c.execute("INSERT INTO mw_config(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (f"rule.{name}", str(int(value))))
    return get_rule()


# ── wallets ───────────────────────────────────────────────────────────────
def add_wallet(address: str, name: str, list_name: str = DEFAULT_LIST) -> dict:
    """Add or rename a monitored wallet. Returns {'status', 'wallet'}.

    status is 'added', 'renamed', 'exists' or 'invalid' — the caller turns that
    into the reply, and the watcher uses 'added' to decide whether it has to
    open a new subscription.
    """
    ensure_schema()
    kind = wallet_kind(address)
    if not kind:
        return {"status": "invalid", "wallet": None}
    key = normalize(address)
    name = (name or "").strip()[:40]
    existing = _rows("SELECT * FROM mw_wallets WHERE key=? AND list=?", (key, list_name))
    with db() as c:
        if existing:
            if existing[0]["name"] == name or not name:
                return {"status": "exists", "wallet": existing[0]}
            c.execute("UPDATE mw_wallets SET name=? WHERE key=? AND list=?",
                      (name, key, list_name))
            return {"status": "renamed", "wallet": {**existing[0], "name": name}}
        c.execute("INSERT INTO mw_wallets(key, list, address, name, kind, added_at) "
                  "VALUES(?,?,?,?,?,?)",
                  (key, list_name, address.strip(), name, kind, time.time()))
    return {"status": "added", "wallet": {"key": key, "list": list_name,
                                          "address": address.strip(), "name": name,
                                          "kind": kind, "added_at": time.time()}}


def remove_wallet(needle: str, list_name: Optional[str] = None) -> list[dict]:
    """Remove by address or by display name. Returns the rows removed."""
    ensure_schema()
    needle = (needle or "").strip()
    if not needle:
        return []
    key = normalize(needle)
    where = "(key=? OR LOWER(name)=LOWER(?))"
    args: list[Any] = [key, needle]
    if list_name:
        where += " AND list=?"
        args.append(list_name)
    doomed = _rows(f"SELECT * FROM mw_wallets WHERE {where}", args)
    if doomed:
        with db() as c:
            c.execute(f"DELETE FROM mw_wallets WHERE {where}", tuple(args))
    return doomed


def list_wallets(list_name: Optional[str] = None, kind: str = "") -> list[dict]:
    sql = "SELECT * FROM mw_wallets"
    args: list[Any] = []
    clauses = []
    if list_name:
        clauses.append("list=?")
        args.append(list_name)
    if kind:
        clauses.append("kind=?")
        args.append(kind)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return _rows(sql + " ORDER BY added_at", args)


def wallet_names() -> dict[str, str]:
    """normalised address → display name, for labelling a detected buy."""
    return {r["key"]: (r["name"] or r["address"][:4] + "…" + r["address"][-4:])
            for r in list_wallets()}


def wallet_lists(key: str) -> list[str]:
    """Which lists a wallet belongs to (one for now; the column is here so
    named lists cost a command, not a migration)."""
    return [r["list"] for r in
            _rows("SELECT list FROM mw_wallets WHERE key=?", (key,))]


# ── buys ──────────────────────────────────────────────────────────────────
def record_buy(buy: dict) -> bool:
    """Store one normalised buy. Returns False when it was already known —
    which is the whole dedup story for reconnects, sweeps and restarts."""
    ensure_schema()
    with db() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO mw_buys"
            "(chain, tx, wallet, token, ts, amount, spent_usd, price, mcap,"
            " quote_sym, quote_amt, symbol, seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (buy["chain"], buy["tx"], normalize(buy["wallet"]), normalize(buy["token"]),
             float(buy.get("ts") or time.time()), float(buy.get("amount") or 0),
             float(buy.get("spent_usd") or 0), float(buy.get("price") or 0),
             float(buy.get("mcap") or 0), (buy.get("quote_sym") or "")[:12],
             float(buy.get("quote_amt") or 0), (buy.get("symbol") or "")[:24], time.time()))
        return cur.rowcount > 0


def known_tx(chain: str, tx: str) -> bool:
    return bool(_rows("SELECT 1 FROM mw_buys WHERE chain=? AND tx=? LIMIT 1", (chain, tx)))


def buys_in_window(chain: str, token: str, since: float) -> list[dict]:
    """Every buy of this token since `since`, oldest first."""
    return _rows("SELECT * FROM mw_buys WHERE chain=? AND token=? AND ts>=? "
                 "ORDER BY ts", (chain, normalize(token), since))


def recent_buys(limit: int = 20) -> list[dict]:
    return _rows("SELECT * FROM mw_buys ORDER BY ts DESC LIMIT ?", (limit,))


def buy_count() -> int:
    rows = _rows("SELECT COUNT(*) AS n FROM mw_buys")
    return int(rows[0]["n"]) if rows else 0


def prune(keep_days: float = 7) -> int:
    """Old buys are only history; the window never looks back further than
    `window_min`. Keeps the file small without touching alert state."""
    cutoff = time.time() - keep_days * 86400
    ensure_schema()
    with db() as c:
        cur = c.execute("DELETE FROM mw_buys WHERE ts < ?", (cutoff,))
        c.execute("DELETE FROM mw_alerts WHERE last_at < ?", (cutoff,))
        return cur.rowcount


# ── alerts ────────────────────────────────────────────────────────────────
def alert_state(list_name: str, chain: str, token: str) -> Optional[dict]:
    rows = _rows("SELECT * FROM mw_alerts WHERE list=? AND chain=? AND token=?",
                 (list_name, chain, normalize(token)))
    return rows[0] if rows else None


def record_alert(list_name: str, chain: str, token: str, count: int,
                 message_id: int = 0) -> None:
    ensure_schema()
    now = time.time()
    with db() as c:
        c.execute(
            "INSERT INTO mw_alerts(list, chain, token, max_count, first_at, last_at, message_id)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(list, chain, token) DO UPDATE SET"
            "   max_count=MAX(max_count, excluded.max_count),"
            "   last_at=excluded.last_at,"
            "   message_id=excluded.message_id",
            (list_name, chain, normalize(token), int(count), now, now, int(message_id or 0)))


def recent_alerts(limit: int = 10) -> list[dict]:
    return _rows("SELECT * FROM mw_alerts ORDER BY last_at DESC LIMIT ?", (limit,))


# ── cursors ───────────────────────────────────────────────────────────────
def get_cursor(key: str) -> str:
    rows = _rows("SELECT value FROM mw_cursors WHERE key=?", (key,))
    return rows[0]["value"] if rows else ""


def set_cursor(key: str, value: str) -> None:
    ensure_schema()
    with db() as c:
        c.execute("INSERT INTO mw_cursors(key, value, updated_at) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                  "updated_at=excluded.updated_at",
                  (key, str(value), time.time()))


# ── token metadata cache ──────────────────────────────────────────────────
def get_token(chain: str, address: str, max_age: float = 0) -> Optional[dict]:
    rows = _rows("SELECT * FROM mw_tokens WHERE chain=? AND address=?",
                 (chain, normalize(address)))
    if not rows:
        return None
    row = rows[0]
    if max_age and time.time() - float(row["updated_at"] or 0) > max_age:
        return None
    try:
        row["links"] = json.loads(row.get("links_json") or "{}")
    except (TypeError, ValueError):
        row["links"] = {}
    return row


# The columns put_token may write, and how a supplied value is coerced. A key
# the caller omits is left alone — see the docstring below for why that matters.
_TOKEN_COLUMNS = {
    "symbol":   lambda v: (str(v or ""))[:24],
    "name":     lambda v: (str(v or ""))[:64],
    "image":    lambda v: str(v or ""),
    "price":    lambda v: float(v or 0),
    "mcap":     lambda v: float(v or 0),
    "supply":   lambda v: float(v or 0),
    "liq":      lambda v: float(v or 0),
    "decimals": lambda v: int(v if v is not None else -1),
}

# Writing any of these means "this is token metadata, the cache is fresh".
# A decimals-only write is not metadata and must not restart fetch_token's TTL.
_TOKEN_META = ("symbol", "name", "image", "price", "mcap", "supply", "liq", "links")


def put_token(chain: str, address: str, data: dict) -> None:
    """Upsert ONLY the fields the caller actually supplied.

    The whole-row version of this cost an evening of "? · Market cap: —" on
    every EVM alert. `EvmWatcher._decimals_for` writes `{"decimals": 18}` for
    each new token it sees; with the other columns defaulted, that INSERT wiped
    the symbol, name, image and market cap Dexscreener had stored — and bumped
    `updated_at`, so `fetch_token` then served the blanked row from cache and
    never asked Dexscreener again. Partial writes are the fix: a caller states
    what it knows and says nothing about the rest.
    """
    ensure_schema()
    fields = {name: cast(data[name])
              for name, cast in _TOKEN_COLUMNS.items() if name in data}
    if "links" in data:
        fields["links_json"] = json.dumps(data.get("links") or {})
    if not fields:
        return
    # 0 here means "not a metadata write"; the ON CONFLICT clause keeps the
    # existing timestamp in that case, and a fresh row starts stale.
    fields["updated_at"] = time.time() if any(k in data for k in _TOKEN_META) else 0.0

    updates = []
    for name in fields:
        if name == "decimals":
            # a token's decimals never change; a caller passing -1 must not
            # erase what an earlier eth_call established
            updates.append("decimals=CASE WHEN excluded.decimals >= 0"
                           " THEN excluded.decimals ELSE mw_tokens.decimals END")
        elif name == "updated_at":
            updates.append("updated_at=CASE WHEN excluded.updated_at > 0"
                           " THEN excluded.updated_at ELSE mw_tokens.updated_at END")
        else:
            updates.append(f"{name}=excluded.{name}")

    columns = ["chain", "address", *fields]
    values = [chain, normalize(address), *fields.values()]
    with db() as c:
        c.execute(f"INSERT INTO mw_tokens({', '.join(columns)})"
                  f" VALUES({', '.join('?' * len(columns))})"
                  f" ON CONFLICT(chain, address) DO UPDATE SET {', '.join(updates)}",
                  values)
