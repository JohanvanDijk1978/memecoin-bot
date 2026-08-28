"""
wgroups.py — Wallet Groups: storage, the convergence poller, and the API.

The page answers one question: which memecoins are held right now by more than
one wallet I track. A token earns a card the moment a second tracked wallet is
in it, and loses the card the moment the count drops back below two.

Shape of a round (holdings every WG_HOLDINGS_INTERVAL seconds):

  1. read every tracked wallet's positions (one request per wallet on Solana,
     one per wallet per chain on EVM)
  2. work out which tokens two or more wallets of the *same group* hold —
     this is the cheap side, and it runs before anything is priced
  3. price only those tokens, plus the ones already on a card
  4. fold the balance changes since the last round into the observed cost
     basis, now that there is a price to value them at
  5. rebuild each group's convergence set; anything that appeared or
     disappeared pushes an SSE event so the page pops the card without waiting
  6. for a few qualifying positions per round, try to replace the observed
     cost basis with the real one from the wallet's swap history

Prices refresh on their own faster loop (WG_PRICE_INTERVAL) over the small set
of tokens currently on a card, so PnL moves without re-scanning any wallet.

With no groups configured, every loop is a no-op: this costs nothing until it
is used.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time

import httpx
from fastapi import APIRouter, Body, HTTPException

import alerts as A
import discover as D
import wallets as W

log = logging.getLogger("memedash.wgroups")
router = APIRouter()

HOLDINGS_INTERVAL = float(os.getenv("WG_HOLDINGS_INTERVAL", "45"))
PRICE_INTERVAL = float(os.getenv("WG_PRICE_INTERVAL", "15"))
MIN_POSITION_USD = float(os.getenv("WG_MIN_POSITION_USD", "50"))  # below this a wallet is not "in"
BASIS_PER_ROUND = int(os.getenv("WG_BASIS_PER_ROUND", "8"))       # chain cost-basis lookups per round
BASIS_RETRY = 6 * 3600            # how long before a failed basis lookup is retried
MIN_WALLETS = 2                   # a card exists at two wallets — the whole point of the page
COOL_SECONDS = float(os.getenv("WG_COOL_SECONDS", "900"))   # how long a card lingers after it breaks
DISCOVER_INTERVAL = float(os.getenv("WG_DISCOVER_INTERVAL", "21600"))   # 6h background refresh

_db = None                        # set by configure()
_notify = None
_scan_now = asyncio.Event()
_state: dict = {"scanned_at": 0, "scanning": False, "error": "", "round_ms": 0}


def configure(db_factory, notify=None) -> None:
    """Wire up main.py's connection factory and SSE notifier."""
    global _db, _notify
    _db = db_factory
    _notify = notify


def init_schema() -> None:
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS wgroups (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wgroup_wallets (
          id INTEGER PRIMARY KEY,
          group_id INTEGER NOT NULL,
          address TEXT NOT NULL,
          label TEXT NOT NULL DEFAULT '',
          kind TEXT NOT NULL DEFAULT '',
          added_at REAL NOT NULL,
          UNIQUE(group_id, address)
        );
        CREATE INDEX IF NOT EXISTS idx_wgw_group ON wgroup_wallets(group_id);
        -- keyed by wallet ADDRESS, not membership: the same wallet in two
        -- groups is scanned once and its positions are shared
        CREATE TABLE IF NOT EXISTS wallet_holdings (
          wallet TEXT NOT NULL,
          token TEXT NOT NULL,
          chain_id TEXT NOT NULL DEFAULT '',
          amount REAL NOT NULL DEFAULT 0,
          decimals INTEGER DEFAULT 0,
          first_seen REAL NOT NULL,
          last_seen REAL NOT NULL,
          PRIMARY KEY (wallet, token)
        );
        CREATE INDEX IF NOT EXISTS idx_wh_token ON wallet_holdings(token);
        CREATE TABLE IF NOT EXISTS wallet_lots (
          wallet TEXT NOT NULL,
          token TEXT NOT NULL,
          cost_usd REAL DEFAULT 0,
          amount REAL DEFAULT 0,
          realized_usd REAL DEFAULT 0,
          source TEXT DEFAULT 'observed',
          checked_at REAL DEFAULT 0,
          PRIMARY KEY (wallet, token)
        );
        CREATE TABLE IF NOT EXISTS wgroup_tokens (
          address TEXT PRIMARY KEY,
          chain_id TEXT DEFAULT '',
          name TEXT DEFAULT '',
          symbol TEXT DEFAULT '',
          image TEXT DEFAULT '',
          price REAL DEFAULT 0,
          mc REAL DEFAULT 0,
          supply REAL DEFAULT 0,
          liq REAL DEFAULT 0,
          decimals INTEGER DEFAULT 0,
          updated_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS wgroup_seen (
          group_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          detected_at REAL NOT NULL,
          PRIMARY KEY (group_id, token)
        );
        -- tokens dismissed from the page with the card's X. Real state, like the
        -- group definitions: it does not re-derive from a scan. A hidden token is
        -- still scanned and still counted in the group's totals -- it just never
        -- gets a card again, which is the point of dismissing it.
        CREATE TABLE IF NOT EXISTS wgroup_hidden (
          group_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          hidden_at REAL NOT NULL,
          PRIMARY KEY (group_id, token)
        );
        -- who is currently counted in a convergence. Diffing this between
        -- rounds is what turns "the number went down" into "Whale B sold out",
        -- and it gives entry order for free.
        CREATE TABLE IF NOT EXISTS wgroup_members (
          group_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          wallet TEXT NOT NULL,
          joined_at REAL NOT NULL,
          last_value REAL DEFAULT 0,
          PRIMARY KEY (group_id, token, wallet)
        );
        -- a wallet that fully left a carded token. Kept for the cooling window
        -- and purged with the card, because an exit is only news while the
        -- token is still on screen.
        CREATE TABLE IF NOT EXISTS wgroup_exits (
          group_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          wallet TEXT NOT NULL,
          label TEXT DEFAULT '',
          exited_at REAL NOT NULL,
          last_value REAL DEFAULT 0,
          PRIMARY KEY (group_id, token, wallet)
        );
        -- the highest wallet count already announced for a token. Johan asked
        -- for one DM per milestone, so a sell-and-rebuy back to the same count
        -- is silent forever while a real 2->3->4 climb sends all three.
        -- suggested wallets, rebuilt wholesale by each scan. A read model:
        -- deleting it costs nothing but the next scan.
        CREATE TABLE IF NOT EXISTS wgroup_candidates (
          group_id INTEGER NOT NULL,
          wallet TEXT NOT NULL,
          convergences INTEGER NOT NULL DEFAULT 0,
          tokens_json TEXT DEFAULT '[]',
          pnl_usd REAL,
          early_n INTEGER DEFAULT 0,
          score REAL DEFAULT 0,
          source TEXT DEFAULT '',
          scanned_at REAL NOT NULL DEFAULT 0,
          PRIMARY KEY (group_id, wallet)
        );
        CREATE TABLE IF NOT EXISTS wgroup_alerts (
          group_id INTEGER NOT NULL,
          token TEXT NOT NULL,
          max_count INTEGER NOT NULL DEFAULT 0,
          sent_at REAL NOT NULL DEFAULT 0,
          PRIMARY KEY (group_id, token)
        );
        """)
        try:   # v1.38: a card lingers after it breaks instead of vanishing
            c.execute("ALTER TABLE wgroup_seen ADD COLUMN ended_at REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass                          # column already exists
        try:   # v1.37: banner art for the card background
            c.execute("ALTER TABLE wgroup_tokens ADD COLUMN banner TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass                          # column already exists


# --------------------------------------------------------------- small helpers

def _rows(sql: str, args: tuple = ()) -> list[dict]:
    with _db() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def _group_wallets(group_id: int | None = None) -> list[dict]:
    if group_id is None:
        return _rows("SELECT * FROM wgroup_wallets ORDER BY id")
    return _rows("SELECT * FROM wgroup_wallets WHERE group_id=? ORDER BY id", (group_id,))


def short(address: str) -> str:
    return f"{address[:4]}…{address[-4:]}" if len(address) > 10 else address


def _clean_wallets(raw) -> list[dict]:
    """Validate and de-duplicate a submitted wallet list."""
    out, seen = [], set()
    for item in raw or []:
        address = W.normalize_wallet(str((item or {}).get("address", "")))
        kind = W.wallet_kind(address)
        if not kind:
            raise HTTPException(400, f"'{address[:24]}' is not a Solana or EVM address")
        if address in seen:
            continue
        seen.add(address)
        label = str((item or {}).get("label", "")).strip()[:40] or short(address)
        out.append({"address": address, "label": label, "kind": kind})
    return out


# --------------------------------------------------------------- scanning

def _evm_watchlists(chains: list[str]) -> dict[str, list[str]]:
    """The EVM tokens worth a balanceOf, per chain.

    Everything the dashboard already knows plus everything we are already
    watching. A token whose chain we never learned goes on every chain's list —
    balanceOf against a contract that isn't there just answers empty.
    """
    known = _rows("""
      SELECT address, IFNULL(chain_id,'') AS chain_id FROM tokens
       WHERE dead=0 AND address LIKE '0x%'
      UNION SELECT address, IFNULL(chain_id,'') FROM wgroup_tokens WHERE address LIKE '0x%'
      UNION SELECT token, IFNULL(chain_id,'') FROM wallet_holdings WHERE token LIKE '0x%'
    """)
    out: dict[str, list[str]] = {c: [] for c in chains}
    for row in known:
        address = row["address"].lower()
        chain = (row["chain_id"] or "").lower()
        for c in ([chain] if chain in out else chains):
            if address not in out[c]:
                out[c].append(address)
    return out


async def _scan_wallet(client, wallet: dict, watchlists: dict, decimals: dict) -> list[dict]:
    address, kind = wallet["address"], wallet["kind"]
    if kind == "sol":
        return await W.sol_holdings(client, address)
    # wallet_holdings is keyed (wallet, token) — one contract address is one
    # position, however many chains answer for it. The same address holding on
    # several chains is normal, not an anomaly: deterministic deploys put a
    # token at one address everywhere, and airdrop spam is blasted at the same
    # address on every chain at once. Concatenating the chains instead of
    # folding them is a UNIQUE constraint violation that aborts the entire
    # scan round for every wallet, which is exactly what it did once discovery
    # started returning real EVM positions. Keep the largest and drop the rest.
    found: dict[str, dict] = {}
    for chain in watchlists:
        try:
            rows, _provider = await W.evm_holdings(client, address, chain,
                                                   watchlists[chain], decimals)
        except Exception as e:
            log.debug(f"evm scan {address} on {chain}: {e}")
            continue
        for h in rows:
            best = found.get(h["address"])
            if best is None or float(h["amount"]) > float(best["amount"]):
                found[h["address"]] = h
    return list(found.values())


def _apply_holdings(wallet: str, found: list[dict], prices: dict, now: float,
                    first_scan: bool) -> None:
    """Write the new snapshot and fold the change into the observed cost basis.

    Runs after pricing, so a balance that grew can be valued at what it was
    worth when we noticed it.

    The first time a wallet is scanned its positions are recorded with no
    basis at all ('pre-existing'): we did not watch those buys, so any cost we
    invented would be a guess presented as a fact. Those positions get a real
    average entry from the wallet's swap history once they qualify for a card.
    """
    with _db() as c:
        before = {r["token"]: r for r in
                  c.execute("SELECT * FROM wallet_holdings WHERE wallet=?", (wallet,)).fetchall()}
        lots = {r["token"]: dict(r) for r in
                c.execute("SELECT * FROM wallet_lots WHERE wallet=?", (wallet,)).fetchall()}
        seen = set()
        for h in found:
            token, amount = h["address"], float(h["amount"])
            if token in seen:
                continue          # one position per (wallet, token); never INSERT twice
            seen.add(token)
            prev = float(before[token]["amount"]) if token in before else 0.0
            if token in before:
                c.execute("""UPDATE wallet_holdings SET amount=?, last_seen=?, chain_id=?,
                             decimals=? WHERE wallet=? AND token=?""",
                          (amount, now, h.get("chain_id", ""), h.get("decimals", 0), wallet, token))
            else:
                c.execute("""INSERT INTO wallet_holdings
                             (wallet, token, chain_id, amount, decimals, first_seen, last_seen)
                             VALUES (?,?,?,?,?,?,?)""",
                          (wallet, token, h.get("chain_id", ""), amount,
                           h.get("decimals", 0), now, now))
            lot = lots.get(token)
            if lot is None:
                lot = {"cost_usd": 0.0, "amount": 0.0, "realized_usd": 0.0,
                       "source": "pre-existing" if first_scan else "observed", "checked_at": 0}
                _put_lot(c, wallet, token, lot)
            if first_scan or lot.get("source") in ("chain", "partial", "pre-existing"):
                continue          # real history owns this basis, or there is none to observe yet
            delta = amount - prev
            price = (prices.get(token) or {}).get("price") or 0
            if abs(delta) < 1e-12:
                continue
            if not price:
                lot["source"] = "unknown"      # it moved and we could not value the move
                _put_lot(c, wallet, token, lot)
                continue
            if delta > 0:
                lot["cost_usd"] = float(lot["cost_usd"]) + delta * price
                lot["amount"] = float(lot["amount"]) + delta
            else:
                held = float(lot["amount"])
                sold = min(-delta, held)
                if held > 0:
                    avg = float(lot["cost_usd"]) / held
                    lot["realized_usd"] = float(lot["realized_usd"]) + sold * (price - avg)
                    lot["cost_usd"] = max(0.0, float(lot["cost_usd"]) - sold * avg)
                    lot["amount"] = max(0.0, held - sold)
            _put_lot(c, wallet, token, lot)
        for token in [t for t in before if t not in seen]:
            c.execute("DELETE FROM wallet_holdings WHERE wallet=? AND token=?", (wallet, token))
            # position closed: the basis is spent, and a re-entry starts clean
            c.execute("DELETE FROM wallet_lots WHERE wallet=? AND token=?", (wallet, token))


def _put_lot(c, wallet: str, token: str, lot: dict) -> None:
    c.execute("""INSERT INTO wallet_lots (wallet, token, cost_usd, amount, realized_usd,
                 source, checked_at) VALUES (?,?,?,?,?,?,?)
                 ON CONFLICT(wallet, token) DO UPDATE SET cost_usd=excluded.cost_usd,
                 amount=excluded.amount, realized_usd=excluded.realized_usd,
                 source=excluded.source, checked_at=excluded.checked_at""",
              (wallet, token, float(lot.get("cost_usd") or 0), float(lot.get("amount") or 0),
               float(lot.get("realized_usd") or 0), lot.get("source") or "observed",
               float(lot.get("checked_at") or 0)))


def _store_markets(markets: dict, now: float) -> None:
    with _db() as c:
        for address, m in markets.items():
            c.execute("""INSERT INTO wgroup_tokens
                (address, chain_id, name, symbol, image, banner, price, mc, supply, liq, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(address) DO UPDATE SET chain_id=excluded.chain_id,
                  name=CASE WHEN excluded.name!='' THEN excluded.name ELSE wgroup_tokens.name END,
                  symbol=CASE WHEN excluded.symbol!='' THEN excluded.symbol ELSE wgroup_tokens.symbol END,
                  image=CASE WHEN excluded.image!='' THEN excluded.image ELSE wgroup_tokens.image END,
                  banner=CASE WHEN excluded.banner!='' THEN excluded.banner ELSE wgroup_tokens.banner END,
                  price=excluded.price, mc=excluded.mc,
                  supply=CASE WHEN excluded.supply>0 THEN excluded.supply ELSE wgroup_tokens.supply END,
                  liq=excluded.liq, updated_at=excluded.updated_at""",
                      (address, m.get("chain_id", ""), m.get("name", ""), m.get("symbol", ""),
                       m.get("image", ""), m.get("banner", ""),
                       m.get("price", 0), m.get("mc", 0),
                       m.get("supply", 0), m.get("liq", 0), now))


# --------------------------------------------------------------- a round

async def holdings_round(client) -> None:
    started = time.time()
    _state["scanning"] = True
    try:
        wallets = _group_wallets()
        if not wallets:
            return
        # one scan per address, however many groups it belongs to
        targets: dict[str, dict] = {}
        for w in wallets:
            targets.setdefault(w["address"], {"address": w["address"], "kind": w["kind"]})
        # drop holdings for wallets nobody tracks any more
        with _db() as c:
            marks = ",".join("?" * len(targets))
            c.execute(f"DELETE FROM wallet_holdings WHERE wallet NOT IN ({marks})", tuple(targets))
            c.execute(f"DELETE FROM wallet_lots WHERE wallet NOT IN ({marks})", tuple(targets))

        chains = W.evm_chains() if any(t["kind"] == "evm" for t in targets.values()) else []
        watchlists = _evm_watchlists(chains) if chains else {}
        decimals = {r["address"]: r["decimals"] for r in
                    _rows("SELECT address, decimals FROM wgroup_tokens WHERE decimals>0")}

        known = {r["wallet"] for r in _rows("SELECT DISTINCT wallet FROM wallet_holdings")}
        prev = {(r["wallet"], r["token"]): r["amount"] for r in
                _rows("SELECT wallet, token, amount FROM wallet_holdings")}

        found: dict[str, list[dict]] = {}
        for address, target in targets.items():
            try:
                found[address] = await _scan_wallet(client, target, watchlists, decimals)
            except Exception as e:
                log.warning(f"wallet scan failed for {short(address)}: {e}")
                found[address] = None            # keep the last snapshot rather than wiping it
        if decimals:
            with _db() as c:
                for token, dec in decimals.items():
                    c.execute("""INSERT INTO wgroup_tokens (address, decimals) VALUES (?,?)
                                 ON CONFLICT(address) DO UPDATE SET decimals=excluded.decimals""",
                              (token, dec))

        # what two wallets of one group both hold — computed before anything is priced
        candidates: set[str] = set()
        by_group: dict[int, list[dict]] = {}
        for w in wallets:
            by_group.setdefault(w["group_id"], []).append(w)
        for members in by_group.values():
            counts: dict[str, int] = {}
            for m in members:
                rows = found.get(m["address"])
                held = ({h["address"] for h in rows} if rows is not None
                        else {t for (wal, t) in prev if wal == m["address"]})
                for token in held:
                    counts[token] = counts.get(token, 0) + 1
            candidates |= {t for t, n in counts.items() if n >= MIN_WALLETS}

        carded = {r["token"] for r in _rows("SELECT DISTINCT token FROM wgroup_seen")}
        changed = {h["address"] for address, rows in found.items() if rows is not None
                   and address in known
                   for h in rows if abs(h["amount"] - prev.get((address, h["address"]), 0.0)) > 1e-12}
        to_price = [t for t in (candidates | carded | changed) if not W.is_boring(t)]
        to_price = to_price[:200]
        prices = await W.dex_markets(client, to_price) if to_price else {}
        if prices:
            _store_markets(prices, time.time())

        now = time.time()
        for address, rows in found.items():
            if rows is None:
                continue
            _apply_holdings(address, rows, prices, now, first_scan=address not in known)

        changed_groups, convergences = _refresh_seen(list(by_group))
        _state["scanned_at"] = now
        if changed_groups and _notify:
            _notify()                    # a card appeared or disappeared — push it now
        await _alert_round(client, convergences)
        await _resolve_basis(client)
    finally:
        _state["scanning"] = False
        _state["round_ms"] = int((time.time() - started) * 1000)


def _refresh_seen(group_ids: list[int]) -> tuple[bool, dict[int, list[dict]]]:
    """Keep wgroup_seen in step with what qualifies, and record who came and went.

    Returns (something changed, convergences per group) — the caller reuses the
    convergence lists for alerting rather than recomputing them.

    A token that stops qualifying is NOT deleted any more. It gets `ended_at`
    stamped and lingers for COOL_SECONDS as a cooling card, because "three
    wallets sold out of this" is a signal, and deleting the row threw it away.
    """
    changed = False
    now = time.time()
    out: dict[int, list[dict]] = {}
    for gid in group_ids:
        conv = _convergences(gid)
        out[gid] = conv
        live = {t["address"] for t in conv}
        # membership is tracked against every position above the floor, not
        # against the convergence set — see _holdings_by_token
        grouped, _tokens, _members = _holdings_by_token(gid)
        holders = {token: {w["address"]: w for w in ws} for token, ws in grouped.items()}
        labels = {m["address"]: m["label"] for m in _group_wallets(gid)}
        with _db() as c:
            rows = c.execute("SELECT token, ended_at FROM wgroup_seen WHERE group_id=?",
                             (gid,)).fetchall()
            old = {r["token"] for r in rows}
            active = {r["token"] for r in rows if not r["ended_at"]}

            for token in live - old:
                c.execute("INSERT OR IGNORE INTO wgroup_seen (group_id, token, detected_at, ended_at)"
                          " VALUES (?,?,?,0)", (gid, token, now))
            # a cooling token that qualifies again is live again, not a new find
            for token in live & old:
                c.execute("UPDATE wgroup_seen SET ended_at=0 WHERE group_id=? AND token=? AND ended_at!=0",
                          (gid, token))
            for token in active - live:
                c.execute("UPDATE wgroup_seen SET ended_at=? WHERE group_id=? AND token=?",
                          (now, gid, token))

            # --- membership diff: this is what names the wallet that left
            for token in old | live:
                was = {r["wallet"]: r["last_value"] for r in
                       c.execute("SELECT wallet, last_value FROM wgroup_members"
                                 " WHERE group_id=? AND token=?", (gid, token)).fetchall()}
                nowin = holders.get(token, {})
                for wallet, w in nowin.items():
                    if wallet in was:
                        c.execute("UPDATE wgroup_members SET last_value=? "
                                  "WHERE group_id=? AND token=? AND wallet=?",
                                  (w["value_usd"], gid, token, wallet))
                    else:
                        c.execute("INSERT OR IGNORE INTO wgroup_members VALUES (?,?,?,?,?)",
                                  (gid, token, wallet, now, w["value_usd"]))
                        # rejoining clears the old exit, or the card would claim
                        # a wallet is gone while it is sitting in the table
                        c.execute("DELETE FROM wgroup_exits WHERE group_id=? AND token=? AND wallet=?",
                                  (gid, token, wallet))
                for wallet, last in was.items():
                    if wallet in nowin:
                        continue
                    # Full exits only, by Johan's choice: the wallet is under the
                    # $50 floor or out entirely. Partial trims are not reported.
                    c.execute("""INSERT INTO wgroup_exits VALUES (?,?,?,?,?,?)
                                 ON CONFLICT(group_id, token, wallet) DO UPDATE SET
                                   exited_at=excluded.exited_at, last_value=excluded.last_value""",
                              (gid, token, wallet, labels.get(wallet, ""), now, last))
                    c.execute("DELETE FROM wgroup_members WHERE group_id=? AND token=? AND wallet=?",
                              (gid, token, wallet))
                    changed = True

            # --- purge whatever has finished cooling
            gone = [r["token"] for r in
                    c.execute("SELECT token FROM wgroup_seen WHERE group_id=? AND ended_at!=0"
                              " AND ended_at < ?", (gid, now - COOL_SECONDS)).fetchall()]
            for token in gone:
                for table in ("wgroup_seen", "wgroup_members", "wgroup_exits", "wgroup_alerts"):
                    c.execute(f"DELETE FROM {table} WHERE group_id=? AND token=?", (gid, token))
        changed = changed or bool(live ^ active) or bool(gone)
    return changed, out


async def _alert_round(client, convergences: dict[int, list[dict]]) -> None:
    """DM Johan when a token reaches a wallet count it has never reached before.

    Milestone-based, not time-based: 2 wallets alerts once, 3 alerts once, and
    a wallet that sells and rebuys back to the same count is silent forever.
    Dismissed tokens never alert — hiding a card is a statement about the token,
    not about the page.
    """
    if not A.enabled():
        return
    names = {r["id"]: r["name"] for r in _rows("SELECT id, name FROM wgroups")}
    for gid, tokens in convergences.items():
        hidden = {r["token"] for r in
                  _rows("SELECT token FROM wgroup_hidden WHERE group_id=?", (gid,))}
        sent = {r["token"]: r["max_count"] for r in
                _rows("SELECT token, max_count FROM wgroup_alerts WHERE group_id=?", (gid,))}
        for t in tokens:
            count = t["holders_n"]
            if t["address"] in hidden or count <= sent.get(t["address"], 0):
                continue
            text = A.convergence_text(t, names.get(gid, "Wallet group"), count)
            ok = await A.send(client, text, t.get("banner") or t.get("image") or "")
            if not ok:
                continue          # retry on the next round rather than lose it
            with _db() as c:
                c.execute("""INSERT INTO wgroup_alerts VALUES (?,?,?,?)
                             ON CONFLICT(group_id, token) DO UPDATE SET
                               max_count=excluded.max_count, sent_at=excluded.sent_at""",
                          (gid, t["address"], count, time.time()))


def _basis_provider_ok(chain_id: str) -> bool:
    """Did the cost-basis provider for this chain actually answer last time?"""
    name = "solscan" if chain_id == "solana" else "etherscan"
    status = W.PROVIDER_STATUS.get(name)
    return bool(status and status.get("ok"))


async def _resolve_basis(client) -> None:
    """Replace a few unknown cost bases per round with the real one on chain.

    Only positions that are on a card right now are worth a request, which is
    what keeps this bounded no matter how many tokens the wallets hold.
    """
    pending = _rows("""
      SELECT h.wallet, h.token, h.chain_id, h.amount
        FROM wgroup_seen s
        JOIN wgroup_wallets g ON g.group_id = s.group_id
        JOIN wallet_holdings h ON h.wallet = g.address AND h.token = s.token
        LEFT JOIN wallet_lots l ON l.wallet = h.wallet AND l.token = h.token
       WHERE h.amount > 0
         AND s.ended_at = 0
         AND NOT EXISTS (SELECT 1 FROM wgroup_hidden x
                          WHERE x.group_id = s.group_id AND x.token = s.token)
         AND IFNULL(l.source,'pre-existing') IN ('pre-existing','unknown')
         AND IFNULL(l.checked_at,0) < ?
       GROUP BY h.wallet, h.token
       LIMIT ?
    """, (time.time() - BASIS_RETRY, BASIS_PER_ROUND))
    for row in pending:
        chain = row["chain_id"] or ("solana" if not row["token"].startswith("0x") else "ethereum")
        basis = await W.cost_basis(client, row["wallet"], row["token"], chain, row["amount"])
        with _db() as c:
            if basis:
                _put_lot(c, row["wallet"], row["token"], {
                    "cost_usd": basis["cost_usd"], "amount": basis["amount"],
                    "realized_usd": basis.get("realized_usd", 0),
                    "source": basis["source"], "checked_at": time.time()})
            else:
                # "the provider is down" and "this wallet has no readable
                # history" are the same None here, so back off differently:
                # a provider that is out comes back in minutes, a position we
                # cannot reconstruct will still be unreadable in six hours.
                stamp = time.time()
                if not _basis_provider_ok(chain):
                    stamp -= BASIS_RETRY - 900
                c.execute("""INSERT INTO wallet_lots (wallet, token, source, checked_at)
                             VALUES (?,?,'pre-existing',?)
                             ON CONFLICT(wallet, token) DO UPDATE SET checked_at=excluded.checked_at""",
                          (row["wallet"], row["token"], stamp))


# --------------------------------------------------------------- convergences

def _holdings_by_token(group_id: int) -> tuple[dict[str, list[dict]], dict[str, dict], list[dict]]:
    """Every position this group holds above the floor, keyed by token.

    Deliberately has NO wallet-count threshold. `_convergences` applies that;
    the membership diff must not, because a token that falls to one holder
    leaves the convergence list entirely — and diffing against a list it is
    absent from reports the remaining holder as having sold, which is both
    wrong and alarming.
    """
    members = _group_wallets(group_id)
    if not members:
        return {}, {}, []
    by_address = {m["address"]: m for m in members}
    marks = ",".join("?" * len(by_address))
    holdings = _rows(f"""SELECT * FROM wallet_holdings
                          WHERE wallet IN ({marks}) AND amount > 0""", tuple(by_address))
    if not holdings:
        return {}, {}, members
    held_tokens = tuple({h["token"] for h in holdings})
    tokens = {r["address"]: r for r in
              _rows("SELECT * FROM wgroup_tokens WHERE address IN (%s)"
                    % ",".join("?" * len(held_tokens)), held_tokens)}
    lots = {(r["wallet"], r["token"]): r for r in
            _rows(f"SELECT * FROM wallet_lots WHERE wallet IN ({marks})", tuple(by_address))}

    grouped: dict[str, list[dict]] = {}
    for h in holdings:
        market = tokens.get(h["token"])
        price = (market or {}).get("price") or 0
        if not market or not price or W.is_boring(h["token"], market.get("symbol", "")):
            continue
        value = h["amount"] * price
        if value < MIN_POSITION_USD:
            continue                      # dust and airdrops are not a position
        member = by_address[h["wallet"]]
        lot = lots.get((h["wallet"], h["token"])) or {}
        supply = market.get("supply") or 0
        avg_entry = None
        if lot.get("source") in ("chain", "partial", "observed") \
                and (lot.get("amount") or 0) > 0 and (lot.get("cost_usd") or 0) > 0:
            avg_entry = lot["cost_usd"] / lot["amount"]
        cost = avg_entry * h["amount"] if avg_entry else None
        grouped.setdefault(h["token"], []).append({
            "wallet_id": member["id"],
            "label": member["label"],
            "address": h["wallet"],
            "short": short(h["wallet"]),
            "amount": h["amount"],
            "supply_pct": (h["amount"] / supply * 100) if supply else None,
            "value_usd": value,
            "avg_entry": avg_entry,
            "cost_usd": cost,
            "pnl_usd": (value - cost) if cost else None,
            "pnl_pct": ((value - cost) / cost * 100) if cost else None,
            "realized_usd": lot.get("realized_usd") or 0,
            "basis": lot.get("source") or "pre-existing",
            "since": h["first_seen"],
        })
    return grouped, tokens, members


def _convergences(group_id: int) -> list[dict]:
    """Tokens at least MIN_WALLETS wallets of this group hold right now."""
    members = _group_wallets(group_id)
    if len(members) < MIN_WALLETS:
        return []
    grouped, tokens, members = _holdings_by_token(group_id)
    seen = {r["token"]: r["detected_at"] for r in
            _rows("SELECT token, detected_at FROM wgroup_seen WHERE group_id=?", (group_id,))}

    out = []
    for token, holders in grouped.items():
        if len(holders) < MIN_WALLETS:
            continue
        market = tokens[token]
        holders.sort(key=lambda w: w["value_usd"], reverse=True)
        priced = [w for w in holders if w["cost_usd"]]
        cost = sum(w["cost_usd"] for w in priced)
        value = sum(w["value_usd"] for w in holders)
        pnl = sum(w["pnl_usd"] for w in priced) if priced else None
        supply_pct = [w["supply_pct"] for w in holders if w["supply_pct"] is not None]
        out.append({
            "address": token,
            "chain_id": market.get("chain_id") or "",
            "name": market.get("name") or "",
            "symbol": market.get("symbol") or "",
            "image": market.get("image") or "",
            "banner": market.get("banner") or "",
            "price": market.get("price") or 0,
            "mc": market.get("mc") or 0,
            "supply": market.get("supply") or 0,
            "liq": market.get("liq") or 0,
            "updated_at": market.get("updated_at") or 0,
            "holders_n": len(holders),
            "wallets_total": len(members),
            "supply_pct": sum(supply_pct) if supply_pct else None,
            "position_usd": value,
            "cost_usd": cost if priced else None,
            "pnl_usd": pnl,
            "pnl_pct": (pnl / cost * 100) if pnl is not None and cost else None,
            "priced_n": len(priced),
            "detected_at": seen.get(token) or 0,
            "wallets": holders,
        })
    out.sort(key=lambda t: (t["holders_n"], t["position_usd"]), reverse=True)
    return out


def _exits(group_id: int) -> dict[str, list[dict]]:
    """Recent full exits per token, newest first."""
    out: dict[str, list[dict]] = {}
    for r in _rows("""SELECT token, wallet, label, exited_at, last_value
                        FROM wgroup_exits WHERE group_id=? AND exited_at > ?
                       ORDER BY exited_at DESC""",
                   (group_id, time.time() - COOL_SECONDS)):
        out.setdefault(r["token"], []).append({
            "wallet": r["wallet"], "short": short(r["wallet"]),
            "label": r["label"] or short(r["wallet"]),
            "exited_at": r["exited_at"], "last_value": r["last_value"],
        })
    return out


def _cooling(group_id: int, exits: dict[str, list[dict]]) -> list[dict]:
    """Cards for tokens that stopped qualifying inside the cooling window.

    These are deliberately cheap: last known market data and the membership
    table, no pricing and no PnL. The card is dimmed and on its way out — its
    job is to tell you *who sold*, which is the part the old code deleted.
    """
    rows = _rows("""SELECT s.token, s.detected_at, s.ended_at, t.*
                      FROM wgroup_seen s
                      LEFT JOIN wgroup_tokens t ON t.address = s.token
                     WHERE s.group_id=? AND s.ended_at != 0""", (group_id,))
    members = {}
    for r in _rows("SELECT token, wallet, last_value FROM wgroup_members WHERE group_id=?",
                   (group_id,)):
        members.setdefault(r["token"], []).append(r)
    labels = {m["address"]: m["label"] for m in _group_wallets(group_id)}
    total = len(labels)

    out = []
    for r in rows:
        token = r["token"]
        left = exits.get(token, [])
        held = [{"wallet_id": 0, "label": labels.get(m["wallet"], short(m["wallet"])),
                 "address": m["wallet"], "short": short(m["wallet"]),
                 "amount": None, "supply_pct": None, "value_usd": m["last_value"],
                 "avg_entry": None, "cost_usd": None, "pnl_usd": None, "pnl_pct": None,
                 "realized_usd": 0, "basis": "unknown", "since": 0}
                for m in members.get(token, [])]
        out.append({
            "address": token,
            "chain_id": r["chain_id"] or "",
            "name": r["name"] or "",
            "symbol": r["symbol"] or "",
            "image": r["image"] or "",
            "banner": r["banner"] or "",
            "price": r["price"] or 0,
            "mc": r["mc"] or 0,
            "supply": r["supply"] or 0,
            "liq": r["liq"] or 0,
            "updated_at": r["updated_at"] or 0,
            "holders_n": len(held),
            "wallets_total": total,
            "supply_pct": None,
            "position_usd": sum(h["value_usd"] or 0 for h in held),
            "cost_usd": None, "pnl_usd": None, "pnl_pct": None, "priced_n": 0,
            "detected_at": r["detected_at"] or 0,
            "wallets": held,
            "exits": left,
            "cooling": True,
            "ended_at": r["ended_at"],
        })
    out.sort(key=lambda t: t["ended_at"], reverse=True)
    return out


# --------------------------------------------------------------- loops

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": "memedash/1.0"}, follow_redirects=True)


async def holdings_loop():
    await asyncio.sleep(8)               # let the first ingest land
    client = _client()
    while True:
        try:
            if _rows("SELECT id FROM wgroups LIMIT 1"):
                await holdings_round(client)
                _state["error"] = ""
        except Exception as e:
            _state["error"] = str(e)[:200]
            log.warning(f"wallet-group round failed: {e}")
        try:                              # a new group or an edit wakes the loop immediately
            await asyncio.wait_for(_scan_now.wait(), timeout=HOLDINGS_INTERVAL)
            _scan_now.clear()
        except asyncio.TimeoutError:
            pass


async def price_loop():
    """Re-price only what is on a card. Keeps PnL live between wallet scans."""
    await asyncio.sleep(20)
    client = _client()
    while True:
        try:
            tracked = [r["token"] for r in _rows("SELECT DISTINCT token FROM wgroup_seen")]
            if tracked:
                markets = await W.dex_markets(client, tracked[:120])
                if markets:
                    _store_markets(markets, time.time())
        except Exception as e:
            log.debug(f"wallet-group price refresh: {e}")
        await asyncio.sleep(PRICE_INTERVAL)


_discover_now = asyncio.Event()
_discover_state = {"scanning": False, "at": 0.0, "error": ""}


async def discover_group(client, gid: int) -> int:
    """Rebuild one group's candidate list. Returns how many survived the cut."""
    tracked = {m["address"] for m in _group_wallets(gid)}
    tokens = [t for t in _convergences(gid)]
    # newest convergences first: what a wallet did last week says more about it
    # than what it did in the group's first week
    tokens.sort(key=lambda t: t.get("detected_at") or 0, reverse=True)
    found = await D.scan(client, gid, tokens, tracked)
    now = time.time()
    with _db() as c:
        c.execute("DELETE FROM wgroup_candidates WHERE group_id=?", (gid,))
        for e in found:
            c.execute("INSERT INTO wgroup_candidates VALUES (?,?,?,?,?,?,?,?,?)",
                      D.to_row(gid, e, now))
    return len(found)


async def discover_loop() -> None:
    """Slow background refresh, plus whatever the button asks for.

    Discovery is the expensive call pattern on this page — one history request
    per convergence per group — so it runs on its own clock, far slower than
    the scanners, and never inside a holdings round.
    """
    while True:
        try:
            await asyncio.wait_for(_discover_now.wait(), timeout=DISCOVER_INTERVAL)
            _discover_now.clear()
        except asyncio.TimeoutError:
            pass
        groups = [r["id"] for r in _rows("SELECT id FROM wgroups ORDER BY id")]
        if not groups:
            continue
        _discover_state.update(scanning=True, error="")
        try:
            async with _client() as client:
                for gid in groups:
                    await discover_group(client, gid)
            _discover_state["at"] = time.time()
            if _notify:
                _notify()          # zero-arg by contract: main.py binds the message
        except Exception as e:
            log.warning("wallet discovery round failed: %s", e)
            _discover_state["error"] = str(e)[:200]
        finally:
            _discover_state["scanning"] = False


def start(loop_tasks: list) -> None:
    """Called from main.py's lifespan; returns the tasks it created."""
    loop_tasks.append(asyncio.create_task(holdings_loop()))
    loop_tasks.append(asyncio.create_task(price_loop()))
    loop_tasks.append(asyncio.create_task(discover_loop()))


# --------------------------------------------------------------- api

def _group_row(gid: int) -> dict:
    rows = _rows("SELECT * FROM wgroups WHERE id=?", (gid,))
    if not rows:
        raise HTTPException(404, "no such wallet group")
    return rows[0]


def _group_json(row: dict) -> dict:
    members = _group_wallets(row["id"])
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"],
            "wallets": [{"id": m["id"], "address": m["address"], "label": m["label"],
                         "short": short(m["address"]), "kind": m["kind"]} for m in members]}


@router.get("/api/wgroups")
def list_groups():
    groups = [_group_json(r) for r in _rows("SELECT * FROM wgroups ORDER BY id")]
    counts = {r["group_id"]: r["n"] for r in
              _rows("""SELECT s.group_id, COUNT(*) AS n FROM wgroup_seen s
                        WHERE NOT EXISTS (SELECT 1 FROM wgroup_hidden x
                                           WHERE x.group_id = s.group_id AND x.token = s.token)
                        GROUP BY s.group_id""")}
    for g in groups:
        g["tokens_n"] = counts.get(g["id"], 0)
    return {"groups": groups, "scanned_at": _state["scanned_at"]}


@router.post("/api/wgroups")
def create_group(payload: dict = Body(...)):
    name = str(payload.get("name", "")).strip()[:60] or "Wallet group"
    members = _clean_wallets(payload.get("wallets"))
    if len(members) < MIN_WALLETS:
        raise HTTPException(400, "a wallet group needs at least 2 wallets")
    now = time.time()
    with _db() as c:
        cur = c.execute("INSERT INTO wgroups (name, created_at) VALUES (?,?)", (name, now))
        gid = cur.lastrowid
        for m in members:
            c.execute("""INSERT INTO wgroup_wallets (group_id, address, label, kind, added_at)
                         VALUES (?,?,?,?,?)""", (gid, m["address"], m["label"], m["kind"], now))
    _scan_now.set()
    return _group_json(_group_row(gid))


@router.put("/api/wgroups/{gid}")
def update_group(gid: int, payload: dict = Body(...)):
    row = _group_row(gid)
    name = str(payload.get("name", row["name"])).strip()[:60] or row["name"]
    members = _clean_wallets(payload.get("wallets"))
    if len(members) < MIN_WALLETS:
        raise HTTPException(400, "a wallet group needs at least 2 wallets")
    now = time.time()
    keep = {m["address"] for m in members}
    with _db() as c:
        c.execute("UPDATE wgroups SET name=? WHERE id=?", (name, gid))
        existing = {r["address"]: r for r in
                    c.execute("SELECT * FROM wgroup_wallets WHERE group_id=?", (gid,)).fetchall()}
        for address in existing:
            if address not in keep:
                c.execute("DELETE FROM wgroup_wallets WHERE group_id=? AND address=?", (gid, address))
        for m in members:
            if m["address"] in existing:
                c.execute("UPDATE wgroup_wallets SET label=? WHERE group_id=? AND address=?",
                          (m["label"], gid, m["address"]))
            else:
                c.execute("""INSERT INTO wgroup_wallets (group_id, address, label, kind, added_at)
                             VALUES (?,?,?,?,?)""", (gid, m["address"], m["label"], m["kind"], now))
    _scan_now.set()
    return _group_json(_group_row(gid))


@router.delete("/api/wgroups/{gid}")
def delete_group(gid: int):
    _group_row(gid)
    with _db() as c:
        c.execute("DELETE FROM wgroups WHERE id=?", (gid,))
        c.execute("DELETE FROM wgroup_wallets WHERE group_id=?", (gid,))
        c.execute("DELETE FROM wgroup_seen WHERE group_id=?", (gid,))
        c.execute("DELETE FROM wgroup_hidden WHERE group_id=?", (gid,))
    _scan_now.set()
    return {"deleted": gid}


@router.post("/api/wgroups/{gid}/scan")
def rescan(gid: int):
    _group_row(gid)
    _scan_now.set()
    return {"queued": True, "scanned_at": _state["scanned_at"]}


@router.get("/api/wgroups/{gid}/live")
def live(gid: int):
    row = _group_row(gid)
    found = _convergences(gid)
    exits = _exits(gid)
    for t in found:
        t["exits"] = exits.get(t["address"], [])
        t["cooling"] = False
    found += _cooling(gid, exits)
    hidden_at = {r["token"]: r["hidden_at"] for r in
                 _rows("SELECT token, hidden_at FROM wgroup_hidden WHERE group_id=?", (gid,))}
    # A dismissed token is dropped from the feed, not from the scan: it still
    # holds its place in the wallets' positions, it just never shows a card
    # again. The list of what is hidden goes down with the payload so the page
    # can offer them back without a second request.
    tokens = [t for t in found if t["address"] not in hidden_at]
    hidden = [{"address": t["address"], "symbol": t["symbol"], "name": t["name"],
               "image": t["image"], "chain_id": t["chain_id"], "holders_n": t["holders_n"],
               "position_usd": t["position_usd"], "hidden_at": hidden_at[t["address"]]}
              for t in found if t["address"] in hidden_at]
    now = time.time()
    members = _group_wallets(gid)
    evm = [m for m in members if m["kind"] == "evm"]
    notes = []
    if evm:
        watchlist_chains = [c for c in W.evm_chains()
                            if W.PROVIDER_STATUS.get(f"evm_holdings:{c}", {}).get("note", "")
                            .startswith("watchlist")]
        if watchlist_chains:
            notes.append("EVM wallets are scanned against tokens this dashboard already knows "
                         f"({', '.join(watchlist_chains)}) — set a Pro Etherscan key for full discovery")
    alerts = A.status()
    if not alerts["ok"]:
        # a silent phone should be explainable from the page, not from the logs
        notes.append(f"Telegram alerts are off ({alerts['note']}) — convergences "
                     "appear here but will not reach your DMs")
    solscan = W.PROVIDER_STATUS.get("solscan")
    if not os.getenv("SOLSCAN_API_KEY", "").strip():
        notes.append("No Solscan key: average entry comes from what the dashboard has watched, "
                     "not from full swap history")
    elif solscan and not solscan.get("ok"):
        # a free key reaches /playground but not /v2.0, and some paths are on
        # neither — say which, rather than leaving blank entry columns unexplained
        notes.append(f"Solscan is not answering ({solscan.get('note', 'no route')}): positions that "
                     "predate tracking will show no average entry")
    return {
        "group": _group_json(row),
        "summary": {
            "wallets": len(members),
            "tokens": sum(1 for t in tokens if not t["cooling"]),
            "cooling_n": sum(1 for t in tokens if t["cooling"]),
            "hidden_n": len(hidden),
            "cool_seconds": COOL_SECONDS,
            "alerts": A.status(),
            "min_position_usd": MIN_POSITION_USD,
            "new_1h": sum(1 for t in tokens
                          if not t["cooling"] and t["detected_at"] and now - t["detected_at"] < 3600),
            "scanned_at": _state["scanned_at"],
            "scanning": _state["scanning"],
            "round_ms": _state["round_ms"],
            "error": _state["error"],
            "interval": HOLDINGS_INTERVAL,
            "notes": notes,
        },
        "providers": W.PROVIDER_STATUS,
        "tokens": tokens,
        "hidden": hidden,
    }


# --------------------------------------------------------------- dismissals

@router.post("/api/wgroups/{gid}/hide/{address}")
def hide_token(gid: int, address: str):
    """Dismiss a token from this group's feed. Permanent until un-hidden."""
    _group_row(gid)
    with _db() as c:
        c.execute("INSERT OR IGNORE INTO wgroup_hidden VALUES (?,?,?)", (gid, address, time.time()))
    return {"hidden": address}


@router.delete("/api/wgroups/{gid}/hide/{address}")
def unhide_token(gid: int, address: str):
    _group_row(gid)
    with _db() as c:
        c.execute("DELETE FROM wgroup_hidden WHERE group_id=? AND token=?", (gid, address))
    return {"shown": address}


# --------------------------------------------------------------- discovery

@router.get("/api/wgroups/{gid}/candidates")
def candidates(gid: int):
    """Wallets worth adding, most recurrent first."""
    _group_row(gid)
    rows = _rows("""SELECT * FROM wgroup_candidates WHERE group_id=?
                     ORDER BY convergences DESC, score DESC LIMIT 50""", (gid,))
    return {
        "candidates": [D.from_row(r) for r in rows],
        "scanning": _discover_state["scanning"],
        "scanned_at": _discover_state["at"],
        "error": _discover_state["error"],
        "provider": D.STATUS,
        "min_convergences": D.MIN_CONVERGENCES,
    }


@router.post("/api/wgroups/{gid}/discover")
def rediscover(gid: int):
    """Ask for a fresh scan now. The loop owns the work; this only wakes it."""
    _group_row(gid)
    _discover_now.set()
    return {"queued": True, "scanning": _discover_state["scanning"]}


@router.post("/api/wgroups/{gid}/wallets")
def add_wallet(gid: int, payload: dict = Body(...)):
    """Add one wallet to a group — the candidate list's 'Add to group' button.

    Separate from PUT /api/wgroups/{gid} on purpose: that replaces the whole
    membership, so using it from a modal would race with any other edit.
    """
    _group_row(gid)
    members = _clean_wallets([{"address": payload.get("address") or "",
                               "label": payload.get("label") or ""}])
    if not members:
        raise HTTPException(400, "not a Solana or EVM address")
    m = members[0]
    with _db() as c:
        if c.execute("SELECT 1 FROM wgroup_wallets WHERE group_id=? AND address=?",
                     (gid, m["address"])).fetchone():
            raise HTTPException(409, "that wallet is already in this group")
        c.execute("""INSERT INTO wgroup_wallets (group_id, address, label, kind, added_at)
                     VALUES (?,?,?,?,?)""",
                  (gid, m["address"], m["label"], m["kind"], time.time()))
        # it is a member now, so it can no longer be its own suggestion
        c.execute("DELETE FROM wgroup_candidates WHERE group_id=? AND wallet=?",
                  (gid, m["address"]))
    _scan_now.set()
    return _group_json(_group_row(gid))


@router.delete("/api/wgroups/{gid}/hide")
def unhide_all(gid: int):
    _group_row(gid)
    with _db() as c:
        n = c.execute("DELETE FROM wgroup_hidden WHERE group_id=?", (gid,)).rowcount
    return {"shown": n}
