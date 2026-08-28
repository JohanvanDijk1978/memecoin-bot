"""
wallets.py — holdings, token supply and cost basis for the Wallet Groups page.

This module knows nothing about the dashboard. It takes an httpx client and
addresses and returns plain dicts; `wgroups.py` owns the storage and the loop.

Providers, in the order they are tried:

  Solana holdings  `getTokenAccountsByOwner` on SOLANA_RPC (Helius) — one
                   request per wallet, returns every SPL and Token-2022
                   account it owns.

  EVM holdings     1. Etherscan V2 `addresstokenbalance` — one request per
                      wallet per chain. It is a Pro-plan action; if the key is
                      not entitled the provider retires itself for the life of
                      the process instead of spending a request per round.
                   2. `alchemy_getTokenBalances` on the chain's own RPC — every
                      ERC-20 the wallet holds, on the free plan, over the URL
                      eth_call already uses. This and Etherscan are the only
                      two that DISCOVER; a plain RPC answers "method not found"
                      and the chain drops to the watchlist for good.
                   3. Watchlist scan — batched `eth_call balanceOf` over the
                      EVM tokens this dashboard already knows on that chain.
                      Free, works on any public RPC, and CONFIRMS only tokens
                      the bot has already seen — it can never surface a new
                      one. The UI says so when this is the provider in use.

  Cost basis       Solana: Solscan `account/defi/activities` for that exact
                   (wallet, mint) pair — one request, and only for pairs that
                   already qualify for a card.
                   EVM: Etherscan `tokentx` + `txlist`, off unless
                   WG_EVM_BASIS=1.
                   Anything that cannot be read on chain falls back to the
                   cost basis wgroups.py observes while it watches the wallet.

Keys are read from the environment, then from ../fomo/.env and ../.env if they
exist (they do on the VPS). Nothing here is required: with no keys at all the
page still works — public RPCs for Solana and EVM, observed cost basis only.

Every provider reports itself through `PROVIDER_STATUS` so the page can say
which source a number actually came from, and `tools/diag_wallet_groups.py`
can show it without reading logs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("memedash.wallets")

# --------------------------------------------------------------- env loading

def load_env_files(*paths: Path) -> None:
    """Fill missing env vars from .env files. Never overrides a real env var."""
    for path in paths:
        try:
            if not path.exists():
                continue
            for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if val[:1] in ("'", '"') and val[-1:] == val[:1] and len(val) > 1:
                    val = val[1:-1]
                elif " #" in val:            # trailing comment on an unquoted value
                    val = val.split(" #", 1)[0].strip()
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception as e:                # a malformed .env must not stop the app
            log.warning(f"could not read {path}: {e}")


# Loaded once, at import: the dashboard's own .env first, then the bot's and
# fomo's, which is where SOLANA_RPC / SOLSCAN_API_KEY / ETHERSCAN_API_KEY
# already live on the VPS. A real environment variable always wins.
_HERE = Path(__file__).resolve().parent
load_env_files(_HERE / ".env", _HERE.parent / "fomo" / ".env", _HERE.parent / ".env")


# How a provider is doing, for the UI and the diagnostics tool.
PROVIDER_STATUS: dict[str, dict] = {}


def _mark(name: str, ok: bool, note: str = "") -> None:
    PROVIDER_STATUS[name] = {"ok": ok, "note": note, "at": time.time()}


# --------------------------------------------------------------- chain basics

SOL_TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022
)
WSOL = "So11111111111111111111111111111111111111112"

# Dexscreener chainId -> EVM chain id, for Etherscan V2's `chainid` parameter.
EVM_CHAIN_IDS: dict[str, int] = {"ethereum": 1, "base": 8453, "bsc": 56}

# Public RPCs that need no key. Override any of them with EVM_RPC_<CHAIN>.
DEFAULT_EVM_RPC = {
    "ethereum": "https://ethereum-rpc.publicnode.com",
    "base":     "https://base-rpc.publicnode.com",
    "bsc":      "https://bsc-rpc.publicnode.com",
}
# Wrapped native per chain — used to price the other leg of a swap.
NATIVE_WRAPPED = {
    "solana":   WSOL,
    "ethereum": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "base":     "0x4200000000000000000000000000000000000006",
    "bsc":      "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
}

# Stablecoins price at $1 — the cheap, exact side of a swap.
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA",    # USDS
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",   # PYUSD
}

# Never a "shared memecoin": majors, wrapped natives, stables, LSTs.
BORING_SYMBOLS = {
    "SOL", "WSOL", "MSOL", "JITOSOL", "BSOL", "JUPSOL", "INF", "STSOL",
    "ETH", "WETH", "STETH", "WSTETH", "WEETH", "RETH", "CBETH",
    "BTC", "WBTC", "CBBTC", "TBTC", "BNB", "WBNB",
    "USDC", "USDT", "USDS", "DAI", "BUSD", "FDUSD", "PYUSD", "USDE", "SUSDE",
    "USDC.E", "USDT0", "FRAX", "TUSD", "USDD", "LUSD", "GUSD", "EURC",
}
BORING_MINTS = STABLE_MINTS | {WSOL} | {v for v in NATIVE_WRAPPED.values()}


def wallet_kind(address: str) -> str:
    """'evm', 'sol', or '' when the string is not a wallet address at all."""
    a = (address or "").strip()
    if a.startswith("0x") and len(a) == 42:
        try:
            int(a[2:], 16)
            return "evm"
        except ValueError:
            return ""
    if 32 <= len(a) <= 44 and a.strip("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz") == "":
        return "sol"
    return ""


def normalize_wallet(address: str) -> str:
    a = (address or "").strip()
    return a.lower() if a.startswith("0x") else a


def is_boring(address: str, symbol: str = "") -> bool:
    return (address or "").lower() in {b.lower() for b in BORING_MINTS} \
        or (symbol or "").upper() in BORING_SYMBOLS


def rpc_display_name(url: str) -> str:
    """An endpoint label that does not leak the API key in its path."""
    from urllib.parse import urlsplit
    parsed = urlsplit(url or "")
    host = parsed.hostname or ""
    if not host:
        return ""
    if parsed.port:
        host += f":{parsed.port}"
    return f"{parsed.scheme}://{host}" if parsed.scheme else host


def solana_rpcs() -> list[str]:
    """SOLANA_RPC then SOLANA_RPC_FALLBACKS, then the public endpoint.

    The public endpoint throttles hard, so it is only ever the last resort —
    but it means the page works on a machine with no keys configured at all.
    """
    urls = [os.getenv("SOLANA_RPC", "").strip()]
    urls += [u.strip() for u in os.getenv("SOLANA_RPC_FALLBACKS", "").split(",")]
    urls.append("https://api.mainnet-beta.solana.com")
    out, seen = [], set()
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _chain_prefix(chain_id: str) -> str:
    """Short prefix for a chain name. E.g. 'ethereum' -> 'ETH', 'base' -> 'BASE'."""
    prefixes = {"ethereum": "ETH", "base": "BASE", "bsc": "BSC", "robinhood": "ROBINHOOD"}
    return prefixes.get(chain_id.lower(), chain_id.upper())


def evm_rpc_source(chain_id: str) -> str:
    """Which env var supplies this chain's RPC — '' when nothing does.

    Two naming conventions are in play on the VPS: this module's own
    EVM_RPC_<CHAIN>, and fomo/.env's <PREFIX>_RPC (ETH_RPC, BASE_RPC, …),
    which `load_env_files` above pulls into the environment. Returning the key
    rather than the URL is what lets the diagnostics tool say where a value
    came from without printing an API key.
    """
    for key in (f"EVM_RPC_{chain_id.upper()}",
                f"{chain_id.upper()}_RPC",
                f"{_chain_prefix(chain_id)}_RPC"):
        if os.getenv(key, "").strip():
            return key
    return ""


def evm_rpc(chain_id: str) -> str:
    """EVM RPC URL for a chain, or the public default when none is configured."""
    key = evm_rpc_source(chain_id)
    return os.getenv(key, "").strip() if key else DEFAULT_EVM_RPC.get(chain_id, "")


def evm_chain_ids() -> dict[str, int]:
    """EVM_CHAIN_IDS plus anything configured, e.g. WG_CHAIN_ID_ROBINHOOD=…

    Robinhood Chain is in the bot's chain list but its numeric id isn't
    hard-coded here: set WG_CHAIN_ID_ROBINHOOD and EVM_RPC_ROBINHOOD and it
    joins the scan with no code change.
    """
    ids = dict(EVM_CHAIN_IDS)
    for key, val in os.environ.items():
        if key.startswith("WG_CHAIN_ID_") and val.strip().isdigit():
            ids[key[len("WG_CHAIN_ID_"):].lower()] = int(val.strip())
    return ids


def evm_chains() -> list[str]:
    """EVM chains we can actually scan — one with neither RPC nor key is skipped."""
    want = [c.strip().lower() for c in
            os.getenv("WG_EVM_CHAINS", "ethereum,base,bsc,robinhood").split(",") if c.strip()]
    ids = evm_chain_ids()
    return [c for c in want if evm_rpc(c) or (ids.get(c) and os.getenv("ETHERSCAN_API_KEY", "").strip())]


# --------------------------------------------------------------- solana holdings

async def sol_rpc(client, method: str, params: list, timeout: float = 20.0):
    """One JSON-RPC call, walking the configured endpoints until one answers."""
    last = "no endpoint configured"
    for url in solana_rpcs():
        try:
            r = await client.post(url, json={"jsonrpc": "2.0", "id": 1,
                                             "method": method, "params": params},
                                  timeout=timeout)
            if r.status_code in (429, 503):       # throttled — try the next endpoint
                last = f"{r.status_code} from {url.split('//')[-1].split('/')[0]}"
                continue
            r.raise_for_status()
            payload = r.json()
            if payload.get("error"):
                last = payload["error"]
                continue
            return payload.get("result")
        except Exception as e:
            last = repr(e)
    raise RuntimeError(f"solana {method}: {last}")


async def sol_holdings(client, wallet: str) -> list[dict]:
    """Every SPL/Token-2022 position of one wallet. One request per program.

    A wallet can hold the same mint in more than one token account, so the
    amounts are summed per mint rather than returned per account.
    """
    amounts: dict[str, float] = {}
    decimals: dict[str, int] = {}
    for i, program in enumerate(SOL_TOKEN_PROGRAMS):
        try:
            res = await sol_rpc(client, "getTokenAccountsByOwner",
                                [wallet, {"programId": program}, {"encoding": "jsonParsed"}])
        except Exception as e:
            if i == 0:
                _mark("solana_holdings", False, str(e)[:160])
                raise
            log.debug(f"token-2022 scan skipped for {wallet}: {e}")
            continue          # not every endpoint indexes Token-2022; SPL is the one that matters
        for acc in (res or {}).get("value") or []:
            info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            mint = info.get("mint")
            ta = info.get("tokenAmount") or {}
            try:
                amt = float(ta.get("uiAmountString") or ta.get("uiAmount") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            if mint and amt > 0:
                amounts[mint] = amounts.get(mint, 0.0) + amt
                decimals[mint] = int(ta.get("decimals") or 0)
    _mark("solana_holdings", True, f"{len(amounts)} positions")
    return [{"address": m, "chain_id": "solana", "amount": a, "decimals": decimals.get(m, 0)}
            for m, a in amounts.items()]


# --------------------------------------------------------------- evm holdings

ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
BALANCE_OF = "0x70a08231"
DECIMALS_SEL = "0x313ce567"
TOTAL_SUPPLY = "0x18160ddd"
RPC_BATCH = 40                       # eth_calls per HTTP request

# Chains whose Etherscan `addresstokenbalance` this key may not call. Populated
# on the first refusal and never re-probed: a plan does not change mid-process,
# and re-probing would spend a request per wallet per round to learn nothing.
_etherscan_retired: set[str] = set()


class EtherscanRefused(Exception):
    pass


async def etherscan(client, chain_id: str, action: str, params: dict, module: str = "account"):
    """Etherscan V2 — one host, `chainid` selects the chain. None = unusable."""
    key = os.getenv("ETHERSCAN_API_KEY", "").strip()
    cid = evm_chain_ids().get(chain_id)
    if not key or not cid:
        return None
    r = await client.get(ETHERSCAN_V2, timeout=25, params={
        "chainid": cid, "module": module, "action": action, "apikey": key, **params})
    r.raise_for_status()
    payload = r.json()
    if str(payload.get("status")) == "1":
        return payload.get("result")
    note = f"{payload.get('message', '')} {payload.get('result', '')}".strip().lower()
    if "no transactions found" in note or "no records found" in note:
        return []                     # a real, empty answer — not a failure
    raise EtherscanRefused(note[:200] or "etherscan returned NOTOK")


async def _evm_rpc_batch(client, chain_id: str, calls: list[dict]) -> list:
    """A JSON-RPC batch. Returns results positionally; failures come back None."""
    url = evm_rpc(chain_id)
    if not url or not calls:
        return [None] * len(calls)
    out: list = [None] * len(calls)
    for start in range(0, len(calls), RPC_BATCH):
        chunk = calls[start:start + RPC_BATCH]
        body = [{"jsonrpc": "2.0", "id": start + i, **c} for i, c in enumerate(chunk)]
        try:
            r = await client.post(url, json=body, timeout=25)
            r.raise_for_status()
            payload = r.json()
            if isinstance(payload, dict):        # some RPCs answer a batch with one error object
                raise RuntimeError(str(payload.get("error") or payload)[:160])
            for item in payload:
                idx = item.get("id")
                if isinstance(idx, int) and 0 <= idx < len(out) and not item.get("error"):
                    out[idx] = item.get("result")
        except Exception as e:
            log.debug(f"evm batch on {chain_id} failed: {e}")
    return out


def _hex_int(value) -> int:
    try:
        return int(value, 16) if isinstance(value, str) and value.startswith("0x") else 0
    except ValueError:
        return 0


def _balance_call(token: str, wallet: str) -> dict:
    return {"method": "eth_call",
            "params": [{"to": token, "data": BALANCE_OF + wallet[2:].lower().zfill(64)}, "latest"]}


async def evm_decimals(client, chain_id: str, tokens: list[str]) -> dict[str, int]:
    """decimals() for tokens we have never seen. Callers cache the result."""
    calls = [{"method": "eth_call", "params": [{"to": t, "data": DECIMALS_SEL}, "latest"]} for t in tokens]
    res = await _evm_rpc_batch(client, chain_id, calls)
    out = {}
    for token, value in zip(tokens, res):
        dec = _hex_int(value)
        out[token] = dec if 0 < dec <= 36 else 18      # 18 is the ERC-20 default
    return out


# Chains whose RPC does not implement `alchemy_getTokenBalances`. Populated on
# the first "method not found" and never re-probed — a public endpoint is not
# going to grow the method halfway through the process.
_alchemy_retired: set[str] = set()


def _discover_failed(chain_id: str, reason: str) -> None:
    """Record why discovery did not answer, and return None for the caller.

    The reason has to reach PROVIDER_STATUS. When it only went to log.debug,
    a rate-limited round was indistinguishable from "this RPC cannot do it",
    and the page and the diagnostics tool both said the same useless word:
    unavailable.
    """
    _mark(f"evm_discover:{chain_id}", False, reason)
    log.warning(f"alchemy_getTokenBalances on {chain_id}: {reason}")
    return None


async def alchemy_balances(client, wallet: str, chain_id: str,
                           max_pages: int = 4) -> dict[str, int] | None:
    """Every non-zero ERC-20 balance of one wallet: {token address -> raw amount}.

    `alchemy_getTokenBalances` is answered by Alchemy on the very URL this
    module already posts eth_calls to, on the free plan, in one request per
    100 tokens. It matters because it is the only EVM provider here that
    DISCOVERS: the watchlist scan can merely confirm tokens the dashboard has
    already seen, so a wallet's position in a token the bot never posted is
    invisible without this — which is exactly how an EVM wallet ends up
    showing nothing at all while Solana wallets show cards.

    None means "this endpoint cannot answer" — a plain RPC, or a network
    failure — and the caller falls through to the watchlist scan. An empty
    dict means the wallet genuinely holds no ERC-20 on this chain.
    """
    url = evm_rpc(chain_id)
    if not url or chain_id in _alchemy_retired:
        return None
    out: dict[str, int] = {}
    page_key = None
    for _ in range(max(1, max_pages)):
        params: list = [wallet, "erc20"]
        if page_key:
            params.append({"pageKey": page_key})
        try:
            r = await client.post(url, timeout=25, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "alchemy_getTokenBalances", "params": params})
        except Exception as e:          # transient: try again next round
            return _discover_failed(chain_id, f"{type(e).__name__}: {str(e)[:110]}")
        if getattr(r, "status_code", 200) != 200:
            # 429 is the one that actually happens: Alchemy's free tier is
            # throughput-limited, and a first scan's decimals() batches will
            # sit on that limit. Say so out loud — a silent debug line here is
            # what made this look like "discovery does not work" once already.
            body = ""
            try:
                body = (r.text or "")[:130].replace("\n", " ")
            except Exception:
                pass
            return _discover_failed(chain_id, f"HTTP {r.status_code} {body}".strip())
        try:
            payload = r.json() or {}
        except Exception as e:
            return _discover_failed(chain_id, f"unreadable response: {str(e)[:100]}")
        err = payload.get("error")
        if err:
            message = str((err or {}).get("message") or err)[:140]
            if (err or {}).get("code") == -32601 or "not found" in message.lower() \
                    or "not supported" in message.lower():
                _alchemy_retired.add(chain_id)
                log.info(f"alchemy_getTokenBalances unavailable on {chain_id} "
                         f"({message}) — using the watchlist scan from here on")
                return _discover_failed(chain_id, f"not implemented here: {message}")
            return _discover_failed(chain_id, message)
        result = payload.get("result") or {}
        for row in result.get("tokenBalances") or []:
            address = (row.get("contractAddress") or "").lower()
            raw = _hex_int(row.get("tokenBalance"))
            if address.startswith("0x") and raw > 0:
                out[address] = raw
        page_key = result.get("pageKey")
        if not page_key:
            break
    _mark(f"evm_discover:{chain_id}", True, f"{len(out)} ERC-20 positions")
    return out


async def evm_holdings(client, wallet: str, chain_id: str, watchlist: list[str],
                       decimals: dict[str, int]) -> tuple[list[dict], str]:
    """Positions of one EVM wallet on one chain. Returns (holdings, provider).

    `watchlist` is the fallback universe — the EVM tokens this dashboard
    already knows on this chain. `decimals` is a cache the caller owns; this
    function fills in what it had to look up.
    """
    if chain_id not in _etherscan_retired:
        try:
            rows = await etherscan(client, chain_id, "addresstokenbalance",
                                   {"address": wallet, "page": 1, "offset": 200})
            if rows is not None:
                out = []
                for row in rows:
                    try:
                        divisor = float(row.get("TokenDivisor") or 0) or \
                                  10 ** int(row.get("TokenDecimals") or 18)
                        amt = float(row.get("TokenQuantity") or 0) / divisor
                    except (TypeError, ValueError, ZeroDivisionError):
                        continue
                    addr = (row.get("TokenAddress") or "").lower()
                    if addr and amt > 0:
                        out.append({"address": addr, "chain_id": chain_id, "amount": amt,
                                    "decimals": int(row.get("TokenDecimals") or 18),
                                    "symbol": row.get("TokenSymbol") or ""})
                _mark(f"evm_holdings:{chain_id}", True, f"etherscan · {len(out)} positions")
                return out, "etherscan"
        except EtherscanRefused as e:
            _etherscan_retired.add(chain_id)
            log.info(f"etherscan addresstokenbalance unavailable on {chain_id} ({e}) — "
                     f"using the watchlist scan from here on")
        except Exception as e:
            log.debug(f"etherscan balances on {chain_id}: {e}")

    # Full discovery over the chain's own RPC, when it can do it.
    raw = await alchemy_balances(client, wallet, chain_id)
    if raw is not None:
        missing = [t for t in raw if t not in decimals]
        if missing:
            decimals.update(await evm_decimals(client, chain_id, missing))
        out = [{"address": token, "chain_id": chain_id,
                "amount": amount / (10 ** decimals.get(token, 18)),
                "decimals": decimals.get(token, 18)}
               for token, amount in raw.items()]
        _mark(f"evm_holdings:{chain_id}", True, f"alchemy · {len(out)} positions")
        return out, "alchemy"

    # Watchlist scan: balanceOf over the tokens we already know on this chain.
    tokens = [t for t in watchlist if t.startswith("0x")]
    if not tokens:
        _mark(f"evm_holdings:{chain_id}", True, "watchlist · nothing to scan yet")
        return [], "watchlist"
    if not evm_rpc(chain_id):
        _mark(f"evm_holdings:{chain_id}", False, "no RPC configured")
        return [], "none"
    res = await _evm_rpc_batch(client, chain_id, [_balance_call(t, wallet) for t in tokens])
    hits = [t for t, v in zip(tokens, res) if _hex_int(v) > 0]
    missing = [t for t in hits if t not in decimals]
    if missing:
        decimals.update(await evm_decimals(client, chain_id, missing))
    out = []
    for token, value in zip(tokens, res):
        raw = _hex_int(value)
        if raw <= 0:
            continue
        dec = decimals.get(token, 18)
        out.append({"address": token, "chain_id": chain_id, "amount": raw / (10 ** dec),
                    "decimals": dec})
    _mark(f"evm_holdings:{chain_id}", True, f"watchlist · {len(tokens)} scanned, {len(out)} held")
    return out, "watchlist"


# --------------------------------------------------------------- market data

DEX_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/"
DEX_BATCH = 30
MIN_LIQ_USD = 250          # same floor the peak poller uses


def _best_pair(pairs: list[dict], address: str) -> dict | None:
    """Highest-liquidity pair for this token. Never max-mcap across pools — a
    dust pool with a manipulated price reports absurd caps."""
    best, best_liq = None, -1.0
    for p in pairs:
        if ((p.get("baseToken") or {}).get("address", "")).lower() != address.lower():
            continue
        liq = float(((p.get("liquidity") or {}).get("usd")) or 0)
        if liq > best_liq:
            best, best_liq = p, liq
    return best


def _market_from_pair(address: str, pair: dict) -> dict:
    base = pair.get("baseToken") or {}
    info = pair.get("info") or {}
    try:
        price = float(pair.get("priceUsd") or 0)
    except (TypeError, ValueError):
        price = 0.0
    mc = float(pair.get("marketCap") or pair.get("fdv") or 0)
    liq = float(((pair.get("liquidity") or {}).get("usd")) or 0)
    return {
        "address": address,
        "chain_id": (pair.get("chainId") or "").lower(),
        "name": base.get("name") or "",
        "symbol": base.get("symbol") or "",
        "image": info.get("imageUrl") or "",
        # banner art, used as the card background on the Wallet Groups page.
        # Dexscreener calls it "header"; openGraph is the social preview and is
        # the better-than-nothing fallback when a token never uploaded a banner.
        "banner": info.get("header") or info.get("openGraph") or "",
        "price": price,
        "mc": mc,
        "liq": liq,
        # circulating supply, which is the right denominator for "% of supply
        # held" — and it comes free with the price we already fetched
        "supply": (mc / price) if price > 0 and mc > 0 else 0.0,
        "pair": pair.get("pairAddress") or "",
    }


async def dex_markets(client, addresses: list[str]) -> dict[str, dict]:
    """Price / mcap / supply / logo per token, keyed by the address given.

    Solana addresses go up in batches; EVM addresses go one at a time, because
    Dexscreener's multi-address endpoint silently drops 0x addresses from a
    mixed batch (no error, they just are not in the response).
    """
    sol = [a for a in addresses if not a.startswith("0x")]
    evm = [a for a in addresses if a.startswith("0x")]
    groups = [sol[i:i + DEX_BATCH] for i in range(0, len(sol), DEX_BATCH)] + [[a] for a in evm]
    out: dict[str, dict] = {}
    for group in groups:
        try:
            r = await client.get(DEX_TOKENS + ",".join(group), timeout=15)
            r.raise_for_status()
            pairs = (r.json() or {}).get("pairs") or []
        except Exception as e:
            log.debug(f"dexscreener batch failed: {e}")
            continue
        for address in group:
            pair = _best_pair(pairs, address)
            if pair:
                out[address] = _market_from_pair(address, pair)
        await asyncio.sleep(0.35)          # Dexscreener is rate-limited; stay polite
    _mark("dexscreener", True, f"{len(out)}/{len(addresses)} priced")
    return out


_native_cache: dict[str, tuple[float, float]] = {}


async def native_price(client, chain_id: str) -> float:
    """USD price of the chain's native asset, cached for 5 minutes."""
    hit = _native_cache.get(chain_id)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    token = NATIVE_WRAPPED.get(chain_id)
    if not token:
        return 0.0
    markets = await dex_markets(client, [token])
    price = (markets.get(token) or {}).get("price") or 0.0
    if price:
        _native_cache[chain_id] = (time.time(), price)
    return price


async def token_supply(client, address: str, chain_id: str, decimals: int = 0) -> float:
    """On-chain supply, for the rare token Dexscreener prices but reports no
    market cap for (so mc/price could not give us a denominator)."""
    try:
        if chain_id == "solana":
            res = await sol_rpc(client, "getTokenSupply", [address])
            value = (res or {}).get("value") or {}
            return float(value.get("uiAmountString") or value.get("uiAmount") or 0)
        res = await _evm_rpc_batch(client, chain_id,
                                   [{"method": "eth_call",
                                     "params": [{"to": address, "data": TOTAL_SUPPLY}, "latest"]}])
        return _hex_int(res[0]) / (10 ** (decimals or 18))
    except Exception as e:
        log.debug(f"supply lookup failed for {address}: {e}")
        return 0.0


# --------------------------------------------------------------- cost basis

def fold_trades(trades: list[dict], actual_amount: float) -> dict | None:
    """Moving-average cost basis over a chronological trade list.

    Returns None when the history cannot honestly price the position that is
    open right now. That is deliberate: an unpriced buy folded in as $0 would
    render as an infinite gain, which is worse than showing nothing and
    falling back to the basis we observe ourselves.
    """
    amount = cost = realized = 0.0
    for t in sorted(trades, key=lambda x: x.get("ts") or 0):
        qty = float(t.get("qty") or 0)
        if qty <= 0:
            continue
        usd = t.get("usd")
        if t.get("side") == "buy":
            if usd is None:
                return None                      # cannot price a buy — give up cleanly
            amount += qty
            cost += float(usd)
        else:
            if amount <= 0:
                continue                          # sold something we never saw bought
            sold = min(qty, amount)
            avg = cost / amount
            if usd is not None:
                realized += float(usd) * (sold / qty) - sold * avg
            cost -= sold * avg
            amount -= sold
    if amount <= 0 or cost <= 0:
        return None                               # history says flat; the chain says otherwise
    source = "chain"
    # Transfers in/out, an airdrop, or a truncated page make the reconstructed
    # size disagree with the balance. Keep the average entry and rescale the
    # basis onto the amount actually held, and say the number is partial.
    if actual_amount > 0 and abs(actual_amount - amount) / max(amount, 1e-12) > 0.02:
        cost *= actual_amount / amount
        amount = actual_amount
        source = "partial"
    return {"amount": amount, "cost_usd": cost, "realized_usd": realized,
            "avg_entry": cost / amount, "source": source, "trades": len(trades)}


# -- Solscan ------------------------------------------------------------------
# Solscan serves the same data under /v2.0 (paid) and /playground (any account),
# and reads the key from a bare `token` header — but a rejected key looks the
# same whichever spelling you use. So the route is negotiated once at runtime
# and remembered, exactly as fomo/solscan_api.py does it.

SOLSCAN_HOST = os.getenv("SOLSCAN_HOST", "https://pro-api.solscan.io").rstrip("/")

# Playground first, deliberately. Johan's Solscan key is a FREE one: every
# `/v2.0` path answers 401 for it, and `/playground` serves the same engine to
# any account. A paid key can reach playground too, so trying it first costs a
# paid plan nothing and saves a free plan a doomed request on every new path.
# Pin with SOLSCAN_PREFIXES="v2.0" if the plan is ever upgraded.
SOLSCAN_PREFIXES = os.getenv("SOLSCAN_PREFIXES", "playground,v2.0")
_solscan_route: tuple[str, str] | None = None
_solscan_dead_until = 0.0


async def solscan_get(client, path: str, params: dict):
    global _solscan_route, _solscan_dead_until
    key = os.getenv("SOLSCAN_API_KEY", "").strip()
    if not key or time.time() < _solscan_dead_until:
        return None
    routes = [_solscan_route] if _solscan_route else [
        (prefix, style)
        for prefix in SOLSCAN_PREFIXES.split(",")
        for style in ("token", "bearer")
    ]
    for prefix, style in routes:
        prefix = prefix.strip().strip("/")
        headers = {"token": key} if style == "token" else {"Authorization": f"Bearer {key}"}
        try:
            r = await client.get(f"{SOLSCAN_HOST}/{prefix}/{path}", params=params,
                                 headers=headers, timeout=25)
            if r.status_code in (401, 403, 404):
                continue
            payload = r.json()
        except Exception as e:
            log.debug(f"solscan {path}: {e}")
            continue
        if payload.get("success") is False or payload.get("data") is None:
            continue
        _solscan_route = (prefix, style)
        _mark("solscan", True, f"{prefix} · {style}")
        return payload.get("data")
    if _solscan_route is None:                    # nothing worked — stop trying for a while
        _solscan_dead_until = time.time() + 600
        _mark("solscan", False, "no working route/plan for this key")
    return None


def _solscan_legs(row: dict) -> list[tuple[str, float]]:
    """(token, human amount) for both sides of a swap row, aggregators included."""
    legs: list[tuple[str, float]] = []

    def take(router: dict):
        for n in ("1", "2"):
            token = router.get(f"token{n}")
            amount = router.get(f"amount{n}")
            dec = router.get(f"token{n}_decimals", router.get(f"decimals{n}"))
            try:
                if token and amount is not None:
                    legs.append((token, abs(float(amount)) / (10 ** int(dec or 0))))
            except (TypeError, ValueError):
                pass

    router = row.get("routers") or row.get("router") or {}
    if isinstance(router, dict):
        take(router)
        for child in router.get("child_routers") or []:
            if isinstance(child, dict):
                take(child)
    return legs


def _leg_usd(token: str, qty: float, sol_price: float) -> float | None:
    if token in STABLE_MINTS:
        return qty
    if token == WSOL and sol_price > 0:
        return qty * sol_price
    return None


async def sol_cost_basis(client, wallet: str, mint: str, actual_amount: float) -> dict | None:
    """Average entry for one (wallet, mint) pair from its swap history.

    One request, and only ever for a pair that already qualifies for a card.
    """
    params = {"address": wallet, "token": mint, "page": 1, "page_size": 100,
              "sort_by": "block_time", "sort_order": "asc"}
    rows = await solscan_get(client, "account/defi/activities",
                             {**params, "activity_type[]": ["ACTIVITY_TOKEN_SWAP",
                                                            "ACTIVITY_AGG_TOKEN_SWAP"]})
    if rows is None:
        rows = await solscan_get(client, "account/defi/activities", params)
    if not rows:
        return None
    sol_price = await native_price(client, "solana")
    now, approx = time.time(), False
    trades = []
    for row in rows:
        legs = _solscan_legs(row)
        mine = [l for l in legs if l[0] == mint]
        other = [l for l in legs if l[0] != mint]
        if not mine or not other:
            continue
        qty = max(q for _, q in mine)
        counter_token, counter_qty = max(other, key=lambda l: l[1])
        ts = float(row.get("block_time") or row.get("time") or 0)
        # Solscan's own USD value when it is there; otherwise price the other leg.
        usd = None
        try:
            value = float(row.get("value") or 0)
            usd = value if value > 0 else None
        except (TypeError, ValueError):
            usd = None
        if usd is None:
            usd = _leg_usd(counter_token, counter_qty, sol_price)
            # A SOL-priced leg uses today's SOL price. Fine for a position
            # opened this week, a guess for one opened last quarter — say so.
            if usd is not None and counter_token == WSOL and now - ts > 7 * 86400:
                approx = True
        # token1 is the input side: the mint leaving the wallet is a sell.
        first = legs[0][0] if legs else ""
        side = "sell" if first == mint else "buy"
        trades.append({"ts": ts, "side": side, "qty": qty, "usd": usd})
    basis = fold_trades(trades, actual_amount)
    if basis and approx and basis["source"] == "chain":
        basis["source"] = "partial"
    return basis


# -- EVM ----------------------------------------------------------------------
# Off unless WG_EVM_BASIS=1. Etherscan gives the token flow for free
# (`tokentx`), but the USD side has to be reconstructed from the native value
# moving in the same transaction, which is two more calls per wallet and only
# as good as today's native price. Solana is the accurate path; EVM positions
# fall back to observed cost basis unless this is switched on deliberately.

_evm_tx_cache: dict[str, tuple[float, dict]] = {}


async def _evm_native_by_hash(client, wallet: str, chain_id: str) -> dict[str, float]:
    key = f"{chain_id}:{wallet}"
    hit = _evm_tx_cache.get(key)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    by_hash: dict[str, float] = {}
    for action in ("txlist", "txlistinternal"):
        try:
            rows = await etherscan(client, chain_id, action,
                                   {"address": wallet, "sort": "desc", "page": 1, "offset": 1000})
        except Exception as e:
            log.debug(f"etherscan {action} for {wallet} on {chain_id}: {e}")
            continue
        for row in rows or []:
            try:
                value = int(row.get("value") or 0) / 1e18
            except (TypeError, ValueError):
                continue
            if value > 0:
                h = (row.get("hash") or "").lower()
                by_hash[h] = by_hash.get(h, 0.0) + value
    _evm_tx_cache[key] = (time.time(), by_hash)
    return by_hash


async def evm_cost_basis(client, wallet: str, token: str, chain_id: str,
                         actual_amount: float) -> dict | None:
    if os.getenv("WG_EVM_BASIS", "") != "1":
        return None
    try:
        rows = await etherscan(client, chain_id, "tokentx",
                               {"address": wallet, "contractaddress": token,
                                "sort": "asc", "page": 1, "offset": 300})
    except Exception as e:
        log.debug(f"etherscan tokentx for {wallet}/{token}: {e}")
        return None
    if not rows:
        return None
    native = await _evm_native_by_hash(client, wallet, chain_id)
    price = await native_price(client, chain_id)
    if not price:
        return None
    wallet_l = wallet.lower()
    trades = []
    for row in rows:
        try:
            dec = int(row.get("tokenDecimal") or 18)
            qty = int(row.get("value") or 0) / (10 ** dec)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        incoming = (row.get("to") or "").lower() == wallet_l
        h = (row.get("hash") or "").lower()
        spent = native.get(h)
        trades.append({"ts": float(row.get("timeStamp") or 0),
                       "side": "buy" if incoming else "sell",
                       "qty": qty,
                       "usd": spent * price if spent else None})
    basis = fold_trades(trades, actual_amount)
    if basis:
        basis["source"] = "partial"     # today's native price applied to old trades
    return basis


async def cost_basis(client, wallet: str, token: str, chain_id: str,
                     actual_amount: float) -> dict | None:
    """On-chain average entry for one position, or None to use observed basis."""
    try:
        if chain_id == "solana":
            return await sol_cost_basis(client, wallet, token, actual_amount)
        return await evm_cost_basis(client, wallet, token, chain_id, actual_amount)
    except Exception as e:
        log.debug(f"cost basis {wallet}/{token}: {e}")
        return None
