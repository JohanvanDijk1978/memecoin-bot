"""
ca_probe.py
───────────
Tells a token contract from a wallet, on-chain, before we ping it.

A 0x-shaped string in an alpha group is not necessarily a token. It is just
as often a wallet, a deployer, a pair address or a contract that has nothing
to do with trading — and Dexscreener answers all of those identically
("pairs": null), which is the same answer it gives for a real token that has
no pool yet. The ping then goes out bare and we cannot tell which case it was.

This module asks the chains directly:

  * `eth_getCode` empty on every chain we can reach  →  wallet (an EOA)
  * code starting `0xef0100`                          →  wallet (EIP-7702
    delegated EOA: the 3-byte prefix is the delegation indicator, the 20
    bytes after it are the implementation it points at)
  * code that answers symbol()/decimals()/totalSupply() →  token, and we
    learn its ticker, name and supply without any indexer
  * code that answers none of those                  →  some other contract
  * nothing answered at all                          →  unknown (RPC down);
    never cached as a verdict

Endpoints come from `multiwallet_sources.evm_rpc()` — the same fomo/.env keys
the multi-wallet watcher already resolves — with a public fallback per chain so
a box without those keys still classifies instead of silently degrading.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

import aiohttp

from .multiwallet_sources import (
    ERC20_DECIMALS,
    ERC20_NAME,
    ERC20_SYMBOL,
    ERC20_TOTAL_SUPPLY,
    Rpc,
    _hex_int,
    decode_abi_string,
    evm_rpc,
)

logger = logging.getLogger(__name__)

# Chains we probe, in Dexscreener's chainId spelling (the same strings
# src/utils.py maps to explorers and trading links), in preference order: when
# an address carries code on more than one chain, the earlier chain wins.
PROBE_CHAINS = [c.strip().lower() for c in
                os.getenv("CA_PROBE_CHAINS", "ethereum,base,bsc,robinhood").split(",")
                if c.strip()]

# Keyless endpoints, so classification survives a missing fomo/.env. These are
# only ever asked eth_getCode and three constant eth_calls, which is well
# inside what a public node tolerates.
_PUBLIC_RPC: dict[str, tuple[str, ...]] = {
    "ethereum":  ("https://ethereum-rpc.publicnode.com", "https://eth.drpc.org"),
    "base":      ("https://mainnet.base.org", "https://base-rpc.publicnode.com"),
    "bsc":       ("https://bsc-dataseed.binance.org", "https://bsc-rpc.publicnode.com"),
    "robinhood": (),   # no public node known — configured ROBINHOOD_RPC only
}

RPC_TIMEOUT = float(os.getenv("CA_PROBE_RPC_TIMEOUT", "4"))
TOTAL_TIMEOUT = float(os.getenv("CA_PROBE_TIMEOUT", "8"))

# EIP-7702 delegation indicator: code is exactly 0xef0100 || implementation.
DELEGATION_PREFIX = "0xef0100"

# Verdict cache. A wallet never becomes a token and a token never stops being
# one, so those verdicts keep; "unknown" means we failed to ask, and must not
# stick.
_CACHE_TTL = float(os.getenv("CA_PROBE_CACHE_TTL", str(6 * 3600)))
_CACHE_TTL_UNKNOWN = 60.0
_CACHE_MAX = 2000
_cache: dict[str, tuple[float, dict]] = {}

# Verdict strength — a token beats a bare contract, and any on-chain answer
# beats "we could not ask".
_RANK = {"token": 3, "contract": 2, "wallet": 1, "unknown": 0}


def _rpc_urls(chain: str) -> list[str]:
    """Configured endpoint first, public fallbacks after it."""
    urls = []
    configured = evm_rpc(chain)
    if configured:
        urls.append(configured)
    for url in _PUBLIC_RPC.get(chain, ()):
        if url not in urls:
            urls.append(url)
    return urls


def blank(kind: str = "unknown") -> dict:
    return {"kind": kind, "chain": "", "symbol": "", "name": "",
            "decimals": None, "supply": 0.0, "delegated": False,
            "chains_with_code": [], "chains_answered": []}


async def _probe_chain(session: aiohttp.ClientSession, chain: str, address: str) -> dict:
    """One chain's verdict for one address. Never raises."""
    out = blank()
    urls = _rpc_urls(chain)
    if not urls:
        return out

    rpc = Rpc(session, urls, f"ca_probe:{chain}")
    code = await rpc.call("eth_getCode", [address, "latest"], timeout=RPC_TIMEOUT)
    if not isinstance(code, str):
        return out                      # RPC failed — stays "unknown"

    out["chains_answered"] = [chain]
    code = code.lower()

    if code in ("", "0x"):
        return {**out, "kind": "wallet", "chain": chain}

    out["chains_with_code"] = [chain]

    # A 7702 account is a wallet that has delegated its code. Decide this
    # before the ERC-20 probe: whatever it delegates to may well answer
    # symbol(), and it would still be somebody's wallet.
    if code.startswith(DELEGATION_PREFIX):
        return {**out, "kind": "wallet", "chain": chain, "delegated": True}

    async def call(selector: str) -> Any:
        return await rpc.call("eth_call", [{"to": address, "data": selector}, "latest"],
                              timeout=RPC_TIMEOUT)

    symbol = decode_abi_string(await call(ERC20_SYMBOL))
    raw_decimals = await call(ERC20_DECIMALS)
    decimals = _hex_int(raw_decimals) if raw_decimals and raw_decimals != "0x" else -1
    if not 0 <= decimals <= 36:
        decimals = -1
    raw_supply = await call(ERC20_TOTAL_SUPPLY)
    supply_raw = _hex_int(raw_supply) if raw_supply and raw_supply != "0x" else 0

    # Two of the three is the bar: some tokens revert on symbol(), some report
    # no supply before their mint, but a non-token contract answers none.
    signals = sum([bool(symbol), decimals >= 0, supply_raw > 0])
    if signals < 2:
        return {**out, "kind": "contract", "chain": chain}

    name = decode_abi_string(await call(ERC20_NAME))
    return {**out,
            "kind": "token",
            "chain": chain,
            "symbol": symbol[:24],
            "name": name[:64],
            "decimals": decimals if decimals >= 0 else None,
            "supply": supply_raw / (10 ** decimals) if decimals >= 0 and supply_raw else 0.0}


def _merge(results: list[dict]) -> dict:
    """Best verdict across chains, PROBE_CHAINS order breaking ties.

    Carrying code is the second key, not just the verdict rank: an address can
    be a bare EOA on Ethereum and a 7702 wallet on BSC, and BSC is the chain
    worth naming — it is the one where the account has actually been used.
    """
    best = blank()
    best_key = (_RANK[best["kind"]], 0)
    answered: list[str] = []
    with_code: list[str] = []
    for result in results:
        answered += result.get("chains_answered") or []
        with_code += result.get("chains_with_code") or []
        key = (_RANK[result["kind"]], 1 if result.get("chains_with_code") else 0)
        if key > best_key:
            best, best_key = result, key
    best = dict(best)
    best["chains_answered"] = answered
    best["chains_with_code"] = with_code

    # An EOA on every chain belongs to no chain in particular. Naming the first
    # one we happened to ask would be a guess, and it would point the trading
    # links at it.
    if best["kind"] == "wallet" and not with_code:
        best["chain"] = ""
    return best


async def classify_evm(address: str, session: Optional[aiohttp.ClientSession] = None) -> dict:
    """What is this 0x address? Returns a `blank()`-shaped dict, never raises.

    Chains are probed concurrently and the whole call is bounded by
    TOTAL_TIMEOUT, because this sits in the alert path: a slow RPC must cost
    the ping its classification, never its delivery.
    """
    address = (address or "").strip().lower()
    if not address.startswith("0x") or len(address) != 42:
        return blank()

    hit = _cache.get(address)
    if hit and hit[0] > time.time():
        return dict(hit[1])

    own_session = session is None
    try:
        if own_session:
            session = aiohttp.ClientSession()
        results = await asyncio.wait_for(
            asyncio.gather(*(_probe_chain(session, chain, address) for chain in PROBE_CHAINS),
                           return_exceptions=True),
            timeout=TOTAL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.debug("ca_probe: %s timed out after %ss", address, TOTAL_TIMEOUT)
        return blank()
    except Exception as e:
        logger.debug("ca_probe: %s failed: %r", address, e)
        return blank()
    finally:
        if own_session and session is not None:
            await session.close()

    verdict = _merge([r for r in results if isinstance(r, dict)])

    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    ttl = _CACHE_TTL_UNKNOWN if verdict["kind"] == "unknown" else _CACHE_TTL
    _cache[address] = (time.time() + ttl, dict(verdict))
    return verdict


def describe(probe: dict) -> str:
    """One line for the alert body. Empty string when there is nothing to say.

    The name and symbol are whatever the contract's own `name()`/`symbol()`
    return, i.e. whatever the deployer chose. A token called `*` or `[` would
    otherwise break Telegram's Markdown parse and the alert would fail to send
    entirely, so both are escaped.
    """
    if not probe or probe.get("kind") in ("", "unknown"):
        return ""
    from .utils import chain_display_name, escape_md

    chain = probe.get("chain") or ""
    where = f" on {chain_display_name(chain)}" if chain else ""
    kind = probe["kind"]

    if kind == "wallet":
        what = "delegated wallet" if probe.get("delegated") else "wallet address"
        return f"👛 *Not a token* — {what}{where}"
    if kind == "contract":
        return f"📄 Contract{where} — no ERC-20 interface"

    ticker = f"${escape_md(probe['symbol'])}" if probe.get("symbol") else ""
    name = escape_md(probe.get("name") or "")
    label = " | ".join(p for p in (f"*{name}*" if name else "", f"*{ticker}*" if ticker else "") if p)
    return f"🪙 {label}{where} — *no pool yet*" if label else f"🪙 ERC-20{where} — *no pool yet*"
