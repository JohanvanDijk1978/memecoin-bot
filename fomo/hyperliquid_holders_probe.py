"""
hyperliquid_holders_probe.py -- can `/token` see holders on Hyperliquid?

    python hyperliquid_holders_probe.py 0xb75d5ee14708e7efbea939311090061d72265608
    python hyperliquid_holders_probe.py <address> --limit 50 --verify

`/token` returned zero holders for every Hyperliquid token because neither of
its EVM holder sources covers HyperEVM: CoinMarketCap has no `hyperevm`
platform, and there is no Blockscout instance for chain 999.

Pump.fun is not the answer either, though its coin page shows a holders panel:

  * `frontend-api-v3.pump.fun/token-holders/{address}/count` answers
    `Codex has no holder data for this token` for a HyperEVM address, and
    `/coins/top-holders/{mint}` rejects `0x...` with
    `mint is not a valid base58 public key`. Both are Solana/Codex routes.
  * the `Pump.fun (n)` panel itself is *positions of pump.fun users*, computed
    live from the trade stream -- a cold page load issues no holders request at
    all, and the numbers move while you watch them.

What does answer is hl.eco's explorer API (`scan-api.hl.eco`), which indexes
Transfer events and then reads the leading candidates' balances on-chain. This
probe walks that route end to end:

  1. DEX Screener -> the chain label `/token` will use (expects `Hyperliquid`)
  2. the raw hl.eco holders payload -- supply, holder count, index coverage
  3. `TokenIntelligenceClient.lookup()` -- exactly what `/token` renders
  4. `--verify`: `balanceOf` for the top rows straight off the HyperEVM RPC,
     which is what turns "the API said so" into "the chain says so"

Read-only. No writes, no trades, no key material.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from decimal import Decimal

import httpx
from dotenv import load_dotenv

load_dotenv()

from token_intelligence import (  # noqa: E402
    HYPEREVM_HOLDERS_URL,
    HYPEREVM_USER_AGENT,
    TokenIntelligenceClient,
)

# `balanceOf(address)` and `totalSupply()` -- the two selectors this needs, so
# there is no ABI or keccak dependency here.
BALANCE_OF = "0x70a08231"
TOTAL_SUPPLY = "0x18160ddd"
DEFAULT_RPC = "https://rpc.hyperliquid.xyz/evm"


def _rpc_url(explicit: str | None) -> str:
    return (
        explicit
        or os.getenv("HYPEREVM_RPC")
        or os.getenv("HYPERLIQUID_RPC")
        or DEFAULT_RPC
    )


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


def _units(raw: int, decimals: int) -> Decimal:
    return Decimal(raw) / (Decimal(10) ** decimals)


def _human(value: Decimal) -> str:
    number = float(value)
    for cutoff, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= cutoff:
            return f"{number / cutoff:.2f}{suffix}"
    return f"{number:,.2f}"


async def _dex_chain(http: httpx.AsyncClient, address: str) -> tuple[str, str]:
    """What DEX Screener calls this token's chain, and the dex it trades on."""
    try:
        response = await http.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=20
        )
        pairs = (response.json() or {}).get("pairs") or []
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        return f"lookup failed: {exc}", "-"
    if not pairs:
        return "no pairs", "-"
    best = max(pairs, key=lambda pair: (pair.get("liquidity") or {}).get("usd") or 0)
    return str(best.get("chainId") or "?"), str(best.get("dexId") or "?")


async def _raw_payload(http: httpx.AsyncClient, address: str, limit: int) -> dict:
    response = await http.get(
        f"{HYPEREVM_HOLDERS_URL}/api/token/{address}/holders?limit={limit}",
        headers={"Accept": "application/json", "User-Agent": HYPEREVM_USER_AGENT},
        timeout=30,
    )
    print(f"  HTTP {response.status_code} from {HYPEREVM_HOLDERS_URL}")
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


async def _eth_call(http: httpx.AsyncClient, rpc: str, to: str, data: str) -> int | None:
    try:
        response = await http.post(
            rpc,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"],
            },
            timeout=20,
        )
        result = (response.json() or {}).get("result")
        return int(result, 16) if isinstance(result, str) and result.startswith("0x") else None
    except Exception as exc:  # noqa: BLE001
        print(f"  RPC call failed: {exc}")
        return None


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", help="HyperEVM token contract (0x…)")
    parser.add_argument("--limit", type=int, default=10, help="holders to show (default 10)")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="read the top rows' balanceOf off the HyperEVM RPC as a cross-check",
    )
    parser.add_argument("--rpc", help="HyperEVM RPC (default: $HYPEREVM_RPC or the public node)")
    args = parser.parse_args()

    address = args.address.strip().strip("`")

    async with httpx.AsyncClient(follow_redirects=True) as http:
        print(f"\n=== 1. chain detection ===  {address}")
        chain_id, dex = await _dex_chain(http, address)
        expected = "hyperevm"
        verdict = "→ Hyperliquid" if chain_id == expected else f"(expected {expected})"
        print(f"  DEX Screener chainId: {chain_id} {verdict}   dex: {dex}")

        print("\n=== 2. hl.eco holders payload ===")
        try:
            payload = await _raw_payload(http, address, max(args.limit, 10))
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: {exc}")
            print("  Nothing below can work until this call does -- check egress/CDN.")
            return
        decimals = int(payload.get("decimals") or 18)
        supply_raw = payload.get("totalSupply")
        page = payload.get("page") or {}
        print(f"  symbol        : {payload.get('symbol') or '-'}")
        print(f"  decimals      : {decimals}")
        print(f"  total supply  : {_human(_units(int(supply_raw), decimals)) if supply_raw else '-'}")
        print(f"  holder count  : {payload.get('holderCount')}")
        print(f"  index reach   : {page.get('reachable')} candidates, hasMore={page.get('hasMore')}")
        print(f"  note          : {payload.get('holderCountNote') or '-'}")

        print(f"\n=== 3. what /token will render (top {args.limit}) ===")
        client = TokenIntelligenceClient(http, [])
        token = await client.lookup(address, limit=args.limit)
        print(f"  chain: {token.chain}   {token.name} ({token.symbol})   "
              f"MC {token.market_cap or token.fdv or '-'}")
        if not token.holders:
            print("  NO HOLDERS -- the card would still be blank. Stop here and say so.")
            return
        for index, holder in enumerate(token.holders, 1):
            percentage = f"{holder.percentage:.2f}%" if holder.percentage is not None else "  -  "
            print(f"  {index:>2}. {_short(holder.address)}  {percentage:>7}  "
                  f"{_human(holder.balance):>12}")

        if not args.verify:
            print("\n  (--verify reads these balances off the chain itself)")
            return

        rpc = _rpc_url(args.rpc)
        print(f"\n=== 4. on-chain cross-check via {rpc.split('/v2/')[0]} ===")
        supply_onchain = await _eth_call(http, rpc, address, TOTAL_SUPPLY)
        if supply_onchain is not None:
            print(f"  totalSupply on-chain: {_human(_units(supply_onchain, decimals))}")
        for holder in token.holders[:5]:
            data = BALANCE_OF + "0" * 24 + holder.address[2:]
            raw = await _eth_call(http, rpc, address, data)
            if raw is None:
                continue
            onchain = _units(raw, decimals)
            drift = abs(onchain - holder.balance)
            flag = "OK" if drift <= holder.balance * Decimal("0.01") else "DRIFT"
            print(f"  {_short(holder.address)}  api {_human(holder.balance):>12}  "
                  f"chain {_human(onchain):>12}  {flag}")
        print("\n  A row marked DRIFT means the holder traded between the two reads,")
        print("  or the index is stale -- one repeat run tells you which.")


if __name__ == "__main__":
    asyncio.run(main())
