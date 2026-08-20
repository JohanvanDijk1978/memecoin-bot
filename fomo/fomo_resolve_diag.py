"""
fomo_resolve_diag.py -- why did /fomo not show a Solana and/or EVM wallet?

    python fomo_resolve_diag.py Rowdy
    python fomo_resolve_diag.py Rowdy frankdegods Konito --fresh
    python fomo_resolve_diag.py Rowdy --chain solana --no-deep -v
    python fomo_resolve_diag.py Rowdy --json hunt_out/diag_rowdy.json
    python fomo_resolve_diag.py Rowdy unipcs asta --csv hunt_out/wallets.csv

One handle in, one verdict per chain out, plus the stage that lost the wallet:

    cache -> profile -> panels -> evidence -> discovery -> verification

It drives the SAME functions `/fomo` uses -- `WalletResolver.resolve()`,
`WalletResolver.resolve_from_balances()`, `EvmWalletResolver.resolve()` and
`evm_trade_evidence()` -- so it cannot drift from the bot, and it captures the
`fomo.wallet` / `fomo.evm` log records that already carry the precise reason.
Those reasons are invisible in production because the bot only writes them at
INFO/DEBUG to its own log.

This is the triage tool: it says WHICH stage failed for both chains at once.
When the answer is "EVM discovery found no matching transfer", `evm_diag.py
--handle X --expect 0x...` is the microscope that says which gate dropped a
known-correct wallet.

Exit code 0 when every requested chain resolved, 1 when any did not, 2 on a
setup error (bad handle, no credentials, httpx missing).

Stop `fomo_bot.py` first -- both use the same persistent Chrome profile.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from fomo_api import FomoClient, FomoError  # noqa: E402
from fomo_evm import (  # noqa: E402
    CHAIN_NAMES,
    EVM_RPCS,
    EvmWalletResolver,
    cached_evm_wallet,
    evm_balance_positions,
    evm_trade_evidence,
    evm_trade_ids,
    select_evidence_groups,
)
from fomo_features import fetch_trader_stats  # noqa: E402
from fomo_wallet import (  # noqa: E402
    DEEP_ATTEMPTS,
    DEEP_DEFAULT,
    QUOTES,
    SOLANA_ADDRESS_RE,
    SOLANA_NETWORK_ID,
    _load_cache,
    cached_wallet,
    pick_swaps,
    solana_balance_positions,
    swap_search_leg,
)
from rpc_config import env_rpc_urls, rpc_display_name  # noqa: E402

SOLANA_RPCS = env_rpc_urls(
    "SOLANA_RPC", "SOLANA_RPC_FALLBACKS", "https://api.mainnet-beta.solana.com"
)

OK = "OK  "
BAD = "FAIL"
WARN = "note"


# --------------------------------------------------------------- log capture


class LogCapture(logging.Handler):
    """Collect the resolvers' own explanations while they run."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[tuple[str, str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(
                (record.name, record.levelname, record.getMessage())
            )
        except Exception:  # never let diagnostics break on a bad format string
            pass

    def messages(self, *prefixes: str) -> list[str]:
        wanted = prefixes or ("fomo.",)
        return [
            message for name, _level, message in self.records
            if any(name.startswith(prefix) for prefix in wanted)
        ]


# ------------------------------------------------------------- pure explains


def solana_swap_reason(swap: Any) -> str | None:
    """None when the row is usable Solana evidence, else why it is not.

    Mirrors `fomo_wallet.is_solana_swap()`. The bot silently drops these rows,
    which is exactly why an EVM-only trader looks like "no wallet" rather than
    "no Solana swaps in the window".
    """
    if not isinstance(swap, dict):
        return "not an object"
    mint, amount, direction = swap_search_leg(swap)
    if not mint:
        return "no usable token leg"
    if not amount:
        return "zero amount"
    if not SOLANA_ADDRESS_RE.fullmatch(mint):
        kind = "EVM contract" if mint.startswith("0x") else "not base58"
        return f"non-Solana mint ({kind})"
    side = swap.get("outNetworkId" if direction > 0 else "inNetworkId")
    network = side if side is not None else swap.get("networkId")
    if network is None:
        return None
    try:
        if int(network) != SOLANA_NETWORK_ID:
            return f"networkId {int(network)} ({CHAIN_NAMES.get(int(network), 'other chain')})"
    except (TypeError, ValueError):
        return f"unreadable networkId {network!r}"
    return None


def explain_solana_swaps(payload: Any) -> dict[str, Any]:
    """Count the FOMO swap window by usability, with a reason breakdown."""
    rows = payload.get("swaps") if isinstance(payload, dict) else payload
    rows = rows if isinstance(rows, list) else []
    usable: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    for row in rows:
        reason = solana_swap_reason(row)
        if reason is None:
            usable.append(row)
        else:
            reasons[reason] = reasons.get(reason, 0) + 1
    buys = sum(
        1 for row in usable
        if row.get("inTokenAddress") in QUOTES
        and row.get("outTokenAddress") not in QUOTES
    )
    picked = pick_swaps(usable, want=4) if usable else []
    return {
        "total": len(rows),
        "usable": len(usable),
        "buys": buys,
        "sells": len(usable) - buys,
        "rejected": reasons,
        "picked": len(picked),
        "distinct_mints": len({swap_search_leg(row)[0] for row in picked}),
        "rows": usable,
        "picks": picked,
    }


def explain_evm_evidence(swaps: Any, trades: Any, details: Any = None) -> dict[str, Any]:
    """Count the exact vs aggregate EVM fingerprints discovery can search."""
    evidence = evm_trade_evidence(swaps, trades, details)
    exact = sum(1 for item in evidence if not item.aggregate)
    tokens = {(item.chain_id, item.token) for item in evidence}
    groups = select_evidence_groups(evidence) if evidence else {}
    chains: dict[str, int] = {}
    for item in evidence:
        name = CHAIN_NAMES.get(item.chain_id, str(item.chain_id))
        chains[name] = chains.get(name, 0) + 1
    return {
        "items": len(evidence),
        "exact": exact,
        "aggregate": len(evidence) - exact,
        "tokens": len(tokens),
        "searched_tokens": len(groups),
        "chains": chains,
        "evidence": evidence,
    }


def evm_transfer_providers(rpcs: dict[str, list[str]] | None = None) -> dict[str, bool]:
    """Which chains have the Alchemy endpoint historical transfer search needs.

    `_transfers_for_token()` only calls `alchemy_getAssetTransfers`; a chain
    configured with a public RPC alone falls through to Blockscout, which does
    not cover BSC at all.
    """
    configured = EVM_RPCS if rpcs is None else rpcs
    return {
        chain: any("alchemy.com" in str(url).lower() for url in (urls or []))
        for chain, urls in configured.items()
    }


# ------------------------------------------------------------- verdict rules

# Substring of a resolver log line -> (stage that lost the wallet, what to do).
SOLANA_RULES: tuple[tuple[str, str, str], ...] = (
    ("cooling down", "rpc",
     "every configured Solana RPC failed, so discovery is paused for 15s. "
     "Check SOLANA_RPC / SOLANA_RPC_FALLBACKS and rerun."),
    ("All configured Solana RPCs failed", "rpc",
     "every configured Solana RPC failed. Check SOLANA_RPC / "
     "SOLANA_RPC_FALLBACKS (Helius or QuickNode; the public endpoint "
     "throttles and prunes)."),
    ("FOMO returned no swaps", "evidence",
     "FOMO's swap window holds no Solana rows for this handle -- an EVM-only "
     "trader, or the swaps panel came back empty."),
    ("block route off", "config",
     "only the sponsor and mint routes ran, and both stop at 12000 signatures. "
     "Set FOMO_WALLET_DEEP=1 (the default) so the block route runs as a second "
     "pass."),
    ("no transaction-backed Solana wallet match", "discovery",
     "no route found the transaction behind any picked swap. Check "
     "FOMO_SPONSORS if FOMO rotated its gas payer, and confirm the RPC still "
     "serves blocks that old -- the block route needs getBlock at the swap's "
     "slot. Rerun with -v to see which route reached how far."),
    ("no usable Solana balances", "balances",
     "the balance fallback had no Solana positions to fingerprint -- the "
     "trader has sold out, or holds only EVM tokens."),
    ("no verified owner", "balances",
     "exact balance fingerprints matched zero or more than one on-chain "
     "owner, so no wallet could be claimed safely."),
    ("no Helius RPC configured", "config",
     "the balance fallback needs Helius DAS getTokenAccounts. Put a Helius "
     "URL in SOLANA_RPC or SOLANA_RPC_FALLBACKS."),
)

EVM_RULES: tuple[tuple[str, str, str], ...] = (
    ("EVM wallet resolution failed", "error",
     "the resolver raised. The message above is the exception it swallowed in "
     "production."),
    ("is not deployed on an evidence chain", "deployment",
     "a candidate explained two transactions but has no contract code on a "
     "chain it traded on, so it was rejected as an EOA relayer."),
    ("ambiguous transaction-backed EVM wallet", "ranking",
     "two addresses scored identically. Discovery refuses to guess -- add "
     "more evidence with --details, or resolve it by hand with evm_resolve.py "
     "--handle X --wallet 0x..."),
    ("none explaining two transactions", "discovery",
     "candidate transfers were found but no single address explained two "
     "independent transactions. Run evm_diag.py --handle X --expect 0x... to "
     "see which gate dropped the right one."),
    ("no EVM transfers found", "transfers",
     "the transfer search returned nothing in the trade's time window. Check "
     "that the chain has an Alchemy endpoint configured; Blockscout does not "
     "cover BSC."),
    ("EVM transfer search failed", "transfers",
     "the transfer provider errored. The chain and error are logged above."),
)


def classify(messages: list[str], rules: tuple[tuple[str, str, str], ...]
             ) -> tuple[str, str, str] | None:
    """First matching rule wins; rules are ordered most specific first."""
    for needle, stage, hint in rules:
        for message in messages:
            if needle in message:
                return stage, hint, message
    return None


def solana_verdict(facts: dict[str, Any], messages: list[str]) -> tuple[str, str, str]:
    """(stage, reason, hint) for a Solana miss, from facts then log records."""
    hit = classify(messages, SOLANA_RULES)
    if hit:
        stage, hint, message = hit
        return stage, message, hint
    if not facts.get("rpc_urls"):
        return ("config", "no Solana RPC configured",
                "set SOLANA_RPC in .env.")
    if facts.get("swaps", {}).get("total", 0) == 0:
        return ("panels", "FOMO returned no swaps at all",
                "the swaps panel failed or the profile has never traded; "
                "check the FOMO transport (browser mode beats Cloudflare).")
    if facts.get("swaps", {}).get("usable", 0) == 0:
        return ("evidence", "no Solana rows in the FOMO swap window",
                "this trader is EVM-only in the last 50 swaps.")
    return ("discovery", "no wallet returned and no reason was logged",
            "rerun with -v to see the library's DEBUG lines.")


def evm_verdict(facts: dict[str, Any], messages: list[str]) -> tuple[str, str, str]:
    """(stage, reason, hint) for an EVM miss, from facts then log records."""
    evidence = facts.get("evidence", {})
    if evidence.get("items", 0) == 0:
        return ("evidence", "no EVM trade fingerprints in the profile payloads",
                "this trader has no EVM trades FOMO exposes -- a Solana-only "
                "profile. Confirm with --details.")
    if evidence.get("items", 0) < 2:
        return ("evidence",
                f"only {evidence['items']} EVM evidence item(s); two independent "
                "ones are required",
                "rerun with --details so /trades/{id} contributes exact swap "
                "legs, or wait for the trader to make another EVM trade.")
    hit = classify(messages, EVM_RULES)
    if hit:
        stage, hint, message = hit
        return stage, message, hint
    missing = [chain for chain, ok in facts.get("providers", {}).items() if not ok]
    if missing:
        return ("config",
                f"no Alchemy transfer endpoint for: {', '.join(sorted(missing))}",
                "historical transfer search uses alchemy_getAssetTransfers; set "
                f"{'/'.join(sorted(chain.upper() + '_RPC' for chain in missing))}.")
    return ("discovery", "no wallet returned and no reason was logged",
            "rerun with -v to see the library's DEBUG lines.")


# --------------------------------------------------------------- run one chain


@dataclass
class ChainReport:
    chain: str
    wallet: str | None = None
    cached: str | None = None
    stage: str = ""
    reason: str = ""
    hint: str = ""
    lines: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return bool(self.wallet)


def _mark(ok: bool) -> str:
    """FAIL is reserved for a stage that can actually block resolution."""
    return OK if ok else BAD


def _note(ok: bool) -> str:
    """Informational: worth knowing, but not on its own a reason to fail."""
    return OK if ok else WARN


async def diagnose_solana(
    fomo: FomoClient, http: Any, user: Any, stats: Any, args: argparse.Namespace
) -> ChainReport:
    from fomo_wallet import WalletResolver

    handle = (user.handle or "").lower()
    report = ChainReport("solana")
    report.cached = cached_wallet(handle)
    report.lines.append(
        f"  {_note(bool(report.cached))} cache      "
        + (f"{report.cached} (cached; --fresh re-resolves)" if report.cached
           else "no cached Solana wallet")
    )
    report.facts["deep"] = bool(args.deep)
    report.lines.append(
        f"  {_note(bool(args.deep))} routes     "
        + ("sponsor, mint, then blocks on the "
           f"{min(DEEP_ATTEMPTS, 4)} newest swap(s)" if args.deep
           else "sponsor, mint only (FOMO_WALLET_DEEP=0 / --no-deep)")
    )
    report.facts["rpc_urls"] = [rpc_display_name(url) for url in SOLANA_RPCS]
    public_only = all("api.mainnet-beta.solana.com" in url for url in SOLANA_RPCS)
    report.lines.append(
        f"  {_note(not public_only)} rpc        "
        f"{', '.join(report.facts['rpc_urls']) or 'none'}"
        + ("  <- public endpoint throttles and prunes history" if public_only else "")
    )

    if report.cached and not args.fresh:
        report.wallet = report.cached
        report.stage = "cache"
        return report

    swaps = stats.raw_swaps
    facts = explain_solana_swaps(swaps)
    report.facts["swaps"] = {k: v for k, v in facts.items() if k not in ("rows", "picks")}
    rejected = ", ".join(f"{count}x {reason}"
                         for reason, count in sorted(facts["rejected"].items(),
                                                     key=lambda item: -item[1]))
    report.lines.append(
        f"  {_mark(facts['usable'] > 0)} swaps      "
        f"{facts['usable']}/{facts['total']} usable Solana rows "
        f"({facts['buys']} buy / {facts['sells']} sell)"
        + (f"; dropped: {rejected}" if rejected else "")
    )
    if facts["picks"]:
        report.lines.append(
            f"  {_mark(facts['distinct_mints'] > 0)} picked     "
            f"{facts['picked']} swap(s) across {facts['distinct_mints']} distinct mint(s)"
        )
        for row in facts["picks"]:
            mint, amount, direction = swap_search_leg(row)
            report.lines.append(
                f"       - {'buy ' if direction > 0 else 'sell'} "
                f"{amount:,.6f} {str(mint)[:8]}… at {str(row.get('createdAt'))[:19]}"
            )

    positions = solana_balance_positions(stats.raw_balances)
    report.facts["balance_positions"] = len(positions)
    helius = any("helius" in url.lower() for url in SOLANA_RPCS)
    report.facts["helius"] = helius
    report.lines.append(
        f"  {_note(bool(positions) and helius)} balances   "
        f"{len(positions)} Solana position(s) for the fallback"
        + ("" if helius else "; no Helius RPC -> fallback disabled")
    )

    resolver = WalletResolver(http, SOLANA_RPCS, deep=bool(args.deep),
                              verify_targets=args.verify)
    report.wallet = await resolver.resolve(fomo, user, use_cache=not args.fresh)
    route = "transactions"
    if not report.wallet and stats.raw_balances is not None:
        report.wallet = await resolver.resolve_from_balances(
            user, stats.raw_balances, use_cache=not args.fresh
        )
        route = "balances"
    if report.wallet:
        entry = _load_cache().get(handle) or {}
        report.stage = "resolved"
        report.reason = (
            f"via {entry.get('walletSource', route)}, "
            f"{entry.get('confirmed', 0)} confirmation(s), "
            f"{resolver.rpc.calls} RPC call(s)"
        )
    return report


async def diagnose_evm(
    fomo: FomoClient, http: Any, user: Any, stats: Any, args: argparse.Namespace
) -> ChainReport:
    handle = (user.handle or "").lower()
    report = ChainReport("evm")
    report.cached = cached_evm_wallet(handle)
    report.lines.append(
        f"  {_note(bool(report.cached))} cache      "
        + (f"{report.cached} (cached; --fresh re-resolves)" if report.cached
           else "no cached EVM wallet")
    )
    providers = evm_transfer_providers()
    report.facts["providers"] = providers
    report.lines.append(
        f"  {_note(all(providers.values()))} rpc        "
        + ", ".join(f"{chain}={'alchemy' if ok else 'public only'}"
                    for chain, ok in sorted(providers.items()))
    )

    if report.cached and not args.fresh:
        report.wallet = report.cached
        report.stage = "cache"
        return report

    details: tuple[Any, ...] = ()
    trade_ids = evm_trade_ids(stats.raw_trades)
    facts = explain_evm_evidence(stats.raw_swaps, stats.raw_trades)
    if args.details and facts["exact"] < 2 and trade_ids:
        results = await fomo.trade_details(trade_ids, background=True)
        details = tuple(item for item in results if not isinstance(item, Exception))
        report.lines.append(
            f"  {_note(bool(details))} details    "
            f"fetched {len(details)}/{len(trade_ids)} /trades/id payload(s)"
        )
        facts = explain_evm_evidence(stats.raw_swaps, stats.raw_trades, details)
    elif trade_ids:
        report.lines.append(
            f"  {WARN} details    {len(trade_ids)} trade id(s) available; "
            "pass --details to mine them for exact swap legs"
        )

    report.facts["evidence"] = {k: v for k, v in facts.items() if k != "evidence"}
    chains = ", ".join(f"{name}:{count}" for name, count in sorted(facts["chains"].items()))
    report.lines.append(
        f"  {_mark(facts['items'] >= 2)} evidence   "
        f"{facts['items']} item(s) ({facts['exact']} exact, {facts['aggregate']} aggregate) "
        f"over {facts['tokens']} token(s)"
        + (f" [{chains}]" if chains else "")
    )
    report.lines.append(
        f"  {_note(facts['searched_tokens'] > 0)} searched   "
        f"{facts['searched_tokens']}/{facts['tokens']} token(s) inside the "
        "discovery budget (FOMO_EVM_DISCOVERY_TOKENS)"
    )
    positions = evm_balance_positions(stats.raw_balances)
    report.facts["balance_positions"] = len(positions)
    report.lines.append(
        f"  {_note(bool(positions))} balances   "
        f"{len(positions)} EVM position(s) for the fallback"
    )

    resolver = EvmWalletResolver(http)
    report.wallet = await resolver.resolve(
        user,
        use_cache=not args.fresh,
        balances=stats.raw_balances,
        swaps=stats.raw_swaps,
        trades=stats.raw_trades,
        trade_details=details,
    )
    if report.wallet:
        entry = _load_cache().get(handle) or {}
        report.stage = "resolved"
        report.reason = (
            f"via {entry.get('evmSource', 'unknown')}, "
            f"{entry.get('evmConfirmed', 0)} transaction(s), "
            f"deployed on {', '.join(entry.get('evmChains') or []) or 'unknown'}"
        )
    return report


async def diagnose_handle(
    fomo: FomoClient, http: Any, handle: str, args: argparse.Namespace
) -> dict[str, Any]:
    capture = LogCapture()
    root = logging.getLogger("fomo")
    previous = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    try:
        user = await fomo.resolve(handle)
        stats = await fetch_trader_stats(fomo, user.id)
        print(f"\n@{user.handle}  ({user.display_name})  id={user.id}")
        print(f"  panels     balances={'yes' if stats.raw_balances else 'no'} "
              f"trades={'yes' if stats.raw_trades else 'no'} "
              f"swaps={'yes' if stats.raw_swaps else 'no'}")
        if user.sol_address or user.evm_address:
            print("  published  user.address / user.evmAddress are synthetic "
                  "and are never the trading wallet")

        reports: list[ChainReport] = []
        if args.chain in ("both", "solana"):
            reports.append(await diagnose_solana(fomo, http, user, stats, args))
        if args.chain in ("both", "evm"):
            reports.append(await diagnose_evm(fomo, http, user, stats, args))
    finally:
        root.removeHandler(capture)
        root.setLevel(previous)

    result: dict[str, Any] = {"handle": user.handle, "id": user.id, "chains": {}}
    for report in reports:
        messages = capture.messages("fomo.wallet" if report.chain == "solana"
                                    else "fomo.evm")
        if not report.resolved:
            verdict = (solana_verdict if report.chain == "solana" else evm_verdict)(
                report.facts, messages
            )
            report.stage, report.reason, report.hint = verdict

        print(f"\n  [{report.chain}]")
        for line in report.lines:
            print(line)
        if report.resolved:
            print(f"  {OK} VERDICT    {report.wallet}")
            print(f"       {report.reason}")
        else:
            print(f"  {BAD} VERDICT    failed at '{report.stage}'")
            print(f"       reason: {report.reason}")
            print(f"       fix:    {report.hint}")
        if args.verbose and messages:
            print("       log:")
            for message in messages:
                print(f"         {message}")

        result["chains"][report.chain] = {
            "wallet": report.wallet,
            "cached": report.cached,
            "stage": report.stage,
            "reason": report.reason,
            "hint": report.hint,
            "facts": report.facts,
            "log": messages,
        }
    return result


def summary_rows(reports: list[dict[str, Any]],
                 chains: tuple[str, ...] = ("solana", "evm")
                 ) -> list[dict[str, str]]:
    """One flat row per handle: the full wallet, or the stage that lost it.

    Wallets are never abbreviated here -- a summary you cannot paste into a
    tracker or a block explorer is not a summary.
    """
    rows: list[dict[str, str]] = []
    for report in reports:
        row = {"handle": report.get("handle", "")}
        if "error" in report:
            for chain in chains:
                row[chain] = ""
                row[f"{chain}_status"] = "error"
            row["error"] = str(report["error"])
            rows.append(row)
            continue
        for chain in chains:
            data = report.get("chains", {}).get(chain)
            if data is None:
                row[chain] = ""
                row[f"{chain}_status"] = "not requested"
                continue
            row[chain] = data.get("wallet") or ""
            row[f"{chain}_status"] = (
                "resolved" if data.get("wallet") else (data.get("stage") or "failed")
            )
        row["error"] = ""
        rows.append(row)
    return rows


def requested_chains(chain: str) -> tuple[str, ...]:
    return ("solana", "evm") if chain == "both" else (chain,)


def print_summary(reports: list[dict[str, Any]], chain: str) -> None:
    """Full addresses in aligned columns -- copy/paste ready, no ellipsis."""
    chains = requested_chains(chain)
    rows = summary_rows(reports, chains)
    if not rows:
        return
    handle_width = max(len(row["handle"]) for row in rows) + 1
    widths = {
        name: max([len(row[name]) for row in rows]
                  + [len(f"{name}/{name}_status") // 2, len(name)])
        for name in chains
    }
    print("\nsummary")
    header = f"  {'handle':<{handle_width}}" + "".join(
        f"  {name:<{widths[name]}}" for name in chains
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in rows:
        line = f"  {row['handle']:<{handle_width}}"
        for name in chains:
            value = row[name] or f"[{row[f'{name}_status']}]"
            line += f"  {value:<{widths[name]}}"
        if row.get("error"):
            line += f"  {row['error']}"
        print(line.rstrip())
    missing = [row for row in rows
               if any(not row[name] for name in chains)]
    if missing:
        print(f"  {len(missing)} of {len(rows)} handle(s) missing a wallet; "
              "the per-handle VERDICT above names the stage and the fix")


def write_summary_csv(reports: list[dict[str, Any]], chain: str,
                      destination: str | Path) -> Path:
    """Export the same rows as CSV for a spreadsheet or a tracker."""
    chains = requested_chains(chain)
    rows = summary_rows(reports, chains)
    fields = ["handle"]
    for name in chains:
        fields += [name, f"{name}_status"]
    fields.append("error")
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    return path


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("handles", nargs="+", help="FOMO handles, with or without @")
    parser.add_argument("--chain", choices=("both", "solana", "evm"), default="both")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore wallet_cache.json and resolve again")
    parser.add_argument("--deep", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="Solana: block route as a second pass "
                             f"(default: {'on' if DEEP_DEFAULT else 'off'}, "
                             "from FOMO_WALLET_DEEP -- same as the bot)")
    parser.add_argument("--details", action="store_true",
                        help="EVM: mine /trades/{id} for exact swap legs")
    parser.add_argument("--verify", type=int, default=2,
                        help="Solana: corroborating swaps to verify (default 2)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the captured fomo.* log records")
    parser.add_argument("--json", dest="json_path", default="",
                        help="also write the full report to this path")
    parser.add_argument("--csv", dest="csv_path", default="",
                        help="write the summary (handle, full wallets, status) "
                             "to this path as CSV")
    args = parser.parse_args()
    if args.deep is None:
        args.deep = DEEP_DEFAULT

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs at INFO, API keys included.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        import httpx
    except ImportError:
        print("httpx is required: pip install httpx", file=sys.stderr)
        return 2

    reports: list[dict[str, Any]] = []
    failed = False
    try:
        async with FomoClient(
            refresh_token=os.getenv("FOMO_PRIVY_REFRESH_TOKEN") or None,
            access_token=os.getenv("FOMO_PRIVY_ACCESS_TOKEN") or None,
        ) as fomo, httpx.AsyncClient(timeout=60) as http:
            for handle in args.handles:
                clean = handle.strip().lstrip("@")
                try:
                    report = await diagnose_handle(fomo, http, clean, args)
                except FomoError as exc:
                    print(f"\n@{clean}\n  {BAD} profile    {exc}")
                    reports.append({"handle": clean, "error": str(exc)})
                    failed = True
                    continue
                reports.append(report)
                failed = failed or any(
                    not chain["wallet"] for chain in report["chains"].values()
                )
    except Exception as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return 2

    print_summary(reports, args.chain)

    if args.csv_path:
        path = write_summary_csv(reports, args.chain, args.csv_path)
        print(f"wrote {path}")

    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "generatedAt": datetime.now(timezone.utc).isoformat(),
                    "reports": reports,
                },
                indent=1,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
