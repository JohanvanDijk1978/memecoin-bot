"""
multiwallet_sources.py
──────────────────────
Chain-side detection for the multi-wallet watcher: turn "a wallet I track
bought something" into a normalised record, as fast as the providers allow.

Transport, and why
──────────────────
Solana: ONE websocket, one `logsSubscribe {mentions:[wallet]}` per tracked
wallet. Notifications arrive in well under a second, cost no quota beyond the
`getTransaction` we make on a hit, and adding a wallet is one more subscribe on
the same connection — no reconnect, no restart. Polling
`getSignaturesForAddress` for 40 wallets every 20s is ~5M calls a month to be
20 seconds late; this is the same information for a rounding error.

EVM: ONE `eth_subscribe("logs")` per chain, filtered server-side on the ERC-20
Transfer topic with every tracked wallet in the `to` position. One subscription
covers the whole wallet list, so cost is flat in the number of wallets.

Both are supervised: a dropped connection reconnects with backoff, and a
reconcile sweep (`getSignaturesForAddress` per wallet on Solana, `eth_getLogs`
from the last seen block on EVM) runs on a slow timer so a gap during a
reconnect is filled rather than lost. A chain with no websocket URL configured
degrades to that sweep alone at a faster interval — reduced, not dead.

What counts as a buy
────────────────────
Tokens went UP for the wallet, and value went OUT of the same wallet in the
same transaction: SOL/ETH/BNB, wrapped native, or a stablecoin. An airdrop, a
transfer in from another wallet, or an LP/router movement therefore never
counts, which is what keeps a shared list of wallets from converging on junk.

The parsers are pure functions of the transaction JSON (`parse_solana_buys`,
`parse_evm_buys`) so they can be tested against saved transactions with no
network at all — which is the only way this code can be checked from a
sandbox that cannot reach an RPC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

import aiohttp
from dotenv import load_dotenv

from src import multiwallet_store as store

logger = logging.getLogger(__name__)

# The RPC/WSS keys live in fomo/.env on Johan's laptop, and the bot's own .env
# on the VPS holds almost none of them. Read both, root .env first (already
# loaded by main.py), then fomo/ and dashboard/ WITHOUT overriding — same
# precedence dashboard/wallets.py::load_env_files uses, so one deployed file
# serves both services.
load_dotenv()
for _extra in ("fomo/.env", "dashboard/.env"):
    if os.path.exists(_extra):
        load_dotenv(_extra, override=False)


# ── tuning ────────────────────────────────────────────────────────────────
RECONCILE_SEC = float(os.getenv("MULTIWALLET_RECONCILE_SEC", "300"))   # gap-filling sweep
POLL_SEC = float(os.getenv("MULTIWALLET_POLL_SEC", "30"))              # sweep when no WSS
WALLET_SYNC_SEC = float(os.getenv("MULTIWALLET_SYNC_SEC", "20"))       # notice /add and /remove
RPC_TIMEOUT = float(os.getenv("MULTIWALLET_RPC_TIMEOUT", "20"))
MAX_BACKOFF = 120.0
EVM_LOG_SPAN = int(os.getenv("MULTIWALLET_EVM_LOG_SPAN", "1800"))      # blocks per sweep

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Chains, in Dexscreener's chainId spelling — the same strings src/utils.py
# already maps to explorers and trading links.
DEFAULT_EVM_CHAINS = "ethereum,base,bsc,robinhood"

# env prefixes per chain, in the spelling fomo/.env already uses
_EVM_ENV = {
    "ethereum":  ("ETH_RPC", "ETH_WSS"),
    "base":      ("BASE_RPC", "BASE_WSS"),
    "bsc":       ("BSC_RPC", "BSC_WSS"),
    "robinhood": ("ROBINHOOD_RPC", "ROBINHOOD_WSS"),
}

PUBLIC_SOL_RPC = "https://api.mainnet-beta.solana.com"

# Solana quote assets: receiving these is not "a buy"; spending them is.
WSOL = "So11111111111111111111111111111111111111112"
SOL_STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}
SOL_QUOTE_MINTS = {WSOL, *SOL_STABLES}

# EVM quote assets per chain: wrapped native and the stables worth paying with.
# (symbol, decimals, is_stable)
_EVM_QUOTES: dict[str, dict[str, tuple[str, int, bool]]] = {
    "ethereum": {
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ("WETH", 18, False),
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6, True),
        "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6, True),
        "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 18, True),
    },
    "base": {
        "0x4200000000000000000000000000000000000006": ("WETH", 18, False),
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC", 6, True),
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": ("USDbC", 6, True),
    },
    "bsc": {
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": ("WBNB", 18, False),
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": ("USDC", 18, True),
        "0x55d398326f99059ff775485246999027b3197955": ("USDT", 18, True),
    },
    "robinhood": {},   # wrapped-native address unknown; native-value buys still detected
}

NATIVE_SYMBOL = {"ethereum": "ETH", "base": "ETH", "bsc": "BNB",
                 "robinhood": "ETH", "solana": "SOL"}


# ── endpoint resolution ───────────────────────────────────────────────────
def _clean_urls(*values: str) -> list[str]:
    out, seen = [], set()
    for value in values:
        for url in str(value or "").split(","):
            url = url.strip()
            if url and url not in seen:
                seen.add(url)
                out.append(url)
    return out


def solana_rpcs() -> list[str]:
    urls = _clean_urls(os.getenv("SOLANA_RPC", ""), os.getenv("SOLANA_RPC_FALLBACKS", ""))
    return urls + [PUBLIC_SOL_RPC] if PUBLIC_SOL_RPC not in urls else urls


def _to_wss(url: str) -> str:
    """Alchemy, Helius and Chainstack all serve the websocket on the same host
    and path as the HTTP endpoint. Deriving it means one env var, not two."""
    if url.startswith("wss://") or url.startswith("ws://"):
        return url
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return ""


def solana_wss() -> str:
    explicit = os.getenv("SOLANA_WSS", "").strip()
    if explicit:
        return explicit
    for url in solana_rpcs():
        if url == PUBLIC_SOL_RPC:
            continue          # the public endpoint's socket is not worth trusting
        derived = _to_wss(url)
        if derived:
            return derived
    return ""


def evm_chains() -> list[str]:
    want = [c.strip().lower() for c in
            os.getenv("MULTIWALLET_EVM_CHAINS", DEFAULT_EVM_CHAINS).split(",") if c.strip()]
    return [c for c in want if evm_rpc(c)]


def evm_rpc(chain: str) -> str:
    rpc_key, _ = _EVM_ENV.get(chain, ("", ""))
    for key in (f"MULTIWALLET_RPC_{chain.upper()}", rpc_key,
                f"EVM_RPC_{chain.upper()}", f"{chain.upper()}_RPC"):
        if key and os.getenv(key, "").strip():
            return os.getenv(key, "").strip()
    return ""


def evm_wss(chain: str) -> str:
    _, wss_key = _EVM_ENV.get(chain, ("", ""))
    for key in (f"MULTIWALLET_WSS_{chain.upper()}", wss_key, f"{chain.upper()}_WSS"):
        if key and os.getenv(key, "").strip():
            return os.getenv(key, "").strip()
    return _to_wss(evm_rpc(chain))


def endpoint_report() -> list[dict]:
    """What each chain resolved to — endpoint host only, never the key. Feeds
    tools/diag_multiwallet.py and the /list footer, so a silent chain is
    explainable without reading the .env on the box."""
    from urllib.parse import urlsplit

    def host(url: str) -> str:
        if not url:
            return ""
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.hostname}"

    rows = [{"chain": "solana", "rpc": host(solana_rpcs()[0] if solana_rpcs() else ""),
             "wss": host(solana_wss()), "live": bool(solana_wss())}]
    for chain in [c.strip().lower() for c in
                  os.getenv("MULTIWALLET_EVM_CHAINS", DEFAULT_EVM_CHAINS).split(",") if c.strip()]:
        rows.append({"chain": chain, "rpc": host(evm_rpc(chain)),
                     "wss": host(evm_wss(chain)), "live": bool(evm_wss(chain))})
    return rows


# ── JSON-RPC over HTTP ────────────────────────────────────────────────────
class Rpc:
    """Minimal JSON-RPC client with ordered fallbacks.

    Fallbacks matter more than retries here: Helius rate-limits per key, and
    the point of a second URL is to answer the same question from somewhere
    else rather than to ask the throttled endpoint again.
    """

    def __init__(self, session: aiohttp.ClientSession, urls: list[str], label: str = ""):
        self.session = session
        self.urls = [u for u in urls if u]
        self.label = label
        self._id = 0

    async def call(self, method: str, params: list, timeout: float = RPC_TIMEOUT) -> Any:
        if not self.urls:
            return None
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        last_error = ""
        for url in self.urls:
            try:
                async with self.session.post(
                        url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status != 200:
                        last_error = f"HTTP {resp.status}"
                        continue
                    data = await resp.json()
                if isinstance(data, dict) and data.get("error"):
                    # An application-level error is an answer, not an outage:
                    # asking the next endpoint the same malformed question just
                    # wastes another request.
                    logger.debug("multiwallet rpc %s %s: %s", self.label, method, data["error"])
                    return None
                return (data or {}).get("result")
            except Exception as e:
                last_error = repr(e)
                continue
        if last_error:
            logger.debug("multiwallet rpc %s %s failed: %s", self.label, method, last_error)
        return None


# ── Solana parsing ────────────────────────────────────────────────────────
def _sol_account_keys(tx: dict) -> list[str]:
    message = ((tx.get("transaction") or {}).get("message") or {})
    keys = message.get("accountKeys") or []
    out = []
    for key in keys:
        out.append(key.get("pubkey") if isinstance(key, dict) else key)
    # versioned transactions put looked-up accounts in meta, after the static
    # keys — balance indexes are into the combined list
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    out += list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    return out


def _token_deltas(meta: dict, wallet: str) -> dict[str, float]:
    """Net change in the wallet's SPL balances, by mint."""
    deltas: dict[str, float] = {}
    for sign, field in ((-1.0, "preTokenBalances"), (1.0, "postTokenBalances")):
        for entry in (meta.get(field) or []):
            if entry.get("owner") != wallet:
                continue
            mint = entry.get("mint") or ""
            amount = (entry.get("uiTokenAmount") or {}).get("uiAmountString")
            try:
                value = float(amount or 0)
            except (TypeError, ValueError):
                value = 0.0
            deltas[mint] = deltas.get(mint, 0.0) + sign * value
    return deltas


def parse_solana_buys(tx: dict, wallet: str) -> list[dict]:
    """Buys made by `wallet` in this transaction. Pure — no network.

    Reads balances rather than instructions on purpose: Jupiter, Pump, PumpSwap,
    Raydium, Meteora and every aggregator that has not shipped yet all move the
    same balances, and a route through three of them still nets out to "these
    tokens arrived, that much SOL left".
    """
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []                      # a failed swap is not a buy
    keys = _sol_account_keys(tx)
    deltas = _token_deltas(meta, wallet)

    # what left the wallet, in USD-able terms
    sol_spent = 0.0
    if wallet in keys:
        index = keys.index(wallet)
        pre = (meta.get("preBalances") or [])
        post = (meta.get("postBalances") or [])
        if index < len(pre) and index < len(post):
            lamports = pre[index] - post[index]
            if index == 0:             # fee payer also paid the fee
                lamports -= int(meta.get("fee") or 0)
            sol_spent = max(lamports, 0) / 1e9
    wsol_spent = max(-deltas.get(WSOL, 0.0), 0.0)
    stable_spent, stable_symbol = 0.0, ""
    for mint, symbol in SOL_STABLES.items():
        spent = max(-deltas.get(mint, 0.0), 0.0)
        if spent > stable_spent:
            stable_spent, stable_symbol = spent, symbol

    native_spent = sol_spent + wsol_spent
    if native_spent < 0.000_1 and stable_spent <= 0:
        return []                      # nothing left the wallet: airdrop or transfer in

    gained = [(mint, amount) for mint, amount in deltas.items()
              if amount > 0 and mint not in SOL_QUOTE_MINTS]
    if not gained:
        return []
    gained.sort(key=lambda pair: pair[1], reverse=True)

    if stable_spent > 0 and stable_spent >= native_spent * 1e6:   # stable-quoted swap
        quote_sym, quote_amt, stable = stable_symbol, stable_spent, True
    else:
        quote_sym, quote_amt, stable = "SOL", native_spent, False

    signature = ((tx.get("transaction") or {}).get("signatures") or [""])[0]
    ts = float(tx.get("blockTime") or 0) or time.time()
    mint, amount = gained[0]           # a swap produces one token; extras are dust/fees
    return [{
        "chain": "solana", "tx": signature, "wallet": wallet, "token": mint,
        "ts": ts, "amount": amount, "quote_sym": quote_sym, "quote_amt": quote_amt,
        "quote_is_stable": stable,
    }]


# ── EVM parsing ───────────────────────────────────────────────────────────
def _topic_address(topic: str) -> str:
    return "0x" + (topic or "")[-40:].lower()


def pad_address(address: str) -> str:
    return "0x" + "0" * 24 + address.lower().replace("0x", "")


def _hex_int(value: Any) -> int:
    try:
        if isinstance(value, int):
            return value
        return int(str(value), 16)
    except (TypeError, ValueError):
        return 0


def parse_evm_buys(receipt: dict, tx: dict, wallets: set[str], chain: str,
                   decimals: dict[str, int]) -> list[dict]:
    """Buys inside one EVM transaction, for any of `wallets`. Pure — no network.

    `decimals` maps token address → decimals for tokens already known; a token
    missing from it is reported with amount 0 and the caller fills it in, so a
    metadata lookup can never sit in front of detection.
    """
    if _hex_int(receipt.get("status", "0x1")) == 0:
        return []
    quotes = _EVM_QUOTES.get(chain, {})
    tx_from = (tx.get("from") or "").lower()
    tx_value = _hex_int(tx.get("value") or "0x0") / 1e18

    gained: dict[str, dict[str, float]] = {}      # wallet -> token -> raw in
    lost: dict[str, dict[str, float]] = {}        # wallet -> token -> raw out
    for log in (receipt.get("logs") or []):
        topics = log.get("topics") or []
        if len(topics) != 3 or (topics[0] or "").lower() != TRANSFER_TOPIC:
            continue
        token = (log.get("address") or "").lower()
        sender, receiver = _topic_address(topics[1]), _topic_address(topics[2])
        raw = _hex_int(log.get("data") or "0x0")
        if receiver in wallets:
            gained.setdefault(receiver, {})[token] = \
                gained.setdefault(receiver, {}).get(token, 0) + raw
        if sender in wallets:
            lost.setdefault(sender, {})[token] = \
                lost.setdefault(sender, {}).get(token, 0) + raw

    out: list[dict] = []
    for wallet, tokens in gained.items():
        spent_native = tx_value if tx_from == wallet else 0.0
        quote_sym = NATIVE_SYMBOL.get(chain, "ETH") if spent_native > 0 else ""
        quote_amt, quote_stable = spent_native, False
        for token, raw in (lost.get(wallet) or {}).items():
            quote = quotes.get(token)
            if not quote:
                continue
            symbol, quote_dec, is_stable = quote
            amount = raw / (10 ** quote_dec)
            if amount > quote_amt or (is_stable and not quote_stable):
                quote_sym, quote_amt, quote_stable = symbol, amount, is_stable
        if quote_amt <= 0:
            continue                   # airdrop, claim, or a transfer between wallets

        best_token, best_raw = "", 0.0
        for token, raw in tokens.items():
            if token in quotes:
                continue               # receiving WETH back is change, not the buy
            net = raw - (lost.get(wallet) or {}).get(token, 0)
            if net > best_raw:
                best_token, best_raw = token, net
        if not best_token:
            continue

        dec = decimals.get(best_token, -1)
        amount = best_raw / (10 ** dec) if dec >= 0 else 0.0
        out.append({
            "chain": chain, "tx": (receipt.get("transactionHash") or tx.get("hash") or ""),
            "wallet": wallet, "token": best_token, "ts": 0.0, "amount": amount,
            "raw_amount": best_raw, "quote_sym": quote_sym, "quote_amt": quote_amt,
            "quote_is_stable": quote_stable,
            "block": _hex_int(receipt.get("blockNumber") or "0x0"),
        })
    return out


# ── ERC-20 metadata, straight from the contract ───────────────────────────
# Dexscreener only knows a token once it has an indexed pool, and it does not
# index every chain at all — a Robinhood-chain coin has no entry there, which
# is how an alert ends up reading "? · Market cap: —". The contract always
# answers, and totalSupply is what turns a per-transaction price into a market
# cap, so this is the floor under the metadata rather than a nicety.
ERC20_SYMBOL = "0x95d89b41"
ERC20_NAME = "0x06fdde03"
ERC20_DECIMALS = "0x313ce567"
ERC20_TOTAL_SUPPLY = "0x18160ddd"


def decode_abi_string(result: Any) -> str:
    """A `symbol()`/`name()` return value, ABI string or the bare bytes32 that
    a handful of older tokens still answer with."""
    if not isinstance(result, str) or not result.startswith("0x"):
        return ""
    body = result[2:]
    try:
        raw = bytes.fromhex(body if len(body) % 2 == 0 else "0" + body)
    except ValueError:
        return ""
    if len(raw) >= 64:
        offset = int.from_bytes(raw[:32], "big")
        if 0 < offset <= len(raw) - 32:
            length = int.from_bytes(raw[offset:offset + 32], "big")
            if 0 < length <= len(raw) - offset - 32:
                raw = raw[offset + 32:offset + 32 + length]
    text = raw.rstrip(b"\x00").decode("utf-8", "ignore")
    return "".join(ch for ch in text if ch.isprintable()).strip()


async def evm_token_meta(session: aiohttp.ClientSession, chain: str, token: str) -> dict:
    """symbol, name, decimals and total supply for one ERC-20. Never raises.

    Returns only the keys it actually established, because `store.put_token`
    writes exactly the fields it is given — an empty answer must not blank a
    row Dexscreener has already filled in.
    """
    url = evm_rpc(chain)
    if not url:
        return {}
    rpc = Rpc(session, [url], chain)

    async def call(selector: str) -> Any:
        return await rpc.call("eth_call", [{"to": token, "data": selector}, "latest"])

    out: dict[str, Any] = {}
    symbol = decode_abi_string(await call(ERC20_SYMBOL))
    name = decode_abi_string(await call(ERC20_NAME))
    if symbol:
        out["symbol"] = symbol[:24]
    if name:
        out["name"] = name[:64]

    raw_decimals = await call(ERC20_DECIMALS)
    decimals = _hex_int(raw_decimals) if raw_decimals and raw_decimals != "0x" else -1
    if not 0 <= decimals <= 36:
        decimals = -1
    if decimals >= 0:
        out["decimals"] = decimals

    raw_supply = await call(ERC20_TOTAL_SUPPLY)
    supply = _hex_int(raw_supply) if raw_supply and raw_supply != "0x" else 0
    if supply > 0 and decimals >= 0:
        out["supply"] = supply / (10 ** decimals)
    return out


# ── watchers ──────────────────────────────────────────────────────────────
OnBuy = Callable[[dict], Awaitable[None]]


class SolanaWatcher:
    """One websocket, one logsSubscribe per tracked wallet, plus a slow sweep."""

    name = "solana"

    def __init__(self, session: aiohttp.ClientSession, on_buy: OnBuy):
        self.session = session
        self.on_buy = on_buy
        self.rpc = Rpc(session, solana_rpcs(), "solana")
        self._subs: dict[str, int] = {}        # wallet -> subscription id
        self._pending: dict[int, str] = {}     # request id -> wallet
        self._req = 0
        self._recent: dict[str, float] = {}    # signature -> seen at
        self.connected = False

    # -- shared ------------------------------------------------------------
    def _wallets(self) -> list[str]:
        return [w["address"] for w in store.list_wallets(kind="sol")]

    def _seen_recently(self, signature: str) -> bool:
        now = time.time()
        if now - self._recent.get(signature, 0) < 600:
            return True
        self._recent[signature] = now
        if len(self._recent) > 4000:
            cutoff = now - 600
            self._recent = {k: v for k, v in self._recent.items() if v > cutoff}
        return False

    async def _handle_signature(self, signature: str, wallets: Optional[list[str]] = None) -> None:
        if not signature or self._seen_recently(signature):
            return
        if store.known_tx("solana", signature):
            return
        tx = await self.rpc.call("getTransaction", [signature, {
            "encoding": "jsonParsed", "commitment": "confirmed",
            "maxSupportedTransactionVersion": 0}])
        if not tx:
            return
        tracked = wallets if wallets is not None else self._wallets()
        keys = set(_sol_account_keys(tx))
        for wallet in tracked:
            if wallet not in keys:
                continue
            for buy in parse_solana_buys(tx, wallet):
                await self.on_buy(buy)

    # -- live --------------------------------------------------------------
    async def _sync_subscriptions(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        wallets = set(self._wallets())
        for wallet in wallets - set(self._subs) - set(self._pending.values()):
            self._req += 1
            self._pending[self._req] = wallet
            await ws.send_json({"jsonrpc": "2.0", "id": self._req, "method": "logsSubscribe",
                                "params": [{"mentions": [wallet]},
                                           {"commitment": "confirmed"}]})
        for wallet in set(self._subs) - wallets:
            sub_id = self._subs.pop(wallet)
            self._req += 1
            await ws.send_json({"jsonrpc": "2.0", "id": self._req,
                                "method": "logsUnsubscribe", "params": [sub_id]})

    async def _sync_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not ws.closed:
            await asyncio.sleep(WALLET_SYNC_SEC)
            try:
                await self._sync_subscriptions(ws)
            except Exception as e:
                logger.debug("multiwallet solana: sync failed (%r)", e)
                return

    async def _session_once(self, url: str) -> None:
        self._subs, self._pending = {}, {}
        async with self.session.ws_connect(url, heartbeat=30, max_msg_size=0,
                                           timeout=aiohttp.ClientTimeout(total=30)) as ws:
            await self._sync_subscriptions(ws)
            self.connected = True
            logger.info("multiwallet solana: websocket up, %d wallets", len(self._pending))
            syncer = asyncio.create_task(self._sync_loop(ws))
            try:
                async for message in ws:
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        continue
                    try:
                        data = json.loads(message.data)
                    except ValueError:
                        continue
                    if "id" in data and "result" in data:
                        wallet = self._pending.pop(int(data["id"]), "")
                        if wallet and isinstance(data["result"], int):
                            self._subs[wallet] = data["result"]
                        continue
                    if data.get("method") != "logsNotification":
                        continue
                    value = ((data.get("params") or {}).get("result") or {}).get("value") or {}
                    if value.get("err"):
                        continue
                    asyncio.create_task(self._guarded(value.get("signature") or ""))
            finally:
                self.connected = False
                syncer.cancel()

    async def _guarded(self, signature: str) -> None:
        try:
            await self._handle_signature(signature)
        except Exception as e:
            logger.warning("multiwallet solana: %s failed (%r)", signature[:12], e)

    async def run_live(self) -> None:
        url = solana_wss()
        if not url:
            logger.info("multiwallet solana: no SOLANA_WSS/SOLANA_RPC — sweep only")
            return
        backoff = 2.0
        while True:
            try:
                await self._session_once(url)
                backoff = 2.0                     # a clean close is not a failure
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("multiwallet solana: websocket dropped (%r), retry in %.0fs",
                               e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    # -- sweep -------------------------------------------------------------
    async def sweep(self) -> int:
        """Catch anything the socket missed. Cheap: one call per wallet, and
        `until` stops it at the last signature already handled."""
        found = 0
        for wallet in self._wallets():
            cursor_key = f"sol:{wallet}"
            params: dict[str, Any] = {"limit": 25, "commitment": "confirmed"}
            until = store.get_cursor(cursor_key)
            if until:
                params["until"] = until
            rows = await self.rpc.call("getSignaturesForAddress", [wallet, params])
            if not rows:
                continue
            newest = rows[0].get("signature") or ""
            for row in reversed(rows):
                if row.get("err"):
                    continue
                signature = row.get("signature") or ""
                if not signature or store.known_tx("solana", signature):
                    continue
                before = store.buy_count()
                await self._handle_signature(signature, [wallet])
                found += store.buy_count() - before
            if newest:
                store.set_cursor(cursor_key, newest)
        return found


class EvmWatcher:
    """One logs subscription per chain, filtered on the tracked wallets."""

    def __init__(self, session: aiohttp.ClientSession, chain: str, on_buy: OnBuy):
        self.session = session
        self.chain = chain
        self.name = chain
        self.on_buy = on_buy
        self.rpc = Rpc(session, [evm_rpc(chain)], chain)
        self._decimals: dict[str, int] = {}
        self._recent: dict[str, float] = {}
        self._sub_wallets: frozenset[str] = frozenset()
        self.connected = False

    def _wallets(self) -> set[str]:
        return {w["key"] for w in store.list_wallets(kind="evm")}

    async def _decimals_for(self, token: str) -> int:
        if token in self._decimals:
            return self._decimals[token]
        cached = store.get_token(self.chain, token)
        if cached and int(cached.get("decimals", -1)) >= 0:
            self._decimals[token] = int(cached["decimals"])
            return self._decimals[token]
        result = await self.rpc.call("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
        dec = _hex_int(result) if result and result != "0x" else 18
        if not 0 <= dec <= 36:
            dec = 18
        self._decimals[token] = dec
        store.put_token(self.chain, token, {"decimals": dec})
        return dec

    async def _block_time(self, block_number: int) -> float:
        if not block_number:
            return time.time()
        block = await self.rpc.call("eth_getBlockByNumber", [hex(block_number), False])
        stamp = _hex_int((block or {}).get("timestamp") or 0)
        return float(stamp) if stamp else time.time()

    async def _handle_tx(self, tx_hash: str) -> None:
        if not tx_hash:
            return
        now = time.time()
        if now - self._recent.get(tx_hash, 0) < 600:
            return
        self._recent[tx_hash] = now
        if len(self._recent) > 4000:
            self._recent = {k: v for k, v in self._recent.items() if v > now - 600}
        if store.known_tx(self.chain, tx_hash):
            return

        receipt = await self.rpc.call("eth_getTransactionReceipt", [tx_hash])
        if not receipt:
            return
        tx = await self.rpc.call("eth_getTransactionByHash", [tx_hash]) or {}
        wallets = self._wallets()
        # First pass with what we know, to find out which tokens are involved;
        # decimals are then fetched only for those, never for the whole log set.
        candidates = parse_evm_buys(receipt, tx, wallets, self.chain, self._decimals)
        if not candidates:
            return
        for candidate in candidates:
            if candidate["amount"] <= 0:
                dec = await self._decimals_for(candidate["token"])
                candidate["amount"] = float(candidate.get("raw_amount") or 0) / (10 ** dec)
            candidate["ts"] = await self._block_time(candidate.get("block") or 0)
            await self.on_buy(candidate)

    async def _session_once(self, url: str) -> None:
        wallets = self._wallets()
        if not wallets:
            await asyncio.sleep(WALLET_SYNC_SEC)
            return
        topics = [TRANSFER_TOPIC, None, [pad_address(w) for w in sorted(wallets)]]
        async with self.session.ws_connect(url, heartbeat=30, max_msg_size=0,
                                           timeout=aiohttp.ClientTimeout(total=30)) as ws:
            await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                                "params": ["logs", {"topics": topics}]})
            self._sub_wallets = frozenset(wallets)
            self.connected = True
            logger.info("multiwallet %s: websocket up, %d wallets in one subscription",
                        self.chain, len(wallets))
            try:
                while True:
                    try:
                        message = await ws.receive(timeout=WALLET_SYNC_SEC)
                    except asyncio.TimeoutError:
                        # idle tick: a changed wallet set means a new filter, so
                        # drop the connection and let run_live resubscribe
                        if self._wallets() != set(self._sub_wallets):
                            logger.info("multiwallet %s: wallet list changed, resubscribing",
                                        self.chain)
                            break
                        continue
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR,
                                            aiohttp.WSMsgType.CLOSING):
                            break
                        continue
                    try:
                        data = json.loads(message.data)
                    except ValueError:
                        continue
                    if data.get("method") != "eth_subscription":
                        continue
                    log = ((data.get("params") or {}).get("result") or {})
                    asyncio.create_task(self._guarded(log.get("transactionHash") or ""))
            finally:
                self.connected = False

    async def _guarded(self, tx_hash: str) -> None:
        try:
            await self._handle_tx(tx_hash)
        except Exception as e:
            logger.warning("multiwallet %s: %s failed (%r)", self.chain, tx_hash[:12], e)

    async def run_live(self) -> None:
        url = evm_wss(self.chain)
        if not url:
            logger.info("multiwallet %s: no websocket URL — sweep only", self.chain)
            return
        backoff = 2.0
        while True:
            try:
                await self._session_once(url)
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("multiwallet %s: websocket dropped (%r), retry in %.0fs",
                               self.chain, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def sweep(self) -> int:
        """eth_getLogs from the last block we handled. One call for every
        wallet on the chain, so this stays affordable as the list grows."""
        wallets = self._wallets()
        if not wallets:
            return 0
        latest = _hex_int(await self.rpc.call("eth_blockNumber", []) or "0x0")
        if not latest:
            return 0
        cursor_key = f"evm:{self.chain}"
        start = int(store.get_cursor(cursor_key) or 0) or max(latest - 200, 0)
        start = max(start, latest - EVM_LOG_SPAN)      # never ask for an unbounded range
        logs = await self.rpc.call("eth_getLogs", [{
            "fromBlock": hex(start + 1), "toBlock": hex(latest),
            "topics": [TRANSFER_TOPIC, None, [pad_address(w) for w in sorted(wallets)]]}])
        if logs is None:
            return 0                     # provider refused the range; cursor stays put
        found = 0
        for tx_hash in dict.fromkeys(log.get("transactionHash") or "" for log in logs):
            before = store.buy_count()
            await self._handle_tx(tx_hash)
            found += store.buy_count() - before
        store.set_cursor(cursor_key, str(latest))
        return found
