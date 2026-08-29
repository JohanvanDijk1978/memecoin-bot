"""
pump_resolve_diag.py -- both of a Pump profile's wallets, or the stage that
lost one.

    python pump_resolve_diag.py eth
    python pump_resolve_diag.py hdegroot 1000XCryptoD --details
    python pump_resolve_diag.py <wallet> --fresh -v
    python pump_resolve_diag.py <wallet> --json hunt_out/pump_diag.json
    python pump_resolve_diag.py w1 w2 w3 --csv hunt_out/pump_wallets.csv

`fomo_resolve_diag.py` answers "why did /fomo not show a Solana and/or EVM
wallet?" for a FOMO handle. This is that tool for a Pump handle, and it prints
BOTH wallets for every term:

    Solana:  input -> cache -> evm-map -> profile -> panels
    EVM:     evm-cache -> portfolio -> holder index -> fingerprint -> balanceOf

Why `/pump eth` shows a Solana wallet and no EVM one
----------------------------------------------------

Because Pump publishes the first and not the second. A Pump profile IS a
Solana address, so naming it costs one request. The EVM account is never
returned by any Pump route: `pump_evm.py` has to *discover* it, by taking the
exact `(chain, token, amountHeld)` balance of a position Pump publishes and
finding the one address in that token's public holder index holding exactly
that much -- then confirming it with `balanceOf` through the chain's own RPC.

Four things have to be true at once for that to produce an address, and this
tool says which one was not:

* the trader currently holds an **open** position on Ethereum, BSC, Base or
  Robinhood (a Solana-only trader can never be matched -- there is nothing to
  fingerprint, and that is the most common answer of all);
* a holder index answers for that token (CoinMarketCap's keyless route, or
  Blockscout for Robinhood);
* exactly **one** indexed holder has that exact balance -- zero means the
  wallet sits outside the indexed page or the balance moved, and more than one
  is refused rather than guessed;
* the chain RPC confirms the balance independently.

It drives the SAME objects `/pump`, `/wallet` and `/token` drive
(`PumpProfileResolver.lookup()` and `PumpEvmResolver`), walking the resolver's
own candidate ordering through the resolver's own helpers, and it installs a
temporary handler on the `pump.*` loggers -- so the resolvers explain
themselves rather than being reimplemented here, and the tool cannot drift
from the bot. A confirmed EVM candidate is handed back to
`PumpEvmResolver.resolve()` for the authoritative answer, which is what writes
`pump_evm_cache.json` and what `/pump` will show from then on.

This is `fomo_resolve_diag.py`'s counterpart and follows the same rules: it
drives the SAME object `/pump`, `/wallet` and `/token` drive
(`PumpProfileResolver.lookup()`), and it installs a temporary handler on the
`pump.*` loggers, so the resolver's own explanations are captured rather than
reimplemented and the tool cannot drift from the bot.

The stages are shorter than FOMO's because Pump's mapping is published rather
than inferred -- there is no sponsor index, no mint scan, no block route and no
corroboration gate. What there IS, and what this tool exists to make visible,
is a cache with three distinguishable kinds of "no":

    cache-missing  Pump answered 404 recently. Cached on purpose.
    unsupported    the term cannot address a Pump profile at all (an 0x…
                   wallet whose Pump profile has not been discovered).
    unavailable    Pump did not answer. Deliberately NOT cached, so this one
                   clears by itself.

Exit code 0 when every requested term resolved a Pump profile, 1 when any did
not, 2 on a setup error. `--require-evm` also demands an EVM wallet;
`--no-evm` skips EVM discovery entirely.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

from pump_api import PumpError, PumpClient  # noqa: E402
import pump_evm  # noqa: E402
from pump_evm import (  # noqa: E402
    CHAIN_NAMES,
    EXAMINED_POSITIONS,
    HOLDER_PAGES,
    SOLANA_CHAIN_ID,
    PumpEvmMatch,
    PumpEvmResolver,
    _same_balance,
    order_positions,
)
from pump_profiles import (  # noqa: E402
    CACHE_FILE,
    CACHED,
    CACHED_MISSING,
    CARD_TTL,
    MISSING,
    NEGATIVE_TTL,
    PROFILE_TTL,
    RESOLVED,
    UNAVAILABLE,
    UNSUPPORTED,
    PumpProfileResolver,
    is_evm_address,
    is_solana_address,
    normalize_term,
)
from rpc_config import rpc_display_name  # noqa: E402

PUMP_EVM_CACHE_FILE = Path("pump_evm_cache.json")

OK = "OK  "
BAD = "FAIL"
WARN = "note"


class LogCapture(logging.Handler):
    """Collect the resolver's own explanations while it runs."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:
            pass


# ---------------------------------------------------------------- verdicts

# Ordered most specific first, exactly like the FOMO rule tables.
VERDICTS: dict[str, tuple[str, str, str]] = {
    RESOLVED: ("profile", "Pump returned the profile",
               "it is now cached; the next lookup costs nothing."),
    CACHED: ("cache", "answered from the cache without a request",
             "pass --fresh to re-ask Pump and refresh the record."),
    MISSING: ("profile", "Pump has no profile for this wallet",
              "the absence is cached for the negative TTL so /token does not "
              "re-ask. It clears by itself when the TTL expires."),
    CACHED_MISSING: ("cache", "known to have no profile (negative cache hit)",
                     "no request was made. Pass --fresh to re-check now, or "
                     "wait out PUMP_PROFILE_NEGATIVE_TTL."),
    UNSUPPORTED: ("input", "this term cannot address a Pump profile",
                  "an 0x… wallet only maps to a Pump profile once pump_evm.py "
                  "has discovered it: run /pump <handle> or "
                  "`python pump_map_top.py --from-fomo-cache` first."),
    UNAVAILABLE: ("transport", "Pump did not answer",
                  "a transient failure is deliberately NOT cached as an "
                  "absence. Re-run; if it persists, Pump's public API is "
                  "refusing this host."),
}


@dataclass
class TermReport:
    term: str
    key: str = ""
    kind: str = ""
    query: str = ""
    status: str = ""
    stage: str = ""
    reason: str = ""
    hint: str = ""
    wallet: str = ""
    username: str = ""
    x_username: str = ""
    followers: int = 0
    cache_state: str = ""
    cache_age: float | None = None
    requests: int = 0
    error: str = ""
    panels: dict[str, Any] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    # -- the EVM half -------------------------------------------------
    evm_wallet: str = ""
    evm_chain: str = ""
    evm_chain_id: int = 0
    evm_token: str = ""
    evm_balance: str = ""
    evm_verified: bool = False
    evm_corroborations: int = 0
    evm_discovered_at: str = ""
    evm_status: str = ""
    evm_stage: str = ""
    evm_reason: str = ""
    evm_hint: str = ""
    evm_error: str = ""
    positions_total: int = 0
    positions_usable: int = 0
    chains: dict[str, int] = field(default_factory=dict)
    gates: list["GateReport"] = field(default_factory=list)
    http: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return bool(self.wallet)

    @property
    def evm_resolved(self) -> bool:
        return bool(self.evm_wallet)

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term, "key": self.key, "kind": self.kind,
            "query": self.query, "status": self.status, "stage": self.stage,
            "reason": self.reason, "hint": self.hint, "wallet": self.wallet,
            "username": self.username, "x": self.x_username,
            "followers": self.followers, "cache": self.cache_state,
            "cache_age_seconds": self.cache_age, "requests": self.requests,
            "error": self.error, "panels": self.panels,
            "log": self.lines,
            "evm": {
                "wallet": self.evm_wallet,
                "chain": self.evm_chain,
                "chain_id": self.evm_chain_id,
                "token": self.evm_token,
                "balance": self.evm_balance,
                "verified_onchain": self.evm_verified,
                "corroborations": self.evm_corroborations,
                "discovered_at": self.evm_discovered_at,
                "status": self.evm_status,
                "stage": self.evm_stage,
                "reason": self.evm_reason,
                "hint": self.evm_hint,
                "error": self.evm_error,
                "positions": self.positions_total,
                "usable_positions": self.positions_usable,
                "chains": self.chains,
                "candidates": [gate.to_dict() for gate in self.gates],
                "http": self.http,
            },
        }


# ------------------------------------------------------------ evm verdicts

# The EVM half has its own ladder because Pump does not publish the address:
# it is *discovered* from a balance fingerprint, so every stage below is a
# place a real wallet can be lost without anything being wrong.
EVM_CACHED = "evm-cached"
EVM_RESOLVED = "evm-resolved"
EVM_SKIPPED = "evm-skipped"
EVM_NO_PROFILE = "evm-no-profile"
EVM_NO_POSITIONS = "evm-no-positions"
EVM_NO_INDEX = "evm-no-index"
EVM_NO_MATCH = "evm-no-match"
EVM_TRUNCATED = "evm-truncated"
EVM_ADOPTED = "evm-adopted"
EVM_ADOPT_REFUSED = "evm-adopt-refused"
EVM_AMBIGUOUS = "evm-ambiguous"
EVM_UNVERIFIED = "evm-unverified"

# Least to most progress. When eight positions fail eight different ways, the
# one that got furthest is the one worth reporting -- it names the gate that
# is actually in the way.
EVM_PROGRESS = (
    EVM_NO_PROFILE,
    EVM_NO_POSITIONS,
    EVM_NO_INDEX,
    EVM_NO_MATCH,
    EVM_TRUNCATED,
    EVM_AMBIGUOUS,
    EVM_UNVERIFIED,
    EVM_RESOLVED,
)

EVM_VERDICTS: dict[str, tuple[str, str, str]] = {
    EVM_CACHED: (
        "evm-cache", "already discovered, confirmed on chain and cached",
        "pass --fresh to run discovery again from Pump's current balances.",
    ),
    EVM_RESOLVED: (
        "evm-verify", "discovered and independently confirmed on chain",
        "written to pump_evm_cache.json; /pump shows it from now on.",
    ),
    EVM_ADOPTED: (
        "evm-verify", "supplied address confirmed against Pump's own balances",
        "the chain agreed, so it is cached exactly as a discovered wallet is; "
        "/pump shows it from now on.",
    ),
    EVM_ADOPT_REFUSED: (
        "evm-verify", "the supplied address holds none of Pump's balances",
        "balanceOf disagreed with every published position, so it was NOT "
        "cached. Check the address, or drop --adopt-evm and let discovery "
        "search.",
    ),
    EVM_SKIPPED: (
        "evm", "not attempted (--no-evm)",
        "drop --no-evm to run EVM discovery.",
    ),
    EVM_NO_PROFILE: (
        "profile", "no Pump profile, so there is no portfolio to fingerprint",
        "the Solana verdict above is the one to fix first.",
    ),
    EVM_NO_POSITIONS: (
        "evm-portfolio",
        "Pump publishes no OPEN position on a supported EVM chain",
        "discovery needs a token the wallet still HOLDS on Ethereum, BSC, "
        "Base or Robinhood -- a Solana-only trader has no current balance to "
        "fingerprint. Note that Pump does publish an exact `amountBought` for "
        "closed positions too (user-portfolio?filter=closed), which this "
        "route does not use yet.",
    ),
    EVM_NO_INDEX: (
        "evm-holders", "no holder index answered for any candidate token",
        "CoinMarketCap's keyless holder route (Ethereum/BSC/Base) or "
        "Blockscout (Robinhood) returned nothing. Re-run with -v to see the "
        "status codes; a refusal there stops discovery cold.",
    ),
    EVM_NO_MATCH: (
        "evm-fingerprint",
        "the complete holder list holds no address with Pump's exact balance",
        "the index was read to the end, so the balance moved between Pump's "
        "snapshot and the index's. --fresh later often works.",
    ),
    EVM_TRUNCATED: (
        "evm-holders",
        "no holder at that balance in the rows read -- and the index was "
        "truncated, not exhausted",
        "raise PUMP_EVM_HOLDER_PAGES (or pass --evm-holder-pages). Holders "
        "come back ranked by balance, so a dust fingerprint sits deep in the "
        "tail: this is exactly what hid eth's wallet at holder rank ~1211 of "
        "2493 while discovery read only the first 250.",
    ),
    EVM_AMBIGUOUS: (
        "evm-fingerprint", "more than one holder has that exact balance",
        "refused on purpose rather than guessed -- an ownership claim is only "
        "worth making when it is unique.",
    ),
    EVM_UNVERIFIED: (
        "evm-verify",
        "a unique candidate failed the on-chain balanceOf confirmation",
        "a missing or broken chain RPC fails this gate exactly like a wrong "
        "candidate does. Check ETH_RPC / BSC_RPC / BASE_RPC / ROBINHOOD_RPC "
        "in .env before assuming the candidate was wrong.",
    ),
}


def evm_verdict(status: str) -> tuple[str, str, str]:
    return EVM_VERDICTS.get(
        status, ("evm", "unclassified outcome", "re-run with -v.")
    )


def furthest(statuses: Iterable[str]) -> str:
    """The gate that got closest to an answer."""
    ranked = [status for status in statuses if status in EVM_PROGRESS]
    if not ranked:
        return EVM_NO_POSITIONS
    return max(ranked, key=EVM_PROGRESS.index)


def chain_name(chain_id: int) -> str:
    if chain_id == SOLANA_CHAIN_ID:
        return "Solana"
    return CHAIN_NAMES.get(chain_id, f"chain {chain_id}")


def chain_breakdown(rows: Iterable[Any]) -> dict[str, int]:
    """What the portfolio is made of, including the rows discovery drops."""
    counts: dict[str, int] = {}
    for row in rows:
        name = "unknown"
        if isinstance(row, dict):
            try:
                name = chain_name(int(row.get("chainId")))
            except (TypeError, ValueError):
                name = "unknown"
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _short(address: str) -> str:
    clean = (address or "").strip()
    return clean if len(clean) <= 14 else f"{clean[:8]}…{clean[-6:]}"


def _amount(value: Any) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    text = f"{number:,.6f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


class RequestLog:
    """Every HTTP call the run made, with API keys kept out of the labels."""

    def __init__(self) -> None:
        self.entries: list[str] = []
        self.rpc_urls: set[str] = set()

    def label(self, url: str) -> str:
        text = str(url)
        if any(text.startswith(rpc) for rpc in self.rpc_urls):
            return rpc_display_name(text)
        parsed = urlsplit(text)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    async def record(self, response: Any) -> None:
        try:
            request = response.request
            self.entries.append(
                f"{request.method} {self.label(request.url)} "
                f"-> {response.status_code}"
            )
        except Exception:
            pass

    def since(self, mark: int) -> list[str]:
        return self.entries[mark:]


@dataclass
class GateReport:
    """One candidate position, and how far it got."""

    chain_id: int = 0
    chain: str = ""
    token: str = ""
    amount: str = ""
    value_usd: float = 0.0
    source: str = ""
    holders: int = 0
    complete: bool = False
    pages: int = 0
    stopped: str = ""
    status_code: int | None = None
    matches: int = 0
    candidate: str = ""
    verified: bool = False
    status: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain, "chain_id": self.chain_id,
            "token": self.token, "amount": self.amount,
            "value_usd": self.value_usd, "index": self.source,
            "holders": self.holders, "complete": self.complete,
            "pages": self.pages, "stopped": self.stopped,
            "http_status": self.status_code,
            "exact_matches": self.matches,
            "candidate": self.candidate, "verified": self.verified,
            "status": self.status, "note": self.note,
        }

    def line(self) -> str:
        read = f"{self.source} {self.holders} holder(s)"
        if self.pages:
            read += f" over {self.pages} page(s)"
        read += " (complete)" if self.complete else " (truncated)"
        if self.stopped:
            read += f", stopped: {self.stopped}"
        if self.status_code and self.status_code >= 400:
            read += f" HTTP {self.status_code}"
        parts = [
            f"{self.chain} · {_short(self.token)} · {self.amount}",
            read,
            f"{self.matches} exact",
        ]
        if self.candidate:
            parts.append(
                f"{_short(self.candidate)} "
                + ("confirmed on chain" if self.verified
                   else "NOT confirmed on chain")
            )
        if self.note:
            parts.append(self.note)
        return " · ".join(parts)


async def walk_evm(
    evm: PumpEvmResolver, positions: list[Any], limit: int
) -> list[GateReport]:
    """Re-run discovery's own loop, recording the gate each position dies at.

    It calls the resolver's own helpers on the resolver's own ordering, so it
    examines exactly the candidates `/pump` examines. It never *decides*
    anything: when a candidate survives every gate, `PumpEvmResolver.resolve()`
    is asked for the answer that gets cached.
    """
    gates: list[GateReport] = []
    for position in positions[:limit]:
        gate = GateReport(
            chain_id=position.chain_id,
            chain=chain_name(position.chain_id),
            token=position.token,
            amount=_amount(position.amount),
            value_usd=round(position.value_usd, 2),
            status=EVM_NO_INDEX,
        )
        try:
            index = await evm.holder_index(position)
        except Exception as exc:
            gate.note = f"index error: {str(exc)[:100]}"
            gates.append(gate)
            continue
        gate.source = index.source
        gate.holders = len(index.holders)
        gate.complete = index.complete
        gate.pages = index.pages
        gate.stopped = index.stopped
        gate.status_code = index.status
        if index.error:
            gate.note = index.error
        if not index.holders:
            gates.append(gate)
            continue
        matches = [
            (address, balance) for address, balance in index.holders
            if _same_balance(position.amount, balance)
        ]
        gate.matches = len(matches)
        if len(matches) != 1:
            if not matches:
                gate.status = EVM_NO_MATCH if index.complete else EVM_TRUNCATED
            else:
                gate.status = EVM_AMBIGUOUS
            gates.append(gate)
            continue
        gate.candidate = matches[0][0]
        gate.status = EVM_UNVERIFIED
        try:
            verified, _balance = await evm._verify_balance(position, gate.candidate)
        except Exception as exc:
            gate.note = f"rpc error: {str(exc)[:100]}"
            gates.append(gate)
            continue
        gate.verified = bool(verified)
        if verified:
            gate.status = EVM_RESOLVED
            gates.append(gate)
            break
        gates.append(gate)
    return gates


async def diagnose_evm(
    evm: PumpEvmResolver,
    profile: Any,
    report: TermReport,
    args: argparse.Namespace,
    http: RequestLog,
) -> None:
    """The second half of the card: the separate EVM wallet, or why not."""
    if not args.evm:
        report.evm_status = EVM_SKIPPED
    elif profile is None or not report.wallet:
        report.evm_status = EVM_NO_PROFILE
    if report.evm_status:
        report.evm_stage, report.evm_reason, report.evm_hint = evm_verdict(
            report.evm_status
        )
        return

    mark = len(http.entries)

    # At ~6.5s a holder page, a silent search is indistinguishable from a hung
    # one. The resolver already narrates itself at INFO; this just lets it
    # through while the walk runs.
    progress = logging.StreamHandler(sys.stdout)
    progress.setFormatter(logging.Formatter("       … %(message)s"))
    evm_log = logging.getLogger("pump.evm")
    previous_level, previous_propagate = evm_log.level, evm_log.propagate
    evm_log.addHandler(progress)
    evm_log.setLevel(min(evm_log.level or logging.INFO, logging.INFO))
    evm_log.propagate = False
    try:
        await _diagnose_evm(evm, profile, report, args, http, mark)
    finally:
        evm_log.removeHandler(progress)
        evm_log.setLevel(previous_level)
        evm_log.propagate = previous_propagate


async def _diagnose_evm(
    evm: PumpEvmResolver,
    profile: Any,
    report: TermReport,
    args: argparse.Namespace,
    http: RequestLog,
    mark: int,
) -> None:
    def adopt(match: PumpEvmMatch, status: str) -> None:
        report.evm_wallet = match.evm
        report.evm_chain_id = match.chain_id
        report.evm_chain = chain_name(match.chain_id)
        report.evm_token = match.token
        report.evm_balance = match.balance
        report.evm_verified = bool(match.verified_onchain)
        report.evm_corroborations = int(getattr(match, "corroborations", 0) or 0)
        report.evm_discovered_at = match.discovered_at
        report.evm_status = status

    supplied = (getattr(args, "adopt_evm", "") or "").strip()
    if supplied:
        match = await evm.adopt(profile, supplied)
        if isinstance(match, PumpEvmMatch):
            adopt(match, EVM_ADOPTED)
            print(f"  {OK} adopt     {supplied} confirmed against "
                  f"{match.corroborations + 1} published balance(s)")
        else:
            report.evm_status = EVM_ADOPT_REFUSED
            print(f"  {BAD} adopt     {supplied} matched no published balance")
        report.http = http.since(mark)
        report.evm_stage, report.evm_reason, report.evm_hint = evm_verdict(
            report.evm_status
        )
        if report.evm_wallet:
            print(f"  {OK} evm       Ξ {report.evm_wallet} · {report.evm_chain}"
                  " · verified on chain")
        print(f"  EVM VERDICT [{report.evm_stage}] {report.evm_reason}")
        print(f"              {report.evm_hint}")
        return

    cached = evm.cached(report.wallet)
    if cached and cached.verified_onchain and not args.fresh:
        adopt(cached, EVM_CACHED)
        print(f"  {OK} evm-cache Ξ {cached.evm} · {report.evm_chain} · "
              f"discovered {cached.discovered_at[:10]}")
    else:
        if cached and args.fresh:
            note = "re-running discovery (--fresh)"
        elif cached:
            note = (f"{cached.evm} recorded but never confirmed on chain "
                    "— re-discovering")
        else:
            note = (f"nothing recorded · {len(evm._matches)} mapping(s) in "
                    f"{evm.cache_file}")
        print(f"  {WARN} evm-cache {note}")
        try:
            rows = await evm.portfolio_rows(report.wallet)
        except Exception as exc:
            rows = []
            report.evm_error = str(exc)[:180]
        positions = order_positions(rows)
        report.positions_total = len(rows)
        report.positions_usable = len(positions)
        report.chains = chain_breakdown(rows)
        spread = ", ".join(f"{name} {count}"
                           for name, count in report.chains.items()) or "—"
        print(f"  {_note(bool(positions))} portfolio {len(rows)} open "
              f"position(s) · {len(positions)} usable EVM candidate(s) · {spread}")

        if not positions:
            report.evm_status = EVM_NO_POSITIONS
        else:
            gates = await walk_evm(evm, positions, args.evm_positions)
            report.gates = gates
            for gate in gates:
                print(f"  {_note(gate.verified)} candidate {gate.line()}")
            report.evm_status = furthest([gate.status for gate in gates])

        if report.evm_status == EVM_RESOLVED:
            # The walk explains; the resolver decides and persists.
            #
            # It used to decide by re-running `resolve()`, which re-paged the
            # whole holder index -- four more minutes, in silence, AFTER the
            # answer was already on screen. `adopt()` reaches the same
            # decision from the same authority for the cost of a few
            # balanceOf calls, because the candidate is already known: the
            # search was the expensive half, and it is already done.
            if args.no_write:
                winner = next(gate for gate in report.gates if gate.verified)
                report.evm_wallet = winner.candidate
                report.evm_chain_id = winner.chain_id
                report.evm_chain = winner.chain
                report.evm_token = winner.token
                report.evm_balance = winner.amount
                report.evm_verified = True
                print(f"  {WARN} persist   --no-write: not added to "
                      f"{evm.cache_file}")
            else:
                winner = next(gate for gate in report.gates if gate.verified)
                match = await evm.adopt(profile, winner.candidate)
                if isinstance(match, PumpEvmMatch):
                    adopt(match, EVM_RESOLVED)
                else:
                    report.evm_status = EVM_UNVERIFIED
                    report.evm_error = (
                        "the walk confirmed a candidate the resolver then "
                        "refused to adopt -- re-run with -v"
                    )

    report.http = http.since(mark)
    report.evm_stage, report.evm_reason, report.evm_hint = evm_verdict(
        report.evm_status
    )
    if report.evm_wallet:
        extra = " · verified on chain" if report.evm_verified else ""
        if report.evm_corroborations:
            extra += (f" · {report.evm_corroborations} other published "
                      "balance(s) also match")
        print(f"  {OK} evm       Ξ {report.evm_wallet} · {report.evm_chain}{extra}")
    else:
        print(f"  {BAD} evm       no EVM wallet")
    if args.verbose and report.http:
        for entry in report.http:
            print(f"       http: {entry}")
    print(f"  EVM VERDICT [{report.evm_stage}] {report.evm_reason}")
    print(f"              {report.evm_hint}")


def classify_term(term: str) -> str:
    clean = (term or "").strip().strip("`").strip().lstrip("@").strip()
    if not clean:
        return "empty"
    if is_evm_address(clean):
        return "evm address"
    if is_solana_address(clean):
        return "solana address"
    return "username"


def _mark(ok: bool) -> str:
    return OK if ok else BAD


def _note(ok: bool) -> str:
    return OK if ok else WARN


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


async def diagnose(
    resolver: PumpProfileResolver,
    pump: PumpClient,
    evm: PumpEvmResolver,
    term: str,
    args: argparse.Namespace,
    http: RequestLog,
) -> TermReport:
    report = TermReport(term=term, key=normalize_term(term))
    report.kind = classify_term(term)
    print(f"\n=== {term} ===")
    print(f"  {_mark(report.kind != 'empty')} input     {report.kind}")

    # -- what the cache already knows, before anything is asked --------
    entry = resolver.cache.peek(term)
    if entry is None:
        report.cache_state = "miss"
    else:
        report.cache_age = entry.age()
        ttl = PROFILE_TTL if entry.found else NEGATIVE_TTL
        expired = entry.expired(ttl)
        report.cache_state = (
            ("stale " if expired else "")
            + ("profile" if entry.found else "no-profile")
        )
    print(f"  {_note(entry is not None)} cache     {report.cache_state}"
          f"{'' if entry is None else f' · age {_age(report.cache_age)}'}"
          f" · {len(resolver.cache)} wallet(s) in {resolver.cache.path}")

    # -- EVM translation ------------------------------------------------
    query, blocked = resolver._translate(term)
    report.query = query
    if report.kind == "evm address":
        print(f"  {_note(not blocked)} evm-map   "
              + (f"{term} -> {query}" if not blocked else str(blocked)))

    # -- the lookup itself ---------------------------------------------
    capture = LogCapture()
    loggers = [logging.getLogger(name) for name in ("pump.profiles", "pump.evm",
                                                    "wallet.cache")]
    previous = [(logger.level, logger.propagate) for logger in loggers]
    for logger in loggers:
        logger.addHandler(capture)
        logger.setLevel(logging.DEBUG)
    started = time.monotonic()
    try:
        result = await resolver.lookup(
            term, fresh=args.fresh,
            max_age=CARD_TTL if args.card else None,
        )
    finally:
        for logger, (level, propagate) in zip(loggers, previous):
            logger.removeHandler(capture)
            logger.setLevel(level)
            logger.propagate = propagate
    elapsed = time.monotonic() - started
    report.lines = list(capture.messages)
    report.status = result.status
    report.requests = result.requests
    report.error = result.error or ""

    stage, reason, hint = VERDICTS.get(
        result.status, ("profile", "unclassified outcome", "re-run with -v.")
    )
    report.stage, report.reason, report.hint = stage, reason, hint

    if result.profile is not None:
        profile = result.profile
        report.wallet = profile.address
        report.username = profile.username
        report.x_username = profile.x_username or ""
        report.followers = profile.followers
        print(f"  {OK} profile   @{profile.username} · {profile.address}")
        detail = []
        if profile.x_username:
            detail.append(f"x/{profile.x_username}")
        detail.append(f"{profile.followers} follower(s)")
        detail.append(profile.profile_url)
        print(f"       {' · '.join(detail)}")
    else:
        print(f"  {BAD if result.status != CACHED_MISSING else WARN} "
              f"profile   {reason}")

    print(f"  {_note(True)} cost      {result.requests} request(s), "
          f"{elapsed * 1000:.0f}ms"
          f"{' (cache hit)' if result.from_cache else ''}")

    # -- optional: the panels /pump renders after resolution ------------
    if args.details and report.wallet:
        panels: dict[str, Any] = {}
        try:
            portfolio, holdings, callouts, created = await asyncio.gather(
                pump.portfolio(report.wallet),
                pump.holdings(report.wallet, limit=8),
                pump.callouts(report.wallet, limit=8),
                pump.created_coins(report.wallet, limit=5),
                return_exceptions=True,
            )
            panels["portfolio_usd"] = (
                getattr(portfolio, "total_value", None)
                if not isinstance(portfolio, BaseException) else None
            )
            panels["holdings"] = (len(holdings)
                                  if isinstance(holdings, list) else "error")
            panels["callouts"] = (len(callouts)
                                  if isinstance(callouts, list) else "error")
            panels["created"] = (created[0]
                                 if isinstance(created, tuple) else "error")
        except (PumpError, asyncio.TimeoutError) as exc:
            panels["error"] = str(exc)[:180]
        report.panels = panels
        print(f"  {_note('error' not in panels)} panels    "
              + " · ".join(f"{name}={value}" for name, value in panels.items()))

    if args.verbose and report.lines:
        for line in report.lines:
            print(f"       log: {line}")

    print(f"  VERDICT   [{stage}] {reason}")
    print(f"            {hint}")

    # -- the other wallet ----------------------------------------------
    await diagnose_evm(evm, result.profile, report, args, http)
    return report


# ---------------------------------------------------------------- summary


def summary_rows(reports: list[TermReport]) -> list[dict[str, str]]:
    """Full addresses, never abbreviated -- the point of a batch run is to
    collect them."""
    return [{
        "term": report.term,
        "wallet": report.wallet,
        "evm": report.evm_wallet,
        "evm_chain": report.evm_chain,
        "username": report.username,
        "status": ("resolved" if report.resolved else (report.stage or "failed")),
        # The STATUS, not the stage: `evm-truncated` and `evm-no-index`
        # share a stage and are completely different problems.
        "evm_status": ("resolved" if report.evm_resolved
                       else (report.evm_status or "failed")),
        "cache": report.cache_state,
        "x": report.x_username,
        "error": report.error or report.evm_error,
    } for report in reports]


def print_summary(reports: list[TermReport]) -> None:
    rows = summary_rows(reports)
    if not rows:
        return
    columns = ("term", "wallet", "evm", "username", "status", "evm_status")
    widths = {
        name: max([len(row[name] or "") for row in rows] + [len(name)])
        for name in columns
    }
    print("\nsummary")
    header = "  " + "  ".join(f"{name:<{widths[name]}}" for name in columns)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in rows:
        line = "  " + "  ".join(
            f"{(row[name] or '—'):<{widths[name]}}" for name in columns
        )
        if row["error"]:
            line += f"  {row['error'][:60]}"
        print(line.rstrip())
    missing = [row for row in rows if not row["wallet"]]
    if missing:
        print(f"  {len(missing)} of {len(rows)} term(s) have no Pump profile; "
              "the per-term VERDICT above names the stage and the fix")
    no_evm = [row for row in rows if row["wallet"] and not row["evm"]]
    if no_evm:
        print(f"  {len(no_evm)} resolved profile(s) have no EVM wallet; the "
              "EVM VERDICT above names which gate lost it (most often: no "
              "open position on a supported EVM chain to fingerprint)")


def write_summary_csv(reports: list[TermReport], destination: str | Path) -> Path:
    fields = ["term", "wallet", "evm", "evm_chain", "username", "status",
              "evm_status", "cache", "x", "error"]
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in summary_rows(reports):
            writer.writerow({name: row.get(name, "") for name in fields})
    return path


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("terms", nargs="+",
                        help="Pump usernames, Solana wallets or EVM wallets")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore the profile cache and ask Pump again")
    parser.add_argument("--card", action="store_true",
                        help="apply the shorter freshness bar /pump's card uses "
                             f"({CARD_TTL:.0f}s) instead of the full TTL")
    parser.add_argument("--details", action="store_true",
                        help="also load the portfolio/holdings/callout panels")
    parser.add_argument("--no-evm", dest="evm", action="store_false",
                        help="skip EVM discovery and report the Pump profile "
                             "(Solana wallet) only")
    parser.add_argument("--evm-positions", type=int, default=EXAMINED_POSITIONS,
                        metavar="N",
                        help="how many of the profile's ordered positions to "
                             f"fingerprint (default {EXAMINED_POSITIONS}, the "
                             "same slice /pump uses)")
    parser.add_argument("--evm-holder-pages", type=int, default=HOLDER_PAGES,
                        metavar="N",
                        help="how deep to page a Blockscout holder index "
                             f"(default {HOLDER_PAGES} = {HOLDER_PAGES * 50} "
                             "holders). A dust fingerprint sits deep in a "
                             "balance-ranked list")
    parser.add_argument("--adopt-evm", default="", metavar="0x…",
                        help="skip the holder-index search and prove a known "
                             "EVM address directly: it is cached only if "
                             "balanceOf confirms it against a balance Pump "
                             "publishes for this profile")
    parser.add_argument("--require-evm", action="store_true",
                        help="exit non-zero unless every term also produced an "
                             "EVM wallet")
    parser.add_argument("--cache", default=CACHE_FILE,
                        help=f"profile cache path (default {CACHE_FILE})")
    parser.add_argument("--no-write", action="store_true",
                        help="do not persist anything learned by this run")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print the captured pump.* log records")
    parser.add_argument("--json", dest="json_path", default="",
                        help="also write the full report to this path")
    parser.add_argument("--csv", dest="csv_path", default="",
                        help="write the summary (term, full wallet, status) "
                             "to this path as CSV")
    args = parser.parse_args()

    # Holder depth is a correctness parameter, so the flag has to reach the
    # resolver the same way the environment variable does.
    pump_evm.HOLDER_PAGES = max(1, int(args.evm_holder_pages))

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

    reports: list[TermReport] = []
    request_log = RequestLog()
    async with httpx.AsyncClient(
        timeout=60, event_hooks={"response": [request_log.record]}
    ) as http:
        pump = PumpClient(http)
        evm = PumpEvmResolver(http, PUMP_EVM_CACHE_FILE)
        # Several providers carry the API key in the URL path, so the request
        # log labels those endpoints by host only.
        request_log.rpc_urls = {url for urls in evm.rpcs.values() for url in urls}
        resolver = PumpProfileResolver(
            pump, args.cache, evm=evm, persist=not args.no_write
        )
        counts = resolver.counts()
        print(f"cache {args.cache}: {counts['total']} wallet(s) "
              f"({counts['found']} profile, {counts['missing']} none, "
              f"{counts['aliases']} alias)")
        print(f"evm holder depth: {pump_evm.HOLDER_PAGES} page(s) "
              f"(~{pump_evm.HOLDER_PAGES * 50} holders per token)")
        print(f"evm cache {PUMP_EVM_CACHE_FILE}: {len(evm._matches)} "
              f"discovered mapping(s) · "
              f"{len(evm.rpcs)} chain RPC(s) configured "
              f"({', '.join(chain_name(cid) for cid in sorted(evm.rpcs)) or '—'})")
        print(f"ttl: profile {PROFILE_TTL / 3600:.0f}h, "
              f"negative {NEGATIVE_TTL / 3600:.0f}h, card {CARD_TTL:.0f}s")
        for term in args.terms:
            reports.append(
                await diagnose(resolver, pump, evm, term, args, request_log)
            )

    print_summary(reports)
    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([report.to_dict() for report in reports], indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {path}")
    if args.csv_path:
        print(f"wrote {write_summary_csv(reports, args.csv_path)}")
    ok = all(report.resolved for report in reports)
    if args.require_evm:
        ok = ok and all(report.evm_resolved for report in reports)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
