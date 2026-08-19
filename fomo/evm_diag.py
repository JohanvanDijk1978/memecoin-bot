"""Explain exactly why transaction-backed EVM discovery did or did not resolve.

    python evm_diag.py --handle insentos --expect 0x93c006f2051cb72168cf8c27cafe0fb2d71682c8

This drives the resolver's own matching functions rather than reimplementing
them, so it cannot drift from the bot, and reports where a known-correct wallet
is lost:

    evidence -> token selection -> transfer fetch -> time window
             -> amount tolerance -> relay filter -> nearest-4 cut
             -> USD validation -> >=2 transactions -> unambiguous ranking
             -> deployment check

Every fetched payload is written to ``hunt_out/evm_diag_<handle>.json`` so the
analysis can be repeated offline without network access.

Stop ``fomo_bot.py`` first: both use the same persistent Chrome profile.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from fomo_api import FomoClient  # noqa: E402
from fomo_evm import (  # noqa: E402
    CHAIN_NAMES,
    EVM_DISCOVERY_TOKENS,
    EvmTransfer,
    EvmWalletResolver,
    _relays_amount,
    evidence_windows,
    evm_trade_evidence,
    evm_trade_ids,
    match_tolerance,
    match_window,
    select_evidence_groups,
    transfer_candidates,
)
from fomo_features import fetch_trader_stats  # noqa: E402
from rpc_config import rpc_display_name  # noqa: E402

OUT = Path("hunt_out")


def ts(value: int | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def short(value: str, head: int = 10, tail: int = 6) -> str:
    return value if len(value) <= head + tail + 1 else f"{value[:head]}…{value[-tail:]}"


def chain_of(chain_id: int) -> str:
    return CHAIN_NAMES.get(chain_id, str(chain_id))


def dec(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:,.6f}".rstrip("0").rstrip(".")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--handle", required=True)
    parser.add_argument("--expect", default="",
                        help="known-correct wallet to trace through every gate")
    parser.add_argument("--token", action="append", default=[],
                        help="token address that must appear in the evidence (repeatable)")
    parser.add_argument("--details", action="store_true",
                        help="always fetch /trades/{id} detail rows, like a cold profile")
    parser.add_argument("--verbose", action="store_true", help="library debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    expect = args.expect.strip().lower()
    want_tokens = {token.strip().lower() for token in args.token if token.strip()}
    dump: dict[str, Any] = {"handle": args.handle, "expect": expect}

    try:
        import httpx
    except ImportError:
        print("pip install httpx")
        return 1

    async with FomoClient() as fomo, httpx.AsyncClient(timeout=60) as http:
        resolver = EvmWalletResolver(http)
        print("EVM transfer sources:")
        for name, urls in resolver.rpcs.items():
            alchemy = [url for url in urls if "alchemy.com" in url.lower()]
            print(f"  {name:<10} {len(urls)} rpc(s), "
                  f"{'alchemy ' + rpc_display_name(alchemy[0]) if alchemy else 'NO ALCHEMY'}"
                  f"  -> getAssetTransfers {'yes' if alchemy else 'no'}")
        print()

        user = await fomo.resolve(args.handle)
        print(f"@{user.handle}  id={user.id}")
        stats = await fetch_trader_stats(fomo, user.id)

        # --- stage 1: evidence -------------------------------------------------
        details: tuple[Any, ...] = ()
        preliminary_evidence = evm_trade_evidence(stats.raw_swaps, stats.raw_trades)
        exact = sum(not item.aggregate for item in preliminary_evidence)
        if args.details or exact < 2:
            detail_ids = evm_trade_ids(stats.raw_trades)
            print(f"detail fetch: {len(detail_ids)} trade id(s) "
                  f"(exact swap evidence from panels = {exact})")
            if detail_ids:
                results = await fomo.trade_details(detail_ids, background=False)
                details = tuple(item for item in results
                                if not isinstance(item, Exception))
        evidence = evm_trade_evidence(stats.raw_swaps, stats.raw_trades, details)
        dump["evidence"] = [item.__dict__ | {"token_amount": str(item.token_amount),
                                             "usd_amount": str(item.usd_amount)}
                            for item in evidence]

        print(f"\n=== 1. evidence: {len(evidence)} item(s) "
              f"({sum(not i.aggregate for i in evidence)} exact, "
              f"{sum(i.aggregate for i in evidence)} aggregate) ===")
        for index, item in enumerate(evidence):
            mark = " <-- wanted" if item.token in want_tokens else ""
            print(f"  [{index:>2}] {chain_of(item.chain_id):<9} {short(item.token)} "
                  f"{item.direction:<4} {ts(item.created_at)} "
                  f"amount={dec(item.token_amount):>18} usd={dec(item.usd_amount):>10} "
                  f"{'aggregate' if item.aggregate else 'swap':<9}{mark}")

        missing = want_tokens - {item.token for item in evidence}
        if missing:
            print("\n  !! these tokens produced NO evidence at all:")
            for token in sorted(missing):
                print(f"     {token}")
            for source, payload in (("swaps", stats.raw_swaps),
                                    ("trades", stats.raw_trades)):
                text = json.dumps(payload or {}).lower()
                for token in sorted(missing):
                    print(f"     raw {source:<6} contains {short(token)}: "
                          f"{'YES' if token in text else 'no'}")

        # --- stage 2: token selection and transfer fetch -----------------------
        groups = select_evidence_groups(evidence)
        every_token = {(item.chain_id, item.token) for item in evidence}
        windows = evidence_windows(groups)
        print(f"\n=== 2. token selection: {len(groups)} of {len(every_token)} "
              f"token(s) searched (cap {EVM_DISCOVERY_TOKENS}), "
              f"{len(windows)} window(s) ===")
        skipped = every_token - set(groups)
        for key in sorted(skipped, key=lambda entry: entry[1]):
            mark = "  <-- WANTED, NOT SEARCHED" if key[1] in want_tokens else ""
            if mark or args.verbose:
                print(f"  skipped {chain_of(key[0]):<9} {short(key[1])}{mark}")

        transfers_by_token: dict[tuple[int, str], list[EvmTransfer]] = defaultdict(list)
        for key, start, end in windows:
            try:
                rows = await resolver._transfers_for_token(key[0], key[1], start, end)
            except Exception as exc:
                rows = []
                print(f"  {chain_of(key[0]):<9} {short(key[1])} "
                      f"{ts(start)}..{ts(end)} FETCH FAILED: {exc!r}")
            transfers_by_token[key].extend(rows)
            times = [row.created_at for row in rows]
            mine = sum(expect in (row.sender, row.recipient) for row in rows) if expect else 0
            print(f"  {chain_of(key[0]):<9} {short(key[1])} "
                  f"window {ts(start)}..{ts(end)} -> {len(rows):>5} transfer(s) "
                  f"covering {ts(min(times) if times else None)}..{ts(max(times) if times else None)}"
                  + (f", {mine} touching {short(expect)}" if expect else ""))
            if rows and (min(times) > start + 60 or max(times) < end - 60):
                print("     note: returned rows do not span the whole window "
                      "(page cap reached — a very busy token)")
            if not rows:
                print("     !! no transfer source returned rows for this window")
        for key, rows in transfers_by_token.items():
            unique = {(row.transaction, row.sender, row.recipient, row.token_amount): row
                      for row in rows}
            transfers_by_token[key] = list(unique.values())

        dump["transfers"] = {
            f"{key[0]}:{key[1]}": [row.__dict__ | {"token_amount": str(row.token_amount)}
                                   for row in rows]
            for key, rows in transfers_by_token.items()
        }

        if expect:
            print(f"\n  transfers involving {expect}:")
            found = False
            for key, rows in transfers_by_token.items():
                for row in rows:
                    if expect not in (row.sender, row.recipient):
                        continue
                    found = True
                    side = "recv" if row.recipient == expect else "sent"
                    print(f"    {chain_of(key[0]):<9} {short(key[1])} {ts(row.created_at)} "
                          f"{side} {dec(row.token_amount):>18}  tx {short(row.transaction)}")
            if not found:
                print("    NONE — the expected wallet does not appear in any fetched "
                      "transfer.")

        # --- stage 3: matching, using the resolver's own function --------------
        kept = transfer_candidates(evidence, transfers_by_token)
        kept_keys = {(item.evidence_id, transfer.transaction, wallet)
                     for item, transfer, wallet in kept}
        print(f"\n=== 3. window + amount matching ({len(kept)} candidate(s) kept) ===")
        for index, item in enumerate(evidence):
            rows = transfers_by_token.get((item.chain_id, item.token), [])
            window, tolerance = match_window(item), match_tolerance(item)
            siblings: dict[str, list[EvmTransfer]] = defaultdict(list)
            for row in rows:
                siblings[row.transaction].append(row)
            shown: list[tuple[int, EvmTransfer, str, str]] = []
            in_window = 0
            for row in rows:
                distance = abs(row.created_at - item.created_at)
                if distance > window:
                    continue
                in_window += 1
                wallet = row.recipient if item.direction == "buy" else row.sender
                if not wallet or abs(row.token_amount - item.token_amount) > tolerance:
                    continue
                if _relays_amount(siblings[row.transaction], wallet,
                                  item.token_amount, tolerance, item.direction):
                    verdict = "RELAY"
                elif (item.evidence_id, row.transaction, wallet) in kept_keys:
                    verdict = "kept"
                else:
                    verdict = "cut"
                shown.append((distance, row, wallet, verdict))
            shown.sort(key=lambda entry: entry[0])
            print(f"  [{index:>2}] {item.direction:<4} {short(item.token)} "
                  f"{ts(item.created_at)} want {dec(item.token_amount)} "
                  f"(+/-{dec(tolerance)}, +/-{window}s): {in_window} in window, "
                  f"{sum(v == 'kept' for *_, v in shown)} kept, "
                  f"{sum(v == 'RELAY' for *_, v in shown)} relay, "
                  f"{sum(v == 'cut' for *_, v in shown)} cut")
            for distance, row, wallet, verdict in shown[:8]:
                flag = " <-- EXPECTED" if expect and wallet == expect else ""
                print(f"         {distance:>4}s {wallet} {dec(row.token_amount):>18} "
                      f"tx {short(row.transaction)}  {verdict}{flag}")

        # --- stage 4: USD validation ------------------------------------------
        print(f"\n=== 4. USD validation ({len(kept)} candidate(s)) ===")
        rejected: list[str] = []
        matches: list[tuple[Any, EvmTransfer, str, bool | None]] = []
        for item, row, wallet in kept:
            usd_matched: bool | None = None
            if item.usd_amount is not None:
                values = await resolver._transaction_quote_values(
                    item.chain_id, row.transaction
                )
                if values:
                    tol = max(Decimal("10"), item.usd_amount * Decimal("0.20"))
                    usd_matched = any(abs(v - item.usd_amount) <= tol for v in values)
                    if not usd_matched:
                        note = (f"  rejected {wallet} tx {short(row.transaction)}: "
                                f"want ${dec(item.usd_amount)} +/-{dec(tol)}, "
                                f"tx stable legs {[str(v) for v in values]}")
                        if expect and wallet == expect:
                            note += "   <-- EXPECTED WALLET REJECTED HERE"
                        rejected.append(note)
                        continue
            matches.append((item, row, wallet, usd_matched))
        print(f"  {len(matches)} survived, {len(rejected)} rejected on USD mismatch")
        for note in rejected[:12]:
            print(note)

        # --- stage 5: ranking --------------------------------------------------
        by_wallet: dict[str, list[tuple[Any, EvmTransfer, bool | None]]] = defaultdict(list)
        for item, row, wallet, usd_matched in matches:
            by_wallet[wallet].append((item, row, usd_matched))

        print(f"\n=== 5. ranking ({len(by_wallet)} candidate wallet(s)) ===")
        print(f"  {'wallet':<44} {'tokens':>6} {'usd':>4} {'txs':>4}  eligible")
        ranked: list[tuple[tuple[int, int, int], str]] = []
        rows_out = []
        for wallet, wallet_matches in by_wallet.items():
            transactions = {row.transaction for _, row, _ in wallet_matches}
            tokens = {(item.chain_id, item.token) for item, _, _ in wallet_matches}
            usd_matches = sum(flag is True for _, _, flag in wallet_matches)
            eligible = len(transactions) >= 2
            if eligible:
                ranked.append(((len(tokens), usd_matches, len(transactions)), wallet))
            mark = " <-- EXPECTED" if expect and wallet == expect else ""
            rows_out.append((len(tokens), usd_matches, len(transactions), wallet,
                             eligible, mark))
        for tokens, usd, txs, wallet, eligible, mark in sorted(rows_out, reverse=True):
            print(f"  {wallet:<44} {tokens:>6} {usd:>4} {txs:>4}  "
                  f"{'yes' if eligible else 'NO (needs >=2 tx)':<18}{mark}")
        dump["ranking"] = [
            {"wallet": w, "tokens": t, "usd": u, "transactions": x, "eligible": e}
            for t, u, x, w, e, _ in rows_out
        ]

        ranked.sort(reverse=True)
        print("\n=== 6. verdict ===")
        if not ranked:
            print("  FAIL: no wallet explained two independent transactions.")
        elif len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            print(f"  FAIL (tie): {ranked[0][1]} and {ranked[1][1]} both score "
                  f"{ranked[0][0]} — the resolver refuses to guess.")
        else:
            print(f"  RESOLVES: {ranked[0][1]} with score {ranked[0][0]}")
            if expect and ranked[0][1] != expect:
                print(f"  ...but that is NOT the expected {expect}")
            deployed, checked = await resolver._deployed_chains(ranked[0][1])
            print(f"  deployment: checked {checked or '-'}, code on {deployed or 'none'}")

        if expect:
            deployed, checked = await resolver._deployed_chains(expect)
            print(f"  expected wallet deployment: checked {checked or '-'}, "
                  f"code on {deployed or 'none'}")

    OUT.mkdir(exist_ok=True)
    path = OUT / f"evm_diag_{args.handle.lower()}.json"
    path.write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
