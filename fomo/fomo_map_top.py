"""
fomo_map_top.py -- label the top FOMO traders' wallets in bulk.

    python fomo_map_top.py                       # top 100, all-time
    python fomo_map_top.py --top 100 --period 24h
    python fomo_map_top.py --dry-run             # what would be learned, no writes
    python fomo_map_top.py --csv hunt_out/top100.csv

Wallet discovery per handle is expensive: a sponsor index, a mint scan, maybe a
block scan. This does not do that. `/v2/leaderboard?limit=100` returns each
trader's `topHoldings` -- token address, network and the EXACT amount held --
and `/hodlers/top` returns the same for up to ~48 holders of any token. Both
are amounts FOMO itself publishes. The chain publishes the owner of every
balance. One unambiguous amount match is a wallet, for the price of a holder
query per token rather than a scan per trader.

Yield compounds: every token a listed trader holds is also queried through
`/hodlers/top`, so traders far outside the top 100 get labelled as a side
effect of being in the same token.

Matches are written through the same corroboration gates `/token` uses --
Solana needs a FOMO-sponsored transaction on the wallet, EVM needs contract
code on the chain whose token is held -- so nothing lands in the cache that
`/fomo` would not have accepted on its own.

Stop `fomo_bot.py` first: both use the same persistent Chrome profile.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from fomo_api import FomoClient, FomoError, fmt_usd  # noqa: E402
from fomo_evm import EvmWalletResolver  # noqa: E402
from fomo_hodlers import (  # noqa: E402
    CHAIN_NAMES_BY_ID,
    FomoHolder,
    confident_matches,
    parse_token_holders,
)
from fomo_wallet import (  # noqa: E402
    SOLANA_NETWORK_ID,
    WalletResolver,
    _load_cache,
    cached_wallet,
)
from fomo_evm import cached_evm_wallet  # noqa: E402
from rpc_config import env_rpc_urls  # noqa: E402

log = logging.getLogger("fomo.map")

SOLANA_RPCS = env_rpc_urls(
    "SOLANA_RPC", "SOLANA_RPC_FALLBACKS", "https://api.mainnet-beta.solana.com"
)


def leaderboard_holdings(users: list[Any]) -> dict[tuple[str, int], list[FomoHolder]]:
    """(token, networkId) -> the listed traders holding it, with exact amounts.

    `topHoldings` is on the leaderboard row already, so this costs no request
    beyond the leaderboard call itself.
    """
    grouped: dict[tuple[str, int], list[FomoHolder]] = defaultdict(list)
    for user in users:
        raw = getattr(user, "raw", user) or {}
        handle = str(raw.get("userHandle") or "")
        if not handle:
            continue
        for holding in raw.get("topHoldings") or []:
            if not isinstance(holding, dict):
                continue
            token = str(holding.get("tokenAddress") or "").strip()
            try:
                network = int(holding.get("networkId"))
                amount = float(holding.get("humanAmount"))
            except (TypeError, ValueError):
                continue
            if not token or amount <= 0:
                continue
            grouped[(token, network)].append(FomoHolder(
                handle=handle,
                display_name=str(raw.get("displayName") or handle),
                user_id=str(raw.get("id") or ""),
                amount=amount,
                value_usd=_float(holding.get("value")),
                pnl_usd=_float(holding.get("pnl")),
            ))
    return dict(grouped)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def solana_owners(
    resolver: WalletResolver, mint: str
) -> list[tuple[str, float]]:
    """(owner, human balance) for every holder of a Solana mint.

    Helius DAS `getTokenAccounts` pages the full holder set, unlike
    `getTokenLargestAccounts`, which stops at 20 and would silently miss any
    trader below that line.
    """
    supply = await resolver.rpc("getTokenSupply", [mint])
    decimals = ((supply or {}).get("value") or {}).get("decimals")
    if decimals is None:
        return []
    scale = Decimal(10) ** int(decimals)
    totals = await resolver._helius_token_balances(mint)
    return [(owner, float(Decimal(raw) / scale)) for owner, raw in totals.items()]


async def evm_owners(
    tokens: Any, token: str, chain: str, limit: int = 100
) -> list[tuple[str, float]]:
    """(owner, balance) for an EVM token, via the existing holder adapters."""
    rows = await tokens._holders(token, chain, limit)
    return [(row.address, float(row.balance)) for row in rows]


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--top", type=int, default=100,
                        help="how many leaderboard traders to seed from")
    parser.add_argument("--period", default="",
                        help="leaderboard period: 24h, 7d, 30d (default all-time)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be learned without writing")
    parser.add_argument("--skip-hodlers", action="store_true",
                        help="use only leaderboard holdings, not /hodlers/top")
    parser.add_argument("--csv", dest="csv_path", default="",
                        help="write handle, wallet, chain, source to this path")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        import httpx
    except ImportError:
        print("httpx is required: pip install httpx")
        return 2

    from token_intelligence import TokenIntelligenceClient

    learned: list[dict[str, str]] = []
    async with FomoClient() as fomo, httpx.AsyncClient(timeout=60) as http:
        wallets = WalletResolver(http, SOLANA_RPCS)
        evm_wallets = EvmWalletResolver(http)
        tokens_client = TokenIntelligenceClient(http, SOLANA_RPCS)

        users = await fomo.leaderboard(args.period or None, limit=args.top)
        print(f"leaderboard: {len(users)} trader(s)")
        already = sum(
            1 for user in users
            if cached_wallet(user.handle.lower())
            or cached_evm_wallet(user.handle.lower())
        )
        print(f"  {already} already have a cached wallet, "
              f"{len(users) - already} unknown")

        grouped = leaderboard_holdings(users)
        print(f"  {len(grouped)} distinct token(s) across their top holdings\n")

        for index, ((token, network), seeded) in enumerate(sorted(grouped.items()), 1):
            chain = CHAIN_NAMES_BY_ID.get(network, str(network))
            holders = list(seeded)

            # Every holder of this token, not just the listed traders -- one
            # extra call multiplies what the token can teach us.
            if not args.skip_hodlers:
                try:
                    payload = await fomo.token_holders(token, network)
                    extra, total = parse_token_holders(payload)
                    known = {item.handle.lower() for item in holders}
                    holders += [item for item in extra
                                if item.handle.lower() not in known]
                except (FomoError, asyncio.TimeoutError) as exc:
                    log.debug("hodlers for %s failed: %s", token, exc)

            try:
                if network == SOLANA_NETWORK_ID:
                    onchain = await solana_owners(wallets, token)
                else:
                    onchain = await evm_owners(tokens_client, token, chain)
            except Exception as exc:
                print(f"{index:>3}. {chain:<9} {token[:12]}…  "
                      f"holder query failed: {str(exc)[:70]}")
                continue

            matches = confident_matches(holders, onchain)
            fresh = {
                wallet: holder.handle for wallet, holder in matches.items()
                if not (cached_wallet(holder.handle.lower())
                        if network == SOLANA_NETWORK_ID
                        else cached_evm_wallet(holder.handle.lower()))
            }
            print(f"{index:>3}. {chain:<9} {token[:12]}…  "
                  f"{len(holders):>3} FOMO holder(s), {len(onchain):>5} on-chain, "
                  f"{len(matches):>3} matched, {len(fresh):>3} new")

            if not fresh or args.dry_run:
                for wallet, handle in fresh.items():
                    learned.append({"handle": handle, "wallet": wallet,
                                    "chain": chain, "source": "dry-run"})
                continue

            if network == SOLANA_NETWORK_ID:
                written = await wallets.adopt_holder_matches(fresh, token=token)
                source = "hodlers+amount+fomo-sponsor"
            else:
                written = await evm_wallets.adopt_holder_matches(
                    fresh, token=token, chain=chain.lower()
                )
                source = "hodlers+amount+rpc"
            for wallet, handle in written.items():
                learned.append({"handle": handle, "wallet": wallet,
                                "chain": chain, "source": source})
                print(f"      + @{handle} -> {wallet}")

        cache = _load_cache()
        named = sum(1 for user in users
                    if cached_wallet(user.handle.lower())
                    or cached_evm_wallet(user.handle.lower()))
        print(f"\n{len(learned)} new mapping(s) "
              f"{'(dry run, nothing written)' if args.dry_run else 'cached'}")
        print(f"top {len(users)}: {named} labelled, {len(users) - named} still unknown")
        print(f"wallet_cache.json now holds {len(cache)} handle(s)")

        unresolved = [user for user in users
                      if not cached_wallet(user.handle.lower())
                      and not cached_evm_wallet(user.handle.lower())]
        if unresolved:
            print("\nstill unknown (no top holding matched an on-chain owner):")
            for user in unresolved[:20]:
                print(f"  @{user.handle:<20} {fmt_usd(user.total_volume)} volume")
            if len(unresolved) > 20:
                print(f"  … and {len(unresolved) - 20} more")
            print("  these need the scan path: "
                  "python fomo_resolve_diag.py <handle> --fresh")

    if args.csv_path and learned:
        path = Path(args.csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["handle", "wallet", "chain", "source"]
            )
            writer.writeheader()
            writer.writerows(learned)
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
