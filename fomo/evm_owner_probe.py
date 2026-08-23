#!/usr/bin/env python3
"""
evm_owner_probe.py — is FOMO's published `evmAddress` the OWNER of the wallet
that actually trades?

Where this comes from (session 39). The signup capture proved `user.evmAddress`
is the account's Privy embedded EOA, registered by the client at
`POST /v2/users`. `published_vs_verified.py` then proved that EOA is never the
wallet we verified on chain — 0 matches in 37 pairs, no drift by signup month.
So the published field is neither the trading wallet nor a decoy: it is a real
keypair that is not the one trading.

The remaining structural explanation is ERC-4337: the verified wallets are
deployed contracts, and they sit at the *same address on Base and BSC*, which
only happens under CREATE2. If the smart account's owner is the published EOA,
then the trading wallet is counterfactually derivable — computable from a field
the API already gives you, for any user, before they ever trade.

This asks the chain directly. For each pair it reads the deployed code, the
ERC-1967 implementation slot, and every owner-shaped view the wallet exposes,
then checks whether the published EOA comes back.

    python evm_owner_probe.py --csv published_vs_verified.csv --limit 12
    python evm_owner_probe.py --handle konito --handle rowdy -v

Read-only: eth_call and eth_getStorageAt only. Writes nothing, caches nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _keccak import selector  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ERC-1967 implementation slot: keccak256("eip1967.proxy.implementation") - 1
IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"

# Ownership is spelled differently by every smart-account vendor, so ask all of
# them and see which one answers.
NO_ARG_VIEWS = [
    ("owner()", "owner"),
    ("getOwners()", "getOwners"),
    ("ownerAtIndex(uint256)", "ownerAtIndex(0)"),   # Coinbase Smart Wallet
    ("entryPoint()", "entryPoint"),
    ("masterCopy()", "masterCopy"),                  # Safe
    ("getImplementation()", "getImplementation"),
]

CHAINS = {
    "base": os.getenv("BASE_RPC") or "https://mainnet.base.org",
    "bsc": os.getenv("BSC_RPC") or "https://bsc-dataseed.bnbchain.org",
}


def pad_word(hexstr: str) -> str:
    return hexstr[2:].lower().rjust(64, "0")


def addresses_in(blob: str) -> list[str]:
    """Every 32-byte word that looks like a left-padded, non-zero address."""
    if not isinstance(blob, str) or not blob.startswith("0x"):
        return []
    body = blob[2:]
    out = []
    for i in range(0, len(body) - 63, 64):
        word = body[i:i + 64]
        if word[:24] == "0" * 24 and word[24:] != "0" * 40:
            out.append("0x" + word[24:].lower())
    return out


class Rpc:
    def __init__(self, client, url: str) -> None:
        self.client, self.url, self._id = client, url, 0

    async def call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        r = await self.client.post(
            self.url,
            json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
            timeout=25,
        )
        r.raise_for_status()
        payload = r.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(payload["error"])
        return payload.get("result")

    async def eth_call(self, to: str, data: str) -> str | None:
        try:
            return await self.call("eth_call", [{"to": to, "data": data}, "latest"])
        except Exception:
            return None


async def probe_one(rpc: Rpc, row: dict[str, str], verbose: bool) -> dict[str, Any]:
    wallet = row["cached_evm"]
    published = (row.get("published_evm") or "").lower()
    out: dict[str, Any] = {"handle": row["handle"], "wallet": wallet,
                           "published": published, "views": {}, "owners": []}

    code = await rpc.call("eth_getCode", [wallet, "latest"])
    out["code_bytes"] = max(0, (len(code) - 2) // 2) if isinstance(code, str) else 0
    if not out["code_bytes"]:
        out["note"] = "no code on this chain"
        return out

    slot = await rpc.call("eth_getStorageAt", [wallet, IMPL_SLOT, "latest"])
    impl = addresses_in(slot or "")
    out["implementation"] = impl[0] if impl else None

    for sig, label in NO_ARG_VIEWS:
        data = selector(sig)
        if sig == "ownerAtIndex(uint256)":
            data += "0" * 64
        res = await rpc.eth_call(wallet, data)
        if res and res != "0x":
            found = addresses_in(res)
            out["views"][label] = found or res[:66]
            out["owners"].extend(found)

    # The direct question, when the wallet is a Coinbase-style multi-owner account.
    if published:
        res = await rpc.eth_call(wallet, selector("isOwnerAddress(address)") + pad_word(published))
        if res and res != "0x":
            out["is_owner_published"] = res.rstrip("0").endswith("1") or int(res, 16) == 1

    out["owner_matches_published"] = bool(
        published and published in {o.lower() for o in out["owners"]})
    if verbose:
        print(json.dumps(out, indent=2))
    return out


async def main_async(args) -> int:
    try:
        import httpx
    except ImportError:
        print("needs httpx (it is already in this venv for the bot)")
        return 2

    rows: list[dict[str, str]] = []
    if args.csv and Path(args.csv).exists():
        for r in csv.DictReader(open(args.csv, encoding="utf-8")):
            if r.get("cached_evm") and r.get("published_evm"):
                rows.append(r)
    if args.handle:
        wanted = {h.lower().lstrip("@") for h in args.handle}
        rows = [r for r in rows if r["handle"].lower() in wanted] or rows
    if not rows:
        print("no (published, verified) EVM pairs found — run published_vs_verified.py "
              "--csv published_vs_verified.csv first")
        return 1
    rows = rows[: args.limit]

    url = CHAINS.get(args.chain)
    print(f"probing {len(rows)} wallets on {args.chain} via {url.split('//')[-1].split('/')[0]}\n")

    results = []
    async with httpx.AsyncClient() as client:
        rpc = Rpc(client, url)
        for row in rows:
            try:
                results.append(await probe_one(rpc, row, args.verbose))
            except Exception as exc:
                results.append({"handle": row["handle"], "error": str(exc)[:80]})

    w = max([len(r["handle"]) for r in results] + [6])
    print(f"{'handle':<{w}}  {'code':>6}  {'impl':<12}  {'owner views':<28}  match")
    print("-" * (w + 60))
    for r in results:
        if r.get("error"):
            print(f"{r['handle']:<{w}}  ERROR {r['error']}")
            continue
        views = ",".join(sorted(r.get("views", {}))) or "-"
        impl = (r.get("implementation") or "-")
        impl = impl[:10] + "…" if impl != "-" else "-"
        match = "YES" if r.get("owner_matches_published") else (
            "isOwner=YES" if r.get("is_owner_published") else "no")
        print(f"{r['handle']:<{w}}  {r.get('code_bytes', 0):>6}  {impl:<12}  "
              f"{views[:28]:<28}  {match}")

    deployed = [r for r in results if r.get("code_bytes")]
    matched = [r for r in deployed if r.get("owner_matches_published") or r.get("is_owner_published")]
    print(f"\ndeployed contracts : {len(deployed)}/{len(results)}")
    print(f"published EOA is an owner : {len(matched)}/{len(deployed)}")
    impls = {}
    for r in deployed:
        impls[r.get("implementation") or "none"] = impls.get(r.get("implementation") or "none", 0) + 1
    print("implementations seen:")
    for impl, n in sorted(impls.items(), key=lambda x: -x[1]):
        print(f"  {n:>3}  {impl}")
    if not matched and deployed:
        print("\nIf no view answered at all, these are not standard smart accounts and "
              "the next step is reading one wallet's creation transaction to find its "
              "factory, not guessing more selectors.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="probe FOMO trading wallets for ownership")
    ap.add_argument("--csv", default="published_vs_verified.csv")
    ap.add_argument("--handle", action="append", help="limit to these handles")
    ap.add_argument("--chain", default="base", choices=sorted(CHAINS))
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
