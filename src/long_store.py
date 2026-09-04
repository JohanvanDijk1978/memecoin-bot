"""
long_store.py
─────────────
Persistent state for the Long.xyz watcher.

SQLite at `data/long.db` (WAL), same shape and reasoning as
`multiwallet_store.py`: every read is "have I seen this before", every write is
a single hot-path row, and the process must survive a restart without
re-alerting on things it already announced. JSON files cannot give that
cheaply, and the multi-wallet watcher already proved the pattern here.

Tables
──────
`long_numeraires`   the assets Long's own frontend offers as a pairing asset —
                    the authoritative "Long supports this stock" set.
`rh_stocks`         every tokenised stock the Robinhood factory has deployed on
                    Robinhood Chain (chain 4663), listed or not. This is the
                    upstream pool Long picks from: at seeding time there were
                    206 of these against ~56 on Long.
`rh_feeds`          Chainlink EACAggregatorProxy feeds seen on Robinhood Chain,
                    keyed by feed address. A feed appearing for a ticker Long
                    does not list yet is the "they may be prepping it" signal.
`long_numeraire_use` the first coin ever launched against a numeraire. Long's
                    indexer knows this before we can diff a frontend deploy in
                    the worst case, so it doubles as a backstop detector.
`long_alerts`       dedup ledger. One row per alert key, ever. This is the ONLY
                    thing standing between a reconnect and a duplicate ping.
`long_cursors`      last processed block per watcher, the frontend chunk
                    fingerprint, and the `seeded` flag.
`long_latency`      one row per (subject, source) with the first time THIS bot
                    saw that subject through that source. The point of the
                    table is the comparison: which source is consistently first.

Nothing here does any I/O beyond sqlite, so the whole module is testable with
no network — which, from a Cowork session, is the only kind of test available.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_DB_PATH = os.getenv("LONG_DB_PATH") or os.path.join("data", "long.db")
_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


# ── connection ────────────────────────────────────────────────────────────────
def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _conn = conn
    _migrate(conn)
    return conn


def set_db_path(path: str) -> None:
    """Point the store at a different file. Tests use this; nothing else should."""
    global _DB_PATH, _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None
        _DB_PATH = path


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS long_numeraires (
            chain_id     INTEGER NOT NULL,
            address      TEXT    NOT NULL,
            symbol       TEXT,
            name         TEXT,
            kind         TEXT,
            decimals     INTEGER,
            feed_address TEXT,
            extra        TEXT,
            first_seen   REAL    NOT NULL,
            last_seen    REAL    NOT NULL,
            removed_at   REAL,
            PRIMARY KEY (chain_id, address)
        );

        CREATE TABLE IF NOT EXISTS venue_assets (
            venue        TEXT    NOT NULL,
            chain_id     INTEGER NOT NULL,
            address      TEXT    NOT NULL,
            symbol       TEXT,
            name         TEXT,
            kind         TEXT,
            decimals     INTEGER,
            feed_address TEXT,
            extra        TEXT,
            first_seen   REAL    NOT NULL,
            last_seen    REAL    NOT NULL,
            removed_at   REAL,
            PRIMARY KEY (venue, chain_id, address)
        );

        CREATE TABLE IF NOT EXISTS venue_first_use (
            venue         TEXT NOT NULL,
            numeraire     TEXT NOT NULL,
            token_address TEXT,
            token_symbol  TEXT,
            token_name    TEXT,
            created_ts    REAL,
            first_seen    REAL NOT NULL,
            PRIMARY KEY (venue, numeraire)
        );

        CREATE TABLE IF NOT EXISTS rh_stocks (
            address      TEXT PRIMARY KEY,
            symbol       TEXT,
            name         TEXT,
            uid          TEXT,
            block_number INTEGER,
            tx_hash      TEXT,
            chain_ts     REAL,
            first_seen   REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rh_feeds (
            address      TEXT PRIMARY KEY,
            description  TEXT,
            symbol       TEXT,
            block_number INTEGER,
            tx_hash      TEXT,
            chain_ts     REAL,
            first_seen   REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS long_numeraire_use (
            numeraire    TEXT PRIMARY KEY,
            token_address TEXT,
            token_symbol  TEXT,
            token_name    TEXT,
            created_ts    REAL,
            first_seen    REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS long_alerts (
            key        TEXT PRIMARY KEY,
            source     TEXT,
            subject    TEXT,
            sent_at    REAL NOT NULL,
            payload    TEXT
        );

        CREATE TABLE IF NOT EXISTS long_cursors (
            name  TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS long_latency (
            subject    TEXT NOT NULL,
            source     TEXT NOT NULL,
            seen_at    REAL NOT NULL,
            detail     TEXT,
            PRIMARY KEY (subject, source)
        );

        CREATE INDEX IF NOT EXISTS idx_venue_assets ON venue_assets(venue, chain_id);
        CREATE INDEX IF NOT EXISTS idx_latency_subject ON long_latency(subject);
        CREATE INDEX IF NOT EXISTS idx_alerts_sent ON long_alerts(sent_at);
        """
    )
    conn.commit()

    # One-time carry-over from the single-venue schema. `long_numeraires` and
    # `long_numeraire_use` were written before Pons and o1 existed here; their
    # rows are Long's by definition. Copied rather than dropped so a rollback to
    # the previous commit still finds its data.
    try:
        have = conn.execute("SELECT COUNT(*) AS n FROM venue_assets").fetchone()["n"]
        legacy = conn.execute("SELECT COUNT(*) AS n FROM long_numeraires").fetchone()["n"]
        if legacy and not have:
            conn.execute(
                "INSERT OR IGNORE INTO venue_assets(venue, chain_id, address, symbol, "
                "name, kind, decimals, feed_address, extra, first_seen, last_seen, removed_at) "
                "SELECT 'long', chain_id, address, symbol, name, kind, decimals, "
                "feed_address, extra, first_seen, last_seen, removed_at FROM long_numeraires")
            conn.execute(
                "INSERT OR IGNORE INTO venue_first_use(venue, numeraire, token_address, "
                "token_symbol, token_name, created_ts, first_seen) "
                "SELECT 'long', numeraire, token_address, token_symbol, token_name, "
                "created_ts, first_seen FROM long_numeraire_use")
            conn.commit()
            logger.info("long: migrated %d single-venue rows into venue_assets", legacy)
    except sqlite3.Error as e:
        logger.warning("long: venue migration skipped: %s", e)


# ── cursors ───────────────────────────────────────────────────────────────────
def get_cursor(name: str, default: Optional[str] = None) -> Optional[str]:
    with _lock:
        row = _connect().execute(
            "SELECT value FROM long_cursors WHERE name = ?", (name,)
        ).fetchone()
    return row["value"] if row else default


def set_cursor(name: str, value: Any) -> None:
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO long_cursors(name, value) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, str(value)),
        )
        c.commit()


def is_seeded(scope: str) -> bool:
    return get_cursor(f"seeded:{scope}") == "1"


def mark_seeded(scope: str) -> None:
    set_cursor(f"seeded:{scope}", "1")


# ── venue asset lists (what each launchpad offers) ───────────────────────────
def known_numeraires(chain_id: int, venue: str = "long") -> dict[str, sqlite3.Row]:
    """Everything `venue` currently offers as a pairing asset, by address."""
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM venue_assets WHERE venue = ? AND chain_id = ? "
            "AND removed_at IS NULL", (venue, chain_id),
        ).fetchall()
    return {r["address"].lower(): r for r in rows}


def all_venue_assets(venue: str) -> list[sqlite3.Row]:
    with _lock:
        return _connect().execute(
            "SELECT * FROM venue_assets WHERE venue = ? AND removed_at IS NULL "
            "ORDER BY kind, symbol", (venue,)).fetchall()


def venues_offering(address: str) -> list[str]:
    """Which venues list this asset. Turns a stock-deploy alert from 'not on
    Long' into 'already on o1 and Pons, not on Long', which is the actually
    useful sentence."""
    with _lock:
        rows = _connect().execute(
            "SELECT DISTINCT venue FROM venue_assets WHERE address = ? "
            "AND removed_at IS NULL ORDER BY venue", (address.lower(),)).fetchall()
    return [r["venue"] for r in rows]


def upsert_numeraire(chain_id: int, n: dict, venue: str = "long",
                     *, now: Optional[float] = None) -> bool:
    """Insert or refresh one asset for one venue. True when the row is NEW."""
    now = now if now is not None else time.time()
    addr = (n.get("address") or "").lower()
    with _lock:
        c = _connect()
        cur = c.execute(
            "SELECT address FROM venue_assets WHERE venue = ? AND chain_id = ? AND address = ?",
            (venue, chain_id, addr),
        ).fetchone()
        is_new = cur is None
        if is_new:
            c.execute(
                "INSERT INTO venue_assets(venue, chain_id, address, symbol, name, kind, "
                "decimals, feed_address, extra, first_seen, last_seen) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    venue, chain_id, addr, n.get("symbol"), n.get("name"), n.get("kind"),
                    n.get("decimals"), (n.get("feed") or None),
                    json.dumps(n.get("extra") or {}), now, now,
                ),
            )
        else:
            c.execute(
                "UPDATE venue_assets SET symbol=?, name=?, kind=?, decimals=?, "
                "feed_address=COALESCE(?, feed_address), last_seen=?, removed_at=NULL "
                "WHERE venue=? AND chain_id=? AND address=?",
                (
                    n.get("symbol"), n.get("name"), n.get("kind"), n.get("decimals"),
                    (n.get("feed") or None), now, venue, chain_id, addr,
                ),
            )
        c.commit()
    return is_new


def mark_numeraire_removed(chain_id: int, address: str, venue: str = "long",
                           *, now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE venue_assets SET removed_at=? WHERE venue=? AND chain_id=? AND address=?",
            (now, venue, chain_id, address.lower()),
        )
        c.commit()


# ── Robinhood stock tokens (upstream pool) ────────────────────────────────────
def has_rh_stock(address: str) -> bool:
    with _lock:
        return _connect().execute(
            "SELECT 1 FROM rh_stocks WHERE address = ?", (address.lower(),)
        ).fetchone() is not None


def add_rh_stock(s: dict, *, now: Optional[float] = None) -> bool:
    """Returns True when this stock token was not already known."""
    now = now if now is not None else time.time()
    with _lock:
        c = _connect()
        try:
            c.execute(
                "INSERT INTO rh_stocks(address, symbol, name, uid, block_number, "
                "tx_hash, chain_ts, first_seen) VALUES(?,?,?,?,?,?,?,?)",
                (
                    (s.get("address") or "").lower(), s.get("symbol"), s.get("name"),
                    s.get("uid"), s.get("block_number"), s.get("tx_hash"),
                    s.get("chain_ts"), now,
                ),
            )
            c.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def all_rh_stocks() -> list[sqlite3.Row]:
    with _lock:
        return _connect().execute(
            "SELECT * FROM rh_stocks ORDER BY block_number DESC"
        ).fetchall()


def unlisted_rh_stocks(chain_id: int, venue: Optional[str] = None) -> list[sqlite3.Row]:
    """Stock tokens that exist on Robinhood Chain but are not offered.

    With `venue`, "not offered by that venue". Without, "not offered by ANY
    venue we watch" — the genuinely untouched pool.
    """
    with _lock:
        if venue:
            return _connect().execute(
                "SELECT s.* FROM rh_stocks s LEFT JOIN venue_assets v "
                "  ON v.address = s.address AND v.venue = ? AND v.chain_id = ? "
                "  AND v.removed_at IS NULL "
                "WHERE v.address IS NULL ORDER BY s.block_number DESC",
                (venue, chain_id),
            ).fetchall()
        return _connect().execute(
            "SELECT s.* FROM rh_stocks s LEFT JOIN venue_assets v "
            "  ON v.address = s.address AND v.chain_id = ? AND v.removed_at IS NULL "
            "WHERE v.address IS NULL ORDER BY s.block_number DESC",
            (chain_id,),
        ).fetchall()


# ── Chainlink feeds ───────────────────────────────────────────────────────────
def add_feed(f: dict, *, now: Optional[float] = None) -> bool:
    now = now if now is not None else time.time()
    with _lock:
        c = _connect()
        try:
            c.execute(
                "INSERT INTO rh_feeds(address, description, symbol, block_number, "
                "tx_hash, chain_ts, first_seen) VALUES(?,?,?,?,?,?,?)",
                (
                    (f.get("address") or "").lower(), f.get("description"), f.get("symbol"),
                    f.get("block_number"), f.get("tx_hash"), f.get("chain_ts"), now,
                ),
            )
            c.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def known_feed_addresses() -> set[str]:
    with _lock:
        rows = _connect().execute("SELECT address FROM rh_feeds").fetchall()
    return {r["address"] for r in rows}


# ── first coin per numeraire ──────────────────────────────────────────────────
def numeraire_first_use(numeraire: str, venue: str = "long") -> Optional[sqlite3.Row]:
    with _lock:
        return _connect().execute(
            "SELECT * FROM venue_first_use WHERE venue = ? AND numeraire = ?",
            (venue, numeraire.lower()),
        ).fetchone()


def record_numeraire_use(u: dict, venue: str = "long", *,
                         now: Optional[float] = None) -> bool:
    """True when this numeraire had never been used on this venue before."""
    now = now if now is not None else time.time()
    with _lock:
        c = _connect()
        try:
            c.execute(
                "INSERT INTO venue_first_use(venue, numeraire, token_address, "
                "token_symbol, token_name, created_ts, first_seen) VALUES(?,?,?,?,?,?,?)",
                (
                    venue, (u.get("numeraire") or "").lower(), u.get("token_address"),
                    u.get("token_symbol"), u.get("token_name"), u.get("created_ts"), now,
                ),
            )
            c.commit()
            return True
        except sqlite3.IntegrityError:
            return False


# ── dedup ─────────────────────────────────────────────────────────────────────
def claim_alert(key: str, source: str, subject: str, payload: Optional[dict] = None,
                *, now: Optional[float] = None) -> bool:
    """Atomically claim an alert key.

    Returns True exactly once per key, for the lifetime of the database. Every
    notification path goes through this, so a reconnect, a reconcile sweep and
    a second detector finding the same thing all collapse to one ping.
    """
    now = now if now is not None else time.time()
    with _lock:
        c = _connect()
        try:
            c.execute(
                "INSERT INTO long_alerts(key, source, subject, sent_at, payload) "
                "VALUES(?,?,?,?,?)",
                (key, source, subject, now, json.dumps(payload or {})[:4000]),
            )
            c.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def alert_count() -> int:
    with _lock:
        return _connect().execute("SELECT COUNT(*) AS n FROM long_alerts").fetchone()["n"]


# ── latency instrumentation ───────────────────────────────────────────────────
def record_sighting(subject: str, source: str, detail: Optional[str] = None,
                    *, now: Optional[float] = None) -> bool:
    """First time this bot saw `subject` via `source`. Later calls are ignored,
    so the row always holds the EARLIEST sighting — which is the whole point."""
    now = now if now is not None else time.time()
    with _lock:
        c = _connect()
        try:
            c.execute(
                "INSERT INTO long_latency(subject, source, seen_at, detail) VALUES(?,?,?,?)",
                (subject, source, now, (detail or "")[:500]),
            )
            c.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def sightings(subject: str) -> list[sqlite3.Row]:
    with _lock:
        return _connect().execute(
            "SELECT * FROM long_latency WHERE subject = ? ORDER BY seen_at ASC", (subject,)
        ).fetchall()


def latency_report(limit: int = 50) -> list[dict]:
    """Per subject: every source that saw it and how far behind the first it was."""
    with _lock:
        rows = _connect().execute(
            "SELECT subject, source, seen_at, detail FROM long_latency "
            "ORDER BY seen_at DESC LIMIT ?", (limit * 6,)
        ).fetchall()
    by_subject: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_subject.setdefault(r["subject"], []).append(r)
    out = []
    for subject, rs in list(by_subject.items())[:limit]:
        rs = sorted(rs, key=lambda r: r["seen_at"])
        base = rs[0]["seen_at"]
        out.append({
            "subject": subject,
            "first_source": rs[0]["source"],
            "first_seen": base,
            "sources": [
                {"source": r["source"], "seen_at": r["seen_at"],
                 "delta_ms": int((r["seen_at"] - base) * 1000), "detail": r["detail"]}
                for r in rs
            ],
        })
    return sorted(out, key=lambda d: d["first_seen"], reverse=True)
