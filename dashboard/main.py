"""
memedash — analytics dashboard for memecoin-bot.

Reads ../data/ca_history.json (bot untouched), keeps its own SQLite read model,
polls Dexscreener for live/current peaks, serves JSON API + static frontend.

Run:  uvicorn main:app --host 0.0.0.0 --port 8080   (from dashboard/)
Env:  DASH_PASSWORD  — enables HTTP Basic auth (any username)
      HISTORY_FILE   — override path to ca_history.json
"""

import asyncio
import base64
import json
import logging
import os
import secrets
import sqlite3
import statistics
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("memedash")

BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = Path(os.environ.get("HISTORY_FILE", BASE_DIR.parent / "data" / "ca_history.json"))
DB_FILE = Path(os.environ.get("DASH_DB", BASE_DIR / "data" / "dash.db"))
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")

INGEST_INTERVAL = 60          # s
PEAK_INTERVAL = 300           # s between poll rounds
ACTIVE_WINDOW = 48 * 3600     # poll tokens called within this window
STALE_RECHECK = 24 * 3600     # older tokens: once a day
DEX_BATCH = 30
DEX_DELAY = 2.0               # s between Dexscreener requests
MIN_LIQ_USD = 250             # ignore mcap from pools with less liquidity than this
CACHE_TTL = 30                # s for aggregate cache

WIN_X = 2.0                   # "win" = peak >= 2x first_mc
VERSION = "1.03"              # bump together with VERSION in static/app.js

# ---------------------------------------------------------------- database

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    DB_FILE.parent.mkdir(exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS calls (
          id INTEGER PRIMARY KEY,
          address TEXT NOT NULL,
          chain TEXT NOT NULL,
          group_name TEXT NOT NULL DEFAULT '',
          sender_name TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL DEFAULT '',
          ticker TEXT DEFAULT '',
          first_mc REAL DEFAULT 0,
          peak_mc_bot REAL DEFAULT 0,
          scan_count INTEGER DEFAULT 1,
          called_at REAL NOT NULL,
          UNIQUE(address, group_name)
        );
        CREATE INDEX IF NOT EXISTS idx_calls_addr ON calls(address);
        CREATE INDEX IF NOT EXISTS idx_calls_time ON calls(called_at);
        CREATE INDEX IF NOT EXISTS idx_calls_sender ON calls(sender_name);
        CREATE INDEX IF NOT EXISTS idx_calls_group ON calls(group_name);
        CREATE TABLE IF NOT EXISTS tokens (
          address TEXT PRIMARY KEY,
          chain TEXT NOT NULL,
          ticker TEXT DEFAULT '',
          current_mc REAL DEFAULT 0,
          peak_mc_dash REAL DEFAULT 0,
          peak_at REAL DEFAULT 0,
          first_seen REAL NOT NULL,
          last_checked REAL DEFAULT 0,
          miss_count INTEGER DEFAULT 0,
          dead INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)

# ---------------------------------------------------------------- ingest

def ingest_history() -> int:
    """Upsert ca_history.json into SQLite. Returns rows touched. Idempotent."""
    if not HISTORY_FILE.exists():
        return 0
    mtime = str(HISTORY_FILE.stat().st_mtime)
    with db() as c:
        row = c.execute("SELECT value FROM meta WHERE key='history_mtime'").fetchone()
        if row and row["value"] == mtime:
            return 0
    try:
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"could not read history file: {e}")
        return 0

    n = 0
    with db() as c:
        for address, entries in history.items():
            chain = "ETH" if address.startswith("0x") else "SOL"
            for e in entries:
                first_mc = e.get("first_mc") or e.get("market_cap") or 0
                c.execute("""
                  INSERT INTO calls (address, chain, group_name, sender_name, source,
                                     ticker, first_mc, peak_mc_bot, scan_count, called_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(address, group_name) DO UPDATE SET
                    peak_mc_bot = MAX(peak_mc_bot, excluded.peak_mc_bot),
                    scan_count  = excluded.scan_count,
                    ticker      = CASE WHEN calls.ticker='' THEN excluded.ticker ELSE calls.ticker END
                """, (address, chain, e.get("group_name", ""), e.get("sender_name", ""),
                      e.get("source", ""), e.get("ticker") or "", first_mc,
                      e.get("peak_mc") or 0, e.get("scan_count", 1), e.get("timestamp", 0)))
                n += 1
            first_seen = min((e.get("timestamp", 0) for e in entries), default=0)
            c.execute("""
              INSERT INTO tokens (address, chain, first_seen) VALUES (?,?,?)
              ON CONFLICT(address) DO NOTHING
            """, (address, chain, first_seen))
        c.execute("INSERT OR REPLACE INTO meta VALUES ('history_mtime', ?)", (mtime,))
        c.execute("INSERT OR REPLACE INTO meta VALUES ('last_ingest', ?)", (str(time.time()),))
    _cache.clear()
    return n


async def ingest_loop():
    while True:
        try:
            n = await asyncio.to_thread(ingest_history)
            if n:
                log.info(f"ingested {n} call rows")
        except Exception as e:
            log.warning(f"ingest error: {e}")
        await asyncio.sleep(INGEST_INTERVAL)

# ---------------------------------------------------------------- peak poller

async def poll_batch(client: httpx.AsyncClient, addresses: list[str]):
    """One Dexscreener request for up to 30 addresses (chain-agnostic endpoint)."""
    url = "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(addresses)
    r = await client.get(url, timeout=15)
    r.raise_for_status()
    pairs = (r.json() or {}).get("pairs") or []
    # Per token: use the HIGHEST-LIQUIDITY pair's marketCap (same as the bot's
    # _fetch_pair_data). Never take max mcap across pools — dust pools with a
    # manipulated price report absurd caps ($1e28-style).
    best: dict[str, dict] = {}  # lowered address -> {mc, ticker, liq}
    for p in pairs:
        addr = (p.get("baseToken") or {}).get("address", "")
        liq = float(((p.get("liquidity") or {}).get("usd")) or 0)
        mc = p.get("marketCap") or p.get("fdv") or 0
        sym = (p.get("baseToken") or {}).get("symbol", "")
        key = addr.lower()
        if key and mc and (key not in best or liq > best[key]["liq"]):
            best[key] = {"mc": mc, "ticker": sym, "liq": liq}
    now = time.time()
    with db() as c:
        for a in addresses:
            hit = best.get(a.lower())
            if hit and hit["liq"] < MIN_LIQ_USD:
                # token responded but only dust pools remain — record the check,
                # don't let a manipulated price set current/peak mcap
                c.execute("UPDATE tokens SET miss_count=0, last_checked=? WHERE address=?", (now, a))
            elif hit:
                c.execute("""
                  UPDATE tokens SET current_mc=?, ticker=CASE WHEN ticker='' THEN ? ELSE ticker END,
                    peak_mc_dash=MAX(peak_mc_dash, ?), miss_count=0, last_checked=?,
                    peak_at=CASE WHEN ? > peak_mc_dash THEN ? ELSE peak_at END
                  WHERE address=?
                """, (hit["mc"], hit["ticker"], hit["mc"], now, hit["mc"], now, a))
            else:
                c.execute("""
                  UPDATE tokens SET miss_count=miss_count+1, last_checked=?,
                    dead=CASE WHEN miss_count>=4 AND ?-first_seen > ? THEN 1 ELSE dead END
                  WHERE address=?
                """, (now, now, ACTIVE_WINDOW, a))
    _cache.clear()


async def peak_loop():
    await asyncio.sleep(5)  # let first ingest land
    client = None
    while True:
            try:
                if client is None:
                    client = httpx.AsyncClient(headers={"User-Agent": "memedash/1.0"})
                now = time.time()
                with db() as c:
                    rows = c.execute("""
                      SELECT address, first_seen, last_checked FROM tokens WHERE dead=0
                      AND (? - first_seen < ? OR ? - last_checked > ?)
                      ORDER BY last_checked ASC LIMIT 240
                    """, (now, ACTIVE_WINDOW, now, STALE_RECHECK)).fetchall()
                addrs = [r["address"] for r in rows]
                # SOL: batched. EVM: one address per request — Dexscreener's
                # multi-address endpoint silently drops EVM addrs in mixed
                # batches; single-address is the form the bot uses everywhere.
                sol = [a for a in addrs if not a.startswith("0x")]
                evm = [a for a in addrs if a.startswith("0x")]
                batches = [sol[i:i + DEX_BATCH] for i in range(0, len(sol), DEX_BATCH)]
                batches += [[a] for a in evm]
                for b in batches:
                    await poll_batch(client, b)
                    await asyncio.sleep(DEX_DELAY)
                if addrs:
                    log.info(f"peak poll: {len(addrs)} tokens ({len(evm)} evm)")
                with db() as c:
                    c.execute("INSERT OR REPLACE INTO meta VALUES ('last_peak_poll', ?)", (str(time.time()),))
            except Exception as e:
                log.warning(f"peak poll error: {e}")
            await asyncio.sleep(PEAK_INTERVAL)

# ---------------------------------------------------------------- aggregation

_cache: dict[str, tuple[float, object]] = {}


def cached(key: str, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def fetch_calls(days=0, chain="", caller="", group="", source="", q=""):
    """Call rows joined with token peaks; effective peak + multiplier computed."""
    sql = """
      SELECT c.*, t.current_mc, t.peak_mc_dash, t.peak_at, t.dead,
             CASE WHEN t.ticker!='' THEN t.ticker ELSE c.ticker END AS tick,
             MAX(c.first_mc, c.peak_mc_bot, IFNULL(t.peak_mc_dash,0)) AS eff_peak
      FROM calls c LEFT JOIN tokens t ON t.address = c.address WHERE 1=1
    """
    args: list = []
    if days:
        sql += " AND c.called_at >= ?"; args.append(time.time() - days * 86400)
    if chain:
        sql += " AND c.chain = ?"; args.append(chain)
    if caller:
        sql += " AND c.sender_name = ?"; args.append(caller)
    if group:
        sql += " AND c.group_name = ?"; args.append(group)
    if source:
        sql += " AND c.source = ?"; args.append(source)
    if q:
        sql += """ AND (c.address LIKE ? OR c.ticker LIKE ? OR IFNULL(t.ticker,'') LIKE ?
                   OR c.sender_name LIKE ? OR c.group_name LIKE ?)"""
        args += [f"%{q}%"] * 5
    sql += " ORDER BY c.called_at DESC"
    with db() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    for r in rows:
        r["mult"] = (r["eff_peak"] / r["first_mc"]) if r["first_mc"] else None
    return rows


def agg(rows):
    """Shared metric block for a set of call rows."""
    mults = [r["mult"] for r in rows if r["mult"]]
    n = len(rows)
    def rate(x):
        return round(100 * sum(1 for m in mults if m >= x) / len(mults), 1) if mults else 0
    return {
        "calls": n,
        "unique_cas": len({r["address"] for r in rows}),
        "with_data": len(mults),
        "hit2": rate(2), "hit5": rate(5), "hit10": rate(10), "hit20": rate(20),
        "avg_mult": round(statistics.fmean(mults), 2) if mults else 0,
        "med_mult": round(statistics.median(mults), 2) if mults else 0,
        "best_mult": round(max(mults), 2) if mults else 0,
    }


def consistency(rows):
    """2x hit share shrunk by log sample size. Displayed with n — judge for yourself."""
    import math
    mults = [r["mult"] for r in rows if r["mult"]]
    if not mults:
        return 0
    hit = sum(1 for m in mults if m >= WIN_X) / len(mults)
    return round(hit * math.log(len(mults) + 1) / math.log(51), 3)  # 50 calls → full weight


def leaderboard(rows, key):
    groups: dict[str, list] = {}
    for r in rows:
        k = r[key] or "(unknown)"
        groups.setdefault(k, []).append(r)
    out = []
    for name, rs in groups.items():
        a = agg(rs)
        best = max((r for r in rs if r["mult"]), key=lambda r: r["mult"], default=None)
        a.update({
            "name": name,
            "consistency": consistency(rs),
            "last_active": max(r["called_at"] for r in rs),
            "best_call": {"ticker": best["tick"], "address": best["address"],
                          "mult": round(best["mult"], 2),
                          "peak_mc": round(best["eff_peak"])} if best else None,
        })
        out.append(a)
    out.sort(key=lambda x: (x["consistency"], x["calls"]), reverse=True)
    return out

# ---------------------------------------------------------------- app / auth

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ingest_history()
    t1 = asyncio.create_task(ingest_loop())
    t2 = asyncio.create_task(peak_loop())
    yield
    t1.cancel(); t2.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if DASH_PASSWORD:
        header = request.headers.get("Authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(pwd, DASH_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="memedash"'})
    return await call_next(request)

# ---------------------------------------------------------------- endpoints

@app.get("/api/overview")
def overview(days: int = 30, chain: str = ""):
    def build():
        rows = fetch_calls(days=days, chain=chain)
        out = agg(rows)
        # calls per day
        byday: dict[str, int] = {}
        for r in rows:
            d = time.strftime("%Y-%m-%d", time.gmtime(r["called_at"]))
            byday[d] = byday.get(d, 0) + 1
        out["per_day"] = sorted(byday.items())
        # multiplier histogram buckets
        buckets = [("<1x", 0, 1), ("1-2x", 1, 2), ("2-5x", 2, 5), ("5-10x", 5, 10),
                   ("10-20x", 10, 20), ("20x+", 20, 1e18)]
        mults = [r["mult"] for r in rows if r["mult"]]
        out["histogram"] = [{"label": l, "count": sum(1 for m in mults if lo <= m < hi)}
                            for l, lo, hi in buckets]
        # top movers: called in last 48h, by multiplier
        recent = [r for r in rows if time.time() - r["called_at"] < ACTIVE_WINDOW and r["mult"]]
        seen, movers = set(), []
        for r in sorted(recent, key=lambda r: r["mult"], reverse=True):
            if r["address"] in seen:
                continue
            seen.add(r["address"])
            movers.append({"ticker": r["tick"], "address": r["address"], "chain": r["chain"],
                           "mult": round(r["mult"], 2), "first_mc": r["first_mc"],
                           "current_mc": r["current_mc"], "group": r["group_name"],
                           "caller": r["sender_name"], "called_at": r["called_at"]})
            if len(movers) >= 10:
                break
        out["top_movers"] = movers
        return out
    return cached(f"ov:{days}:{chain}", build)


@app.get("/api/callers")
def callers(days: int = 0, chain: str = "", min_calls: int = 2):
    def build():
        rows = fetch_calls(days=days, chain=chain)
        return [r for r in leaderboard(rows, "sender_name") if r["calls"] >= min_calls]
    return cached(f"callers:{days}:{chain}:{min_calls}", build)


@app.get("/api/groups")
def groups(days: int = 0, chain: str = ""):
    def build():
        rows = fetch_calls(days=days, chain=chain)
        boards = leaderboard(rows, "group_name")
        by_group: dict[str, list] = {}
        for r in rows:
            by_group.setdefault(r["group_name"] or "(unknown)", []).append(r)
        for b in boards:
            senders: dict[str, int] = {}
            for r in by_group[b["name"]]:
                if r["sender_name"]:
                    senders[r["sender_name"]] = senders.get(r["sender_name"], 0) + 1
            b["top_caller"] = max(senders, key=senders.get) if senders else ""
            b["active_callers"] = len(senders)
        return boards
    return cached(f"groups:{days}:{chain}", build)


@app.get("/api/sources")
def sources(days: int = 0):
    def build():
        rows = fetch_calls(days=days)
        boards = leaderboard(rows, "source")
        # avg observed time-to-peak where our poller saw the peak
        with db() as c:
            for b in boards:
                r = c.execute("""
                  SELECT AVG(t.peak_at - m.first_call) AS ttp FROM tokens t
                  JOIN (SELECT address, MIN(called_at) AS first_call FROM calls
                        WHERE source=? GROUP BY address) m ON m.address=t.address
                  WHERE t.peak_at > m.first_call AND t.peak_mc_dash > 0
                """, (b["name"],)).fetchone()
                b["avg_hours_to_peak"] = round(r["ttp"] / 3600, 1) if r and r["ttp"] else None
        return boards
    return cached(f"sources:{days}", build)


@app.get("/api/calls")
def calls_explorer(q: str = "", caller: str = "", group: str = "", chain: str = "",
                   source: str = "", min_mult: float = 0, days: int = 0,
                   sort: str = "called_at", page: int = 1, per: int = 50):
    rows = fetch_calls(days=days, chain=chain, caller=caller, group=group, source=source, q=q)
    if min_mult:
        rows = [r for r in rows if r["mult"] and r["mult"] >= min_mult]
    if sort == "mult":
        rows.sort(key=lambda r: r["mult"] or 0, reverse=True)
    elif sort == "first_mc":
        rows.sort(key=lambda r: r["first_mc"], reverse=True)
    total = len(rows)
    rows = rows[(page - 1) * per: page * per]
    return {"total": total, "page": page,
            "rows": [{"address": r["address"], "ticker": r["tick"], "chain": r["chain"],
                      "group": r["group_name"], "caller": r["sender_name"], "source": r["source"],
                      "first_mc": r["first_mc"], "eff_peak": r["eff_peak"],
                      "current_mc": r["current_mc"], "mult": round(r["mult"], 2) if r["mult"] else None,
                      "scan_count": r["scan_count"], "called_at": r["called_at"],
                      "dead": r["dead"]} for r in rows]}


@app.get("/api/token/{address}")
def token_detail(address: str):
    with db() as c:
        tok = c.execute("SELECT * FROM tokens WHERE address=?", (address,)).fetchone()
    calls = [r for r in fetch_calls() if r["address"] == address]
    calls.sort(key=lambda r: r["called_at"])
    return {
        "token": dict(tok) if tok else None,
        "calls": [{"group": r["group_name"], "caller": r["sender_name"], "source": r["source"],
                   "mc_at_call": r["first_mc"], "mult": round(r["mult"], 2) if r["mult"] else None,
                   "scan_count": r["scan_count"], "called_at": r["called_at"]} for r in calls],
        "earliest": calls[0]["sender_name"] if calls else None,
        "links": {
            "dexscreener": f"https://dexscreener.com/{'ethereum' if address.startswith('0x') else 'solana'}/{address}",
            "padre": f"https://trade.padre.gg/trade/{'eth' if address.startswith('0x') else 'solana'}/{address}",
            "gmgn": f"https://gmgn.ai/{'eth' if address.startswith('0x') else 'sol'}/token/{address}",
            "axiom": f"https://axiom.trade/t/{address}",
        },
    }


def profile(rows):
    monthly: dict[str, list] = {}
    for r in rows:
        m = time.strftime("%Y-%m", time.gmtime(r["called_at"]))
        monthly.setdefault(m, []).append(r)
    months = [{"month": m, **agg(rs)} for m, rs in sorted(monthly.items())]
    chains: dict[str, int] = {}
    for r in rows:
        chains[r["chain"]] = chains.get(r["chain"], 0) + 1
    with_mult = [r for r in rows if r["mult"]]
    best = sorted(with_mult, key=lambda r: r["mult"], reverse=True)[:5]
    worst = sorted(with_mult, key=lambda r: r["mult"])[:5]
    mcs = [r["first_mc"] for r in rows if r["first_mc"]]
    fmt = lambda r: {"ticker": r["tick"], "address": r["address"],
                     "mult": round(r["mult"], 2) if r["mult"] else None,
                     "peak_mc": round(r["eff_peak"]) if r["mult"] else None,
                     "group": r["group_name"], "caller": r["sender_name"], "called_at": r["called_at"]}
    return {"summary": agg(rows), "consistency": consistency(rows), "monthly": months,
            "chains": chains, "typical_mcap": round(statistics.median(mcs)) if mcs else 0,
            "best": [fmt(r) for r in best], "worst": [fmt(r) for r in worst],
            "recent": [fmt(r) for r in rows[:15]]}


@app.get("/api/caller/{name}")
def caller_profile(name: str):
    rows = fetch_calls(caller=name)
    p = profile(rows)
    groups: dict[str, int] = {}
    for r in rows:
        groups[r["group_name"]] = groups.get(r["group_name"], 0) + 1
    p["groups"] = sorted(groups.items(), key=lambda x: -x[1])[:8]
    return p


@app.get("/api/group/{name}")
def group_profile(name: str):
    rows = fetch_calls(group=name)
    p = profile(rows)
    p["top_callers"] = leaderboard(rows, "sender_name")[:10]
    return p


@app.get("/api/health")
def health():
    with db() as c:
        meta = {r["key"]: r["value"] for r in c.execute("SELECT * FROM meta")}
        counts = c.execute("""SELECT (SELECT COUNT(*) FROM calls) AS calls,
                              (SELECT COUNT(*) FROM tokens) AS tokens,
                              (SELECT COUNT(*) FROM tokens WHERE dead=1) AS dead""").fetchone()
    now = time.time()
    return {"version": VERSION,
            "calls": counts["calls"], "tokens": counts["tokens"], "dead": counts["dead"],
            "ingest_lag_s": round(now - float(meta.get("last_ingest", 0))),
            "peak_poll_lag_s": round(now - float(meta.get("last_peak_poll", 0)))
                               if meta.get("last_peak_poll") else None}


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html",
                        headers={"Cache-Control": "no-cache"})

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
# EOF
