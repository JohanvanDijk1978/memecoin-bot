"""
verify_wallet_onchain.py — which wallet actually made this trader's trades?

find_wallet_source.py proved the expected address appears nowhere on the
profile page: not in any of 203 responses, not in the DOM, not in a link. So
fomo.family and whatever source gave us the other address simply disagree.
Chain state is the tiebreaker.

Method: pull the trader's recent swaps from fomo's own API, take the on-chain
signatures, and ask a public Solana RPC who signed them. The wallet that shows
up as fee payer is the one doing the trading — no opinion required.

    python verify_wallet_onchain.py

Falls back to comparing recent-activity timestamps if swaps carry no signature.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from typing import Any

from dotenv import load_dotenv

from fomo_api import FomoClient, FomoError

load_dotenv()

RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
SIG_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")

CANDIDATES = {
    "onmycheck": "Ay77dkJkbjPCLbhHmwNg5z4WVtP2bMUpjKNnWFo1CuD2",
    "FIippingProfits": "DdM1tyCdoEyoxYYmGMjdf5rRPcpmj3UzZTpE7ScuTf7d",
}


def find_sigs(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for v in node.values():
            out += find_sigs(v)
    elif isinstance(node, list):
        for v in node:
            out += find_sigs(v)
    elif isinstance(node, str) and SIG_RE.match(node):
        out.append(node)
    return out


async def rpc(client: Any, method: str, params: list[Any]) -> Any:
    r = await client.post(RPC, json={"jsonrpc": "2.0", "id": 1,
                                     "method": method, "params": params})
    r.raise_for_status()
    return r.json().get("result")


async def check(fomo: FomoClient, http: Any, handle: str, expected: str) -> None:
    print(f"\n{'='*74}\n  @{handle}\n{'='*74}")
    try:
        user = await fomo.user_by_handle(handle)
    except FomoError as exc:
        print(f"  lookup failed: {exc}")
        return

    fomo_addr = user.sol_address
    print(f"  fomo id            {user.id}")
    print(f"  fomo `address`     {fomo_addr}")
    print(f"  expected elsewhere {expected}")

    try:
        swaps = await fomo._get(f"/v2/users/{user.id}/swaps?limit=5", cache=False)
    except FomoError as exc:
        print(f"  swaps failed: {exc}")
        return

    rows = swaps.get("swaps") if isinstance(swaps, dict) else swaps
    if not rows:
        print("  no swaps returned")
        return
    print(f"\n  swap object keys: {sorted(rows[0].keys())}")

    sigs = list(dict.fromkeys(find_sigs(rows)))[:3]
    if not sigs:
        print("  no tx signature in the swap payload — falling back to activity compare")
        for label, addr in (("fomo `address`", fomo_addr), ("expected", expected)):
            if not addr:
                continue
            res = await rpc(http, "getSignaturesForAddress", [addr, {"limit": 5}])
            n = len(res or [])
            newest = (res or [{}])[0].get("blockTime") if res else None
            print(f"    {label:<16} {addr}  recent_txs={n}  newest_blockTime={newest}")
        print("\n  Compare newest_blockTime against the swap timestamps above.")
        return

    print(f"  checking {len(sigs)} signature(s) on chain via {RPC}\n")
    for sig in sigs:
        tx = await rpc(http, "getTransaction",
                       [sig, {"maxSupportedTransactionVersion": 0,
                              "encoding": "jsonParsed"}])
        if not tx:
            print(f"    {sig[:20]}…  not found on chain")
            continue
        keys = tx["transaction"]["message"]["accountKeys"]
        signers = [k["pubkey"] for k in keys if k.get("signer")]
        payer = signers[0] if signers else "?"
        verdict = ("MATCHES fomo `address`" if payer == fomo_addr else
                   "MATCHES the expected address" if payer == expected else
                   "matches NEITHER")
        print(f"    {sig[:20]}…  fee payer {payer}")
        print(f"      -> {verdict}")
        if verdict == "matches NEITHER":
            allk = [k["pubkey"] for k in keys]
            if expected in allk:
                print(f"      (but {expected} IS an account in this tx)")
            if fomo_addr in allk:
                print(f"      (but {fomo_addr} IS an account in this tx)")


async def main() -> int:
    try:
        import httpx
    except ImportError:
        print("pip install httpx")
        return 1

    client = FomoClient(
        refresh_token=os.getenv("FOMO_PRIVY_REFRESH_TOKEN") or None,
        access_token=os.getenv("FOMO_PRIVY_ACCESS_TOKEN") or None,
    )
    async with client as fomo, httpx.AsyncClient(timeout=30) as http:
        for handle, expected in CANDIDATES.items():
            await check(fomo, http, handle, expected)
    print("\nWhoever pays the fee is the trading wallet. That's the one to show.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
