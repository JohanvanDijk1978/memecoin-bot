"""Offline coverage for the `/fomo` wallet-resolution diagnostic.

Every function under test is pure: it turns FOMO payloads and the resolvers'
own log records into the stage that lost the wallet. No network, so this runs
in any sandbox -- the live path is `python fomo_resolve_diag.py <handle>`.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from fomo_resolve_diag import (
    EVM_RULES,
    SOLANA_RULES,
    classify,
    evm_transfer_providers,
    evm_verdict,
    explain_evm_evidence,
    explain_solana_swaps,
    solana_swap_reason,
    solana_verdict,
    summary_rows,
    write_summary_csv,
)

MINT_A = "zj1jpp6VxBkXbYt6cLZQmFCVSPXn7ULhL3dNK6BF8ry2k"[:44]
MINT_B = "5P3DUdtjWQ7q7hRHVvhbFy2mWnqzcpZuBmZuVkNyDVgH"
WSOL = "So11111111111111111111111111111111111111112"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TOKEN_BASE = "0xe172e9b6cfbeeb5593bdce3f077356fdb33af904"


def sol_buy(mint: str = MINT_A, amount: float = 25540.61,
            created: str = "2026-08-19T13:52:03.014Z",
            network: int | None = 1399811149) -> dict:
    row = {
        "createdAt": created,
        "inTokenAddress": WSOL,
        "inHumanAmount": 1.0,
        "outTokenAddress": mint,
        "outHumanAmount": amount,
    }
    if network is not None:
        row["networkId"] = network
    return row


def evm_buy(created: str = "2026-08-19T13:52:03.000Z") -> dict:
    return {
        "createdAt": created,
        "networkId": 8453,
        "inTokenAddress": USDC_BASE,
        "inHumanAmount": 250,
        "outTokenAddress": TOKEN_BASE,
        "outHumanAmount": 1000,
    }


class SolanaSwapReasonTests(unittest.TestCase):
    def test_solana_buy_is_usable(self) -> None:
        self.assertIsNone(solana_swap_reason(sol_buy()))

    def test_legacy_row_without_network_is_usable(self) -> None:
        self.assertIsNone(solana_swap_reason(sol_buy(network=None)))

    def test_evm_contract_is_named_as_such(self) -> None:
        reason = solana_swap_reason(evm_buy())
        self.assertEqual(reason, "non-Solana mint (EVM contract)")

    def test_wrong_network_id_names_the_chain(self) -> None:
        row = sol_buy(network=56)
        self.assertEqual(solana_swap_reason(row), "networkId 56 (bsc)")

    def test_missing_leg_is_reported(self) -> None:
        self.assertEqual(
            solana_swap_reason({"createdAt": "2026-08-19T13:52:03Z"}),
            "no usable token leg",
        )

    def test_non_object_row(self) -> None:
        self.assertEqual(solana_swap_reason("nope"), "not an object")


class ExplainSolanaSwapsTests(unittest.TestCase):
    def test_counts_and_reason_breakdown(self) -> None:
        payload = {"swaps": [sol_buy(), sol_buy(MINT_B, 7.5), evm_buy(), evm_buy()]}
        facts = explain_solana_swaps(payload)
        self.assertEqual(facts["total"], 4)
        self.assertEqual(facts["usable"], 2)
        self.assertEqual(facts["buys"], 2)
        self.assertEqual(facts["rejected"], {"non-Solana mint (EVM contract)": 2})
        self.assertEqual(facts["distinct_mints"], 2)

    def test_evm_only_window_yields_no_usable_rows(self) -> None:
        facts = explain_solana_swaps({"swaps": [evm_buy(), evm_buy()]})
        self.assertEqual((facts["total"], facts["usable"]), (2, 0))
        self.assertEqual(facts["picks"], [])

    def test_bare_list_payload_is_accepted(self) -> None:
        self.assertEqual(explain_solana_swaps([sol_buy()])["usable"], 1)

    def test_missing_payload_is_not_an_error(self) -> None:
        self.assertEqual(explain_solana_swaps(None)["total"], 0)


class ExplainEvmEvidenceTests(unittest.TestCase):
    def test_exact_swap_legs_are_counted_per_chain(self) -> None:
        swaps = {"swaps": [
            dict(evm_buy(), id="s1", outTradeId="t1"),
            dict(evm_buy("2026-08-19T14:10:00.000Z"), id="s2", outTradeId="t1"),
        ]}
        trades = {"activeTrades": [{"trade": {
            "id": "t1", "tokenAddress": TOKEN_BASE, "networkId": 8453,
            "createdAt": "2026-08-19T13:52:03.000Z", "humanTokenAmount": "2000",
        }}]}
        facts = explain_evm_evidence(swaps, trades)
        self.assertEqual(facts["items"], 2)
        self.assertEqual(facts["exact"], 2)
        self.assertEqual(facts["aggregate"], 0)
        self.assertEqual(facts["tokens"], 1)
        self.assertEqual(facts["chains"], {"base": 2})

    def test_trade_row_alone_is_aggregate_evidence(self) -> None:
        trades = {"activeTrades": [{"trade": {
            "id": "t9", "tokenAddress": TOKEN_BASE, "networkId": 8453,
            "createdAt": "2026-08-19T13:52:03.000Z", "humanTokenAmount": "2000",
        }}]}
        facts = explain_evm_evidence(None, trades)
        self.assertEqual((facts["items"], facts["exact"], facts["aggregate"]), (1, 0, 1))

    def test_solana_only_profile_has_no_evm_evidence(self) -> None:
        facts = explain_evm_evidence({"swaps": [sol_buy()]}, None)
        self.assertEqual(facts["items"], 0)
        self.assertEqual(facts["searched_tokens"], 0)


class ProviderTests(unittest.TestCase):
    def test_alchemy_endpoints_are_detected_per_chain(self) -> None:
        providers = evm_transfer_providers({
            "base": ["https://mainnet.base.org"],
            "bsc": ["https://bsc-dataseed.bnbchain.org",
                    "https://bnb-mainnet.g.alchemy.com/v2/key"],
            "ethereum": [],
        })
        self.assertEqual(
            providers, {"base": False, "bsc": True, "ethereum": False}
        )


class ClassifyTests(unittest.TestCase):
    def test_first_matching_rule_wins(self) -> None:
        stage, _hint, message = classify(
            ["All configured Solana RPCs failed; wallet discovery paused for 15s"],
            SOLANA_RULES,
        )
        self.assertEqual(stage, "rpc")
        self.assertIn("paused", message)

    def test_no_match_returns_none(self) -> None:
        self.assertIsNone(classify(["something unrelated"], SOLANA_RULES))


class SolanaVerdictTests(unittest.TestCase):
    def test_rpc_outage_beats_the_swap_facts(self) -> None:
        facts = {"rpc_urls": ["https://mainnet.helius-rpc.com"],
                 "swaps": {"total": 50, "usable": 40}}
        stage, reason, hint = solana_verdict(
            facts, ["getSignaturesForAddress: Solana RPC cooling down for 12.0s"]
        )
        self.assertEqual(stage, "rpc")
        self.assertIn("cooling down", reason)
        self.assertIn("SOLANA_RPC", hint)

    def test_unfound_transaction_after_every_route(self) -> None:
        facts = {"rpc_urls": ["x"], "swaps": {"total": 50, "usable": 4}}
        stage, _reason, hint = solana_verdict(
            facts,
            ["no transaction-backed Solana wallet match for rowdy across 4 usable swap(s)"],
        )
        self.assertEqual(stage, "discovery")
        self.assertIn("FOMO_SPONSORS", hint)
        self.assertIn("getBlock", hint)

    def test_disabled_block_route_is_a_config_problem(self) -> None:
        """The bot ran without the only route FOMO's growth cannot outrun."""
        facts = {"rpc_urls": ["x"], "swaps": {"total": 50, "usable": 4}}
        stage, _reason, hint = solana_verdict(
            facts,
            ["no transaction-backed Solana wallet match for rowdy across 4 "
             "usable swap(s) (block route off: set FOMO_WALLET_DEEP=1)"],
        )
        self.assertEqual(stage, "config")
        self.assertIn("FOMO_WALLET_DEEP=1", hint)

    def test_evm_only_window_is_an_evidence_miss(self) -> None:
        facts = {"rpc_urls": ["x"], "swaps": {"total": 50, "usable": 0}}
        stage, reason, _hint = solana_verdict(facts, [])
        self.assertEqual(stage, "evidence")
        self.assertIn("no Solana rows", reason)

    def test_empty_panel_is_separated_from_an_evm_only_trader(self) -> None:
        facts = {"rpc_urls": ["x"], "swaps": {"total": 0, "usable": 0}}
        stage, reason, _hint = solana_verdict(facts, [])
        self.assertEqual(stage, "panels")
        self.assertIn("no swaps at all", reason)

    def test_missing_rpc_configuration(self) -> None:
        stage, _reason, hint = solana_verdict({"rpc_urls": []}, [])
        self.assertEqual(stage, "config")
        self.assertIn("SOLANA_RPC", hint)

    def test_balance_fallback_without_helius(self) -> None:
        facts = {"rpc_urls": ["x"], "swaps": {"total": 50, "usable": 3}}
        stage, _reason, hint = solana_verdict(
            facts, ["Solana balance fallback skipped: no Helius RPC configured"]
        )
        self.assertEqual(stage, "config")
        self.assertIn("Helius", hint)


class EvmVerdictTests(unittest.TestCase):
    def test_no_evidence_means_a_solana_only_profile(self) -> None:
        facts = {"evidence": {"items": 0}, "providers": {"base": True}}
        stage, reason, _hint = evm_verdict(facts, [])
        self.assertEqual(stage, "evidence")
        self.assertIn("no EVM trade fingerprints", reason)

    def test_single_evidence_item_asks_for_details(self) -> None:
        facts = {"evidence": {"items": 1}, "providers": {"base": True}}
        stage, reason, hint = evm_verdict(facts, [])
        self.assertEqual(stage, "evidence")
        self.assertIn("two independent", reason)
        self.assertIn("--details", hint)

    def test_deployment_rejection_is_named(self) -> None:
        facts = {"evidence": {"items": 4}, "providers": {"base": True}}
        stage, reason, _hint = evm_verdict(
            facts,
            ["transaction candidate 0xabc for rowdy is not deployed on an evidence chain"],
        )
        self.assertEqual(stage, "deployment")
        self.assertIn("not deployed", reason)

    def test_ambiguous_ranking_points_at_manual_resolution(self) -> None:
        facts = {"evidence": {"items": 6}, "providers": {"base": True}}
        stage, _reason, hint = evm_verdict(
            facts,
            ["ambiguous transaction-backed EVM wallet for rowdy across 6 evidence "
             "item(s): 0xa and 0xb both score (2, 0, 2)"],
        )
        self.assertEqual(stage, "ranking")
        self.assertIn("evm_resolve.py", hint)

    def test_empty_transfer_search_is_named(self) -> None:
        facts = {"evidence": {"items": 3}, "providers": {"base": True}}
        stage, _reason, hint = evm_verdict(
            facts, ["no EVM transfers found for 0xtoken on bsc"]
        )
        self.assertEqual(stage, "transfers")
        self.assertIn("Blockscout", hint)

    def test_missing_alchemy_endpoint_is_reported_when_nothing_was_logged(self) -> None:
        facts = {"evidence": {"items": 3},
                 "providers": {"base": True, "bsc": False, "ethereum": False}}
        stage, reason, hint = evm_verdict(facts, [])
        self.assertEqual(stage, "config")
        self.assertIn("bsc", reason)
        self.assertIn("BSC_RPC", hint)

    def test_candidates_without_two_transactions(self) -> None:
        facts = {"evidence": {"items": 5}, "providers": {"base": True}}
        stage, _reason, hint = evm_verdict(
            facts,
            ["no transaction-backed EVM wallet for rowdy: 5 evidence item(s) "
             "produced 3 candidate(s), none explaining two transactions"],
        )
        self.assertEqual(stage, "discovery")
        self.assertIn("evm_diag.py", hint)

    def test_every_evm_rule_carries_a_stage_and_a_fix(self) -> None:
        for needle, stage, hint in EVM_RULES:
            self.assertTrue(needle and stage and hint)


SOL_WALLET = "5dB6rj9CoXMLQCAymoC5UXCb1LtFjbM5rbut3MNuj9Q"
EVM_WALLET = "0xcbaea88c17888e3c7b84117bd6ebbdbf4f95b810"


def report(handle: str, solana: str | None, solana_stage: str,
           evm: str | None, evm_stage: str) -> dict:
    return {"handle": handle, "chains": {
        "solana": {"wallet": solana, "stage": solana_stage},
        "evm": {"wallet": evm, "stage": evm_stage},
    }}


class SummaryTests(unittest.TestCase):
    def test_wallets_are_never_abbreviated(self) -> None:
        """A summary you cannot paste into an explorer is not a summary."""
        rows = summary_rows([report("397397", SOL_WALLET, "resolved",
                                    EVM_WALLET, "resolved")])
        self.assertEqual(rows[0]["solana"], SOL_WALLET)
        self.assertEqual(rows[0]["evm"], EVM_WALLET)
        self.assertEqual(rows[0]["solana_status"], "resolved")

    def test_a_miss_carries_the_stage_not_a_blank(self) -> None:
        rows = summary_rows([report("_keephungry", None, "evidence",
                                    EVM_WALLET, "resolved")])
        self.assertEqual(rows[0]["solana"], "")
        self.assertEqual(rows[0]["solana_status"], "evidence")
        self.assertEqual(rows[0]["evm_status"], "resolved")

    def test_profile_errors_do_not_masquerade_as_wallet_misses(self) -> None:
        rows = summary_rows([{"handle": "ghost", "error": "No FOMO trader"}])
        self.assertEqual(rows[0]["solana_status"], "error")
        self.assertEqual(rows[0]["error"], "No FOMO trader")

    def test_single_chain_runs_omit_the_other_column(self) -> None:
        rows = summary_rows(
            [report("397397", SOL_WALLET, "resolved", None, "evidence")],
            ("solana",),
        )
        self.assertEqual(set(rows[0]), {"handle", "solana", "solana_status", "error"})

    def test_csv_export_round_trips_full_addresses(self) -> None:
        reports = [report("397397", SOL_WALLET, "resolved", EVM_WALLET, "resolved"),
                   report("MemeKingdom", SOL_WALLET, "resolved", None, "deployment")]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_summary_csv(reports, "both", Path(tmp) / "out" / "w.csv")
            with path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["solana"], SOL_WALLET)
        self.assertEqual(rows[0]["evm"], EVM_WALLET)
        self.assertEqual(rows[1]["evm"], "")
        self.assertEqual(rows[1]["evm_status"], "deployment")


if __name__ == "__main__":
    unittest.main()
