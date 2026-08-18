"""
wallet_resolve.py -- CLI over fomo_wallet.py: resolve a FOMO handle to its
real on-chain trading wallet.

The method and the reasoning live in fomo_wallet.py. In short: fomo.family
publishes four addresses per trader and none of them is the trading wallet, and
no tx signature anywhere -- but swap.outTokenAddress + swap.outHumanAmount +
swap.createdAt identify the transaction on chain, and the trader is the signer
that is NOT the fee payer (FOMO sponsors gas, so signers[0] is the platform).

    python wallet_resolve.py --anchor
        Self-test against the known Konito trade. Proves the mint scan can
        reach a transaction whose answer we already know.

    python wallet_resolve.py --handle Konito
    python wallet_resolve.py --handle onmycheck --expect Ay77dkJk...
    python wallet_resolve.py --handle Rowdy --deep

The swap is looked up in the gas sponsor's history first, because that is the
one index whose length is bounded by FOMO's own volume -- a viral mint can put
more than 12000 signatures in front of a two-hour-old swap, and no amount of
paging gets behind that. --deep adds a third route that reads the blocks at
the swap's timestamp directly and depends on no signature history at all.

Results cache to wallet_cache.json. A trader's wallet does not change, so a
handle is resolved once, ever -- pass --fresh to ignore the cache.

Put a Helius/QuickNode URL in SOLANA_RPC in .env; the public endpoint throttles
hard and prunes history.
"""

from __future__ import annotations

import argparse
import asyncio

from dotenv import load_dotenv

load_dotenv()

from fomo_wallet import (  # noqa: E402  (after load_dotenv, which sets SOLANA_RPC)
    CACHE,
    RPC_URL,
    SPONSORS,
    Rpc,
    SponsorIndex,
    cached_wallet,
    derive_trader,
    find_tx,
    iso_epoch,
    locate_swap,
    pick_swaps,
    rpc_display_name,
    verify_wallet,
)

KNOWN_ANCHOR = {
    "handle": "Konito",
    "mint": "5P3DUdtj13HVxrpM9QuuabznPiGXtDwE8DofT4PemCWH",
    "amount": 25540.610209543,
    "created": "2026-08-18T13:52:03.014Z",
    "tx": "63o3ZL1hpSCtf3wwtsdFbpEQPfA5RwP33fhYtKa5Naoyh3z8D6z6gsfkV5Ary4znrDvm28zib46MGoCxpMWofyjp",
    "wallet": "93fjdwW7S3Aw4TkrnMzy51sZ5pP4ArpvtmFYujNyDVgH",
}


def head(text: str) -> None:
    print(f"\n{'=' * 78}\n  {text}\n{'=' * 78}")


async def resolve(rpc: Rpc, handle: str, expect: str | None,
                  limit: int = 50, fresh: bool = False,
                  deep: bool = False) -> str | None:
    from fomo_api import FomoClient

    head(f"@{handle}")
    if not fresh and (hit := cached_wallet(handle)):
        print(f"  cached  {hit}\n  (--fresh to re-resolve)")
        return hit

    async with FomoClient() as fomo:
        user = await fomo.user_by_handle(handle, with_ranks=False)
        data = await fomo._get(f"/v2/users/{user.id}/swaps?limit={limit}", cache=False)
    rows = (data.get("swaps") if isinstance(data, dict) else data) or []
    print(f"  fomo id       {user.id}")
    print(f"  user.address  {user.sol_address}   (synthetic)")
    print(f"  swaps         {len(rows)}")
    if not rows:
        print("  no swaps -- cannot resolve")
        return None

    # Phase 1: find ONE transaction. Sponsor index first, then the mint, then
    # (with --deep) the blocks themselves. The index is built once and shared
    # by every attempt, so four swaps cost one scan.
    index = SponsorIndex(rpc)
    wallet = hit_sig = None
    for i, sw in enumerate(pick_swaps(rows, want=4), 1):
        mint, amount = sw["outTokenAddress"], float(sw["outHumanAmount"])
        print(f"\n  [{i}] {sw['createdAt']}  {amount:,.6f} of {mint[:10]}...")
        try:
            sig, tx, route = await locate_swap(rpc, sw, index, deep=deep)
        except Exception as exc:
            print(f"      scan failed: {str(exc)[:140]}")
            continue
        if not tx:
            continue
        wallet, how = derive_trader(tx)
        payer = tx["transaction"]["message"]["accountKeys"][0]["pubkey"]
        print(f"      route   {route}")
        print(f"      tx      {(sig or '?')[:28]}...")
        print(f"      payer   {payer}"
              + ("   (FOMO gas sponsor)" if payer in SPONSORS else "   (UNKNOWN payer --"
                 " add it to FOMO_SPONSORS if it recurs)"))
        print(f"      trader  {wallet}   [{how}]")
        hit_sig = sig
        break

    if not wallet:
        print("\n  UNRESOLVED -- no swap could be matched on chain.")
        if not deep:
            print("  The sponsor index and the mint scans both came up empty.")
            print("  Next: rerun with --deep, which reads the blocks at the swap's")
            print("  timestamp directly and does not depend on any signature history.")
        else:
            print("  All three routes failed. Either the RPC has pruned these blocks,")
            print("  or FOMO used a fee payer that is not in FOMO_SPONSORS -- run")
            print("  wallet_hunt.py --handle to dump the raw swap rows and check.")
        return None

    # Phase 2: confirm against the wallet's own history, which is short.
    confirmed, checked = await verify_wallet(rpc, wallet, rows, skip_sig=hit_sig)

    print(f"\n  WALLET  {wallet}")
    if checked:
        print(f"  confirmed on {confirmed}/{checked} further swap(s) in this wallet's history")
    print("  confidence: " + (
        "CONFIRMED" if confirmed >= 2 else
        "likely -- 1 independent confirmation" if confirmed == 1 else
        "PROVISIONAL -- only the initial match, nothing corroborated it"))
    if expect:
        print("  MATCH" if wallet == expect else f"  MISMATCH -- expected {expect}")

    import fomo_wallet
    cache = fomo_wallet._load_cache()
    cache[handle.lower()] = {"wallet": wallet, "confirmed": confirmed,
                             "checked": checked, "resolvedAt": int(__import__("time").time())}
    fomo_wallet._save_cache(cache)
    print(f"  cached -> {CACHE}")
    return wallet


async def anchor(rpc: Rpc) -> None:
    """Prove the mint scan reaches a transaction we already know the answer to."""
    a = KNOWN_ANCHOR
    head("ANCHOR SELF-TEST -- known Konito trade")
    print(f"  mint    {a['mint']}")
    print(f"  amount  {a['amount']}")
    print(f"  when    {a['created']}  (epoch {iso_epoch(a['created'])})")
    print(f"  want    {a['tx'][:28]}...  ->  {a['wallet']}\n")

    sig, tx = await find_tx(rpc, a["mint"], a["amount"], iso_epoch(a["created"]))
    if not tx:
        print("\n  FAILED to reach the tx from the mint. Either the RPC pruned it,")
        print("  or this mint is not in the tx's account keys. Next thing to try:")
        print(f"    getSignaturesForAddress({SPONSORS[0]})")
        return

    print(f"\n  found   {sig[:28]}...")
    print("  SIGNATURE MATCHES" if sig == a["tx"] else "  different tx (fine if the wallet matches)")
    wallet, how = derive_trader(tx)
    print(f"  trader  {wallet}   [{how}]")
    print("\n  ANCHOR PASSED -- run --handle next." if wallet == a["wallet"]
          else f"\n  ANCHOR FAILED -- got {wallet}, expected {a['wallet']}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anchor", action="store_true", help="self-test on the known trade")
    ap.add_argument("--handle", help="FOMO handle to resolve")
    ap.add_argument("--expect", help="known-correct wallet, for verification")
    ap.add_argument("--fresh", action="store_true", help="ignore the cache")
    ap.add_argument("--limit", type=int, default=50, help="swaps to pull (default 50)")
    ap.add_argument("--deep", action="store_true",
                    help="also read the blocks at the swap timestamp if the "
                         "sponsor and mint routes fail (slower, always works)")
    args = ap.parse_args()

    if not (args.anchor or args.handle):
        ap.print_help()
        print("\nStart here:\n    python wallet_resolve.py --anchor\n")
        return 0

    try:
        import httpx
    except ImportError:
        print("pip install httpx")
        return 1

    print(f"RPC: {rpc_display_name(RPC_URL)} (credentials hidden)")
    if "api.mainnet-beta" in RPC_URL:
        print("     (public endpoint -- expect throttling and pruned history)")

    async with httpx.AsyncClient(timeout=60) as http:
        rpc = Rpc(http)
        if args.anchor:
            await anchor(rpc)
        if args.handle:
            await resolve(rpc, args.handle, args.expect, args.limit, args.fresh,
                          args.deep)
        print(f"\n{rpc.calls} RPC call(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
