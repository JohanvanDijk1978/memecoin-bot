"""Coverage for `/token`'s Top Traders: parsing, ranking and the client.

Every provider shape here is the one the project actually reads -- Helius
parsed transactions, raw `getTransaction` balance deltas,
`alchemy_getAssetTransfers` and Blockscout token transfers. No network.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from token_traders import (
    POOL_SHARE_MIN_TRANSACTIONS,
    WSOL_MINT,
    QuoteFlow,
    TokenFlow,
    TokenTrader,
    aggregate_traders,
    attach_quote_values,
    candidate_pool,
    evaluate_trader,
    infrastructure_addresses,
    parse_alchemy_quote_flows,
    parse_alchemy_transfers,
    parse_blockscout_transfers,
    parse_helius_transactions,
    parse_rpc_transactions,
    rank_traders,
    sampled_window,
)

MINT = "E3i7sTY5QYEBh3itepnomZQt7Eh5kzmHFk1vkm2pump"
OTHER_MINT = "So11111111111111111111111111111111111111112"
ALICE = "AL1ceAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
BOB = "B0bAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"
POOL = "P00lAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA3"
TOKEN = "0xe172e9b6e0f1e3c1d0a7c2b3a4958600000000ff"
EVM_A = "0x1111111111111111111111111111111111111111"
EVM_B = "0x2222222222222222222222222222222222222222"


def helius_tx(signature: str, timestamp: int, sender: str, recipient: str,
              amount: float, mint: str = MINT) -> dict:
    return {
        "signature": signature,
        "timestamp": timestamp,
        "tokenTransfers": [{
            "fromUserAccount": sender, "toUserAccount": recipient,
            "mint": mint, "tokenAmount": amount,
        }],
    }


class HeliusParsingTests(unittest.TestCase):
    def test_a_transfer_is_a_sale_and_a_purchase(self) -> None:
        flows = parse_helius_transactions([helius_tx("sig1", 100, ALICE, BOB, 5)], MINT)
        self.assertEqual(
            {(flow.address, flow.delta) for flow in flows},
            {(ALICE, Decimal("-5")), (BOB, Decimal("5"))},
        )
        self.assertTrue(all(flow.reference == "sig1" for flow in flows))
        self.assertTrue(all(flow.timestamp == 100 for flow in flows))

    def test_another_mint_in_the_same_transaction_is_ignored(self) -> None:
        entry = helius_tx("sig1", 100, ALICE, BOB, 5)
        entry["tokenTransfers"].append({
            "fromUserAccount": BOB, "toUserAccount": ALICE,
            "mint": OTHER_MINT, "tokenAmount": 900,
        })
        flows = parse_helius_transactions([entry], MINT)
        self.assertEqual(len(flows), 2)
        self.assertEqual(sum(abs(flow.delta) for flow in flows), Decimal("10"))

    def test_a_shape_it_does_not_recognise_is_not_an_error(self) -> None:
        self.assertEqual(parse_helius_transactions({"items": []}, MINT), [])
        self.assertEqual(parse_helius_transactions([None, 7, {}], MINT), [])


class RpcParsingTests(unittest.TestCase):
    def _transaction(self, signature: str, pre: list, post: list,
                     err: object = None) -> dict:
        def rows(entries: list) -> list:
            return [{
                "mint": mint, "owner": owner,
                "uiTokenAmount": {"uiAmountString": str(amount)},
            } for owner, amount, mint in entries]

        return {
            "blockTime": 1700,
            "transaction": {"signatures": [signature]},
            "meta": {"err": err, "preTokenBalances": rows(pre),
                     "postTokenBalances": rows(post)},
        }

    def test_balance_deltas_become_flows(self) -> None:
        tx = self._transaction(
            "sigA",
            pre=[(ALICE, 100, MINT), (BOB, 0, MINT)],
            post=[(ALICE, 40, MINT), (BOB, 60, MINT)],
        )
        flows = {flow.address: flow.delta for flow in parse_rpc_transactions([tx], MINT)}
        self.assertEqual(flows[ALICE], Decimal("-60"))
        self.assertEqual(flows[BOB], Decimal("60"))

    def test_a_failed_transaction_moved_nothing(self) -> None:
        tx = self._transaction(
            "sigA", pre=[(ALICE, 100, MINT)], post=[(ALICE, 40, MINT)],
            err={"InstructionError": [0, "Custom"]},
        )
        self.assertEqual(parse_rpc_transactions([tx], MINT), [])

    def test_other_mints_do_not_leak_in(self) -> None:
        tx = self._transaction(
            "sigA", pre=[(ALICE, 100, OTHER_MINT)], post=[(ALICE, 40, OTHER_MINT)],
        )
        self.assertEqual(parse_rpc_transactions([tx], MINT), [])


class EvmParsingTests(unittest.TestCase):
    def test_alchemy_rows_become_flows(self) -> None:
        payload = {"transfers": [{
            "from": EVM_A.upper(), "to": EVM_B, "value": 12.5, "hash": "0xabc",
            "rawContract": {"address": TOKEN},
            "metadata": {"blockTimestamp": "2026-08-20T13:07:47.000Z"},
        }]}
        flows = parse_alchemy_transfers(payload, TOKEN)
        self.assertEqual({flow.address for flow in flows}, {EVM_A, EVM_B})
        self.assertTrue(all(flow.timestamp for flow in flows))

    def test_alchemy_rows_for_another_contract_are_dropped(self) -> None:
        payload = {"transfers": [{
            "from": EVM_A, "to": EVM_B, "value": 1, "hash": "0xabc",
            "rawContract": {"address": "0x" + "9" * 40},
        }]}
        self.assertEqual(parse_alchemy_transfers(payload, TOKEN), [])

    def test_blockscout_amounts_are_shifted_by_decimals(self) -> None:
        payload = {"items": [{
            "from": {"hash": EVM_A}, "to": {"hash": EVM_B},
            "total": {"value": "2500000000000000000", "decimals": "18"},
            "transaction_hash": "0xdef",
            "timestamp": "2026-08-20T13:07:47.000000Z",
        }]}
        flows = parse_blockscout_transfers(payload, TOKEN)
        self.assertEqual(
            sorted(flow.delta for flow in flows),
            [Decimal("-2.5"), Decimal("2.5")],
        )


class InfrastructureTests(unittest.TestCase):
    def _busy(self, transactions: int) -> list[TokenFlow]:
        flows: list[TokenFlow] = []
        for index in range(transactions):
            reference = f"sig{index}"
            flows.append(TokenFlow(POOL, Decimal("-1"), 100 + index, reference))
            flows.append(TokenFlow(f"trader{index}", Decimal("1"), 100 + index, reference))
        return flows

    def test_the_venue_of_every_swap_is_not_a_trader(self) -> None:
        flows = self._busy(POOL_SHARE_MIN_TRANSACTIONS + 8)
        self.assertIn(POOL, infrastructure_addresses(flows))

    def test_a_sample_too_small_to_judge_accuses_nobody(self) -> None:
        flows = self._busy(POOL_SHARE_MIN_TRANSACTIONS - 1)
        self.assertEqual(infrastructure_addresses(flows), set())

    def test_the_ranking_excludes_what_it_detected(self) -> None:
        traders = aggregate_traders(self._busy(POOL_SHARE_MIN_TRANSACTIONS + 8))
        self.assertNotIn(POOL, {trader.address for trader in traders})


class AggregationTests(unittest.TestCase):
    def _flows(self) -> list[TokenFlow]:
        return [
            TokenFlow(ALICE, Decimal("-100"), 200, "s1"),
            TokenFlow(BOB, Decimal("100"), 200, "s1"),
            TokenFlow(ALICE, Decimal("300"), 400, "s2"),
            TokenFlow(BOB, Decimal("-300"), 400, "s2"),
            TokenFlow(BOB, Decimal("5"), 600, "s3"),
        ]

    def test_volume_is_bought_plus_sold(self) -> None:
        traders = {t.address: t for t in aggregate_traders(
            self._flows(), detect_infrastructure=False)}
        self.assertEqual(traders[ALICE].bought, Decimal("300"))
        self.assertEqual(traders[ALICE].sold, Decimal("100"))
        self.assertEqual(traders[ALICE].volume, Decimal("400"))
        self.assertEqual(traders[ALICE].net, Decimal("200"))

    def test_the_busiest_address_ranks_first(self) -> None:
        traders = aggregate_traders(self._flows(), detect_infrastructure=False)
        self.assertEqual(traders[0].address, BOB)
        self.assertEqual(traders[0].transactions, 3)

    def test_first_and_last_activity_are_kept(self) -> None:
        traders = {t.address: t for t in aggregate_traders(
            self._flows(), detect_infrastructure=False)}
        self.assertEqual(traders[BOB].first_seen, 200)
        self.assertEqual(traders[BOB].last_seen, 600)

    def test_burn_addresses_are_never_traders(self) -> None:
        flows = self._flows() + [
            TokenFlow("1nc1nerator11111111111111111111111111111111",
                      Decimal("999999"), 700, "s4"),
        ]
        traders = aggregate_traders(flows, detect_infrastructure=False)
        self.assertNotIn(
            "1nc1nerator11111111111111111111111111111111",
            {trader.address for trader in traders},
        )

    def test_the_token_itself_can_be_excluded_by_the_caller(self) -> None:
        flows = self._flows() + [TokenFlow(MINT, Decimal("50"), 800, "s5")]
        traders = aggregate_traders(
            flows, exclude={MINT}, detect_infrastructure=False)
        self.assertNotIn(MINT, {trader.address for trader in traders})

    def test_the_limit_is_honoured(self) -> None:
        self.assertEqual(
            len(aggregate_traders(self._flows(), limit=1,
                                  detect_infrastructure=False)), 1)

    def test_the_sampled_window_is_the_real_one(self) -> None:
        self.assertEqual(sampled_window(self._flows()), (200, 600))
        self.assertEqual(sampled_window([]), (None, None))


class FakeResponse:
    def __init__(self, value: object, status_code: int = 200) -> None:
        self.value = value
        self.status_code = status_code

    def json(self) -> object:
        return self.value


class FakeHeliusHttp:
    """Two full pages of parsed history, then a short one."""

    def __init__(self, pages: int = 2) -> None:
        self.pages = pages
        self.requests: list[dict] = []

    async def get(self, url: str, **kwargs) -> FakeResponse:
        if "dexscreener" in url:
            return FakeResponse({"pairs": [{
                "baseToken": {"address": OTHER_MINT},
                "priceUsd": "200", "liquidity": {"usd": 1e9},
            }]})
        params = kwargs.get("params") or {}
        self.requests.append(dict(params))
        index = len(self.requests)
        if index > self.pages:
            return FakeResponse([])
        size = 100 if index < self.pages else 3
        # Forty distinct wallets, so no single address touches enough of the
        # sample to be mistaken for a pool.
        return FakeResponse([
            helius_tx(f"p{index}s{row}", 1000 + row,
                      f"w{row % 40}", f"w{(row + 17) % 40}", float(row + 1))
            for row in range(size)
        ])

    async def post(self, *_args, **_kwargs) -> FakeResponse:
        raise AssertionError("the parsed route should not need JSON-RPC")


class FakeRpcHttp:
    def __init__(self) -> None:
        self.methods: list[str] = []

    async def post(self, _url: str, **kwargs) -> FakeResponse:
        request = kwargs.get("json")
        if isinstance(request, list):
            self.methods.append("batch")
            return FakeResponse([
                {"id": index, "result": {
                    "blockTime": 1000 + index,
                    "transaction": {"signatures": [f"sig{index}"]},
                    "meta": {"err": None,
                             "preTokenBalances": [
                                 {"mint": MINT, "owner": ALICE,
                                  "uiTokenAmount": {"uiAmountString": "10"}}],
                             "postTokenBalances": [
                                 {"mint": MINT, "owner": ALICE,
                                  "uiTokenAmount": {"uiAmountString": "4"}}]},
                }}
                for index in range(len(request))
            ])
        method = request.get("method") if isinstance(request, dict) else ""
        self.methods.append(method)
        if method == "getSignaturesForAddress":
            return FakeResponse({"result": [
                {"signature": "sig1"}, {"signature": "sig2"},
                {"signature": "bad", "err": {"x": 1}},
            ]})
        return FakeResponse({"result": None})


class TopTradersClientTests(unittest.IsolatedAsyncioTestCase):
    HELIUS = "https://mainnet.helius-rpc.com/?api-key=abc123"

    def _client(self, http: object, rpcs: list[str] | None = None):
        from token_intelligence import TokenIntelligenceClient

        return TokenIntelligenceClient(http, rpcs or [self.HELIUS], evm_rpcs={})

    async def test_the_parsed_route_pages_and_ranks(self) -> None:
        http = FakeHeliusHttp()
        client = self._client(http)
        result = await client.top_traders(MINT, "Solana")
        self.assertEqual(result.source, "helius")
        self.assertEqual(result.transactions, 103)
        self.assertTrue(result.traders)
        self.assertLessEqual(len(result.traders), 40)
        volumes = [trader.volume for trader in result.traders]
        self.assertEqual(volumes, sorted(volumes, reverse=True))
        # The second page must have asked for what came before the first.
        self.assertIn("before", http.requests[1])

    async def test_the_api_key_comes_from_the_configured_rpc(self) -> None:
        self.assertEqual(self._client(FakeHeliusHttp())._helius_key(), "abc123")
        self.assertIsNone(
            self._client(FakeHeliusHttp(), ["https://api.mainnet-beta.solana.com"])
            ._helius_key()
        )

    async def test_without_helius_it_falls_back_to_batched_rpc(self) -> None:
        http = FakeRpcHttp()
        client = self._client(http, ["https://api.mainnet-beta.solana.com"])
        result = await client.top_traders(MINT, "Solana")
        self.assertEqual(result.source, "rpc")
        self.assertIn("getSignaturesForAddress", http.methods)
        self.assertIn("batch", http.methods)
        self.assertEqual({t.address for t in result.traders}, {ALICE})

    async def test_a_failing_provider_returns_an_empty_result_not_an_error(self) -> None:
        class Broken:
            async def get(self, *_a, **_k):
                raise RuntimeError("boom")

            async def post(self, *_a, **_k):
                raise RuntimeError("boom")

        result = await self._client(Broken()).top_traders(MINT, "Solana")
        self.assertEqual(result.traders, ())
        self.assertEqual(result.transactions, 0)

    async def test_the_second_lookup_is_served_from_cache(self) -> None:
        http = FakeHeliusHttp()
        client = self._client(http)
        first = await client.top_traders(MINT, "Solana")
        calls = len(http.requests)
        second = await client.top_traders(MINT, "Solana")
        self.assertEqual(len(http.requests), calls)
        self.assertEqual(first.traders, second.traders)

    async def test_an_unsupported_chain_says_so_rather_than_guessing(self) -> None:
        result = await self._client(FakeHeliusHttp()).top_traders("x", "Unknown")
        self.assertEqual(result.source, "unsupported")


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------- pricing --

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_PRICES = {WSOL_MINT: Decimal("200"), USDC: Decimal(1)}


def helius_swap(signature: str, timestamp: int, trader: str, *,
                tokens: float, usdc: float | None = None,
                lamports: int | None = None, sold: bool = False,
                error: object = None) -> dict:
    """One swap: the token leg and the money leg, as Helius returns them."""
    token_row = {
        "fromUserAccount": trader if sold else POOL,
        "toUserAccount": POOL if sold else trader,
        "mint": MINT, "tokenAmount": tokens,
    }
    entry: dict = {
        "signature": signature, "timestamp": timestamp,
        "tokenTransfers": [token_row], "nativeTransfers": [],
    }
    if error is not None:
        entry["transactionError"] = error
    if usdc is not None:
        entry["tokenTransfers"].append({
            "fromUserAccount": POOL if sold else trader,
            "toUserAccount": trader if sold else POOL,
            "mint": USDC, "tokenAmount": usdc,
        })
    if lamports is not None:
        entry["nativeTransfers"].append({
            "fromUserAccount": POOL if sold else trader,
            "toUserAccount": trader if sold else POOL,
            "amount": lamports,
        })
    return entry


class QuoteExtractionTests(unittest.TestCase):
    """The money leg of a swap is in the same transaction as the token leg."""

    def _alice(self, flows) -> TokenFlow:
        return next(flow for flow in flows if flow.address == ALICE)

    def test_a_stablecoin_leg_prices_the_trade(self) -> None:
        flows = parse_helius_transactions(
            [helius_swap("s1", 100, ALICE, tokens=1000, usdc=250)],
            MINT, prices=SOL_PRICES,
        )
        alice = self._alice(flows)
        self.assertEqual(alice.delta, Decimal("1000"))
        self.assertEqual(alice.value_usd, Decimal("250"))

    def test_a_native_sol_leg_is_converted_at_the_supplied_price(self) -> None:
        flows = parse_helius_transactions(
            [helius_swap("s1", 100, ALICE, tokens=1000,
                         lamports=2_000_000_000)],
            MINT, prices=SOL_PRICES,
        )
        self.assertEqual(self._alice(flows).value_usd, Decimal("400"))

    def test_rent_is_not_read_as_a_price(self) -> None:
        # 0.00203928 SOL is the rent for a new token account, not $0.41 of
        # trade. The stated trade-off: a genuine sub-$1 swap is read as a
        # transfer rather than as a tiny buy, which cannot reach a PnL board.
        flows = parse_helius_transactions(
            [helius_swap("s1", 100, ALICE, tokens=1000, lamports=2_039_280)],
            MINT, prices=SOL_PRICES,
        )
        self.assertEqual(self._alice(flows).value_usd, Decimal("0"))

    def test_a_plain_transfer_costs_nothing_and_says_so(self) -> None:
        entry = {
            "signature": "s1", "timestamp": 100,
            "tokenTransfers": [{
                "fromUserAccount": BOB, "toUserAccount": ALICE,
                "mint": MINT, "tokenAmount": 1000,
            }],
        }
        flows = parse_helius_transactions([entry], MINT, prices=SOL_PRICES)
        self.assertEqual(self._alice(flows).value_usd, Decimal("0"))

    def test_an_unreadable_asset_is_not_a_gift(self) -> None:
        entry = helius_swap("s1", 100, ALICE, tokens=1000)
        entry["tokenTransfers"].append({
            "fromUserAccount": ALICE, "toUserAccount": POOL,
            "mint": "SomeOtherMemecoinMint1111111111111111111111",
            "tokenAmount": 42,
        })
        flows = parse_helius_transactions([entry], MINT, prices=SOL_PRICES)
        self.assertIsNone(self._alice(flows).value_usd)

    def test_without_a_price_table_nothing_is_free(self) -> None:
        # Otherwise a failed price lookup would hand every wallet a zero cost
        # basis and an infinite return.
        entry = {
            "signature": "s1", "timestamp": 100,
            "tokenTransfers": [{
                "fromUserAccount": BOB, "toUserAccount": ALICE,
                "mint": MINT, "tokenAmount": 1000,
            }],
        }
        flows = parse_helius_transactions([entry], MINT, prices={})
        self.assertIsNone(self._alice(flows).value_usd)

    def test_without_prices_the_flow_is_unpriced_rather_than_guessed(self) -> None:
        flows = parse_helius_transactions(
            [helius_swap("s1", 100, ALICE, tokens=1000, usdc=250)], MINT
        )
        self.assertIsNone(self._alice(flows).value_usd)

    def test_a_sale_is_priced_by_what_came_back(self) -> None:
        flows = parse_helius_transactions(
            [helius_swap("s1", 100, ALICE, tokens=1000, usdc=900, sold=True)],
            MINT, prices=SOL_PRICES,
        )
        alice = self._alice(flows)
        self.assertEqual(alice.delta, Decimal("-1000"))
        self.assertEqual(alice.value_usd, Decimal("900"))

    def test_a_reverted_transaction_is_not_a_trade(self) -> None:
        flows = parse_helius_transactions(
            [helius_swap("s1", 100, ALICE, tokens=1000, usdc=250,
                         error={"InstructionError": [3, "custom"]})],
            MINT, prices=SOL_PRICES,
        )
        self.assertEqual(flows, [])

    def test_a_multi_hop_route_is_one_trade_at_one_price(self) -> None:
        entry = helius_swap("s1", 100, ALICE, tokens=600, usdc=250)
        entry["tokenTransfers"].append({
            "fromUserAccount": "OtherPool", "toUserAccount": ALICE,
            "mint": MINT, "tokenAmount": 400,
        })
        flows = parse_helius_transactions([entry], MINT, prices=SOL_PRICES)
        alice = self._alice(flows)
        self.assertEqual(alice.delta, Decimal("1000"))
        self.assertEqual(alice.value_usd, Decimal("250"))

    def test_receiving_tokens_and_usdc_together_prices_nothing(self) -> None:
        entry = helius_swap("s1", 100, ALICE, tokens=1000)
        entry["tokenTransfers"].append({
            "fromUserAccount": POOL, "toUserAccount": ALICE,
            "mint": USDC, "tokenAmount": 50,
        })
        flows = parse_helius_transactions([entry], MINT, prices=SOL_PRICES)
        self.assertIsNone(self._alice(flows).value_usd)

    def test_the_rpc_route_prices_from_balances_and_lamports(self) -> None:
        result = {
            "blockTime": 100,
            "transaction": {
                "signatures": ["sig1"],
                "message": {"accountKeys": [{"pubkey": ALICE}, {"pubkey": POOL}]},
            },
            "meta": {
                "err": None, "fee": 5000,
                "preBalances": [3_000_000_000, 0],
                "postBalances": [1_999_995_000, 0],
                "preTokenBalances": [
                    {"mint": MINT, "owner": ALICE,
                     "uiTokenAmount": {"uiAmountString": "0"}}],
                "postTokenBalances": [
                    {"mint": MINT, "owner": ALICE,
                     "uiTokenAmount": {"uiAmountString": "1000"}}],
            },
        }
        flows = parse_rpc_transactions([result], MINT, prices=SOL_PRICES)
        alice = self._alice(flows)
        self.assertEqual(alice.delta, Decimal("1000"))
        # 1 SOL spent, the fee added back, at $200.
        self.assertEqual(alice.value_usd, Decimal("200"))

    def test_the_traded_token_never_prices_itself(self) -> None:
        flows = parse_helius_transactions(
            [helius_swap("s1", 100, ALICE, tokens=1000)],
            MINT, prices={MINT: Decimal("5")},
        )
        self.assertIsNone(self._alice(flows).value_usd)


class VenueJoinTests(unittest.TestCase):
    """On EVM the trader's money arrives via a router; the pool's does not."""

    def test_the_venues_quote_leg_prices_the_swap(self) -> None:
        flows = [
            TokenFlow(EVM_A, Decimal("1000"), 100, "0xhash"),
            TokenFlow(POOL, Decimal("-1000"), 100, "0xhash"),
        ]
        quotes = [QuoteFlow(POOL, Decimal("400"), "0xhash")]
        priced = {f.address: f.value_usd
                  for f in attach_quote_values(flows, quotes, venues={POOL})}
        self.assertEqual(priced[EVM_A], Decimal("400"))

    def test_two_traders_in_one_transaction_split_it_by_size(self) -> None:
        flows = [
            TokenFlow(EVM_A, Decimal("750"), 100, "0xhash"),
            TokenFlow(EVM_B, Decimal("250"), 100, "0xhash"),
            TokenFlow(POOL, Decimal("-1000"), 100, "0xhash"),
        ]
        quotes = [QuoteFlow(POOL, Decimal("400"), "0xhash")]
        priced = {f.address: f.value_usd
                  for f in attach_quote_values(flows, quotes, venues={POOL})}
        self.assertEqual(priced[EVM_A], Decimal("300"))
        self.assertEqual(priced[EVM_B], Decimal("100"))

    def test_the_traders_own_quote_leg_wins_over_the_venues(self) -> None:
        flows = [TokenFlow(EVM_A, Decimal("1000"), 100, "0xhash")]
        quotes = [
            QuoteFlow(EVM_A, Decimal("-250"), "0xhash"),
            QuoteFlow(POOL, Decimal("400"), "0xhash"),
        ]
        priced = attach_quote_values(flows, quotes, venues={POOL})
        self.assertEqual(priced[0].value_usd, Decimal("250"))

    def test_a_transaction_with_no_quote_movement_stays_unpriced(self) -> None:
        flows = [TokenFlow(EVM_A, Decimal("1000"), 100, "0xhash")]
        priced = attach_quote_values(flows, [], venues={POOL})
        self.assertIsNone(priced[0].value_usd)

    def test_alchemy_quote_rows_become_usd(self) -> None:
        payload = {"transfers": [{
            "from": POOL.lower(), "to": EVM_A, "value": 0.5,
            "hash": "0xhash", "category": "erc20",
            "rawContract": {"address": TOKEN.upper()},
        }]}
        quotes = parse_alchemy_quote_flows(payload, {TOKEN: Decimal("3000")})
        self.assertEqual(
            {(flow.address, flow.usd) for flow in quotes},
            {(POOL.lower(), Decimal("-1500.0")), (EVM_A.lower(), Decimal("1500.0"))},
        )

    def test_an_asset_with_no_price_is_not_a_quote(self) -> None:
        payload = {"transfers": [{
            "from": POOL, "to": EVM_A, "value": 1, "hash": "0xh",
            "rawContract": {"address": "0xdeadbeef"},
        }]}
        self.assertEqual(parse_alchemy_quote_flows(payload, {TOKEN: Decimal(1)}), [])


class LedgerTests(unittest.TestCase):
    """Weighted-average cost basis: the arithmetic the ranking depends on."""

    def _trade(self, delta: str, value: str | None, timestamp: int,
               reference: str = "") -> TokenFlow:
        return TokenFlow(
            ALICE, Decimal(delta), timestamp, reference or f"s{timestamp}",
            Decimal(value) if value is not None else None,
        )

    def test_the_entry_price_is_weighted_not_averaged(self) -> None:
        # 100 @ $1 and 900 @ $0.10: the mean of the two prices is $0.55, the
        # weighted average is $0.19.
        trader = evaluate_trader(ALICE, [
            self._trade("100", "100", 100),
            self._trade("900", "90", 200),
        ])
        self.assertEqual(trader.avg_entry_price, Decimal("0.19"))
        self.assertEqual(trader.invested_usd, Decimal("190"))

    def test_a_partial_sell_realises_only_its_share_of_the_cost(self) -> None:
        trader = evaluate_trader(ALICE, [
            self._trade("1000", "100", 100),    # $0.10 each
            self._trade("-400", "200", 200),    # $0.50 each
        ], current_price=Decimal("0.5"))
        self.assertEqual(trader.realized_pnl_usd, Decimal("160"))   # 200 - 40
        self.assertEqual(trader.open_tokens, Decimal("600"))
        self.assertEqual(trader.open_cost_usd, Decimal("60"))
        self.assertEqual(trader.unrealized_pnl_usd, Decimal("240"))
        self.assertEqual(trader.total_pnl_usd, Decimal("400"))
        self.assertEqual(trader.roi_pct, Decimal("400"))

    def test_a_closed_position_is_realised_only(self) -> None:
        trader = evaluate_trader(ALICE, [
            self._trade("1000", "100", 100),
            self._trade("-1000", "450", 200),
        ], current_price=Decimal("0.5"))
        self.assertTrue(trader.realized_only)
        self.assertEqual(trader.total_pnl_usd, Decimal("350"))
        self.assertEqual(trader.unrealized_pnl_usd, Decimal("0"))
        self.assertEqual(trader.avg_exit_price, Decimal("0.45"))

    def test_a_wallet_that_never_sold_is_all_unrealised(self) -> None:
        trader = evaluate_trader(
            ALICE, [self._trade("1000", "100", 100)],
            current_price=Decimal("0.3"),
        )
        self.assertEqual(trader.realized_pnl_usd, Decimal("0"))
        self.assertEqual(trader.unrealized_pnl_usd, Decimal("200"))
        self.assertEqual(trader.roi_pct, Decimal("200"))

    def test_without_a_current_price_an_open_position_has_no_pnl(self) -> None:
        trader = evaluate_trader(ALICE, [self._trade("1000", "100", 100)])
        self.assertIsNone(trader.unrealized_pnl_usd)
        self.assertTrue(trader.partial)
        self.assertEqual(trader.total_pnl_usd, Decimal("0"))

    def test_a_loss_stays_negative(self) -> None:
        trader = evaluate_trader(ALICE, [
            self._trade("1000", "1000", 100),
            self._trade("-1000", "250", 200),
        ])
        self.assertEqual(trader.total_pnl_usd, Decimal("-750"))
        self.assertEqual(trader.roi_pct, Decimal("-75"))

    def test_selling_more_than_the_window_saw_is_excluded_not_profit(self) -> None:
        # 500 bought here, 1500 sold: 1000 of that came from before the sample,
        # at a cost this module cannot know.
        trader = evaluate_trader(ALICE, [
            self._trade("500", "50", 100),
            self._trade("-1500", "900", 200),
        ])
        self.assertEqual(trader.untracked_sold, Decimal("1000"))
        self.assertTrue(trader.partial)
        # Only the 500 it actually saw: a third of $900 against $50.
        self.assertEqual(trader.realized_pnl_usd, Decimal("250"))

    def test_an_unreadable_acquisition_never_becomes_free_profit(self) -> None:
        # Value moved in that transaction and could not be read, so the sale
        # that consumes it realises nothing rather than the whole $5,000.
        trader = evaluate_trader(ALICE, [
            self._trade("1000", None, 100),
            self._trade("-1000", "5000", 200),
        ])
        self.assertEqual(trader.realized_pnl_usd, Decimal("0"))
        self.assertIsNone(trader.roi_pct)
        self.assertTrue(trader.partial)
        self.assertEqual(trader.unpriced_buy_tokens, Decimal("1000"))

    def test_selling_a_gift_is_the_whole_proceeds(self) -> None:
        # A dev allocation or an airdrop: nothing moved when it arrived, so the
        # cost basis is zero as a fact, and the sale is all profit. This is the
        # row a strict "unpriced" rule would have dropped from the board.
        trader = evaluate_trader(ALICE, [
            self._trade("1000", "0", 100),
            self._trade("-1000", "2570", 200),
        ])
        self.assertEqual(trader.realized_pnl_usd, Decimal("2570"))
        self.assertEqual(trader.total_pnl_usd, Decimal("2570"))
        self.assertIsNone(trader.roi_pct)       # no capital was ever at risk
        self.assertIsNone(trader.avg_entry_price)
        self.assertEqual(trader.free_tokens, Decimal("1000"))
        self.assertTrue(trader.partial)

    def test_a_gift_still_held_is_not_counted_as_profit(self) -> None:
        # Otherwise the trader board becomes a holder board: an allocation
        # nobody has sold is a position, not a result.
        trader = evaluate_trader(
            ALICE, [self._trade("1000", "0", 100)],
            current_price=Decimal("5"),
        )
        self.assertEqual(trader.unrealized_pnl_usd, Decimal("0"))
        self.assertEqual(trader.open_tokens, Decimal("1000"))

    def test_a_paid_position_and_a_gift_are_kept_apart(self) -> None:
        trader = evaluate_trader(ALICE, [
            self._trade("1000", "0", 100),      # gift
            self._trade("1000", "500", 150),    # bought at $0.50
            self._trade("-1000", "1000", 200),  # sold half the stack at $1
        ], current_price=Decimal("1"))
        # Half the sale came from the gift (all profit), half from the paid
        # tokens (a $250 cost against $500 of proceeds).
        self.assertEqual(trader.realized_pnl_usd, Decimal("750"))
        self.assertEqual(trader.avg_entry_price, Decimal("0.5"))
        self.assertEqual(trader.unrealized_pnl_usd, Decimal("250"))
        self.assertEqual(trader.roi_pct, Decimal("200"))

    def test_priced_and_unpriced_inventory_are_consumed_in_proportion(self) -> None:
        trader = evaluate_trader(ALICE, [
            self._trade("500", "50", 100),      # $0.10 each
            self._trade("500", None, 150),      # unknown cost
            self._trade("-1000", "400", 200),
        ])
        # Half the sale is attributable: $200 of proceeds against $50 of cost.
        self.assertEqual(trader.realized_pnl_usd, Decimal("150"))

    def test_a_wallet_with_no_priced_leg_at_all_has_no_pnl(self) -> None:
        trader = evaluate_trader(ALICE, [self._trade("1000", None, 100)])
        self.assertFalse(trader.has_pnl)
        self.assertIsNone(trader.total_pnl_usd)
        self.assertIsNone(trader.roi_pct)

    def test_trades_are_replayed_oldest_first_whatever_the_page_order(self) -> None:
        # Providers page backwards from the head, so this is the real input.
        newest_first = [
            TokenFlow(ALICE, Decimal("-1000"), 200, "s2", Decimal("450")),
            TokenFlow(ALICE, Decimal("1000"), 100, "s1", Decimal("100")),
        ]
        trader = aggregate_traders(
            newest_first, detect_infrastructure=False)[0]
        self.assertEqual(trader.realized_pnl_usd, Decimal("350"))
        self.assertEqual(trader.untracked_sold, Decimal("0"))

    def test_a_transaction_counts_once_however_many_legs_it_had(self) -> None:
        trader = evaluate_trader(ALICE, [
            self._trade("500", "50", 100, "same"),
            self._trade("500", "50", 100, "same"),
        ])
        self.assertEqual(trader.transactions, 1)


class RankingTests(unittest.TestCase):
    """Who ends up on top -- the whole point of the change."""

    def _trader(self, address: str, **kwargs) -> TokenTrader:
        base = dict(
            address=address, bought=Decimal("0"), sold=Decimal("0"),
            transactions=1,
        )
        base.update(kwargs)
        return TokenTrader(**base)

    def _whale(self) -> TokenTrader:
        """Moved ten million tokens, made almost nothing doing it."""
        return self._trader(
            "whale", bought=Decimal("10000000"), sold=Decimal("10000000"),
            transactions=40, invested_usd=Decimal("100000"),
            proceeds_usd=Decimal("103000"), realized_pnl_usd=Decimal("3000"),
            unrealized_pnl_usd=Decimal("0"),
        )

    def _sniper(self) -> TokenTrader:
        """Moved a hundredth of that and made four times the money."""
        return self._trader(
            "sniper", bought=Decimal("100000"), sold=Decimal("100000"),
            transactions=3, invested_usd=Decimal("2000"),
            proceeds_usd=Decimal("14000"), realized_pnl_usd=Decimal("12000"),
            unrealized_pnl_usd=Decimal("0"),
        )

    def test_profit_beats_volume(self) -> None:
        ranked = rank_traders([self._whale(), self._sniper()])
        self.assertEqual([t.address for t in ranked], ["sniper", "whale"])

    def test_volume_is_still_available_when_it_is_asked_for(self) -> None:
        ranked = rank_traders([self._sniper(), self._whale()], key="volume")
        self.assertEqual([t.address for t in ranked], ["whale", "sniper"])

    def test_roi_ranks_by_return_not_by_size(self) -> None:
        ranked = rank_traders([self._sniper(), self._whale()], key="roi")
        self.assertEqual([t.address for t in ranked], ["sniper", "whale"])

    def test_a_dust_position_does_not_win_the_roi_board(self) -> None:
        dust = self._trader(
            "dust", bought=Decimal("10"), transactions=1,
            invested_usd=Decimal("2"), proceeds_usd=Decimal("60"),
            realized_pnl_usd=Decimal("58"), unrealized_pnl_usd=Decimal("0"),
        )
        ranked = rank_traders([dust, self._sniper()], key="roi")
        self.assertEqual(ranked[0].address, "sniper")   # 2900% vs 600%

    def test_a_wallet_with_no_pnl_ranks_below_every_wallet_with_one(self) -> None:
        unknown = self._trader(
            "unknown", bought=Decimal("50000000"), transactions=90,
        )
        loser = self._trader(
            "loser", bought=Decimal("10"), transactions=2,
            invested_usd=Decimal("900"), proceeds_usd=Decimal("100"),
            realized_pnl_usd=Decimal("-800"), unrealized_pnl_usd=Decimal("0"),
        )
        ranked = rank_traders([unknown, loser])
        self.assertEqual([t.address for t in ranked], ["loser", "unknown"])

    def test_the_pool_holds_what_any_ranking_would_need(self) -> None:
        dust = self._trader(
            "dust", invested_usd=Decimal("60"), proceeds_usd=Decimal("600"),
            realized_pnl_usd=Decimal("540"), unrealized_pnl_usd=Decimal("0"),
        )
        pool = candidate_pool([self._whale(), self._sniper(), dust], limit=1)
        self.assertEqual(
            {trader.address for trader in pool}, {"whale", "sniper", "dust"}
        )

    def test_the_default_ranking_of_a_real_sample_is_by_money(self) -> None:
        flows = [
            # The whale: one enormous buy, still holding, flat.
            TokenFlow("whale", Decimal("10000000"), 100, "w1", Decimal("50000")),
            # The sniper: in early, out at 5x.
            TokenFlow("sniper", Decimal("100000"), 90, "s1", Decimal("1000")),
            TokenFlow("sniper", Decimal("-100000"), 300, "s2", Decimal("6000")),
        ]
        ranked = aggregate_traders(
            flows, detect_infrastructure=False, current_price=Decimal("0.005"),
        )
        self.assertEqual(ranked[0].address, "sniper")
        self.assertEqual(ranked[0].total_pnl_usd, Decimal("5000"))
        self.assertEqual(ranked[1].address, "whale")
        self.assertEqual(ranked[1].total_pnl_usd, Decimal("0"))
        # ...and the old ranking would have said the opposite.
        by_volume = rank_traders(ranked, key="volume")
        self.assertEqual(by_volume[0].address, "whale")


class PricedClientTests(unittest.IsolatedAsyncioTestCase):
    """The client's half: quote prices in, PnL out, and one EVM join."""

    HELIUS = "https://mainnet.helius-rpc.com/?api-key=abc123"
    ALCHEMY = "https://base-mainnet.g.alchemy.com/v2/key"
    WETH = "0x4200000000000000000000000000000000000006"

    class SwapHttp:
        """One page of parsed history: Alice buys at $0.10, sells at $0.50."""

        def __init__(self) -> None:
            self.pages = 0

        async def get(self, url: str, **kwargs) -> FakeResponse:
            if "dexscreener" in url:
                return FakeResponse({"pairs": [{
                    "baseToken": {"address": OTHER_MINT},
                    "priceUsd": "200", "liquidity": {"usd": 1e9},
                }]})
            self.pages += 1
            if (kwargs.get("params") or {}).get("before"):
                return FakeResponse([])
            return FakeResponse([
                helius_swap("s1", 100, ALICE, tokens=1000, usdc=100),
                helius_swap("s2", 200, ALICE, tokens=400, usdc=200, sold=True),
                helius_swap("s3", 150, BOB, tokens=5000, usdc=2500),
            ])

        async def post(self, *_a, **_k) -> FakeResponse:
            raise AssertionError("the parsed route should not need JSON-RPC")

    def _client(self, http, rpcs=None, evm=None):
        from token_intelligence import TokenIntelligenceClient

        return TokenIntelligenceClient(
            http, rpcs or [self.HELIUS], evm_rpcs=evm if evm is not None else {}
        )

    async def test_the_card_gets_pnl_roi_and_an_entry_price(self) -> None:
        client = self._client(self.SwapHttp())
        result = await client.top_traders(MINT, "Solana", price_usd=0.5)
        traders = {trader.address: trader for trader in result.traders}
        alice = traders[ALICE]
        self.assertEqual(alice.avg_entry_price, Decimal("0.1"))
        self.assertEqual(alice.realized_pnl_usd, Decimal("160"))
        self.assertEqual(alice.unrealized_pnl_usd, Decimal("240"))
        self.assertEqual(alice.roi_pct, Decimal("400"))
        self.assertEqual(result.priced, len(result.traders))

    async def test_the_ranking_is_pnl_and_not_the_bigger_wallet(self) -> None:
        client = self._client(self.SwapHttp())
        result = await client.top_traders(MINT, "Solana", price_usd=0.5)
        # Bob moved five times the tokens and is flat; Alice is up $400.
        self.assertEqual(result.traders[0].address, ALICE)
        self.assertGreater(
            next(t for t in result.traders if t.address == BOB).volume,
            result.traders[0].volume,
        )

    async def test_a_new_price_is_not_served_from_the_old_cache(self) -> None:
        http = self.SwapHttp()
        client = self._client(http)
        first = await client.top_traders(MINT, "Solana", price_usd=0.5)
        second = await client.top_traders(MINT, "Solana", price_usd=1.0)
        self.assertNotEqual(
            first.traders[0].unrealized_pnl_usd,
            second.traders[0].unrealized_pnl_usd,
        )
        third = await client.top_traders(MINT, "Solana", price_usd=1.0)
        self.assertEqual(third.traders[0].unrealized_pnl_usd,
                         second.traders[0].unrealized_pnl_usd)

    class EvmHttp:
        """A token page, then the pool's own WETH movements."""

        def __init__(self) -> None:
            self.quote_requests: list[dict] = []

        async def get(self, url: str, **_kwargs) -> FakeResponse:
            return FakeResponse({"pairs": [{
                "baseToken": {"address": PricedClientTests.WETH},
                "priceUsd": "3000", "liquidity": {"usd": 1e9},
            }]})

        async def post(self, _url: str, **kwargs) -> FakeResponse:
            params = (kwargs.get("json") or {}).get("params", [{}])[0]
            contracts = params.get("contractAddresses") or []
            if TOKEN in contracts:
                transfers = []
                for index in range(POOL_SHARE_MIN_TRANSACTIONS + 4):
                    transfers.append({
                        "from": POOL.lower(), "to": f"0x{index + 1:040x}",
                        "value": 1000, "hash": f"0xh{index}",
                        "rawContract": {"address": TOKEN},
                        "metadata": {"blockTimestamp": "2026-08-20T10:00:00Z"},
                    })
                return FakeResponse({"result": {"transfers": transfers}})
            self.quote_requests.append(params)
            return FakeResponse({"result": {"transfers": [{
                "from": f"0x{1:040x}", "to": POOL.lower(), "value": 0.5,
                "hash": "0xh0", "category": "erc20",
                "rawContract": {"address": PricedClientTests.WETH},
            }]}})

    async def test_an_evm_swap_is_priced_from_the_venues_own_leg(self) -> None:
        http = self.EvmHttp()
        client = self._client(http, evm={"Base": [self.ALCHEMY]})
        result = await client.top_traders(TOKEN, "Base", price_usd=0.002)
        buyer = {trader.address: trader for trader in result.traders}[f"0x{1:040x}"]
        self.assertEqual(buyer.invested_usd, Decimal("1500.0"))
        self.assertEqual(buyer.avg_entry_price, Decimal("1.5"))
        self.assertTrue(http.quote_requests)
        # The venue is queried in both directions, for quote assets only.
        self.assertNotIn(TOKEN, http.quote_requests[0]["contractAddresses"])
        self.assertEqual(
            {"fromAddress", "toAddress"} & set().union(
                *(set(request) for request in http.quote_requests)
            ),
            {"fromAddress", "toAddress"},
        )

    async def test_a_pool_is_never_a_trader_even_once_it_is_priced(self) -> None:
        client = self._client(self.EvmHttp(), evm={"Base": [self.ALCHEMY]})
        result = await client.top_traders(TOKEN, "Base")
        self.assertNotIn(POOL.lower(),
                         {trader.address for trader in result.traders})


class QuotePriceTests(unittest.IsolatedAsyncioTestCase):
    """A quote asset's own USD price, read off the pair the right way round."""

    def _client(self, pair: dict):
        from token_intelligence import TokenIntelligenceClient

        class Http:
            async def get(self, _url: str, **_kwargs) -> FakeResponse:
                return FakeResponse({"pairs": [pair]})

            async def post(self, *_a, **_k) -> FakeResponse:
                raise AssertionError("no RPC for a price")

        return TokenIntelligenceClient(Http(), [], evm_rpcs={})

    async def test_the_asset_as_base_is_read_directly(self) -> None:
        price = await self._client({
            "baseToken": {"address": WSOL_MINT},
            "quoteToken": {"address": USDC},
            "priceUsd": "212.40", "priceNative": "212.40",
            "liquidity": {"usd": 1e9},
        })._native_price(WSOL_MINT)
        self.assertEqual(price, Decimal("212.40"))

    async def test_the_asset_as_quote_is_inverted_not_misread(self) -> None:
        # A SOL-quoted memecoin pair: `priceUsd` is the memecoin's price.
        price = await self._client({
            "baseToken": {"address": MINT},
            "quoteToken": {"address": WSOL_MINT},
            "priceUsd": "0.002124", "priceNative": "0.00001",
            "liquidity": {"usd": 1e9},
        })._native_price(WSOL_MINT)
        self.assertEqual(price, Decimal("212.4"))

    async def test_a_pair_that_names_neither_side_prices_nothing(self) -> None:
        price = await self._client({
            "baseToken": {"address": MINT}, "quoteToken": {"address": USDC},
            "priceUsd": "0.5", "liquidity": {"usd": 1e9},
        })._native_price(WSOL_MINT)
        self.assertIsNone(price)

    async def test_stablecoins_need_no_lookup_and_the_token_is_never_a_quote(self) -> None:
        client = self._client({
            "baseToken": {"address": WSOL_MINT}, "priceUsd": "200",
            "liquidity": {"usd": 1e9},
        })
        prices = await client._quote_prices("Solana", USDC)
        self.assertNotIn(USDC, prices)          # a token cannot price itself
        self.assertEqual(prices[WSOL_MINT], Decimal("200"))
        # ...and the cache still holds the full table for the next token.
        self.assertIn(USDC, (await client._quote_prices("Solana", MINT)))


class SampleDepthTests(unittest.IsolatedAsyncioTestCase):
    """How deep the sample goes decides whether the ranking is even right."""

    HELIUS = "https://mainnet.helius-rpc.com/?api-key=abc123"

    class DeepHttp:
        """`pages` full pages of history, then the token's first transaction."""

        def __init__(self, pages: int) -> None:
            self.pages = pages
            self.calls = 0

        async def get(self, url: str, **kwargs) -> FakeResponse:
            if "dexscreener" in url:
                return FakeResponse({"pairs": [{
                    "baseToken": {"address": OTHER_MINT},
                    "priceUsd": "200", "liquidity": {"usd": 1e9},
                }]})
            self.calls += 1
            last = self.calls >= self.pages
            size = 40 if last else 100
            base = self.calls * 1000
            return FakeResponse([
                helius_swap(f"p{self.calls}s{row}", base + row,
                            f"w{row % 30}", tokens=row + 1, usdc=(row + 1) / 10)
                for row in range(size)
            ])

        async def post(self, *_a, **_k) -> FakeResponse:
            raise AssertionError("the parsed route should not need JSON-RPC")

    def _client(self, http):
        from token_intelligence import TokenIntelligenceClient

        return TokenIntelligenceClient(http, [self.HELIUS], evm_rpcs={})

    async def test_paging_continues_past_the_first_few_hundred(self) -> None:
        # The bug this fixes: five pages of a live memecoin is its newest
        # buyers, and none of them are the wallets that made the money.
        http = self.DeepHttp(pages=12)
        result = await self._client(http).top_traders(MINT, "Solana")
        self.assertEqual(http.calls, 12)
        self.assertEqual(result.transactions, 11 * 100 + 40)
        self.assertFalse(result.truncated)

    async def test_a_short_page_means_the_history_ended(self) -> None:
        result = await self._client(self.DeepHttp(pages=2)).top_traders(
            MINT, "Solana"
        )
        self.assertFalse(result.truncated)   # the card may say "full history"

    async def test_the_page_budget_marks_the_sample_cut_short(self) -> None:
        import token_intelligence

        original = token_intelligence.SOLANA_TRADER_PAGES
        token_intelligence.SOLANA_TRADER_PAGES = 3
        try:
            result = await self._client(self.DeepHttp(pages=99)).top_traders(
                MINT, "Solana"
            )
        finally:
            token_intelligence.SOLANA_TRADER_PAGES = original
        self.assertTrue(result.truncated)

    async def test_the_time_budget_stops_paging_too(self) -> None:
        import token_intelligence

        original = token_intelligence.TRADER_BUDGET_SECONDS
        token_intelligence.TRADER_BUDGET_SECONDS = -1   # already spent
        try:
            result = await self._client(self.DeepHttp(pages=99)).top_traders(
                MINT, "Solana"
            )
        finally:
            token_intelligence.TRADER_BUDGET_SECONDS = original
        self.assertTrue(result.truncated)
        self.assertEqual(result.traders, ())
        # ...and it does not start the slower fallback with no time left.
        self.assertEqual(result.source, "helius")

    async def test_a_failed_page_is_not_a_finished_history(self) -> None:
        class Flaky(SampleDepthTests.DeepHttp):
            async def get(self, url: str, **kwargs):
                if "dexscreener" not in url and self.calls >= 2:
                    raise RuntimeError("provider blipped")
                return await super().get(url, **kwargs)

        result = await self._client(Flaky(pages=99)).top_traders(MINT, "Solana")
        self.assertTrue(result.truncated)
        self.assertTrue(result.traders)

    async def test_the_sample_is_kept_for_the_diagnostic(self) -> None:
        client = self._client(self.DeepHttp(pages=2))
        await client.top_traders(MINT, "Solana")
        self.assertTrue(client.last_sample)
        self.assertTrue(all(hasattr(flow, "delta") for flow in client.last_sample))
