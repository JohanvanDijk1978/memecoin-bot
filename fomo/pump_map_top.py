"""
pump_map_top.py -- label wallets with their Pump.fun profiles in bulk.

    python pump_map_top.py                          # every wallet we already know
    python pump_map_top.py --token E3i7…pump        # a token's holders
    python pump_map_top.py --dry-run                # what would be learned, no writes
    python pump_map_top.py --csv hunt_out/pump.csv
    python pump_map_top.py --refresh --from-tracks  # re-check tracked profiles

This is `fomo_map_top.py`'s counterpart, and the difference between them is the
whole difference between the two platforms.

`fomo_map_top.py` exists because FOMO does not publish its traders' wallets, so
a wallet has to be *inferred* -- match a published position against the chain's
owner set, then corroborate. Pump publishes the mapping outright: a Pump
profile IS a Solana wallet, and `GET /users/{wallet}` answers directly. There
is nothing to match and nothing to corroborate. What is left is exactly the
expensive part of `/fomo`'s design that still applies: **ask once**.

So this tool is a cache warmer. It gathers candidate wallets from sources the
project already has, deduplicates them, asks Pump about each one exactly once
through `PumpProfileResolver`, and leaves both the profiles and the definitive
absences in `pump_profile_cache.json`. After a run, `/token`, `/wallet` and
`/pumpwallet` name those wallets without a single request.

The yield source worth knowing about is `--from-fomo-cache`: every wallet
`fomo_map_top.py` and the `/fomo` resolvers have proved on chain is a candidate
Pump profile, so the two caches compound.

Stop `fomo_bot.py` first if it is running against the same cache file.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

load_dotenv()

from fomo_wallet import SOLANA_ADDRESS_RE, _load_cache  # noqa: E402
from pump_api import PumpClient  # noqa: E402
from pump_evm import PumpEvmResolver  # noqa: E402
from pump_profiles import (  # noqa: E402
    CACHE_FILE,
    MISSING,
    RESOLVED,
    UNAVAILABLE,
    PumpProfileResolver,
    normalize_term,
)
from rpc_config import env_rpc_urls  # noqa: E402

log = logging.getLogger("pump.map")

SOLANA_RPCS = env_rpc_urls(
    "SOLANA_RPC", "SOLANA_RPC_FALLBACKS", "https://api.mainnet-beta.solana.com"
)
PUMP_EVM_CACHE_FILE = Path("pump_evm_cache.json")
PUMP_TRACK_FILE = Path("pump_tracks.json")


# ------------------------------------------------------------------- seeds


def _solana(values: Iterable[str]) -> list[str]:
    """Keep only plausible Solana addresses, in first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value or "").strip()
        if not SOLANA_ADDRESS_RE.fullmatch(clean) or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def wallets_from_fomo_cache(path: str | Path | None = None) -> list[str]:
    """Every Solana wallet `/fomo` has proved on chain.

    These are the highest-value candidates in the project: a wallet that
    survived FOMO's corroboration is a real trader, and real traders are the
    ones with Pump profiles.
    """
    cache = _load_cache(path) if path else _load_cache()
    return _solana(
        entry.get("wallet") for entry in cache.values() if isinstance(entry, dict)
    )


def wallets_from_pump_evm(path: str | Path = PUMP_EVM_CACHE_FILE) -> list[str]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = raw.get("matches") if isinstance(raw, dict) else None
    if not isinstance(rows, dict):
        return []
    return _solana(rows.keys())


def wallets_from_tracks(path: str | Path = PUMP_TRACK_FILE) -> list[str]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    tracks = raw.get("tracks") if isinstance(raw, dict) else raw
    if not isinstance(tracks, dict):
        return []
    found: list[str] = []
    for entry in tracks.values():
        if isinstance(entry, dict):
            found.append(str(entry.get("userId") or entry.get("wallet") or ""))
    return _solana(found)


def wallets_from_file(path: str | Path) -> list[str]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return []
    return _solana(
        part
        for line in text.splitlines()
        for part in line.replace(",", " ").split()
    )


async def token_holder_wallets(
    http: Any, mint: str, limit: int
) -> list[str]:
    """Owner wallets of a Solana token, preferring the complete holder set.

    Helius DAS `getTokenAccounts` pages every holder. `getTokenLargestAccounts`
    -- which `TokenIntelligenceClient` uses for the /token card -- stops at 20,
    which is the same trap `fomo_map_top.py` documents. Use the complete set
    when a Helius endpoint is configured and fall back otherwise.
    """
    from fomo_wallet import WalletResolver

    resolver = WalletResolver(http, SOLANA_RPCS)
    if any("helius" in url.lower() for url in resolver.rpc.urls):
        totals = await resolver._helius_token_balances(mint)
        ordered = sorted(totals.items(), key=lambda item: item[1], reverse=True)
        return _solana(owner for owner, _amount in ordered[:limit] if owner)

    from token_intelligence import TokenIntelligenceClient

    tokens = TokenIntelligenceClient(http, SOLANA_RPCS)
    holders = await tokens._solana_holders(mint, min(limit, 20))
    log.info("no Helius RPC configured: %s limited to the top %d holder(s)",
             mint, len(holders))
    return _solana(holder.address for holder in holders)


# -------------------------------------------------------------------- main


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--token", action="append", default=[],
                        metavar="MINT",
                        help="label the holders of this Solana token "
                             "(repeatable)")
    parser.add_argument("--holders", type=int, default=100,
                        help="how many holders per token (default 100)")
    parser.add_argument("--wallets", default="",
                        help="comma or space separated wallets to label")
    parser.add_argument("--file", dest="wallet_file", default="",
                        help="read wallets from this file, one per line")
    parser.add_argument("--from-fomo-cache", action="store_true",
                        help="seed from every wallet in wallet_cache.json")
    parser.add_argument("--from-pump-evm", action="store_true",
                        help="seed from pump_evm_cache.json")
    parser.add_argument("--from-tracks", action="store_true",
                        help="seed from pump_tracks.json")
    parser.add_argument("--refresh", action="store_true",
                        help="re-ask Pump about wallets already cached")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be learned without writing")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many candidate wallets")
    parser.add_argument("--cache", default=CACHE_FILE,
                        help=f"profile cache path (default {CACHE_FILE})")
    parser.add_argument("--csv", dest="csv_path", default="",
                        help="write wallet, username, status, x to this path")
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

    explicit = (args.from_fomo_cache or args.from_pump_evm or args.from_tracks
                or args.wallets or args.wallet_file or args.token)

    rows: list[dict[str, str]] = []
    async with httpx.AsyncClient(timeout=60) as http:
        pump = PumpClient(http)
        evm = PumpEvmResolver(http, PUMP_EVM_CACHE_FILE)
        resolver = PumpProfileResolver(
            pump, args.cache, evm=evm, persist=not args.dry_run
        )
        before = resolver.counts()
        print(f"cache: {before['total']} wallet(s) known "
              f"({before['found']} with a profile, {before['missing']} without)")

        candidates: list[str] = []
        sources: list[tuple[str, list[str]]] = []
        for mint in args.token:
            try:
                found = await token_holder_wallets(http, mint.strip(), args.holders)
            except Exception as exc:
                print(f"  token {mint[:12]}…  holder query failed: {str(exc)[:70]}")
                continue
            sources.append((f"token {mint[:12]}…", found))
        if args.from_fomo_cache or not explicit:
            sources.append(("wallet_cache.json", wallets_from_fomo_cache()))
        if args.from_pump_evm or not explicit:
            sources.append(("pump_evm_cache.json", wallets_from_pump_evm()))
        if args.from_tracks or not explicit:
            sources.append(("pump_tracks.json", wallets_from_tracks()))
        if args.wallets:
            sources.append(("--wallets", _solana(
                args.wallets.replace(",", " ").split())))
        if args.wallet_file:
            sources.append((args.wallet_file, wallets_from_file(args.wallet_file)))

        seen: set[str] = set()
        for name, found in sources:
            fresh = [wallet for wallet in found
                     if normalize_term(wallet) not in seen]
            seen.update(normalize_term(wallet) for wallet in fresh)
            print(f"  {len(found):>5} from {name}"
                  f"{f' ({len(fresh)} new to this run)' if len(fresh) != len(found) else ''}")
            candidates.extend(fresh)

        if not candidates:
            print("\nno candidate wallets. Pass --token, --wallets or --file.")
            return 2

        pending = candidates if args.refresh else [
            wallet for wallet in candidates
            if resolver.cache.get(wallet) is None
        ]
        print(f"\n{len(candidates)} distinct wallet(s), "
              f"{len(candidates) - len(pending)} already answered by the cache, "
              f"{len(pending)} to ask Pump about")
        if args.limit > 0:
            pending = pending[:args.limit]
            print(f"  limited to {len(pending)}")

        # One request per wallet, bounded concurrency, deduplicated by key --
        # the resolver's per-key lock means a repeat inside this batch is free.
        results = await resolver.lookup_many(pending, fresh=args.refresh)

        named = [result for result in results.values() if result.found]
        absent = [result for result in results.values()
                  if result.status == MISSING]
        broken = [result for result in results.values()
                  if result.status == UNAVAILABLE]

        for result in sorted(named, key=lambda item: item.profile.username.casefold()):
            profile = result.profile
            if result.status == RESOLVED:
                x_handle = f" · x/{profile.x_username}" if profile.x_username else ""
                print(f"  + @{profile.username:<20} {profile.address}{x_handle}")

        for key, result in results.items():
            profile = result.profile
            rows.append({
                "wallet": profile.address if profile else key,
                "username": profile.username if profile else "",
                "status": result.status,
                "x": (profile.x_username or "") if profile else "",
                "followers": str(profile.followers) if profile else "",
                "error": result.error or "",
            })

        after = resolver.counts()
        print(f"\n{len(named)} profile(s), {len(absent)} wallet(s) with none, "
              f"{len(broken)} lookup(s) failed"
              f"{' (dry run, nothing written)' if args.dry_run else ''}")
        print(f"  {resolver.requests} request(s) made for {len(pending)} wallet(s)")
        print(f"  cache now holds {after['total']} wallet(s) "
              f"({after['found']} with a profile, {after['missing']} without, "
              f"{after['aliases']} username alias(es))")
        if broken:
            print("  failures were NOT cached as absences; re-run to retry them")

    if args.csv_path and rows:
        path = Path(args.csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["wallet", "username", "status", "x", "followers", "error"],
            )
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
