"""Coverage for `/connected`: parsing, scoring, filtering and the analyzer.

The rule this file exists to protect is precision. Most of these tests assert
that something is *not* reported -- a single transfer, an exchange, a program
account, a high-degree service -- because a wrong association is worse here
than no association at all. No network.
"""

from __future__ import annotations

import time
import unittest

import connected_wallets as cw
from connected_wallets import (
    DEFAULT_MIN_SCORE,
    MIN_TRANSFERS,
    SCORE_HIGH,
    SCORE_VERY_HIGH,
    Relationship,
    Transfer,
    build_relationships,
    evm_transfers_from_rows,
    known_label,
    link_cross_chain,
    rank_associations,
    report_from_payload,
    report_payload,
    score_relationship,
    solana_transfers_from_history,
)

KNOWN = "KnownAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
FRIEND = "FriendAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"
STRANGER = "StrangeAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA3"
BINANCE = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
EVM_KNOWN = "0x1111111111111111111111111111111111111111"
EVM_FRIEND = "0x2222222222222222222222222222222222222222"
DAY = 86400


def transfer(counterparty: str, outgoing: bool, usd: float | None, day: int,
             reference: str = "") -> Transfer:
    return Transfer(
        counterparty=counterparty, outgoing=outgoing, amount=1.0, symbol="SOL",
        usd=usd, timestamp=1_700_000_000 + day * DAY,
        reference=reference or f"{counterparty[:4]}-{day}-{int(outgoing)}",
    )


def relationship(**overrides) -> Relationship:
    record = Relationship(
        address=FRIEND, chain="solana", known_wallet=KNOWN,
        sent_count=overrides.pop("sent_count", 6),
        received_count=overrides.pop("received_count", 5),
        sent_usd=overrides.pop("sent_usd", 60_000.0),
        received_usd=overrides.pop("received_usd", 55_000.0),
        first_seen=overrides.pop("first_seen", 1_700_000_000),
        last_seen=overrides.pop("last_seen", 1_700_000_000 + 240 * DAY),
        first_direction=overrides.pop("first_direction", "out"),
    )
    record.days = set(overrides.pop("days", range(30)))
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


class LabelTests(unittest.TestCase):
    def test_known_infrastructure_is_named(self) -> None:
        self.assertEqual(known_label(BINANCE), "Binance")
        self.assertEqual(known_label(cw.FOMO_GAS_SPONSOR), "FOMO gas sponsor")
        self.assertEqual(
            known_label("0x7A250D5630B4cF539739dF2C5dAcb4c659F2488D"),
            "Uniswap V2 router",
        )

    def test_an_ordinary_wallet_is_not(self) -> None:
        self.assertIsNone(known_label(FRIEND))
        self.assertIsNone(known_label(EVM_FRIEND))

    def test_operator_labels_win(self) -> None:
        self.assertEqual(
            known_label(FRIEND, {FRIEND: "Team treasury"}), "Team treasury"
        )


class RelationshipTests(unittest.TestCase):
    def test_transfers_group_by_counterparty(self) -> None:
        records = build_relationships([
            transfer(FRIEND, True, 100, 0),
            transfer(FRIEND, False, 50, 3),
            transfer(STRANGER, True, 10, 1),
        ], KNOWN, "solana")
        by_address = {record.address: record for record in records}
        self.assertEqual(by_address[FRIEND].transfers, 2)
        self.assertEqual(by_address[FRIEND].sent_usd, 100)
        self.assertEqual(by_address[FRIEND].received_usd, 50)
        self.assertTrue(by_address[FRIEND].reciprocal)
        self.assertEqual(by_address[FRIEND].active_days, 2)

    def test_the_earliest_transfer_sets_the_direction(self) -> None:
        records = build_relationships([
            transfer(FRIEND, False, 1, 9),
            transfer(FRIEND, True, 1, 2),
        ], KNOWN, "solana")
        self.assertEqual(records[0].first_direction, "out")

    def test_an_exchange_never_becomes_a_candidate(self) -> None:
        records = build_relationships(
            [transfer(BINANCE, True, 10_000, day) for day in range(10)],
            KNOWN, "solana",
        )
        self.assertEqual(records, [])

    def test_the_wallet_is_not_its_own_counterparty(self) -> None:
        records = build_relationships(
            [transfer(KNOWN, True, 1, 0)], KNOWN, "solana")
        self.assertEqual(records, [])

    def test_unpriced_transfers_are_counted_not_invented(self) -> None:
        records = build_relationships(
            [transfer(FRIEND, True, None, day) for day in range(4)],
            KNOWN, "solana",
        )
        self.assertEqual(records[0].unpriced, 4)
        self.assertEqual(records[0].total_usd, 0.0)


class ScoringTests(unittest.TestCase):
    def test_a_single_transfer_is_never_scored(self) -> None:
        record = relationship(sent_count=1, received_count=0, days={0})
        self.assertEqual(rank_associations([record], min_score=0), [])
        self.assertLess(1, MIN_TRANSFERS)

    def test_a_long_reciprocal_high_value_relationship_is_very_high(self) -> None:
        item = score_relationship(relationship())
        self.assertGreaterEqual(item.score, SCORE_VERY_HIGH)
        self.assertEqual(item.band, "Very High")
        self.assertIn("reciprocity", item.signals)
        self.assertIn("longevity", item.signals)
        self.assertIn("funding", item.signals)

    def test_one_loud_signal_cannot_reach_a_band_on_its_own(self) -> None:
        # Fifty transfers, all on one day, one direction, unpriced.
        record = relationship(
            sent_count=50, received_count=0, sent_usd=0.0, received_usd=0.0,
            first_seen=1_700_000_000, last_seen=1_700_000_000, days={0},
            first_direction="out",
        )
        item = score_relationship(record)
        self.assertLessEqual(len(item.signals), 2)
        self.assertNotEqual(item.band, "Very High")

    def test_an_unknown_contract_is_penalised(self) -> None:
        plain = score_relationship(relationship())
        contract = score_relationship(relationship(is_contract=True))
        self.assertLess(contract.score, plain.score)
        self.assertTrue(
            any("contract code" in reason for reason in contract.reasons)
        )

    def test_a_known_identity_is_not_penalised_for_being_a_contract(self) -> None:
        item = score_relationship(relationship(is_contract=True, identity="rowdy"))
        self.assertFalse(any("contract code" in r for r in item.reasons))
        self.assertIn("identity", item.signals)

    def test_the_reasons_name_the_evidence(self) -> None:
        item = score_relationship(relationship())
        joined = " ".join(item.reasons)
        self.assertIn("direct transfers", joined)
        self.assertIn("separate dates", joined)

    def test_ranking_keeps_only_what_clears_the_bar(self) -> None:
        strong = relationship()
        weak = relationship(
            sent_count=3, received_count=0, sent_usd=0.0, received_usd=0.0,
            last_seen=1_700_000_000 + DAY, days={0, 1},
        )
        kept = rank_associations([strong, weak], min_score=DEFAULT_MIN_SCORE)
        self.assertEqual([item.relationship for item in kept], [strong])

    def test_the_score_never_leaves_its_range(self) -> None:
        item = score_relationship(relationship(
            sent_count=500, received_count=500,
            sent_usd=10_000_000.0, received_usd=10_000_000.0,
            days=range(365), identity="rowdy",
        ))
        self.assertLessEqual(item.score, 100)
        self.assertGreaterEqual(item.score, 0)


class CrossChainTests(unittest.TestCase):
    def test_one_identity_on_two_chains_is_stronger(self) -> None:
        solana = score_relationship(relationship(identity="rowdy"))
        evm = score_relationship(relationship(
            address=EVM_FRIEND, chain="base", identity="rowdy"))
        linked = link_cross_chain([solana, evm])
        self.assertTrue(all(item.score >= solana.score for item in linked))
        self.assertTrue(all("cross-chain" in item.signals for item in linked))

    def test_an_identity_on_one_chain_is_left_alone(self) -> None:
        only = score_relationship(relationship(identity="rowdy"))
        self.assertEqual(link_cross_chain([only])[0].score, only.score)


class SolanaParsingTests(unittest.TestCase):
    def _entry(self) -> dict:
        return {
            "signature": "sig1", "timestamp": 1_700_000_000,
            "nativeTransfers": [
                {"fromUserAccount": KNOWN, "toUserAccount": FRIEND,
                 "amount": 2_000_000_000},
            ],
            "tokenTransfers": [
                {"fromUserAccount": FRIEND, "toUserAccount": KNOWN,
                 "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                 "tokenAmount": 500},
                {"fromUserAccount": FRIEND, "toUserAccount": KNOWN,
                 "mint": "SomeMemeMint111111111111111111111111111111",
                 "tokenAmount": 1_000_000},
            ],
        }

    def test_sol_is_priced_and_direction_is_read(self) -> None:
        transfers = solana_transfers_from_history(
            [self._entry()], KNOWN, sol_price=150.0)
        native = next(t for t in transfers if t.symbol == "SOL")
        self.assertTrue(native.outgoing)
        self.assertEqual(native.amount, 2.0)
        self.assertEqual(native.usd, 300.0)

    def test_stablecoins_are_priced_and_memecoins_are_not(self) -> None:
        transfers = solana_transfers_from_history([self._entry()], KNOWN)
        usdc = next(t for t in transfers if t.symbol == "USDC")
        self.assertEqual(usdc.usd, 500)
        self.assertFalse(usdc.outgoing)
        unpriced = [t for t in transfers if t.usd is None and t.symbol != "SOL"]
        self.assertEqual(len(unpriced), 1)

    def test_transfers_between_other_people_are_ignored(self) -> None:
        entry = {
            "signature": "sig2", "timestamp": 1,
            "nativeTransfers": [{"fromUserAccount": FRIEND,
                                 "toUserAccount": STRANGER, "amount": 10}],
        }
        self.assertEqual(solana_transfers_from_history([entry], KNOWN), [])


class EvmParsingTests(unittest.TestCase):
    def _row(self, sender: str, recipient: str, asset: str, value: float,
             reference: str = "0xabc") -> dict:
        return {
            "from": sender, "to": recipient, "asset": asset, "value": value,
            "hash": reference,
            "metadata": {"blockTimestamp": "2026-08-20T13:07:47.000Z"},
        }

    def test_stables_and_native_are_priced(self) -> None:
        transfers = evm_transfers_from_rows(
            [self._row(EVM_KNOWN, EVM_FRIEND, "ETH", 2.0)],
            [self._row(EVM_FRIEND, EVM_KNOWN, "USDC", 900.0, "0xdef")],
            EVM_KNOWN, "base", native_price=3000.0,
        )
        by_symbol = {item.symbol: item for item in transfers}
        self.assertEqual(by_symbol["ETH"].usd, 6000.0)
        self.assertTrue(by_symbol["ETH"].outgoing)
        self.assertEqual(by_symbol["USDC"].usd, 900.0)
        self.assertFalse(by_symbol["USDC"].outgoing)

    def test_an_unknown_token_carries_no_usd(self) -> None:
        transfers = evm_transfers_from_rows(
            [self._row(EVM_KNOWN, EVM_FRIEND, "PEPE", 1e9)], [],
            EVM_KNOWN, "base", native_price=3000.0,
        )
        self.assertIsNone(transfers[0].usd)

    def test_a_row_returned_by_both_directions_is_counted_once(self) -> None:
        row = self._row(EVM_KNOWN, EVM_KNOWN, "ETH", 1.0)
        transfers = evm_transfers_from_rows([row], [row], EVM_KNOWN, "base")
        self.assertEqual(len(transfers), 1)


class ReportRoundTripTests(unittest.TestCase):
    def test_a_report_survives_the_cache(self) -> None:
        item = score_relationship(relationship(references=["sig-a", "sig-b"]))
        report = cw.ConnectedReport(
            wallets=((KNOWN, "solana"),), associations=(item,), weaker=(),
            transactions=412, warnings=("solana: truncated",),
            generated_at=int(time.time()),
        )
        restored = report_from_payload(report_payload(report))
        self.assertEqual(restored.transactions, 412)
        self.assertEqual(restored.wallets, report.wallets)
        self.assertEqual(restored.associations[0].score, item.score)
        self.assertEqual(
            restored.associations[0].relationship.references, ["sig-a", "sig-b"]
        )
        self.assertEqual(
            restored.associations[0].relationship.active_days,
            item.relationship.active_days,
        )
        self.assertTrue(restored.cached)


class FakeResponse:
    def __init__(self, value: object, status_code: int = 200) -> None:
        self.value = value
        self.status_code = status_code

    def json(self) -> object:
        return self.value


POOL_ACCOUNT = "PoolAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4"
SERVICE = "ServiceAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA5"


def native_tx(reference: str, day: int, sender: str, recipient: str,
              lamports: int = 5_000_000_000) -> dict:
    return {
        "signature": reference,
        "timestamp": 1_700_000_000 + day * DAY,
        "nativeTransfers": [{"fromUserAccount": sender,
                             "toUserAccount": recipient, "amount": lamports}],
    }


class FakeChainHttp:
    """One Helius history per address, one account-type reply, one price."""

    def __init__(self) -> None:
        self.history: dict[str, list[dict]] = {}
        self.owners: dict[str, dict] = {}
        self.gets: list[str] = []

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        self.gets.append(url)
        if "dexscreener" in url:
            return FakeResponse({"pairs": [
                {"priceUsd": "150", "liquidity": {"usd": 1_000_000}},
            ]})
        address = url.rsplit("/addresses/", 1)[-1].split("/")[0]
        return FakeResponse(self.history.get(address, []))

    async def post(self, _url: str, **kwargs) -> FakeResponse:
        request = kwargs.get("json") or {}
        if request.get("method") == "getMultipleAccounts":
            addresses = request["params"][0]
            return FakeResponse({"result": {"value": [
                self.owners.get(address, {
                    "owner": cw.SOLANA_SYSTEM_PROGRAM, "executable": False,
                })
                for address in addresses
            ]}})
        return FakeResponse({"result": None})


class AnalyzerTests(unittest.IsolatedAsyncioTestCase):
    HELIUS = "https://mainnet.helius-rpc.com/?api-key=k"

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.http = FakeChainHttp()
        # A long, reciprocal, well-spread relationship with FRIEND; a handful
        # of transfers with a pool account and with a busy service wallet.
        history = []
        for index in range(12):
            outgoing = index % 2 == 0
            history.append(native_tx(
                f"friend-{index}", index * 20,
                KNOWN if outgoing else FRIEND, FRIEND if outgoing else KNOWN,
            ))
        # The pool and the service get the *same* strong pattern, so the only
        # thing that can keep them off the card is the filtering.
        for label, other in (("pool", POOL_ACCOUNT), ("svc", SERVICE)):
            for index in range(12):
                outgoing = index % 2 == 0
                history.append(native_tx(
                    f"{label}-{index}", index * 20,
                    KNOWN if outgoing else other, other if outgoing else KNOWN,
                ))
        self.http.history[KNOWN] = history
        self.http.history[FRIEND] = [
            native_tx("f-own", 1, FRIEND, KNOWN),
        ]
        self.http.history[SERVICE] = [
            native_tx(f"svc-own-{index}", index, SERVICE, f"user{index}")
            for index in range(cw.HIGH_DEGREE_COUNTERPARTIES + 5)
        ]
        self.http.owners[POOL_ACCOUNT] = {
            "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "executable": False,
        }

    def tearDown(self) -> None:
        import os

        os.unlink(self.tmp.name)

    def _analyzer(self, **overrides) -> cw.ConnectedWalletAnalyzer:
        return cw.ConnectedWalletAnalyzer(
            self.http, [self.HELIUS], evm_rpcs={},
            cache_path=self.tmp.name, **overrides,
        )

    async def test_a_strong_relationship_is_reported(self) -> None:
        report = await self._analyzer().analyse([(KNOWN, "solana")])
        addresses = {item.address for item in report.associations}
        self.assertIn(FRIEND, addresses)
        found = next(item for item in report.associations if item.address == FRIEND)
        self.assertGreaterEqual(found.score, SCORE_HIGH)
        self.assertEqual(found.relationship.transfers, 12)
        self.assertTrue(found.relationship.reciprocal)
        self.assertTrue(found.relationship.references)

    async def _everything(self, **kwargs) -> set:
        report = await self._analyzer().analyse(
            [(KNOWN, "solana")], min_score=0, **kwargs)
        return {item.address for item in report.associations + report.weaker}

    async def test_a_program_owned_account_is_excluded(self) -> None:
        self.assertNotIn(POOL_ACCOUNT, await self._everything())

    async def test_a_high_degree_wallet_is_excluded(self) -> None:
        self.assertNotIn(SERVICE, await self._everything())

    async def test_both_would_have_qualified_without_the_filters(self) -> None:
        """The exclusions above have to be doing the work, not the score."""
        self.http.owners.clear()
        original = cw.HIGH_DEGREE_COUNTERPARTIES
        cw.HIGH_DEGREE_COUNTERPARTIES = 10_000
        try:
            everything = await self._everything()
        finally:
            cw.HIGH_DEGREE_COUNTERPARTIES = original
        self.assertIn(POOL_ACCOUNT, everything)
        self.assertIn(SERVICE, everything)

    async def test_the_cache_answers_the_second_run(self) -> None:
        analyzer = self._analyzer()
        await analyzer.analyse([(KNOWN, "solana")])
        calls = len(self.http.gets)
        again = await analyzer.analyse([(KNOWN, "solana")])
        self.assertEqual(len(self.http.gets), calls)
        self.assertTrue(again.cached)

    async def test_fresh_bypasses_the_cache(self) -> None:
        analyzer = self._analyzer()
        await analyzer.analyse([(KNOWN, "solana")])
        calls = len(self.http.gets)
        await analyzer.analyse([(KNOWN, "solana")], fresh=True)
        self.assertGreater(len(self.http.gets), calls)

    async def test_the_cache_key_separates_different_bars(self) -> None:
        analyzer = self._analyzer()
        strict = await analyzer.analyse([(KNOWN, "solana")], min_score=SCORE_VERY_HIGH)
        loose = await analyzer.analyse([(KNOWN, "solana")], min_score=0)
        self.assertFalse(loose.cached)
        self.assertLessEqual(len(strict.associations), len(loose.associations))

    async def test_an_identity_lookup_names_the_candidate(self) -> None:
        analyzer = self._analyzer(
            identify=lambda address: "rowdy" if address == FRIEND else None
        )
        report = await analyzer.analyse([(KNOWN, "solana")])
        found = next(item for item in report.associations if item.address == FRIEND)
        self.assertEqual(found.relationship.identity, "rowdy")

    async def test_without_a_helius_endpoint_it_says_so(self) -> None:
        analyzer = cw.ConnectedWalletAnalyzer(
            self.http, ["https://api.mainnet-beta.solana.com"], evm_rpcs={},
            cache_path=self.tmp.name,
        )
        report = await analyzer.analyse([(KNOWN, "solana")])
        self.assertEqual(report.associations, ())
        self.assertTrue(any("Helius" in warning for warning in report.warnings))

    async def test_no_wallet_is_a_clean_answer(self) -> None:
        report = await self._analyzer().analyse([])
        self.assertEqual(report.associations, ())
        self.assertTrue(report.warnings)

    async def test_a_provider_failure_does_not_raise(self) -> None:
        class Broken:
            async def get(self, *_a, **_k):
                raise RuntimeError("upstream 429")

            async def post(self, *_a, **_k):
                raise RuntimeError("upstream 429")

        analyzer = cw.ConnectedWalletAnalyzer(
            Broken(), [self.HELIUS], evm_rpcs={}, cache_path=self.tmp.name,
        )
        report = await analyzer.analyse([(KNOWN, "solana")])
        self.assertEqual(report.associations, ())
        self.assertTrue(any("history unavailable" in w for w in report.warnings))


if __name__ == "__main__":
    unittest.main()
