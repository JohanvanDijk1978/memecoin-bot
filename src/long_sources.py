"""
long_sources.py
───────────────
Detection sources for Long.xyz (https://app.long.xyz), the Robinhood-Chain
launchpad that pairs new coins with tokenised real-world stocks.

How Long actually knows which stocks it supports
────────────────────────────────────────────────
Not from its own API. Long's GraphQL API (`https://api.long.xyz/v1/graphql`,
Hasura over an Envio HyperIndex indexer, introspection open, no auth) indexes
what has ALREADY been launched — assets, pools, swaps. The set of stocks you
are allowed to pair with is a hardcoded array compiled into a frontend JS
chunk, of the shape

    s("NVDA","NVIDIA","stock","0xd0601CE1…9EEC",18,"0x379EC4f7…9F15")
      symbol  name     kind    numeraire token  dec  chainlink feed (optional)

So "Long added a stock" is, literally, "Long shipped a frontend build with a
new entry in that array". That is the authoritative listing signal, and
`LongFrontendWatcher` is the detector for it.

But the stock TOKEN exists on-chain long before Long lists it. Every Robinhood
tokenised stock is a BeaconProxy (implementation `Stock`) deployed by one
factory on Robinhood Chain, which emits

    Deployed(bytes32 indexed uid, address stock, string name, string symbol)
    topic0 0xd9b0c6a1c0de228715ad0fa09f3259686ee84f8cc675e03ef7e47a9cdafa76d6

At seeding time that factory had deployed 206 stock tokens while Long listed
about 56 of them, so the two signals answer different questions:

  * factory `Deployed`  → "a new tokenised stock now EXISTS"   (earliest, push)
  * frontend array diff → "Long will let you pair with it"     (the tradeable one)

Both are watched, and every alert says which one fired.

Two more, as backstops:

  * `LongIndexerWatcher` — the first coin ever launched against a numeraire.
    If Long enables a stock, someone launches on it within minutes, and the
    indexer shows the new `token_numeraire_address` even if a frontend rebuild
    broke our chunk parser. Cheap insurance against our own bug.
  * `FeedWatcher` — Chainlink `EACAggregatorProxy` feeds on Robinhood Chain are
    all deployed by one EOA. About half of Long's listed stocks carry a feed
    address, so a feed appearing for a ticker Long does NOT list yet is a
    plausible "they are prepping it" tell. Treated as low confidence until it
    has predicted a real listing at least once — see HANDOFF_LONG.md.

Everything that parses is a pure function of bytes we were handed, so the whole
detection path is testable with no network at all (`tools/test_long.py`) — the
only kind of test a sandbox without crypto egress can run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, Optional

import aiohttp
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Endpoints live in fomo/.env on the VPS (ROBINHOOD_RPC / ROBINHOOD_WSS were
# deployed there 2026-08-30). Root .env first — already loaded by main.py —
# then fomo/ and dashboard/ WITHOUT overriding, the precedence
# dashboard/wallets.py::load_env_files established.
load_dotenv()
for _extra in ("fomo/.env", "dashboard/.env"):
    if os.path.exists(_extra):
        load_dotenv(_extra, override=False)


# ── constants discovered by inspecting the live app (2026-09-04) ──────────────
ROBINHOOD_CHAIN_ID = 4663
LONG_APP_BASE = os.getenv("LONG_APP_BASE", "https://app.long.xyz")
LONG_GRAPHQL_URL = os.getenv("LONG_GRAPHQL_URL", "https://api.long.xyz/v1/graphql")
LONG_REST_BASE = os.getenv("LONG_REST_BASE", "https://api.long.xyz/v1")
# Public key compiled into the frontend bundle; it gates query parameters on the
# REST routes and nothing else. Not a secret, but overridable.
LONG_API_KEY = os.getenv("LONG_API_KEY", "lxyz_49534dc2febae30294149790a8152f44bf915ebbe0332213")

RH_STOCK_FACTORY = os.getenv(
    "ROBINHOOD_STOCK_FACTORY", "0x4783C67b63dE2B358Ac5951a7D41F47A38F3C046"
).lower()
TOPIC_DEPLOYED = "0xd9b0c6a1c0de228715ad0fa09f3259686ee84f8cc675e03ef7e47a9cdafa76d6"
RH_FEED_DEPLOYER = os.getenv(
    "ROBINHOOD_FEED_DEPLOYER", "0xfE3c266C0F994f9552b70D9107214Fe0ED0d74d8"
).lower()

ROBINHOOD_RPC = os.getenv("ROBINHOOD_RPC", "")
ROBINHOOD_WSS = os.getenv("ROBINHOOD_WSS", "")
ROBINHOOD_EXPLORER_API = os.getenv(
    "ROBINHOOD_EXPLORER_API", "https://robinhoodchain.blockscout.com/api/v2"
).rstrip("/")

# Pages whose bundles carry the numeraire array. /create is the launch form, so
# it is the page that must know every pairable asset; / is a cheap second look.
LONG_CONFIG_PAGES = [p.strip() for p in os.getenv(
    "LONG_CONFIG_PAGES", "/create,/").split(",") if p.strip()]

USER_AGENT = os.getenv(
    "LONG_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
)


# ═══════════════════════════════════════════════════════════════════════════
# Pure parsers — no I/O, no clock, no globals. Tested offline.
# ═══════════════════════════════════════════════════════════════════════════

_CHUNK_RE = re.compile(r'/_next/static/chunks/[A-Za-z0-9._-]+\.js')

# The array entries are emitted through a helper whose name the minifier picks
# fresh on every build, so the entry is matched by SHAPE, never by identifier:
#   (<sym>, <name>, <kind>, <address|X.zeroAddress>, <decimals>, <feed|void 0>)
_NUMERAIRE_RE = re.compile(
    r'\(\s*"([A-Z0-9.\-]{1,12})"\s*,'          # symbol
    r'\s*"([^"]{1,80})"\s*,'                   # display name
    r'\s*"(stock|etf|stable|native|crypto|index|commodity|forex|leverage)"\s*,'
    r'\s*(?:"(0x[0-9a-fA-F]{40})"|[A-Za-z_$][\w$.]*)\s*,'   # address or X.zeroAddress
    r'\s*(\d{1,2})\s*,'                        # decimals
    r'\s*(?:"(0x[0-9a-fA-F]{40})"|void\s+0|[A-Za-z_$][\w$.]*)'  # feed | void 0 | ident
)

_LEVERAGE_RE = re.compile(
    r'\(\s*"(0x[0-9a-fA-F]{40})"\s*\)\s*,\s*name\s*:\s*"([^"]{1,60})"\s*,'
    r'\s*ticker\s*:\s*"([A-Za-z0-9]{1,16})"[^}]*?underlyingSymbol\s*:\s*"([A-Z0-9.\-]{1,12})"'
)

ZERO_ADDRESS = "0x" + "0" * 40


def chunk_urls_from_html(html: str, base: str = LONG_APP_BASE) -> list[str]:
    """Every `_next/static/chunks/*.js` referenced by a page, de-duped, sorted.

    Sorted so the fingerprint is stable against Next.js reordering its
    preloads — we want to react to a DEPLOY, not to a shuffled tag order.
    """
    return sorted({base.rstrip("/") + m for m in set(_CHUNK_RE.findall(html))})


def chunk_fingerprint(urls: list[str]) -> str:
    """Cheap identity of a frontend build. Chunk filenames are content-hashed,
    so this string changes if and only if something shipped."""
    import hashlib
    return hashlib.sha256("\n".join(sorted(urls)).encode()).hexdigest()[:16]


def parse_numeraires(js: str) -> list[dict]:
    """Extract Long's pairable-asset array from a JS chunk.

    Returns [] for a chunk that does not contain it, which is how the chunk
    scan decides which chunk is the config chunk. A partial match is worse than
    no match, so callers should require a plausible count before trusting it.
    """
    out: dict[str, dict] = {}
    for m in _NUMERAIRE_RE.finditer(js):
        symbol, name, kind, address, decimals, feed = m.groups()
        addr = (address or ZERO_ADDRESS).lower()
        row = {
            "symbol": symbol,
            "name": name,
            "kind": kind,
            "address": addr,
            "decimals": int(decimals),
            "feed": (feed or "").lower() or None,
        }
        # Later definitions win; the array is emitted once per build.
        out[addr] = row
    return sorted(out.values(), key=lambda r: (r["kind"], r["symbol"]))


def parse_leverage_tokens(js: str) -> list[dict]:
    """The leverage pairing tokens (NVDA 3x Long and friends) are a second,
    differently-shaped array in the same bundle. Same idea, own regex."""
    out = []
    for m in _LEVERAGE_RE.finditer(js):
        address, name, ticker, underlying = m.groups()
        out.append({
            "symbol": ticker, "name": name, "kind": "leverage",
            "address": address.lower(), "decimals": 18, "feed": None,
            "extra": {"underlying": underlying},
        })
    return out


def looks_like_config_chunk(rows: list[dict], min_stocks: int = 8) -> bool:
    """Guard against a regex that matched something unrelated. The real array
    always carries many `stock` entries; anything less is not it."""
    return sum(1 for r in rows if r["kind"] in ("stock", "etf")) >= min_stocks


def clean_stock_name(name: str) -> str:
    """`Apple • Robinhood Token` → `Apple`."""
    if not name:
        return ""
    return re.split(r"\s*[•·|]\s*", name)[0].strip()


# ── minimal ABI decoding (no eth-abi dependency in this repo) ────────────────
def _word(data: str, i: int) -> str:
    return data[i * 64:(i + 1) * 64]


def _decode_dyn_string(data: str, offset_bytes: int) -> str:
    start = offset_bytes * 2
    length = int(data[start:start + 64] or "0", 16)
    raw = data[start + 64:start + 64 + length * 2]
    try:
        return bytes.fromhex(raw).decode("utf-8", "replace")
    except ValueError:
        return ""


def decode_deployed_log(log: dict) -> Optional[dict]:
    """Decode one `Deployed(bytes32 indexed uid, address stock, string name,
    string symbol)` log into a plain dict. Returns None if it isn't one.

    Written by hand rather than pulled from eth-abi because memebot has no web3
    dependency and this is three words of head plus two strings.
    """
    topics = [t.lower() for t in (log.get("topics") or [])]
    if not topics or topics[0] != TOPIC_DEPLOYED:
        return None
    data = (log.get("data") or "").lower()
    if data.startswith("0x"):
        data = data[2:]
    if len(data) < 192:
        return None
    try:
        stock = "0x" + _word(data, 0)[24:]
        name = _decode_dyn_string(data, int(_word(data, 1), 16))
        symbol = _decode_dyn_string(data, int(_word(data, 2), 16))
    except (ValueError, IndexError):
        return None
    blk = log.get("blockNumber")
    if isinstance(blk, str):
        blk = int(blk, 16) if blk.startswith("0x") else int(blk)
    return {
        "address": stock.lower(),
        "name": clean_stock_name(name),
        "raw_name": name,
        "symbol": symbol,
        "uid": topics[1] if len(topics) > 1 else None,
        "block_number": blk,
        "tx_hash": log.get("transactionHash"),
    }


def decode_description(hex_result: str) -> Optional[str]:
    """Decode the ABI-encoded string returned by a Chainlink feed's
    `description()` (selector 0x7284e416) — e.g. `AAPL / USD`."""
    if not hex_result or hex_result in ("0x", "0x0"):
        return None
    data = hex_result[2:] if hex_result.startswith("0x") else hex_result
    if len(data) < 128:
        return None
    try:
        return _decode_dyn_string(data, int(_word(data, 0), 16)) or None
    except (ValueError, IndexError):
        return None


def symbol_from_description(desc: str) -> Optional[str]:
    """`AAPL / USD` → `AAPL`."""
    if not desc:
        return None
    head = desc.split("/")[0].strip().upper()
    return head if re.fullmatch(r"[A-Z0-9.\-]{1,12}", head or "") else None


# ═══════════════════════════════════════════════════════════════════════════
# Transports
# ═══════════════════════════════════════════════════════════════════════════

class Http:
    """One shared aiohttp session with sane timeouts and connection reuse.

    Connection reuse matters here: the frontend poll runs every few seconds and
    a fresh TLS handshake each time would cost more than the request.
    """

    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self._session = session
        self._own = session is None

    async def __aenter__(self) -> "Http":
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=20),
                connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300),
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own and self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        assert self._session is not None, "Http used outside its context manager"
        return self._session

    async def get_text(self, url: str, **kw) -> tuple[int, str, dict]:
        async with self.session.get(url, **kw) as r:
            return r.status, await r.text(), dict(r.headers)

    async def post_json(self, url: str, payload: dict, **kw) -> Any:
        async with self.session.post(url, json=payload, **kw) as r:
            if r.status >= 400:
                raise RuntimeError(f"{url} -> HTTP {r.status}: {(await r.text())[:200]}")
            return await r.json(content_type=None)


class JsonRpc:
    """Tiny JSON-RPC client over the shared session, with id bookkeeping."""

    def __init__(self, http: Http, url: str):
        self.http = http
        self.url = url
        self._id = 0

    async def call(self, method: str, params: list) -> Any:
        self._id += 1
        res = await self.http.post_json(
            self.url, {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        )
        if isinstance(res, dict) and res.get("error"):
            raise RuntimeError(f"{method}: {res['error']}")
        return res.get("result") if isinstance(res, dict) else None

    async def block_number(self) -> int:
        return int(await self.call("eth_blockNumber", []), 16)

    async def get_logs(self, address: str, topics: list, from_block: int, to_block: int) -> list:
        return await self.call("eth_getLogs", [{
            "address": address,
            "topics": topics,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }]) or []

    async def eth_call(self, to: str, data: str) -> Optional[str]:
        try:
            return await self.call("eth_call", [{"to": to, "data": data}, "latest"])
        except Exception:
            return None


# ═══════════════════════════════════════════════════════════════════════════
# Source 1 — Long's frontend: the authoritative "Long supports this" signal
# ═══════════════════════════════════════════════════════════════════════════

class LongFrontendWatcher:
    """Detect a change to Long's pairable-asset array.

    Cost model, which is the whole design:

      * The steady-state poll is ONE request for a page's HTML. Chunk filenames
        are content-hashed, so the sorted chunk list is a fingerprint of the
        build. Unchanged fingerprint ⇒ nothing shipped ⇒ stop. No JS is fetched
        and nothing is parsed 99.99% of the time.
      * When the fingerprint moves, only the chunks that are NEW in this build
        are downloaded — an unchanged chunk keeps its hash, so its bytes cannot
        have changed. A full rescan is the fallback if the array is not in any
        of them (which happens when the config chunk was merged into an
        existing name).

    Cloudflare caches the HTML at the edge (`s-maxage`, `x-nextjs-stale-time:
    300`), so a plain GET can be up to five minutes stale — which would eat the
    entire latency budget. A unique query string makes the edge cache key miss
    and the request reach the origin, which is why every poll carries `?_lw=`.
    """

    def __init__(self, http: Http, pages: Optional[list[str]] = None,
                 base: str = LONG_APP_BASE):
        self.http = http
        self.base = base.rstrip("/")
        self.pages = pages or LONG_CONFIG_PAGES
        self._seen_chunks: set[str] = set()
        self._config_chunk: Optional[str] = None
        self._last_fingerprint: Optional[str] = None

    async def _page_html(self, page: str) -> str:
        url = f"{self.base}{page}"
        sep = "&" if "?" in url else "?"
        status, text, _ = await self.http.get_text(
            f"{url}{sep}_lw={int(time.time() * 1000)}",
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
        if status != 200:
            raise RuntimeError(f"{url} -> HTTP {status}")
        return text

    async def _chunk_text(self, url: str) -> str:
        # Chunk URLs are immutable (content hash in the name), so they may be
        # served from any cache without risk.
        status, text, _ = await self.http.get_text(url)
        if status != 200:
            raise RuntimeError(f"{url} -> HTTP {status}")
        return text

    async def snapshot(self) -> dict:
        """Read the current pairable-asset set. Raises if it cannot be found —
        a silent empty list would look exactly like 'Long delisted everything'.
        """
        urls: list[str] = []
        for page in self.pages:
            try:
                urls.extend(chunk_urls_from_html(await self._page_html(page), self.base))
            except Exception as e:
                logger.warning("long: page %s failed: %s", page, e)
        urls = sorted(set(urls))
        if not urls:
            raise RuntimeError("no chunk URLs found on any Long page")

        fingerprint = chunk_fingerprint(urls)

        # Candidate order: the chunk that held the array last time (cheapest
        # possible hit), then everything this build introduced, then the rest.
        candidates: list[str] = []
        if self._config_chunk and self._config_chunk in urls:
            candidates.append(self._config_chunk)
        candidates += [u for u in urls if u not in self._seen_chunks and u not in candidates]
        candidates += [u for u in urls if u not in candidates]

        for url in candidates:
            try:
                js = await self._chunk_text(url)
            except Exception:
                continue
            self._seen_chunks.add(url)
            rows = parse_numeraires(js)
            if looks_like_config_chunk(rows):
                rows += [r for r in parse_leverage_tokens(js)
                         if r["address"] not in {x["address"] for x in rows}]
                self._config_chunk = url
                self._last_fingerprint = fingerprint
                self._seen_chunks.update(urls)
                return {
                    "fingerprint": fingerprint,
                    "chunk_url": url,
                    "numeraires": rows,
                    "chunk_count": len(urls),
                }
        raise RuntimeError(
            f"pairable-asset array not found in any of {len(urls)} chunks "
            f"(build {fingerprint}) — Long may have changed how it ships config"
        )

    async def build_changed(self) -> Optional[str]:
        """One cheap request. Returns the new fingerprint if the build moved,
        else None. This is what runs on the hot loop."""
        urls: list[str] = []
        for page in self.pages:
            try:
                urls.extend(chunk_urls_from_html(await self._page_html(page), self.base))
            except Exception as e:
                logger.debug("long: build check on %s failed: %s", page, e)
        if not urls:
            return None
        fp = chunk_fingerprint(sorted(set(urls)))
        if self._last_fingerprint is None:
            self._last_fingerprint = fp
            return None
        if fp != self._last_fingerprint:
            return fp
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Source 2 — Robinhood's stock-token factory: the earliest signal that exists
# ═══════════════════════════════════════════════════════════════════════════

class RobinhoodFactoryWatcher:
    """Push detection of a brand-new tokenised stock on Robinhood Chain.

    One `eth_subscribe("logs", {address: factory, topics: [Deployed]})` over the
    Alchemy websocket already configured in `fomo/.env`. The chain produces a
    block roughly every 100 ms, so the notification lands within a block or two
    of the transaction — there is nothing upstream of this short of the mempool,
    and the deploy is a Robinhood-operated transaction that never sits there.

    A subscription can silently die. The reconcile sweep (`eth_getLogs` from the
    last block we processed) runs on a slow timer and is the ONLY thing that
    covers a reconnect gap. Do not remove it when tidying — same lesson as the
    multi-wallet watcher.
    """

    def __init__(self, http: Http, on_event: Callable[[dict], Awaitable[None]],
                 wss_url: str = "", rpc_url: str = "",
                 get_cursor: Optional[Callable[[], Optional[int]]] = None,
                 set_cursor: Optional[Callable[[int], None]] = None):
        self.http = http
        self.on_event = on_event
        self.wss_url = wss_url or ROBINHOOD_WSS
        self.rpc = JsonRpc(http, rpc_url or ROBINHOOD_RPC)
        self.get_cursor = get_cursor or (lambda: None)
        self.set_cursor = set_cursor or (lambda b: None)
        self.connected = False

    async def _emit(self, log: dict, via: str) -> None:
        row = decode_deployed_log(log)
        if not row:
            return
        row["source"] = f"robinhood_factory:{via}"
        # The block timestamp is what makes the latency numbers meaningful: it
        # turns "we alerted at X" into "we alerted X ms after the chain knew".
        row["chain_ts"] = await self._block_ts(log.get("blockNumber"))
        await self.on_event(row)

    async def _block_ts(self, block: Any) -> Optional[float]:
        if block is None or not self.rpc.url:
            return None
        try:
            blk = block if isinstance(block, str) and block.startswith("0x") else hex(int(block))
            b = await self.rpc.call("eth_getBlockByNumber", [blk, False])
            return int(b["timestamp"], 16) if b and b.get("timestamp") else None
        except Exception:
            return None

    async def sweep(self, span: int = 200_000) -> int:
        """Fill any gap between the last processed block and the head."""
        if not self.rpc.url:
            return 0
        head = await self.rpc.block_number()
        start = self.get_cursor()
        if start is None:
            start = max(0, head - span)
        found = 0
        # Robinhood Chain mines ~10 blocks/second, so a gap of even a few
        # minutes is thousands of blocks. Walk it in bounded windows so one
        # oversized eth_getLogs cannot fail the whole sweep.
        window = int(os.getenv("LONG_SWEEP_WINDOW", "50000"))
        cur = start
        while cur <= head:
            end = min(cur + window, head)
            try:
                logs = await self.rpc.get_logs(RH_STOCK_FACTORY, [TOPIC_DEPLOYED], cur, end)
            except Exception as e:
                logger.warning("long: factory sweep %s-%s failed: %s", cur, end, e)
                break
            for lg in logs:
                await self._emit(lg, "sweep")
                found += 1
            self.set_cursor(end)
            cur = end + 1
        return found

    async def run(self) -> None:
        """Subscribe forever, reconnecting with backoff."""
        if not self.wss_url:
            logger.warning("long: ROBINHOOD_WSS unset — factory watcher is sweep-only")
            while True:
                try:
                    await self.sweep()
                except Exception as e:
                    logger.warning("long: factory sweep failed: %s", e)
                await asyncio.sleep(int(os.getenv("LONG_SWEEP_SECONDS", "60")))

        backoff = 1.0
        while True:
            try:
                async with self.http.session.ws_connect(
                    self.wss_url, heartbeat=30
                ) as ws:
                    await ws.send_json({
                        "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                        "params": ["logs", {"address": RH_STOCK_FACTORY,
                                            "topics": [TOPIC_DEPLOYED]}],
                    })
                    self.connected = True
                    backoff = 1.0
                    logger.info("long: subscribed to Robinhood stock factory %s",
                                RH_STOCK_FACTORY)
                    # Anything that happened while we were away.
                    try:
                        await self.sweep()
                    except Exception as e:
                        logger.warning("long: post-connect sweep failed: %s", e)

                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            data = json.loads(msg.data)
                        except Exception:
                            continue
                        if data.get("method") != "eth_subscription":
                            continue
                        log = (data.get("params") or {}).get("result") or {}
                        await self._emit(log, "ws")
                        blk = log.get("blockNumber")
                        if isinstance(blk, str):
                            try:
                                self.set_cursor(int(blk, 16))
                            except ValueError:
                                pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("long: factory websocket dropped (%s), retrying in %.0fs",
                               e, backoff)
            finally:
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ═══════════════════════════════════════════════════════════════════════════
# Source 3 — Long's indexer: first coin ever launched against a numeraire
# ═══════════════════════════════════════════════════════════════════════════

TOKEN_QUERY = """
query NewTokens($after: timestamptz!, $limit: Int!) {
  Token(
    where: {token_creation_timestamp: {_gt: $after}, chain_id: {_eq: %d}}
    order_by: {token_creation_timestamp: asc}
    limit: $limit
  ) {
    id
    token_address
    token_symbol
    token_name
    token_numeraire_address
    token_creation_timestamp
    token_image_public_url
    chain_id
  }
}
""" % ROBINHOOD_CHAIN_ID

NUMERAIRES_IN_USE_QUERY = """
query UsedNumeraires {
  Token(where: {chain_id: {_eq: %d}}, distinct_on: token_numeraire_address) {
    token_numeraire_address
    token_symbol
    token_address
    token_creation_timestamp
  }
}
""" % ROBINHOOD_CHAIN_ID


class LongIndexerWatcher:
    """Poll Long's own GraphQL indexer for newly launched coins.

    Why polling and not a subscription: the schema exposes `Token_stream` and
    friends, but `wss://api.long.xyz/v1/graphql` closed with 1006 on every
    subprotocol tried from a browser on 2026-09-04, so subscriptions are not
    reachable in practice. `try_websocket()` re-tests it at runtime and the
    handoff records what to do if it ever starts answering.

    Polling here is genuinely cheap: one small POST returning only tokens newer
    than a cursor. At a 2 s interval that is well inside what a public Hasura
    endpoint expects, and the coins we care about — the FIRST coin against a
    numeraire nobody has used before — are rare regardless of launch volume.
    """

    def __init__(self, http: Http, on_event: Callable[[dict], Awaitable[None]],
                 url: str = LONG_GRAPHQL_URL,
                 get_cursor: Optional[Callable[[], Optional[str]]] = None,
                 set_cursor: Optional[Callable[[str], None]] = None):
        self.http = http
        self.on_event = on_event
        self.url = url
        self.get_cursor = get_cursor or (lambda: None)
        self.set_cursor = set_cursor or (lambda v: None)

    async def gql(self, query: str, variables: Optional[dict] = None) -> dict:
        res = await self.http.post_json(
            self.url, {"query": query, "variables": variables or {}},
            headers={"content-type": "application/json"},
        )
        if res.get("errors"):
            raise RuntimeError(f"graphql: {json.dumps(res['errors'])[:300]}")
        return res.get("data") or {}

    async def used_numeraires(self) -> list[dict]:
        """Every numeraire that already has at least one coin. Used for seeding
        so the first run cannot alert on 50 stocks at once."""
        data = await self.gql(NUMERAIRES_IN_USE_QUERY)
        return data.get("Token") or []

    async def poll_once(self, limit: int = 200) -> list[dict]:
        after = self.get_cursor() or _iso_minutes_ago(5)
        data = await self.gql(TOKEN_QUERY, {"after": after, "limit": limit})
        tokens = data.get("Token") or []
        for t in tokens:
            t["source"] = "long_indexer:poll"
            await self.on_event(t)
        if tokens:
            self.set_cursor(tokens[-1]["token_creation_timestamp"])
        return tokens

    async def run(self, interval: float = 2.0) -> None:
        backoff = interval
        while True:
            try:
                await self.poll_once()
                backoff = interval
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("long: indexer poll failed: %s", e)
                backoff = min(backoff * 2, 60.0)
            await asyncio.sleep(backoff)

    async def try_websocket(self, timeout: float = 8.0) -> dict:
        """Diagnostic: does the GraphQL websocket answer at all? Reported by
        tools/diag_long.py so a future session can switch to push the day it
        starts working, without re-deriving any of this."""
        ws_url = self.url.replace("https://", "wss://").replace("http://", "ws://")
        for proto in ("graphql-transport-ws", "graphql-ws"):
            try:
                async with self.http.session.ws_connect(
                    ws_url, protocols=(proto,), timeout=timeout
                ) as ws:
                    await ws.send_json({"type": "connection_init", "payload": {}})
                    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
                    return {"ok": True, "protocol": proto, "first": str(msg.data)[:200]}
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
        return {"ok": False, "error": last}


def _iso_minutes_ago(minutes: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00")


# ═══════════════════════════════════════════════════════════════════════════
# Source 4 — Chainlink feeds: the speculative "Long is prepping this" tell
# ═══════════════════════════════════════════════════════════════════════════

class FeedWatcher:
    """Watch for new Chainlink `EACAggregatorProxy` deployments.

    Every feed referenced by Long's array was deployed by one EOA on Robinhood
    Chain. A contract creation emits no log, so there is nothing to subscribe
    to: this polls the Blockscout transaction list for that address and looks
    for `created_contract`. Blockscout's public tier rate-limits hard (429 seen
    while researching), so the interval is deliberately slow — this signal is a
    hint measured in hours, not a race.

    The ticker comes from calling `description()` on the new feed, which returns
    e.g. `AAPL / USD`. Confidence stays LOW until a feed has actually preceded a
    Long listing at least once; the latency table is what will answer that.
    """

    def __init__(self, http: Http, on_event: Callable[[dict], Awaitable[None]],
                 explorer: str = ROBINHOOD_EXPLORER_API, rpc_url: str = "",
                 deployer: str = RH_FEED_DEPLOYER):
        self.http = http
        self.on_event = on_event
        self.explorer = explorer.rstrip("/")
        self.rpc = JsonRpc(http, rpc_url or ROBINHOOD_RPC)
        self.deployer = deployer

    async def _created_contracts(self, pages: int = 1) -> list[dict]:
        out: list[dict] = []
        url = f"{self.explorer}/addresses/{self.deployer}/transactions?filter=from"
        for _ in range(max(1, pages)):
            status, text, _h = await self.http.get_text(url)
            if status == 429:
                logger.info("long: blockscout rate-limited the feed watcher; backing off")
                break
            if status != 200:
                break
            try:
                data = json.loads(text)
            except Exception:
                break
            for t in data.get("items") or []:
                created = (t.get("created_contract") or {}).get("hash")
                if created:
                    out.append({
                        "address": created.lower(),
                        "name": (t.get("created_contract") or {}).get("name"),
                        "tx_hash": t.get("hash"),
                        "block_number": t.get("block_number"),
                        "timestamp": t.get("timestamp"),
                    })
            nxt = data.get("next_page_params")
            if not nxt:
                break
            from urllib.parse import urlencode
            url = (f"{self.explorer}/addresses/{self.deployer}/transactions"
                   f"?filter=from&{urlencode(nxt)}")
        return out

    async def describe(self, feed_address: str) -> Optional[str]:
        """`description()` — selector 0x7284e416."""
        raw = await self.rpc.eth_call(feed_address, "0x7284e416")
        return decode_description(raw or "")

    async def poll_once(self, known: set[str]) -> list[dict]:
        found = []
        for c in await self._created_contracts():
            if c["address"] in known:
                continue
            desc = await self.describe(c["address"])
            c["description"] = desc
            c["symbol"] = symbol_from_description(desc or "")
            c["source"] = "chainlink_feed"
            found.append(c)
            await self.on_event(c)
        return found

    async def run(self, known_provider: Callable[[], set[str]],
                  interval: float = 300.0) -> None:
        while True:
            try:
                await self.poll_once(known_provider())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("long: feed watcher failed: %s", e)
            await asyncio.sleep(interval)
