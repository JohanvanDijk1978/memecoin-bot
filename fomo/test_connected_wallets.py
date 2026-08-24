"""Coverage for `/connected`: what counts as a connection, and what does not.

The rule this file exists to protect is that a **swap is not a connection**.
Most of these tests assert that something is *not* reported -- a Jupiter or
Meteora leg, a transfer too small to mean anything, an exchange, a program
account, a high-degree service -- because the command's whole value is that
the wallets it does list are wallets a person actually moved money with.

The second rule is the funding wallet: it is found by reading a wallet's
*oldest* transaction, and it is reported only when the lookup genuinely
reached that far. No network.
"""

from __future__ import annotations

import time
import unittest

import connected_wallets as cw
import solscan_api
from connected_wallets import (
    MIN_EVM_USD,
    MIN_SOL,
    MIN_STABLE,
    Connection,
    Funding,
    Relationship,
    Transfer,
    build_relationships,
    evm_transfers_from_rows,
    is_plain_transfer,
    known_label,
    rank_connections,
    report_from_payload,
    report_payload,
    solana_transfers_from_history,
)

KNOWN = "KnownAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
FRIEND = "FriendAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"
STRANGER = "StrangeAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA3"
FUNDER = "FunderAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA6"
BINANCE = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
METEORA_POOL = "MeteoraPoolAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA7"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
EVM_KNOWN = "0x1111111111111111111111111111111111111111"
EVM_FRIEND = "0x2222222222222222222222222222222222222222"
DAY = 86400
SOL = 1_000_000_000


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


class PlainTransferTests(unittest.TestCase):
    """The filter the whole command turns on."""

    def test_a_plain_send_is_a_transfer(self) -> None:
        self.assertTrue(is_plain_transfer(
            {"type": "TRANSFER", "source": "SYSTEM_PROGRAM"}))
        self.assertTrue(is_plain_transfer(
            {"type": "TRANSFER", "source": "SOLANA_PROGRAM_LIBRARY"}))

    def test_a_swap_is_not(self) -> None:
        for source in ("JUPITER", "RAYDIUM", "METEORA", "ORCA", "PUMP_FUN"):
            with self.subTest(source=source):
                self.assertFalse(is_plain_transfer(
                    {"type": "SWAP", "source": source}))

    def test_a_swap_wearing_a_transfer_type_is_still_not(self) -> None:
        # Two independent catches: the DEX source, and the swap event.
        self.assertFalse(is_plain_transfer(
            {"type": "TRANSFER", "source": "METEORA"}))
        self.assertFalse(is_plain_transfer({
            "type": "TRANSFER", "source": "UNKNOWN",
            "events": {"swap": {"nativeInput": {"amount": "1"}}},
        }))

    def test_liquidity_and_nft_moves_are_not_transfers(self) -> None:
        for kind in ("ADD_LIQUIDITY", "WITHDRAW_LIQUIDITY", "NFT_SALE",
                     "UNKNOWN", ""):
            with self.subTest(kind=kind):
                self.assertFalse(is_plain_transfer({"type": kind}))

    def test_a_failed_transaction_moved_nothing(self) -> None:
        self.assertFalse(is_plain_transfer(
            {"type": "TRANSFER", "transactionError": {"InstructionError": []}}))


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


class RankingTests(unittest.TestCase):
    def test_the_biggest_mover_leads(self) -> None:
        small = relationship(address=STRANGER, sent_usd=90.0, received_usd=0.0,
                             sent_count=9, received_count=0)
        big = relationship(sent_usd=60_000.0, received_usd=55_000.0)
        ranked = rank_connections([small, big])
        self.assertEqual([item.address for item in ranked], [FRIEND, STRANGER])

    def test_transfer_count_breaks_a_tie(self) -> None:
        few = relationship(address=STRANGER, sent_count=2, received_count=0)
        many = relationship(sent_count=20, received_count=4)
        ranked = rank_connections([few, many])
        self.assertEqual(ranked[0].address, FRIEND)

    def test_the_funder_is_pinned_to_the_top(self) -> None:
        big = relationship(sent_usd=1_000_000.0)
        funder = relationship(address=STRANGER, sent_usd=1.0, received_usd=0.0)
        ranked = rank_connections([big, funder], funders=[STRANGER])
        self.assertEqual(ranked[0].address, STRANGER)
        self.assertTrue(ranked[0].funder)
        self.assertFalse(ranked[1].funder)

    def test_a_single_qualifying_transfer_is_still_a_connection(self) -> None:
        """The transfer rule is the bar; there is no repetition requirement."""
        one = relationship(sent_count=1, received_count=0, sent_usd=4_000.0,
                           received_usd=0.0, days={0})
        self.assertEqual(len(rank_connections([one])), 1)


class SolanaParsingTests(unittest.TestCase):
    def _entry(self, **overrides) -> dict:
        entry = {
            "type": "TRANSFER", "source": "SYSTEM_PROGRAM",
            "signature": "sig1", "timestamp": 1_700_000_000,
            "nativeTransfers": [
                {"fromUserAccount": KNOWN, "toUserAccount": FRIEND,
                 "amount": 2 * SOL},
            ],
            "tokenTransfers": [
                {"fromUserAccount": FRIEND, "toUserAccount": KNOWN,
                 "mint": USDC, "tokenAmount": 500},
                {"fromUserAccount": FRIEND, "toUserAccount": KNOWN,
                 "mint": "SomeMemeMint111111111111111111111111111111",
                 "tokenAmount": 1_000_000},
            ],
        }
        entry.update(overrides)
        return entry

    def test_sol_is_priced_and_direction_is_read(self) -> None:
        transfers = solana_transfers_from_history(
            [self._entry()], KNOWN, sol_price=150.0)
        native = next(t for t in transfers if t.symbol == "SOL")
        self.assertTrue(native.outgoing)
        self.assertEqual(native.amount, 2.0)
        self.assertEqual(native.usd, 300.0)

    def test_a_stablecoin_over_the_bar_is_priced(self) -> None:
        usdc = next(t for t in solana_transfers_from_history(
            [self._entry()], KNOWN) if t.symbol == "USDC")
        self.assertEqual(usdc.usd, 500)
        self.assertFalse(usdc.outgoing)

    def test_a_memecoin_is_not_evidence_of_anything(self) -> None:
        """It cannot be priced honestly, so it cannot clear a value bar."""
        symbols = {t.symbol for t in
                   solana_transfers_from_history([self._entry()], KNOWN)}
        self.assertEqual(symbols, {"SOL", "USDC"})

    def test_a_swap_leg_never_becomes_a_transfer(self) -> None:
        swap = self._entry(
            type="SWAP", source="METEORA", signature="swap1",
            nativeTransfers=[{"fromUserAccount": KNOWN,
                              "toUserAccount": METEORA_POOL,
                              "amount": 40 * SOL}],
            tokenTransfers=[],
        )
        self.assertEqual(solana_transfers_from_history([swap], KNOWN), [])

    def test_dust_and_small_sends_are_below_the_bar(self) -> None:
        small = self._entry(
            nativeTransfers=[{"fromUserAccount": KNOWN,
                              "toUserAccount": FRIEND,
                              "amount": int(0.4 * SOL)}],
            tokenTransfers=[{"fromUserAccount": KNOWN, "toUserAccount": FRIEND,
                             "mint": USDC, "tokenAmount": 12}],
        )
        self.assertEqual(solana_transfers_from_history([small], KNOWN), [])
        self.assertEqual(MIN_SOL, 1.0)
        self.assertEqual(MIN_STABLE, 50.0)

    def test_the_bar_can_be_lifted_for_the_funding_lookup(self) -> None:
        small = self._entry(
            type="SWAP", source="RAYDIUM",
            nativeTransfers=[{"fromUserAccount": FUNDER,
                              "toUserAccount": KNOWN,
                              "amount": int(0.02 * SOL)}],
            tokenTransfers=[],
        )
        found = solana_transfers_from_history(
            [small], KNOWN, min_sol=0.0, min_stable=0.0, skip_swaps=False)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].counterparty, FUNDER)
        self.assertFalse(found[0].outgoing)

    def test_transfers_between_other_people_are_ignored(self) -> None:
        entry = self._entry(
            signature="sig2",
            nativeTransfers=[{"fromUserAccount": FRIEND,
                              "toUserAccount": STRANGER, "amount": 10 * SOL}],
            tokenTransfers=[],
        )
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

    def test_a_native_transfer_under_the_dollar_bar_is_dropped(self) -> None:
        self.assertEqual(MIN_EVM_USD, 200.0)
        transfers = evm_transfers_from_rows(
            [self._row(EVM_KNOWN, EVM_FRIEND, "ETH", 0.01)], [],
            EVM_KNOWN, "base", native_price=3000.0,
        )
        self.assertEqual(transfers, [])

    def test_an_unpriceable_native_transfer_cannot_clear_the_bar(self) -> None:
        transfers = evm_transfers_from_rows(
            [self._row(EVM_KNOWN, EVM_FRIEND, "ETH", 50.0)], [],
            EVM_KNOWN, "base",
        )
        self.assertEqual(transfers, [])

    def test_an_unknown_token_is_not_counted(self) -> None:
        transfers = evm_transfers_from_rows(
            [self._row(EVM_KNOWN, EVM_FRIEND, "PEPE", 1e9)], [],
            EVM_KNOWN, "base", native_price=3000.0,
        )
        self.assertEqual(transfers, [])

    def test_the_bar_can_be_lifted_for_the_funding_lookup(self) -> None:
        transfers = evm_transfers_from_rows(
            [], [self._row(EVM_FRIEND, EVM_KNOWN, "ETH", 0.001)],
            EVM_KNOWN, "base", min_usd=0.0, min_stable=0.0,
        )
        self.assertEqual(len(transfers), 1)
        self.assertFalse(transfers[0].outgoing)

    def test_a_row_returned_by_both_directions_is_counted_once(self) -> None:
        row = self._row(EVM_KNOWN, EVM_KNOWN, "ETH", 1.0)
        transfers = evm_transfers_from_rows(
            [row], [row], EVM_KNOWN, "base", native_price=3000.0)
        self.assertEqual(len(transfers), 1)


class ReportRoundTripTests(unittest.TestCase):
    def test_a_report_survives_the_cache(self) -> None:
        item = Connection(relationship(references=["sig-a", "sig-b"]),
                          funder=True)
        funding = Funding(
            wallet=KNOWN, chain="solana", address=FUNDER, amount=0.05,
            symbol="SOL", timestamp=1_700_000_000, reference="fund-sig",
            identity="rowdy", exact=True,
        )
        report = cw.ConnectedReport(
            wallets=((KNOWN, "solana"),), funding=(funding,),
            connections=(item,), transactions=412,
            warnings=("solana: truncated",), generated_at=int(time.time()),
        )
        restored = report_from_payload(report_payload(report))
        self.assertEqual(restored.transactions, 412)
        self.assertEqual(restored.wallets, report.wallets)
        self.assertTrue(restored.connections[0].funder)
        self.assertEqual(
            restored.connections[0].relationship.references, ["sig-a", "sig-b"]
        )
        self.assertEqual(
            restored.connections[0].relationship.active_days,
            item.relationship.active_days,
        )
        self.assertEqual(restored.funding[0].address, FUNDER)
        self.assertTrue(restored.funding[0].exact)
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
              lamports: int = 5 * SOL, **overrides) -> dict:
    entry = {
        "type": "TRANSFER", "source": "SYSTEM_PROGRAM",
        "signature": reference,
        "timestamp": 1_700_000_000 + day * DAY,
        "nativeTransfers": [{"fromUserAccount": sender,
                             "toUserAccount": recipient, "amount": lamports}],
    }
    entry.update(overrides)
    return entry


class FakeChainHttp:
    """One Helius history per address, one account-type reply, one price.

    History is served newest first, the way Helius serves it, because the
    funding lookup depends on that ordering being what the code thinks it is.
    """

    def __init__(self) -> None:
        self.history: dict[str, list[dict]] = {}
        self.owners: dict[str, dict] = {}
        self.gets: list[str] = []
        self.solscan: object = None

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        self.gets.append(url)
        if "dexscreener" in url:
            return FakeResponse({"pairs": [
                {"priceUsd": "150", "liquidity": {"usd": 1_000_000}},
            ]})
        if "solscan" in url:
            return FakeResponse(self.solscan or {"success": True, "data": []})
        address = url.rsplit("/addresses/", 1)[-1].split("/")[0]
        rows = sorted(self.history.get(address, []),
                      key=lambda row: row.get("timestamp", 0), reverse=True)
        return FakeResponse(rows)

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

        # `solscan_api` remembers which prefix/header answered, and which paths
        # answered nothing at all -- process-wide, by design. Left standing
        # between tests it leaks: a test that rejects the key would silence
        # Solscan for the next one.
        solscan_api.reset_resolution()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.http = FakeChainHttp()
        # A long, reciprocal, well-spread relationship with FRIEND; the same
        # pattern with a pool account and a busy service wallet; the wallet's
        # first ever transaction, a 0.02 SOL top-up from FUNDER; and a pile of
        # Meteora swaps, which is what this command used to report.
        history = [native_tx("fund-sig", -10, FUNDER, KNOWN,
                             lamports=int(0.02 * SOL))]
        for index in range(12):
            outgoing = index % 2 == 0
            history.append(native_tx(
                f"friend-{index}", index * 20,
                KNOWN if outgoing else FRIEND, FRIEND if outgoing else KNOWN,
            ))
        for label, other in (("pool", POOL_ACCOUNT), ("svc", SERVICE)):
            for index in range(12):
                outgoing = index % 2 == 0
                history.append(native_tx(
                    f"{label}-{index}", index * 20,
                    KNOWN if outgoing else other, other if outgoing else KNOWN,
                ))
        for index in range(30):
            history.append(native_tx(
                f"meteora-{index}", index, KNOWN, METEORA_POOL,
                lamports=40 * SOL, type="SWAP", source="METEORA",
            ))
        self.http.history[KNOWN] = history
        self.http.history[FRIEND] = [native_tx("f-own", 1, FRIEND, KNOWN)]
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

    async def _addresses(self, **kwargs) -> set:
        report = await self._analyzer(**kwargs).analyse([(KNOWN, "solana")])
        return {item.address for item in report.connections}

    async def test_a_real_relationship_is_reported(self) -> None:
        report = await self._analyzer().analyse([(KNOWN, "solana")])
        found = next(item for item in report.connections
                     if item.address == FRIEND)
        self.assertEqual(found.relationship.transfers, 12)
        self.assertTrue(found.relationship.reciprocal)
        self.assertTrue(found.relationship.references)

    async def test_a_meteora_pool_is_never_a_connection(self) -> None:
        """Thirty swaps of 40 SOL each, and it is still not a relationship."""
        self.assertNotIn(METEORA_POOL, await self._addresses())

    async def test_a_program_owned_account_is_excluded(self) -> None:
        self.assertNotIn(POOL_ACCOUNT, await self._addresses())

    async def test_a_high_degree_wallet_is_excluded(self) -> None:
        self.assertNotIn(SERVICE, await self._addresses())

    async def test_both_would_have_qualified_without_the_filters(self) -> None:
        """The exclusions have to be doing the work, not the transfer bar."""
        self.http.owners.clear()
        original = cw.HIGH_DEGREE_COUNTERPARTIES
        cw.HIGH_DEGREE_COUNTERPARTIES = 10_000
        try:
            found = await self._addresses()
        finally:
            cw.HIGH_DEGREE_COUNTERPARTIES = original
        self.assertIn(POOL_ACCOUNT, found)
        self.assertIn(SERVICE, found)

    async def test_the_funding_wallet_is_the_oldest_money_in(self) -> None:
        report = await self._analyzer().analyse([(KNOWN, "solana")])
        self.assertEqual(len(report.funding), 1)
        funding = report.funding[0]
        self.assertEqual(funding.address, FUNDER)
        self.assertEqual(funding.reference, "fund-sig")
        self.assertAlmostEqual(funding.amount, 0.02)
        self.assertTrue(funding.exact)

    async def test_the_funder_is_not_held_to_the_transfer_bar(self) -> None:
        """0.02 SOL is far under 1, and it is still the funding wallet."""
        report = await self._analyzer().analyse([(KNOWN, "solana")])
        self.assertNotIn(FUNDER,
                         {item.address for item in report.connections})
        self.assertEqual(report.funding[0].address, FUNDER)

    async def test_an_unreachable_funder_is_admitted_not_guessed(self) -> None:
        original = cw.FUNDING_PAGES
        cw.FUNDING_PAGES = 1
        self.http.history[KNOWN] = [
            native_tx(f"deep-{index}", index, FRIEND, KNOWN)
            for index in range(cw.HELIUS_TX_LIMIT)
        ]
        try:
            report = await self._analyzer().analyse([(KNOWN, "solana")])
        finally:
            cw.FUNDING_PAGES = original
        self.assertEqual(report.funding, ())
        self.assertTrue(any("deeper than" in note for note in report.warnings))

    async def test_solscan_answers_the_funder_in_one_request(self) -> None:
        self.http.solscan = {"success": True, "data": [{
            "from_address": FUNDER, "to_address": KNOWN,
            "token_address": "", "token_decimals": 9,
            "amount": int(0.05 * SOL), "block_time": 1_690_000_000,
            "trans_id": "solscan-sig",
        }]}
        analyzer = self._analyzer()
        analyzer.solscan_key = "test-key"
        report = await analyzer.analyse([(KNOWN, "solana")])
        self.assertEqual(report.funding[0].address, FUNDER)
        self.assertEqual(report.funding[0].reference, "solscan-sig")
        self.assertAlmostEqual(report.funding[0].amount, 0.05)
        self.assertTrue(any("solscan" in url for url in self.http.gets))

    async def test_a_rejected_solscan_key_falls_back_to_helius(self) -> None:
        self.http.solscan = {"success": False}
        analyzer = self._analyzer()
        analyzer.solscan_key = "bad-key"
        report = await analyzer.analyse([(KNOWN, "solana")])
        self.assertEqual(report.funding[0].address, FUNDER)
        self.assertEqual(report.funding[0].reference, "fund-sig")

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

    async def test_an_old_cached_report_is_not_read_back(self) -> None:
        """v1 reports carried scores and bands; the key keeps them out."""
        self.assertTrue(cw.CACHE_SCHEMA)
        analyzer = self._analyzer()
        await analyzer.analyse([(KNOWN, "solana")])
        keys = [key for key in analyzer.cache._entries]  # noqa: SLF001
        self.assertTrue(all(key.startswith(cw.CACHE_SCHEMA) for key in keys))

    async def test_an_identity_lookup_names_the_candidate(self) -> None:
        analyzer = self._analyzer(
            identify=lambda address: "rowdy" if address == FRIEND else None
        )
        report = await analyzer.analyse([(KNOWN, "solana")])
        found = next(item for item in report.connections
                     if item.address == FRIEND)
        self.assertEqual(found.relationship.identity, "rowdy")

    async def test_the_funder_is_named_too(self) -> None:
        analyzer = self._analyzer(
            identify=lambda address: "banker" if address == FUNDER else None
        )
        report = await analyzer.analyse([(KNOWN, "solana")])
        self.assertEqual(report.funding[0].identity, "banker")


if __name__ == "__main__":
    unittest.main()
