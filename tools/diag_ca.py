#!/usr/bin/env python3
"""
Live diagnosis for one CA: why did its ping come out bare?

    python3 tools/diag_ca.py 0xbee56417ac7a217fb720ff9a16b464949ec3174a
    python3 tools/diag_ca.py 9XaVY3ugtSG3HLKGHh3uWZWJ8EF9CoAyTWdSxB8kUFB3

Needs the network — this is the one to run on the box. It asks the same two
questions the scraper asks, in the same order, and prints both answers plus
the alert they produce:

  1. Dexscreener — does this token have an indexed pool?
  2. each chain's RPC — is this a token, a wallet, or something else?

It sends nothing to Telegram and writes nothing to the history file.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import mention_store                     # noqa: E402

# Read-only: never let a diagnostic rewrite the real history.
mention_store.HISTORY_FILE = str(Path(tempfile.mkdtemp()) / "ca_history.json")

from src import ca_enrich as E                    # noqa: E402
from src import ca_probe as P                     # noqa: E402
from src.utils import build_trading_links         # noqa: E402


async def diagnose(address: str) -> int:
    chain = "ETH" if address.lower().startswith("0x") else "SOL"
    print(f"\naddress   {address}")
    print(f"detected  {chain}")

    print("\n1. Dexscreener")
    token = await E.fetch_token_quick(address, chain)
    if token:
        print(f"   ✅ {token['name']} (${token['symbol']}) on {token['chain_id']} "
              f"@ {token['dex_id']}")
        print(f"      mcap ${token['market_cap']:,.0f}   age {token['age']}")
        print("      → the ping would be fully enriched; nothing to explain")
    else:
        print("   ❌ no indexed pool (or the lookup failed — see the log line above)")

    print("\n2. Chains")
    if chain != "ETH":
        print("   – skipped: classification is EVM-only")
        probe = None
    else:
        for name in P.PROBE_CHAINS:
            urls = P._rpc_urls(name)
            source = "configured" if P.evm_rpc(name) else ("public" if urls else "NO ENDPOINT")
            print(f"   {name:<10} {source}")
        probe = await P.classify_evm(address)
        print(f"\n   verdict   {probe['kind']}"
              + (f" on {probe['chain']}" if probe["chain"] else "")
              + (" (EIP-7702 delegated)" if probe["delegated"] else ""))
        if probe["symbol"] or probe["name"]:
            print(f"   metadata  {probe['name']} (${probe['symbol']}) "
                  f"decimals={probe['decimals']} supply={probe['supply']:,.0f}")
        print(f"   answered  {', '.join(probe['chains_answered']) or 'none — every RPC failed'}")
        print(f"   has code  {', '.join(probe['chains_with_code']) or 'nowhere'}")

    actual_chain = ((token or {}).get("chain_id") or (probe or {}).get("chain")
                    or ("solana" if chain == "SOL" else "ethereum"))
    will_recheck = not token and (probe or {}).get("kind", "unknown") in ("token", "unknown")

    print("\n3. The alert")
    if token:
        why = "not needed — the ping is already enriched"
    elif will_recheck:
        why = f"queued — retries at {', '.join(str(int(d)) + 's' for d in E.RECHECK_DELAYS)}"
    else:
        why = "not queued — market data will never arrive for this address"
    print(f"   re-check  {why}")
    msg, image = E.build_ca_message(
        header="👤 *someone* in *some group*", address=address, chain=chain, token=token,
        trading_links=build_trading_links(actual_chain, address), probe=probe,
        pending_note=E.PENDING_NOTE if will_recheck else "",
    )
    print("─" * 62)
    print(msg)
    print("─" * 62)
    if image:
        print(f"image     {image}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    return asyncio.run(diagnose(sys.argv[1].strip()))


if __name__ == "__main__":
    raise SystemExit(main())
