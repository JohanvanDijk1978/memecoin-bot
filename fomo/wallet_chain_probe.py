"""
wallet_chain_probe.py — how do fomo's three addresses relate on chain?

Per trader we now have three distinct addresses:
  user.address   dead, zero transactions ever  (Privy account, never used)
  swap.address   identical across all 25 swaps (fomo-side execution account?)
  expected       what Solscan says is the trader (active, recent)

If swap.address is a token account or a PDA, its on-chain owner/authority is
probably `expected` — which would give us a real derivation path. This asks the
chain directly instead of guessing.

    python wallet_chain_probe.py
"""

from __future__ import annotations

import asyncio
import json
import os

TRIPLES = {
    # handle: (user.address, swap.address, expected)
    "onmycheck": (
        "Cy8zbavbfJZLbWEXkbS87nnFf6ZHqAKQrSsnpYgqjczN",
        "23cpUhkwJCxn2zpgw7Soxbq9juaCyvLMtWZ8JFTUCwE7",
        "Ay77dkJkbjPCLbhHmwNg5z4WVtP2bMUpjKNnWFo1CuD2",
    ),
    "FIippingProfits": (
        "DpSCSD6sdroS6Fb83nznmFqRK1c7y1sm3J3MZjgpCMzc",
        "3cL2SAscCaxZTSCQXJwQDr1DLKMreXbFG6xYwYtpZ4FS",
        "DdM1tyCdoEyoxYYmGMjdf5rRPcpmj3UzZTpE7ScuTf7d",
    ),
}

RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


async def rpc(http, method, params):
    r = await http.post(RPC, json={"jsonrpc": "2.0", "id": 1,
                                   "method": method, "params": params})
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        return {"__error__": j["error"]}
    return j.get("result")


async def describe(http, label: str, addr: str) -> dict:
    info = await rpc(http, "getAccountInfo", [addr, {"encoding": "jsonParsed"}])
    val = (info or {}).get("value") if isinstance(info, dict) else None
    out = {"addr": addr, "exists": bool(val)}
    print(f"\n  {label}\n    {addr}")
    if not val:
        print("    account does not exist on chain (never funded)")
        return out
    owner = val.get("owner")
    lam = val.get("lamports", 0)
    kind = {SYSTEM: "system wallet", TOKEN: "SPL token account",
            TOKEN22: "Token-2022 account"}.get(owner, f"program-owned ({owner})")
    print(f"    type: {kind}   lamports: {lam:,}   executable: {val.get('executable')}")
    out["owner_program"] = owner
    parsed = (val.get("data") or {}).get("parsed") if isinstance(val.get("data"), dict) else None
    if parsed:
        inf = parsed.get("info", {})
        for k in ("owner", "authority", "mint", "delegate", "closeAuthority"):
            if inf.get(k):
                print(f"    {k}: {inf[k]}")
                out[k] = inf[k]
    sigs = await rpc(http, "getSignaturesForAddress", [addr, {"limit": 5}])
    n = len(sigs or []) if isinstance(sigs, list) else 0
    print(f"    recent txs: {n}"
          + (f"   newest blockTime {sigs[0].get('blockTime')}" if n else ""))
    out["sigs"] = [s["signature"] for s in (sigs or [])] if n else []
    return out


async def run(http, handle, triple):
    user_a, swap_a, expected = triple
    print(f"\n{'='*74}\n  @{handle}\n{'='*74}")
    a = await describe(http, "user.address (fomo profile)", user_a)
    b = await describe(http, "swap.address (on every swap)", swap_a)
    c = await describe(http, "expected (from Solscan)", expected)

    print("\n  -- relationship --")
    for key in ("owner", "authority"):
        if b.get(key) == expected:
            print(f"  swap.address.{key} == expected  ->  DERIVATION FOUND: "
                  f"read `{key}` of the swap address")
            return
    # Not a token account? Then look at who shares transactions with it.
    if b.get("sigs"):
        tx = await rpc(http, "getTransaction",
                       [b["sigs"][0], {"maxSupportedTransactionVersion": 0,
                                       "encoding": "jsonParsed"}])
        if isinstance(tx, dict) and tx.get("transaction"):
            keys = tx["transaction"]["message"]["accountKeys"]
            names = [k["pubkey"] for k in keys]
            signers = [k["pubkey"] for k in keys if k.get("signer")]
            print(f"  newest tx of swap.address: fee payer {signers[0] if signers else '?'}")
            print(f"    expected in this tx?     {expected in names}")
            print(f"    user.address in this tx? {user_a in names}")
            print(f"    signers: {signers}")
    else:
        print("  swap.address has no transactions either — it may be an internal id, "
              "not an on-chain account.")


async def main() -> int:
    try:
        import httpx
    except ImportError:
        print("pip install httpx")
        return 1
    async with httpx.AsyncClient(timeout=30) as http:
        for handle, triple in TRIPLES.items():
            await run(http, handle, triple)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
