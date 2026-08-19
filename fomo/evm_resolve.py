"""Resolve a FOMO handle to its verified EVM smart wallet.

    python evm_resolve.py --handle Rowdy
    python evm_resolve.py --handle onmycheck --wallet 0xb6e0...

This does not use FOMO's ``evmAddress`` field because that address is synthetic.
Without ``--wallet`` the command displays an existing cached mapping. With
``--wallet`` it validates an explicitly supplied smart-contract address on the
configured EVM chains and caches it as a manual mapping.
"""

from __future__ import annotations

import argparse
import asyncio
from dotenv import load_dotenv

load_dotenv()

from fomo_evm import EVM_RPCS, EvmWalletResolver, cached_evm_wallet  # noqa: E402
from fomo_wallet import CACHE, rpc_display_name  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--handle", required=True, help="FOMO username")
    parser.add_argument(
        "--wallet",
        help="explicit wallet mapping to deployment-check and cache",
    )
    args = parser.parse_args()

    try:
        import httpx
    except ImportError:
        print("pip install httpx")
        return 1

    print("EVM checks:")
    for name, urls in EVM_RPCS.items():
        for index, url in enumerate(urls, 1):
            suffix = f" fallback {index - 1}" if index > 1 else ""
            print(f"  {name:<10} {rpc_display_name(url)}{suffix}")

    async with httpx.AsyncClient(timeout=30) as http:
        resolver = EvmWalletResolver(http)
        if args.wallet:
            address = await resolver.verify_and_cache(args.handle, args.wallet)
        else:
            address = cached_evm_wallet(args.handle)

    if not address:
        if args.wallet:
            print(f"\n@{args.handle}: supplied wallet is invalid, unreachable, or not deployed")
        else:
            print(f"\n@{args.handle}: no cached EVM wallet; supply --wallet to verify one")
        return 1
    print(f"\n@{args.handle}")
    print(f"  EVM     {address}")
    print(f"  Base    https://basescan.org/address/{address}")
    print(f"  BSC     https://bscscan.com/address/{address}")
    print(f"  cached  {CACHE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
