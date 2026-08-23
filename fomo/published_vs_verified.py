#!/usr/bin/env python3
"""
published_vs_verified.py — does FOMO already publish the wallet we spend a chain
scan deriving?

Signup capture (session 39) showed the account-creation call is:

    POST /v2/users {"address": <privy embedded solana>, "evmAddress": <privy embedded evm>}

i.e. `user.address` / `user.evmAddress` are the user's own Privy embedded
wallets, supplied by the client — not platform-minted decoys. The app then
reads balances straight off `user.evmAddress` (Base `balanceOf`, Hyperliquid
`clearinghouseState`), which is not something you do with a decoy.

That contradicts sessions 1 and 4, which found the published fields dead on
chain for Konito and Rowdy. Both can be true if the architecture changed:
older accounts trade from somewhere else, newer ones trade from the embedded
wallet directly.

This script decides it, using data already on disk plus one read per handle.
For every handle whose wallet we independently verified, it fetches the
published fields and compares. Then it buckets the match rate by signup month.

    python published_vs_verified.py --limit 40
    python published_vs_verified.py --limit 200 --csv published_vs_verified.csv

Read-only. Touches no cache, resolves nothing, writes only the CSV you ask for.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CACHE = Path("wallet_cache.json")


def load_pairs(min_confirmed: int) -> list[dict[str, Any]]:
    """Handles whose wallet we derived and checked, so the comparison means something."""
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"cannot read {CACHE}: {exc}")
        return []

    out = []
    for handle, entry in cache.items():
        if not isinstance(entry, dict):
            continue
        sol = entry.get("wallet")
        evm = entry.get("evmWallet")
        if not sol and not evm:
            continue
        if sol and (entry.get("confirmed") or 0) < min_confirmed:
            sol = None
        if not sol and not evm:
            continue
        out.append({
            "handle": handle,
            "cached_sol": sol,
            "cached_evm": (evm or "").lower() or None,
            "confirmed": entry.get("confirmed"),
            "wallet_source": entry.get("walletSource") or "",
            "evm_source": entry.get("evmSource") or "",
        })
    return out


async def fetch(client, row: dict[str, Any], sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        try:
            user = await client.user_by_handle(row["handle"], with_ranks=False)
        except Exception as exc:
            row["error"] = type(exc).__name__
            return row
    row["published_sol"] = user.sol_address
    row["published_evm"] = (user.evm_address or "").lower() or None
    row["created_at"] = user.created_at or ""
    row["swap_count"] = user.swap_count
    row["num_trades"] = user.num_trades
    return row


def verdict(published: str | None, cached: str | None) -> str:
    if not cached:
        return "no-cached"
    if not published:
        return "no-published"
    return "MATCH" if published.lower() == cached.lower() else "differ"


async def main_async(args) -> int:
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from fomo_api import FomoClient
    except Exception as exc:
        print(f"cannot import fomo_api: {exc}")
        return 2

    rows = load_pairs(args.min_confirmed)
    if not rows:
        print("no verified handles in the cache to compare")
        return 1
    rows = rows[: args.limit]
    print(f"comparing {len(rows)} handles (cache has the verified side already)\n")

    sem = asyncio.Semaphore(args.concurrency)
    async with FomoClient() as client:
        results = await asyncio.gather(*(fetch(client, r, sem) for r in rows))

    for r in results:
        r["sol_verdict"] = verdict(r.get("published_sol"), r.get("cached_sol"))
        r["evm_verdict"] = verdict(r.get("published_evm"), r.get("cached_evm"))

    ok = [r for r in results if not r.get("error")]
    errs = [r for r in results if r.get("error")]
    ok.sort(key=lambda r: r.get("created_at") or "")

    w = max([len(r["handle"]) for r in ok] + [6])
    print(f"{'handle':<{w}}  {'created':<10}  {'sol':<10}  {'evm':<10}  swaps")
    print("-" * (w + 44))
    for r in ok:
        print(f"{r['handle']:<{w}}  {(r.get('created_at') or '')[:10]:<10}  "
              f"{r['sol_verdict']:<10}  {r['evm_verdict']:<10}  {r.get('swap_count', '')}")

    def rate(rs, key):
        rel = [r for r in rs if r[key] in ("MATCH", "differ")]
        m = sum(1 for r in rel if r[key] == "MATCH")
        return m, len(rel)

    sm, st = rate(ok, "sol_verdict")
    em, et = rate(ok, "evm_verdict")
    print(f"\nSolana : {sm}/{st} published == verified")
    print(f"EVM    : {em}/{et} published == verified")
    if errs:
        print(f"({len(errs)} lookups failed: "
              f"{', '.join(sorted({r['error'] for r in errs}))})")

    # The whole point: if the architecture changed, the match rate moves with
    # the signup date rather than being uniformly right or wrong.
    print("\nBy signup month:")
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        buckets[(r.get("created_at") or "unknown")[:7]].append(r)
    print(f"  {'month':<9}  {'n':>3}  {'sol match':>10}  {'evm match':>10}")
    for month in sorted(buckets):
        b = buckets[month]
        sm2, st2 = rate(b, "sol_verdict")
        em2, et2 = rate(b, "evm_verdict")
        print(f"  {month:<9}  {len(b):>3}  {f'{sm2}/{st2}':>10}  {f'{em2}/{et2}':>10}")

    if args.csv:
        cols = ["handle", "created_at", "swap_count", "num_trades",
                "cached_sol", "published_sol", "sol_verdict",
                "cached_evm", "published_evm", "evm_verdict",
                "confirmed", "wallet_source", "evm_source", "error"]
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            wr.writeheader()
            wr.writerows(results)
        print(f"\nwrote {args.csv}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="compare FOMO's published addresses against verified wallets")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--min-confirmed", type=int, default=2,
                    help="minimum corroboration for a cached Solana wallet to count")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--csv")
    args = ap.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
