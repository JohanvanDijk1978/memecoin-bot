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

import wallets as W

log = logging.getLogger("memedash.wgroups")
router = APIRouter()

HOLDINGS_INTERVAL = float(os.getenv("WG_HOLDINGS_INTERVAL", "45"))
PRICE_INTERVAL = float(os.getenv("WG_PRICE_INTERVAL", "15"))
MIN_POSITION_USD = float(os.getenv("WG_MIN_POSITION_USD", "50"))  # below this a wallet is not "in"
BASIS_PER_ROUND = int(os.getenv("WG_BASIS_PER_ROUND", "8"))       # chain cost-basis lookups per round
BASIS_RETRY = 6 * 3600            # how long before a failed basis lookup is retried
MIN_WALLETS = 2                   # a card exists at two wallets — the whole point of the page

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
        """)
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
    found: list[dict] = []
    for chain in watchlists:
        try:
            rows, _provider = await W.evm_holdings(client, address, chain,
                                                   watchlists[chain], decimals)
            found += rows
        except Exception as e:
            log.debug(f"evm scan {address} on {chain}: {e}")
    return found


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

        changed_groups = _refresh_seen(list(by_group))
        _state["scanned_at"] = now
        if changed_groups and _notify:
            _notify()                    # a card appeared or disappeared — push it now
        await _resolve_basis(client)
    finally:
        _state["scanning"] = False
        _state["round_ms"] = int((time.time() - started) * 1000)


def _refresh_seen(group_ids: list[int]) -> bool:
    """Keep wgroup_seen equal to what actually qualifies. Returns True on change."""
    changed = False
    now = time.time()
    for gid in group_ids:
        live = {t["address"] for t in _convergences(gid)}
        with _db() as c:
            old = {r["token"] for r in
                   c.execute("SELECT token FROM wgroup_seen WHERE group_id=?", (gid,)).fetchall()}
            for token in live - old:
                c.execute("INSERT OR IGNORE INTO wgroup_seen VALUES (?,?,?)", (gid, token, now))
            for token in old - live:
                c.execute("DELETE FROM wgroup_seen WHERE group_id=? AND token=?", (gid, token))
        changed = changed or bool(live ^ old)
    return changed


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

def _convergences(group_id: int) -> list[dict]:
    """Tokens at least MIN_WALLETS wallets of this group hold right now."""
    members = _group_wallets(group_id)
    if len(members) < MIN_WALLETS:
        return []
    by_address = {m["address"]: m for m in members}
    marks = ",".join("?" * len(by_address))
    holdings = _rows(f"""SELECT * FROM wallet_holdings
                          WHERE wallet IN ({marks}) AND amount > 0""", tuple(by_address))
    if not holdings:
        return []
    held_tokens = tuple({h["token"] for h in holdings})
    tokens = {r["address"]: r for r in
              _rows("SELECT * FROM wgroup_tokens WHERE address IN (%s)"
                    % ",".join("?" * len(held_tokens)), held_tokens)}
    lots = {(r["wallet"], r["token"]): r for r in
            _rows(f"SELECT * FROM wallet_lots WHERE wallet IN ({marks})", tuple(by_address))}
    seen = {r["token"]: r["detected_at"] for r in
            _rows("SELECT token, detected_at FROM wgroup_seen WHERE group_id=?", (group_id,))}

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


def start(loop_tasks: list) -> None:
    """Called from main.py's lifespan; returns the tasks it created."""
    loop_tasks.append(asyncio.create_task(holdings_loop()))
    loop_tasks.append(asyncio.create_task(price_loop()))


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
            "tokens": len(tokens),
            "hidden_n": len(hidden),
            "min_position_usd": MIN_POSITION_USD,
            "new_1h": sum(1 for t in tokens if t["detected_at"] and now - t["detected_at"] < 3600),
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


@router.delete("/api/wgroups/{gid}/hide")
def unhide_all(gid: int):
    _group_row(gid)
    with _db() as c:
        n = c.execute("DELETE FROM wgroup_hidden WHERE group_id=?", (gid,)).rowcount
    return {"shown": n}
