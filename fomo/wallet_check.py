"""
wallet_check.py — find which API field actually holds a trader's real SOL wallet.

`FomoUser.sol_address` reads `raw["address"]`, and for at least two handles that
returns the wrong wallet. Rather than guess which key is right, this walks every
value the API hands back for a user — the user object, its leaderboard variant,
and /balances — and reports every path holding a wallet-shaped string, marking
the one that matches the address we know is correct.

    python wallet_check.py
    python wallet_check.py SomeHandle=ExpectedSolAddress ...

Also prints the identity of whatever account came back, because a lookalike
handle (capital I vs lowercase l is common on this site) would produce exactly
the same symptom as a wrong field.
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

# handle -> the address Johan confirmed on the profile page
KNOWN = {
    "FIippingProfits": "DdM1tyCdoEyoxYYmGMjdf5rRPcpmj3UzZTpE7ScuTf7d",
    "onmycheck": "Ay77dkJkbjPCLbhHmwNg5z4WVtP2bMUpjKNnWFo1CuD2",
}

SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def walk(node: Any, path: str = "") -> list[tuple[str, str, str]]:
    """Every wallet-shaped string in the tree, as (path, kind, value)."""
    found: list[tuple[str, str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            found += walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node[:25]):
            found += walk(v, f"{path}[{i}]")
    elif isinstance(node, str):
        if EVM_RE.match(node):
            found.append((path, "evm", node))
        elif SOL_RE.match(node) and not node.startswith("http"):
            found.append((path, "sol", node))
    return found


async def inspect(fomo: FomoClient, handle: str, expected: str | None) -> None:
    print(f"\n{'='*74}\n  @{handle}\n{'='*74}")
    try:
        user = await fomo.user_by_handle(handle)
    except FomoError as exc:
        print(f"  lookup failed: {exc}")
        return

    # Identity first — a lookalike handle explains a 'wrong' wallet just as well.
    print(f"  id           {user.id}")
    print(f"  userHandle   {user.handle!r}   (asked for {handle!r})")
    print(f"  displayName  {user.display_name!r}")
    print(f"  followers    {user.followers:,}   swaps {user.swap_count:,}")
    if user.handle.lower() != handle.lower():
        print("  !! the API returned a DIFFERENT handle than requested")

    sources: dict[str, Any] = {"user": user.raw}
    for label, path in (
        ("leaderboard", f"/v2/users/{user.id}/leaderboard"),
        ("balances", f"/v2/users/{user.id}/balances"),
    ):
        try:
            sources[label] = await fomo._get(path)
        except FomoError as exc:
            sources[label] = {"__error__": str(exc)}

    hits: list[tuple[str, str, str, str]] = []
    for label, blob in sources.items():
        for p, kind, val in walk(blob):
            hits.append((label, p, kind, val))

    if not hits:
        print("  no wallet-shaped strings found at all")
        return

    print(f"\n  {'source':<12}{'path':<38}{'kind':<5}value")
    seen: set[tuple[str, str]] = set()
    match_paths = []
    for label, p, kind, val in hits:
        if (label, p) in seen:
            continue
        seen.add((label, p))
        mark = ""
        if expected and val == expected:
            mark = "  <== THIS IS THE ONE"
            match_paths.append(f"{label}.{p}")
        elif expected and kind == "sol" and p.endswith("address") and val != expected:
            mark = "  (what we show today)"
        print(f"  {label:<12}{p:<38}{kind:<5}{val}{mark}")

    print()
    if expected and match_paths:
        print(f"  -> correct address lives at: {', '.join(match_paths)}")
    elif expected:
        print(f"  -> expected {expected} appears NOWHERE in these responses.")
        print("     Either it's on an endpoint we don't call, or this is a different account.")


async def main() -> int:
    targets = dict(KNOWN)
    for arg in sys.argv[1:]:
        h, _, exp = arg.partition("=")
        targets[h] = exp or None

    client = FomoClient(
        refresh_token=os.getenv("FOMO_PRIVY_REFRESH_TOKEN") or None,
        access_token=os.getenv("FOMO_PRIVY_ACCESS_TOKEN") or None,
    )
    async with client as fomo:
        for handle, expected in targets.items():
            await inspect(fomo, handle, expected)
    print("\nIf a single path matched for both handles, that's the field to switch to.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
