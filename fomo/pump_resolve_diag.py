"""
pump_resolve_diag.py -- why did /pump (or /token, or /wallet) not name this wallet?

    python pump_resolve_diag.py 4y2T1ghy…dvE1
    python pump_resolve_diag.py hdegroot 1000XCryptoD --details
    python pump_resolve_diag.py <wallet> --fresh -v
    python pump_resolve_diag.py <wallet> --json hunt_out/pump_diag.json
    python pump_resolve_diag.py w1 w2 w3 --csv hunt_out/pump_wallets.csv

One term in, one verdict out, plus the stage that lost the profile:

    input -> cache -> evm-map -> profile -> panels

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

Exit code 0 when every requested term resolved, 1 when any did not, 2 on a
setup error.
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
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from pump_api import PumpError, PumpClient  # noqa: E402
from pump_evm import PumpEvmResolver  # noqa: E402
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

    @property
    def resolved(self) -> bool:
        return bool(self.wallet)

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
        }


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
    term: str,
    args: argparse.Namespace,
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
    return report


# ---------------------------------------------------------------- summary


def summary_rows(reports: list[TermReport]) -> list[dict[str, str]]:
    """Full addresses, never abbreviated -- the point of a batch run is to
    collect them."""
    return [{
        "term": report.term,
        "wallet": report.wallet,
        "username": report.username,
        "status": ("resolved" if report.resolved else (report.stage or "failed")),
        "cache": report.cache_state,
        "x": report.x_username,
        "error": report.error,
    } for report in reports]


def print_summary(reports: list[TermReport]) -> None:
    rows = summary_rows(reports)
    if not rows:
        return
    widths = {
        name: max([len(row[name]) for row in rows] + [len(name)])
        for name in ("term", "wallet", "username", "status")
    }
    print("\nsummary")
    header = "  " + "  ".join(f"{name:<{widths[name]}}"
                              for name in ("term", "wallet", "username", "status"))
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in rows:
        line = "  " + "  ".join(
            f"{(row[name] or ('' if name != 'wallet' else '')):<{widths[name]}}"
            for name in ("term", "wallet", "username", "status")
        )
        if row["error"]:
            line += f"  {row['error'][:60]}"
        print(line.rstrip())
    missing = [row for row in rows if not row["wallet"]]
    if missing:
        print(f"  {len(missing)} of {len(rows)} term(s) have no Pump profile; "
              "the per-term VERDICT above names the stage and the fix")


def write_summary_csv(reports: list[TermReport], destination: str | Path) -> Path:
    fields = ["term", "wallet", "username", "status", "cache", "x", "error"]
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
    async with httpx.AsyncClient(timeout=60) as http:
        pump = PumpClient(http)
        evm = PumpEvmResolver(http, PUMP_EVM_CACHE_FILE)
        resolver = PumpProfileResolver(
            pump, args.cache, evm=evm, persist=not args.no_write
        )
        counts = resolver.counts()
        print(f"cache {args.cache}: {counts['total']} wallet(s) "
              f"({counts['found']} profile, {counts['missing']} none, "
              f"{counts['aliases']} alias)")
        print(f"ttl: profile {PROFILE_TTL / 3600:.0f}h, "
              f"negative {NEGATIVE_TTL / 3600:.0f}h, card {CARD_TTL:.0f}s")
        for term in args.terms:
            reports.append(await diagnose(resolver, pump, term, args))

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
    return 0 if all(report.resolved for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
