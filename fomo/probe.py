"""
probe.py — CLI to exercise the FOMO API without Discord. Run on borz.

    python probe.py Binkieee
    python probe.py Binkieee --json
    python probe.py --top 24h
    python probe.py --search bink
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

from dotenv import load_dotenv

from fomo_api import FomoClient, FomoError, fmt_duration, fmt_usd

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("handle", nargs="?")
    ap.add_argument("--json", action="store_true", help="dump the raw responseObject")
    ap.add_argument("--search")
    ap.add_argument("--top", nargs="?", const="24h")
    ap.add_argument("--swaps", type=int, default=0)
    args = ap.parse_args()

    client = FomoClient(
        refresh_token=os.getenv("FOMO_PRIVY_REFRESH_TOKEN") or None,
        access_token=os.getenv("FOMO_PRIVY_ACCESS_TOKEN") or None,
    )
    async with client as fomo:
        try:
            if args.search:
                for u in await fomo.search(args.search, limit=10):
                    print(f"@{u.handle:<24} {u.followers:>10,} followers  {fmt_usd(u.total_volume)}")
                return 0

            if args.top:
                period = None if args.top == "all" else args.top
                for i, u in enumerate(await fomo.leaderboard(period, limit=10), 1):
                    pnl = u.raw.get("totalPnL", u.raw.get("pnl24h"))
                    print(f"{i:>2}. @{u.handle:<24} {fmt_usd(pnl):>12}  vol {fmt_usd(u.total_volume)}")
                return 0

            if not args.handle:
                ap.error("give a handle, --search or --top")

            user = await fomo.resolve(args.handle)
            if args.json:
                print(json.dumps(user.raw, indent=2))
                return 0

            print(f"@{user.handle}  ({user.display_name})")
            print(f"  id           {user.id}")
            print(f"  sol          {user.sol_address}")
            print(f"  evm          {user.evm_address}")
            print(f"  x            {user.twitter}")
            print(f"  clan         {user.clan_name}")
            print(f"  followers    {user.followers:,}   following {user.following:,}")
            print(f"  trades       {user.num_trades:,}   swaps {user.swap_count:,}")
            print(f"  volume       {fmt_usd(user.total_volume)}")
            print(f"  avg hold     {fmt_duration(user.avg_hold_seconds)}")
            for label, key in (("all-time", ""), ("24h", "24h"), ("7d", "7d"), ("30d", "30d")):
                block = user.rank(key)
                if block:
                    print(f"  pnl {label:<9} {fmt_usd(block.get('pnl')):>12}  rank #{block.get('rank'):,}")

            if args.swaps:
                data = await fomo.swaps(user.id, limit=args.swaps)
                print(f"\n  last {len(data['swaps'])} swaps (hasNextPage={data['hasNextPage']}):")
                for s in data["swaps"]:
                    print(f"    {s['createdAt']}  in ${s['humanUsdAmountIn']:,.2f} -> out ${s['humanUsdAmountOut']:,.2f}  {s['provider']}")
        except FomoError as exc:
            print(f"ERROR: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
