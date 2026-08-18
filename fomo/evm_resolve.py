"""Resolve a FOMO handle to its verified EVM smart wallet.

    python evm_resolve.py --handle Rowdy
    python evm_resolve.py --handle Rowdy --fresh
    python evm_resolve.py --handle onmycheck --wallet 0xb6e0...

This does not use FOMO's ``evmAddress`` field because that address is synthetic.
Only a verified FomoScan identity result is accepted, with its deployment
checked on Base and BNB Chain before it is cached.

If the public index is stale, ``--wallet`` validates an explicitly supplied
handle/address mapping against the chain and caches it as a manual override.
"""

from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace

from dotenv import load_dotenv

load_dotenv()

from fomo_evm import EVM_RPCS, FOMOSCAN_URL, EvmWalletResolver  # noqa: E402
from fomo_wallet import CACHE, rpc_display_name  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--handle", required=True, help="FOMO username")
    parser.add_argument("--fresh", action="store_true", help="ignore the cache")
    parser.add_argument(
        "--wallet",
        help="explicit wallet mapping to deployment-check and cache (index fallback)",
    )
    args = parser.parse_args()

    if args.handle.lower().endswith("--fresh"):
        parser.error("missing a space before --fresh (example: --handle onmycheck --fresh)")

    try:
        import httpx
    except ImportError:
        print("pip install httpx")
        return 1

    print(f"Identity index: {rpc_display_name(FOMOSCAN_URL)}")
    print("EVM checks:")
    for name, url in EVM_RPCS.items():
        print(f"  {name:<5} {rpc_display_name(url)}")

    async with httpx.AsyncClient(timeout=30) as http:
        resolver = EvmWalletResolver(http)
        if args.wallet:
            address = await resolver.verify_and_cache(args.handle, args.wallet)
        else:
            address = await resolver.resolve(
                SimpleNamespace(handle=args.handle), use_cache=not args.fresh
            )

    if not address:
        if args.wallet:
            print(f"\n@{args.handle}: supplied wallet is invalid, unreachable, or not deployed")
        else:
            print(f"\n@{args.handle}: no verified deployed EVM wallet found")
        return 1
    print(f"\n@{args.handle}")
    print(f"  EVM     {address}")
    print(f"  Base    https://basescan.org/address/{address}")
    print(f"  BSC     https://bscscan.com/address/{address}")
    print(f"  cached  {CACHE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
