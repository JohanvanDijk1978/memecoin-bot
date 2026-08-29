"""Offline coverage for the `/pump` wallet diagnostic.

Everything under test is either pure or driven by a fake resolver, so this
runs with no network -- the live path is `python pump_resolve_diag.py <term>`.

The two things worth protecting here are the ones that make the tool useful
when it says "no EVM wallet": that the gate ladder ranks a near-miss above a
never-started, and that a Solana-only portfolio is reported as *nothing to
fingerprint* rather than as a failure.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import pump_evm
from pump_evm import (
    EXAMINED_POSITIONS,
    HolderIndex,
    PumpEvmMatch,
    order_positions,
)
from pump_resolve_diag import (
    EVM_AMBIGUOUS,
    EVM_CACHED,
    EVM_NO_INDEX,
    EVM_NO_MATCH,
    EVM_NO_POSITIONS,
    EVM_ADOPTED,
    EVM_ADOPT_REFUSED,
    EVM_NO_PROFILE,
    EVM_PROGRESS,
    EVM_TRUNCATED,
    EVM_RESOLVED,
    EVM_SKIPPED,
    EVM_UNVERIFIED,
    EVM_VERDICTS,
    RequestLog,
    TermReport,
    chain_breakdown,
    chain_name,
    diagnose_evm,
    evm_verdict,
    furthest,
    summary_rows,
    walk_evm,
    write_summary_csv,
)

SOLANA = "5f1AoBaqeBZ3sQhNVQp7xYANb7ykj4xzYBh8eW5RYyFE"
EVM = "0x1160079f1463dc5f9f20b1f1b9cf628718649c18"
OTHER = "0xb367caea2d18bf1ee2aa2638af3efc87fc8512d7"
BSC_TOKEN = "0xd2a6d9fb47abb8196c9ef63404b4b31318727777"
RH_TOKEN = "0x020bfc650a365f8bb26819deaabf3e21291018b4"
AMOUNT = Decimal("16626666.454836158008322413")


def evm_row(token: str = BSC_TOKEN, chain_id: int = 56,
            amount: str = str(AMOUNT), value: float = 4200.0) -> dict:
    return {
        "coinMint": token,
        "amountHeld": amount,
        "chainId": chain_id,
        "valueUsd": value,
        "hasTransfers": False,
    }


def solana_row(value: float = 900.0) -> dict:
    return {
        "coinMint": "6mrqa4cDaqBCD9UrUiUyoK78e4AJ8XzRaF8uTbcuTVaepump",
        "amountHeld": "1000",
        "chainId": 1399811149,
        "valueUsd": value,
    }


class FakeProfile:
    def __init__(self, address: str = SOLANA, username: str = "1000XCryptoD"):
        self.address = address
        self.username = username


class FakeEvm:
    """Stands in for `PumpEvmResolver` with the same surface the tool uses."""

    def __init__(self, rows=None, holders=None, verified=True, match=None,
                 complete=True):
        self.rows = rows if rows is not None else []
        self.holders = holders if holders is not None else []
        self.complete = complete
        self.verified = verified
        self.match = match
        self._matches: dict[str, PumpEvmMatch] = {}
        self.cache_file = Path("pump_evm_cache.json")
        self.rpcs = {56: ["https://bsc-dataseed.bnbchain.org"]}
        self.resolved_calls = 0
        self.adopted = None

    def cached(self, wallet: str):
        return self._matches.get(wallet)

    async def portfolio_rows(self, solana: str):
        return list(self.rows)

    async def holder_index(self, position, *, pages=None, budget=None):
        self.index_calls = getattr(self, "index_calls", 0) + 1
        return HolderIndex(list(self.holders), "Blockscout",
                           complete=self.complete, pages=1, status=200,
                           stopped=getattr(self, "stopped", ""))

    async def corroborate(self, address, positions, exclude=None):
        return 0

    async def _verify_balance(self, position, address):
        return (self.verified, position.amount if self.verified else None)

    async def adopt(self, user, address, *, require: int = 1):
        self.adopt_calls = getattr(self, "adopt_calls", 0) + 1
        self.adopted_address = address
        return self.adopted

    async def resolve(self, user, *, fresh: bool = False, pages=None):
        self.resolved_calls += 1
        self.resolved_pages = pages
        return self.match


def options(**overrides) -> argparse.Namespace:
    base = dict(evm=True, fresh=False, no_write=False, verbose=False,
                adopt_evm="", evm_positions=EXAMINED_POSITIONS)
    base.update(overrides)
    return argparse.Namespace(**base)


def run(coroutine):
    return asyncio.run(coroutine)


class GateLadderTests(unittest.TestCase):
    def test_every_outcome_has_a_verdict(self):
        for status in (*EVM_PROGRESS, EVM_CACHED, EVM_SKIPPED):
            with self.subTest(status=status):
                stage, reason, hint = evm_verdict(status)
                self.assertTrue(stage and reason)
                self.assertIn(status, EVM_VERDICTS)

    def test_unknown_status_is_not_silently_a_success(self):
        stage, reason, _hint = evm_verdict("nonsense")
        self.assertEqual(stage, "evm")
        self.assertIn("unclassified", reason)

    def test_furthest_reports_the_gate_that_got_closest(self):
        self.assertEqual(
            furthest([EVM_NO_INDEX, EVM_UNVERIFIED, EVM_NO_MATCH]),
            EVM_UNVERIFIED,
        )
        self.assertEqual(furthest([EVM_NO_MATCH, EVM_NO_INDEX]), EVM_NO_MATCH)

    def test_furthest_of_nothing_is_no_positions(self):
        self.assertEqual(furthest([]), EVM_NO_POSITIONS)
        self.assertEqual(furthest(["unrelated"]), EVM_NO_POSITIONS)


class PortfolioTests(unittest.TestCase):
    def test_solana_rows_are_counted_but_never_candidates(self):
        rows = [solana_row(), solana_row(), evm_row()]
        self.assertEqual(chain_breakdown(rows), {"Solana": 2, "BSC": 1})
        self.assertEqual(len(order_positions(rows)), 1)

    def test_a_solana_only_portfolio_has_nothing_to_fingerprint(self):
        rows = [solana_row(), solana_row()]
        self.assertEqual(order_positions(rows), [])
        report = TermReport(term="x", wallet=SOLANA)
        run(diagnose_evm(FakeEvm(rows=rows), FakeProfile(), report, options(),
                         RequestLog()))
        self.assertEqual(report.evm_status, EVM_NO_POSITIONS)
        self.assertEqual(report.evm_stage, "evm-portfolio")
        self.assertEqual(report.positions_total, 2)
        self.assertEqual(report.positions_usable, 0)
        self.assertFalse(report.evm_resolved)

    def test_unknown_chain_ids_are_named_not_dropped(self):
        self.assertEqual(chain_name(56), "BSC")
        self.assertEqual(chain_name(4663), "Robinhood")
        self.assertEqual(chain_name(1399811149), "Solana")
        self.assertEqual(chain_name(999), "chain 999")
        self.assertEqual(chain_breakdown([{"chainId": None}]), {"unknown": 1})


class WalkTests(unittest.TestCase):
    def positions(self, rows=None):
        return order_positions(rows or [evm_row()])

    def test_silent_holder_index_is_reported_as_such(self):
        gates = run(walk_evm(FakeEvm(holders=[]), self.positions(), 8))
        self.assertEqual([gate.status for gate in gates], [EVM_NO_INDEX])
        self.assertEqual(gates[0].holders, 0)

    def test_no_holder_at_that_balance_in_a_complete_list(self):
        holders = [(OTHER, Decimal("5")), (EVM, Decimal("7"))]
        gates = run(walk_evm(FakeEvm(holders=holders), self.positions(), 8))
        self.assertEqual(gates[0].status, EVM_NO_MATCH)
        self.assertEqual(gates[0].holders, 2)
        self.assertEqual(gates[0].matches, 0)
        self.assertTrue(gates[0].complete)

    def test_a_truncated_index_is_not_reported_as_an_absence(self):
        # The `/pump eth` bug: the wallet held 372.5228803259225 at holder
        # rank ~1211 and discovery read only the first 250 rows. Calling that
        # "no such holder" is the mistake this gate exists to prevent.
        holders = [(OTHER, Decimal("5")), (EVM, Decimal("7"))]
        gates = run(walk_evm(FakeEvm(holders=holders, complete=False),
                             self.positions(), 8))
        self.assertEqual(gates[0].status, EVM_TRUNCATED)
        self.assertFalse(gates[0].complete)

    def test_the_truncated_gate_outranks_a_complete_miss(self):
        self.assertEqual(furthest([EVM_NO_MATCH, EVM_TRUNCATED]), EVM_TRUNCATED)

    def test_two_holders_at_that_balance_are_refused(self):
        holders = [(OTHER, AMOUNT), (EVM, AMOUNT)]
        gates = run(walk_evm(FakeEvm(holders=holders), self.positions(), 8))
        self.assertEqual(gates[0].status, EVM_AMBIGUOUS)
        self.assertEqual(gates[0].matches, 2)
        self.assertFalse(gates[0].candidate)

    def test_unique_match_that_the_chain_does_not_confirm(self):
        gates = run(walk_evm(
            FakeEvm(holders=[(EVM, AMOUNT)], verified=False),
            self.positions(), 8,
        ))
        self.assertEqual(gates[0].status, EVM_UNVERIFIED)
        self.assertEqual(gates[0].candidate, EVM)
        self.assertFalse(gates[0].verified)

    def test_unique_confirmed_match_stops_the_walk(self):
        rows = [evm_row(), evm_row(token=RH_TOKEN, chain_id=4663)]
        gates = run(walk_evm(FakeEvm(holders=[(EVM, AMOUNT)]),
                             order_positions(rows), 8))
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0].status, EVM_RESOLVED)
        self.assertTrue(gates[0].verified)

    def test_the_budget_is_honoured(self):
        rows = [evm_row(token=f"0x{index:040x}") for index in range(1, 6)]
        gates = run(walk_evm(FakeEvm(holders=[]), order_positions(rows), 2))
        self.assertEqual(len(gates), 2)


class DiagnoseEvmTests(unittest.TestCase):
    def report(self) -> TermReport:
        return TermReport(term="1000XCryptoD", wallet=SOLANA)

    def match(self) -> PumpEvmMatch:
        return PumpEvmMatch(
            solana=SOLANA, handle="1000XCryptoD", evm=EVM, chain_id=56,
            token=BSC_TOKEN, balance=str(AMOUNT),
            discovered_at="2026-08-20T10:51:09.532835+00:00",
            verified_onchain=True,
        )

    def test_a_cached_mapping_costs_no_requests(self):
        evm = FakeEvm()
        evm._matches[SOLANA] = self.match()
        report = self.report()
        run(diagnose_evm(evm, FakeProfile(), report, options(), RequestLog()))
        self.assertEqual(report.evm_status, EVM_CACHED)
        self.assertEqual(report.evm_wallet, EVM)
        self.assertEqual(report.evm_chain, "BSC")
        self.assertEqual(evm.resolved_calls, 0)

    def test_fresh_ignores_the_cache_and_rediscovers(self):
        evm = FakeEvm(rows=[evm_row()], holders=[(EVM, AMOUNT)])
        evm.adopted = self.match()
        evm._matches[SOLANA] = self.match()
        report = self.report()
        run(diagnose_evm(evm, FakeProfile(), report, options(fresh=True),
                         RequestLog()))
        self.assertEqual(report.evm_status, EVM_RESOLVED)
        self.assertEqual(evm.index_calls, 1)

    def test_the_resolver_makes_the_decision_the_walk_explains(self):
        evm = FakeEvm(rows=[evm_row()], holders=[(EVM, AMOUNT)])
        evm.adopted = self.match()
        report = self.report()
        run(diagnose_evm(evm, FakeProfile(), report, options(), RequestLog()))
        self.assertEqual(report.evm_status, EVM_RESOLVED)
        self.assertEqual(report.evm_wallet, EVM)
        self.assertTrue(report.evm_verified)
        # The decision still comes from the resolver...
        self.assertEqual(evm.adopt_calls, 1)
        self.assertEqual(evm.adopted_address, EVM)

    def test_a_found_wallet_is_never_searched_for_twice(self):
        # The bug this exists to prevent: the walk found and confirmed the
        # wallet in four minutes of paging, printed it, and then re-ran the
        # entire holder search to "let the resolver decide". It looked hung.
        evm = FakeEvm(rows=[evm_row()], holders=[(EVM, AMOUNT)])
        evm.adopted = self.match()
        report = self.report()
        run(diagnose_evm(evm, FakeProfile(), report, options(), RequestLog()))
        self.assertEqual(report.evm_wallet, EVM)
        self.assertEqual(evm.index_calls, 1)
        self.assertEqual(evm.resolved_calls, 0)

    def test_the_diagnostic_can_afford_depth_and_the_card_cannot(self):
        self.assertGreater(pump_evm.HOLDER_PAGES, pump_evm.HOLDER_PAGES_CARD)
        self.assertGreater(pump_evm.HOLDER_SECONDS, pump_evm.CARD_SECONDS)

    def test_no_write_reports_the_candidate_without_persisting_it(self):
        evm = FakeEvm(rows=[evm_row()], holders=[(EVM, AMOUNT)])
        report = self.report()
        run(diagnose_evm(evm, FakeProfile(), report, options(no_write=True),
                         RequestLog()))
        self.assertEqual(report.evm_wallet, EVM)
        self.assertEqual(evm.resolved_calls, 0)
        self.assertEqual(getattr(evm, "adopt_calls", 0), 0)

    def test_no_profile_means_the_solana_verdict_is_the_one_to_fix(self):
        report = TermReport(term="ghost")
        run(diagnose_evm(FakeEvm(), None, report, options(), RequestLog()))
        self.assertEqual(report.evm_status, EVM_NO_PROFILE)
        self.assertEqual(report.evm_stage, "profile")

    def test_no_evm_flag_skips_discovery_entirely(self):
        evm = FakeEvm(rows=[evm_row()], holders=[(EVM, AMOUNT)])
        report = self.report()
        run(diagnose_evm(evm, FakeProfile(), report, options(evm=False),
                         RequestLog()))
        self.assertEqual(report.evm_status, EVM_SKIPPED)
        self.assertEqual(evm.resolved_calls, 0)


class AdoptTests(unittest.TestCase):
    """`--adopt-evm` is a shortcut past the SEARCH, never past the evidence."""

    def match(self) -> PumpEvmMatch:
        return PumpEvmMatch(
            solana=SOLANA, handle="eth", evm=EVM, chain_id=4663,
            token=RH_TOKEN, balance="372.5228803259225",
            discovered_at="2026-08-29T18:00:00+00:00",
            verified_onchain=True, corroborations=2,
        )

    def test_a_confirmed_address_is_cached_like_a_discovered_one(self):
        evm = FakeEvm()
        evm.adopted = self.match()
        report = TermReport(term="eth", wallet=SOLANA)
        run(diagnose_evm(evm, FakeProfile(), report, options(adopt_evm=EVM),
                         RequestLog()))
        self.assertEqual(report.evm_status, EVM_ADOPTED)
        self.assertEqual(report.evm_wallet, EVM)
        self.assertTrue(report.evm_verified)
        self.assertEqual(report.evm_corroborations, 2)

    def test_an_address_the_chain_rejects_is_not_written(self):
        evm = FakeEvm()          # adopted stays None -> resolver refused it
        report = TermReport(term="eth", wallet=SOLANA)
        run(diagnose_evm(evm, FakeProfile(), report, options(adopt_evm=OTHER),
                         RequestLog()))
        self.assertEqual(report.evm_status, EVM_ADOPT_REFUSED)
        self.assertEqual(report.evm_wallet, "")
        self.assertFalse(report.evm_resolved)

    def test_adopting_never_falls_through_to_a_holder_search(self):
        evm = FakeEvm(rows=[evm_row()], holders=[(EVM, AMOUNT)])
        evm.adopted = self.match()
        report = TermReport(term="eth", wallet=SOLANA)
        run(diagnose_evm(evm, FakeProfile(), report, options(adopt_evm=EVM),
                         RequestLog()))
        self.assertEqual(evm.resolved_calls, 0)
        self.assertEqual(evm.adopt_calls, 1)


class BudgetTests(unittest.TestCase):
    """A deep search still has to end, and has to say why it ended."""

    def test_a_budgeted_stop_is_reported_not_hidden(self):
        evm = FakeEvm(rows=[evm_row()], holders=[(OTHER, Decimal("5"))],
                      complete=False)
        evm.stopped = "budget"
        gates = run(walk_evm(evm, order_positions([evm_row()]), 8))
        self.assertEqual(gates[0].stopped, "budget")
        self.assertIn("stopped: budget", gates[0].line())

    def test_rate_limit_headroom_is_read_from_the_response(self):
        class Response:
            headers = {"x-ratelimit-remaining": "3"}
        self.assertEqual(pump_evm._rate_remaining(Response()), 3)

        class Bare:
            headers: dict = {}
        self.assertIsNone(pump_evm._rate_remaining(Bare()))


class ExplorerHeaderTests(unittest.TestCase):
    """Cloudflare refuses httpx's default UA, and the refusal looks like an
    empty holder list. Every explorer call must carry a User-Agent."""

    def test_explorer_requests_identify_themselves(self):
        self.assertIn("User-Agent", pump_evm.EXPLORER_HEADERS)
        self.assertTrue(pump_evm.EXPLORER_HEADERS["User-Agent"])
        self.assertNotIn("httpx", pump_evm.EXPLORER_HEADERS["User-Agent"].lower())

    def test_no_explorer_call_sends_a_bare_accept_header(self):
        source = Path(pump_evm.__file__).read_text(encoding="utf-8")
        self.assertNotIn('headers={"Accept": "application/json"}', source)


class RequestLogTests(unittest.TestCase):
    def test_path_based_api_keys_are_not_logged(self):
        log = RequestLog()
        secret = "https://eth-mainnet.g.alchemy.com/v2/SUPERSECRETKEY"
        log.rpc_urls = {secret}
        self.assertNotIn("SUPERSECRET", log.label(secret))
        self.assertEqual(log.label(secret), "https://eth-mainnet.g.alchemy.com")

    def test_public_routes_keep_their_path_but_drop_the_query(self):
        log = RequestLog()
        self.assertEqual(
            log.label("https://frontend-api-v3.pump.fun/users/eth?x=1"),
            "https://frontend-api-v3.pump.fun/users/eth",
        )


class SummaryTests(unittest.TestCase):
    def reports(self) -> list[TermReport]:
        found = TermReport(term="1000XCryptoD", wallet=SOLANA,
                           username="1000XCryptoD", evm_wallet=EVM,
                           evm_chain="BSC")
        none = TermReport(term="eth", wallet="9CQKjrYoMht2b19dm7gXpLAt1EuE8izq",
                          username="eth", evm_stage="evm-holders",
                          evm_status=EVM_TRUNCATED)
        return [found, none]

    def test_summary_carries_both_wallets_in_full(self):
        rows = summary_rows(self.reports())
        self.assertEqual(rows[0]["evm"], EVM)
        self.assertEqual(rows[0]["evm_status"], "resolved")
        self.assertEqual(rows[1]["evm"], "")
        # The status, not the stage: a truncated index and a silent one both
        # sit in the `evm-holders` stage and need completely different fixes.
        self.assertEqual(rows[1]["evm_status"], EVM_TRUNCATED)

    def test_csv_is_exportable_and_untruncated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_summary_csv(self.reports(),
                                     Path(directory) / "out.csv")
            with path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["wallet"], SOLANA)
        self.assertEqual(rows[0]["evm"], EVM)
        self.assertEqual(rows[0]["evm_chain"], "BSC")
        self.assertEqual(rows[1]["evm"], "")


if __name__ == "__main__":
    unittest.main()
