"""
swap_fields.py — which swap field holds the real trading wallet?

verify_wallet_onchain.py showed fomo's user.address has ZERO on-chain activity
while the real wallet trades constantly. But the swap objects carry their own
`address`, `recipient` and `referralFeeAddress` fields — and fomo indexes
`isOffPlatform` trades, so it must know the wallet. Check whether one of those
fields already is it, which would save an RPC round-trip per lookup.

    python swap_fields.py
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter

from dotenv import load_dotenv

from fomo_api import FomoClient, FomoError

load_dotenv()

EXPECTED = {
    "onmycheck": "Ay77dkJkbjPCLbhHmwNg5z4WVtP2bMUpjKNnWFo1CuD2",
    "FIippingProfits": "DdM1tyCdoEyoxYYmGMjdf5rRPcpmj3UzZTpE7ScuTf7d",
}
FIELDS = ("address", "recipient", "referralFeeAddress")
LIMIT = 25


async def look(fomo: FomoClient, handle: str, expected: str) -> None:
    print(f"\n{'='*74}\n  @{handle}   want {expected}\n{'='*74}")
    user = await fomo.user_by_handle(handle)
    data = await fomo._get(f"/v2/users/{user.id}/swaps?limit={LIMIT}", cache=False)
    rows = data.get("swaps") if isinstance(data, dict) else data
    if not rows:
        print("  no swaps")
        return
    print(f"  user.address = {user.sol_address}   ({len(rows)} swaps)\n")

    for f in FIELDS:
        vals = Counter(str(r.get(f)) for r in rows if r.get(f))
        if not vals:
            print(f"  {f:<20} (always empty)")
            continue
        print(f"  {f}")
        for v, n in vals.most_common(5):
            tag = ""
            if v == expected:
                tag = "   <== THE REAL WALLET"
            elif v == user.sol_address:
                tag = "   (= user.address, the dead one)"
            print(f"      {n:>3}x  {v}{tag}")

    # Does one field give the right answer on every swap, or only sometimes?
    for f in FIELDS:
        vals = [r.get(f) for r in rows if r.get(f)]
        if vals and all(v == expected for v in vals):
            print(f"\n  -> `{f}` is the real wallet on ALL {len(vals)} swaps. Use it.")
        elif expected in vals:
            print(f"\n  -> `{f}` matches on {vals.count(expected)}/{len(vals)} swaps "
                  f"— varies, so pick the most common rather than swaps[0].")

    off = sum(1 for r in rows if r.get("isOffPlatform"))
    print(f"\n  isOffPlatform: {off}/{len(rows)} swaps happened outside the fomo app")


async def main() -> int:
    client = FomoClient(
        refresh_token=os.getenv("FOMO_PRIVY_REFRESH_TOKEN") or None,
        access_token=os.getenv("FOMO_PRIVY_ACCESS_TOKEN") or None,
    )
    async with client as fomo:
        for handle, expected in EXPECTED.items():
            try:
                await look(fomo, handle, expected)
            except FomoError as exc:
                print(f"  {handle}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
