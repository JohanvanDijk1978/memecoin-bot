from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fomo_features import build_trader_stats, fmt_price, iso_to_datetime, open_positions
from fomo_evm_activity import _activity, _alchemy_activities, fetch_robinhood_buys
from fomo_features import TraderStats, merge_latest_buys, merge_latest_sells
from fomo_tracking import (
    SEEN_ID_LIMIT,
    TrackingStore,
    _remember_ids,
    activity_allowed,
    activity_filter_label,
    detect_events,
    fmt_native_amount,
    native_value_from_usd,
    padre_trade_url,
    normalize_activity_filters,
    snapshot,
)


TRADES = {
    "activeTrades": [
        {
            "trade": {
                "id": "active-1", "tokenAddress": "TOKEN", "networkId": 1399811149,
                "createdAt": "2026-08-18T12:00:00Z", "totalCostBasis": 500,
                "tokenMetadata": {"symbol": "MEME"},
            },
            "comment": {"id": "thesis-1", "comment": "Strong catalyst", "createdAt": "2026-08-18T12:01:00Z"},
        },
        {"trade": {"id": "base-1", "tokenAddress": "0xbasecoin", "networkId": 8453,
                   "createdAt": "2026-08-18T14:00:00Z", "tokenMetadata": {"symbol": "BASEY"}}},
        {"trade": {"id": "bsc-1", "tokenAddress": "0xbnbcoin", "networkId": 56,
                   "createdAt": "2026-08-18T13:00:00Z", "tokenMetadata": {"symbol": "BNBY"}}},
    ],
    "closedTrades": [
        {"trade": {"id": "closed-1", "realizedPnlUsd": 100}},
        {"trade": {"id": "closed-2", "realizedPnlUsd": -25}},
    ],
    "closedCount": 10,
}

SWAPS = {"swaps": [
    {
        "id": "swap-base", "inTokenAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "outTokenAddress": "0xbasecoin", "inNetworkId": 8453, "outNetworkId": 8453,
        "outTradeId": "base-1", "humanUsdAmountIn": 2000, "humanUsdAmountOut": 2000,
        "outHumanAmount": 500,
        "provider": "DFLOW", "createdAt": "2026-08-18T14:00:00Z",
    },
    {
        "id": "swap-bsc", "inTokenAddress": "0x55d398326f99059fF775485246999027B3197955",
        "outTokenAddress": "0xbnbcoin", "inNetworkId": 56, "outNetworkId": 56,
        "outTradeId": "bsc-1", "humanUsdAmountIn": 1750, "humanUsdAmountOut": 1750,
        "outHumanAmount": 3500,
        "provider": "DFLOW", "createdAt": "2026-08-18T13:30:00Z",
    },
    {
        "id": "swap-sol", "inTokenAddress": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "outTokenAddress": "TOKEN", "inNetworkId": 1399811149, "outNetworkId": 1399811149,
        "outTradeId": "active-1", "humanUsdAmountIn": 1500, "humanUsdAmountOut": 1500,
        "outHumanAmount": 7500,
        "provider": "DFLOW", "createdAt": "2026-08-18T13:00:00Z",
    },
]}


class FeatureTests(unittest.TestCase):
    def test_fomo_z_timestamp_parses_on_python_310(self) -> None:
        parsed = iso_to_datetime("2026-08-18T18:09:41.493Z")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.isoformat(), "2026-08-18T18:09:41.493000+00:00")  # type: ignore[union-attr]

    def test_stats_derivation(self) -> None:
        balances = {"balances": [
            {
                "balance": {"shiftedBalance": 2},
                "tokenFilterResult": {"priceUSD": "125"},
                "userToken": {},
            },
            {
                "balance": {"tokenAddress": "0xbasecoin", "shiftedBalance": 0},
                "tokenFilterResult": {"priceUSD": 2, "marketCap": 1_000_000},
                "userToken": {"networkId": 8453},
            },
            {
                "balance": {"tokenAddress": "0xbnbcoin", "shiftedBalance": 0},
                "tokenFilterResult": {"priceUSD": 1, "marketCap": 500_000},
                "userToken": {"networkId": 56},
            },
            {
                "balance": {"tokenAddress": "TOKEN", "shiftedBalance": 0},
                "tokenFilterResult": {"priceUSD": 0.1, "marketCap": 100_000},
                "userToken": {"networkId": 1399811149},
            },
        ]}
        spotlight = {"bestTrades": [{"trade": {
            "id": "best", "realizedPnlUsd": 300, "totalCostBasis": 200,
            "tokenMetadata": {"symbol": "WIN"},
        }}]}
        stats = build_trader_stats(balances, spotlight, TRADES, SWAPS)
        self.assertEqual(stats.portfolio_value, 250)
        self.assertEqual(stats.best_trade.symbol, "WIN")  # type: ignore[union-attr]
        self.assertEqual(stats.best_trade.roi, 150)  # type: ignore[union-attr]
        self.assertEqual(len(stats.latest_buys), 3)
        self.assertEqual([buy.symbol for buy in stats.latest_buys], ["BASEY", "BNBY", "MEME"])
        self.assertEqual([buy.chain for buy in stats.latest_buys], ["Base", "BSC", "Solana"])
        self.assertEqual([buy.market_cap for buy in stats.latest_buys],
                         [2_000_000, 250_000, 200_000])
        self.assertTrue(all(buy.market_cap_estimated for buy in stats.latest_buys))
        self.assertEqual([event.detail for event in stats.latest_theses], ["Strong catalyst"])

    def test_stats_exposes_recent_sells_for_profile_filter(self) -> None:
        sell = {"swaps": [{
            "id": "sell-1", "inTokenAddress": "TOKEN",
            "outTokenAddress": "USDC", "inNetworkId": 1399811149,
            "outNetworkId": 1399811149, "inTradeId": "active-1",
            "humanUsdAmountOut": 2490, "createdAt": "2026-08-18T15:00:00Z",
        }]}
        stats = build_trader_stats(trades=TRADES, swaps=sell)
        self.assertEqual(len(stats.latest_sells), 1)
        self.assertEqual(stats.latest_sells[0].symbol, "MEME")
        self.assertEqual(stats.latest_sells[0].usd_value, 2490)

    def test_tracking_baseline_and_events(self) -> None:
        old = snapshot(SWAPS, TRADES)
        self.assertEqual(detect_events(SWAPS, TRADES, old, 1000), [])
        events = detect_events(SWAPS, TRADES, {}, 1000)
        self.assertEqual({event.kind for event in events}, {"buy", "thesis"})
        buys = [event for event in events if event.kind == "buy"]
        self.assertEqual([event.symbol for event in buys], ["MEME", "BNBY", "BASEY"])
        self.assertTrue(all(event.token_address for event in buys))
        thesis = next(event for event in events if event.kind == "thesis")
        self.assertEqual(thesis.detail, "Strong catalyst")

        base_trade = next(event for event in buys if event.symbol == "BASEY")
        self.assertEqual(base_trade.value_label, "Value")

    def test_tracking_labels_large_sell_with_token_context(self) -> None:
        sell = {"swaps": [{
            "id": "sell-1", "inTokenAddress": "TOKEN",
            "outTokenAddress": "USDC", "inNetworkId": 1399811149,
            "outNetworkId": 1399811149, "inTradeId": "active-1",
            "humanUsdAmountIn": 2500, "humanUsdAmountOut": 2490,
            "provider": "JUPITER", "createdAt": "2026-08-18T15:00:00Z",
        }]}
        event = next(
            event for event in detect_events(sell, TRADES, {}, 1000)
            if event.kind == "sell"
        )
        self.assertEqual(event.kind, "sell")
        self.assertEqual(event.symbol, "MEME")
        self.assertEqual(event.usd_value, 2490)
        self.assertEqual(
            padre_trade_url(event.network_id, event.token_address),
            "https://trade.padre.gg/trade/solana/TOKEN",
        )

    def test_tracking_uses_exact_native_swap_amount_when_available(self) -> None:
        buy = {"swaps": [{
            "id": "native-buy",
            "inTokenAddress": "So11111111111111111111111111111111111111112",
            "outTokenAddress": "TOKEN",
            "inNetworkId": 1399811149,
            "outNetworkId": 1399811149,
            "outTradeId": "active-1",
            "inHumanAmount": 4.91595,
            "humanUsdAmountIn": 983.19,
            "createdAt": "2026-08-18T15:00:00Z",
        }]}
        event = next(
            event for event in detect_events(buy, TRADES, {}, 100)
            if event.kind == "buy" and event.native_value is not None
        )
        self.assertEqual(event.native_symbol, "SOL")
        self.assertEqual(event.native_value, 4.91595)

    def test_native_amount_conversion_and_formatting(self) -> None:
        self.assertAlmostEqual(native_value_from_usd(983.19, 200) or 0, 4.91595)
        self.assertEqual(fmt_native_amount(4.91595, "SOL"), "4.9159 SOL")
        self.assertEqual(fmt_native_amount(0.2457975, "ETH"), "0.245798 ETH")
        self.assertEqual(fmt_native_amount(None, "BNB"), "— BNB")

    def test_tracking_keeps_token_metadata_for_later_sells(self) -> None:
        baseline = snapshot({}, TRADES)
        sell = {"swaps": [{
            "id": "sell-later", "inTokenAddress": "TOKEN",
            "outTokenAddress": "USDC", "inNetworkId": 1399811149,
            "outNetworkId": 1399811149, "inTradeId": "active-1",
            "humanUsdAmountOut": 3000, "createdAt": "2026-08-19T15:00:00Z",
        }]}
        event = next(event for event in detect_events(sell, {}, baseline, 1000)
                     if event.kind == "sell")
        self.assertEqual(event.symbol, "MEME")
        refreshed = snapshot(sell, {}, baseline)
        self.assertEqual(refreshed["tokens"]["active-1"]["tokenMetadata"]["symbol"], "MEME")

    def test_returning_trade_row_is_not_reannounced(self) -> None:
        """FOMO drops rows from /trades and brings them back a poll later."""
        def rows(*ids: str) -> dict:
            return {"activeTrades": [
                {"trade": {"id": trade_id, "tokenAddress": f"0x{trade_id}",
                           "networkId": 56, "createdAt": "2026-08-11T23:48:00Z",
                           "totalCostBasis": 100,
                           "tokenMetadata": {"symbol": "TSLAB"}}}
                for trade_id in ids
            ], "closedTrades": []}

        baseline = snapshot({}, rows("a", "b"))
        flapped = snapshot({}, rows("a"), baseline)

        self.assertIn("b", flapped["tradeIds"])
        self.assertEqual(detect_events({}, rows("a", "b"), flapped, 10), [])

    def test_returning_swap_is_not_reannounced(self) -> None:
        def rows(*ids: str) -> dict:
            return {"swaps": [
                {"id": swap_id, "outTradeId": "trade-1", "outTokenAddress": "0xaa",
                 "outNetworkId": 56, "humanUsdAmountIn": 500,
                 "createdAt": "2026-08-11T23:48:00Z"}
                for swap_id in ids
            ]}

        baseline = snapshot(rows("s1", "s2"), {})
        flapped = snapshot(rows("s1"), {}, baseline)

        self.assertEqual(detect_events(rows("s1", "s2"), {}, flapped, 10), [])

    def test_remembered_ids_are_bounded_and_newest_first(self) -> None:
        older = [f"old-{index}" for index in range(SEEN_ID_LIMIT)]

        merged = _remember_ids(["fresh"], older)

        self.assertEqual(merged[0], "fresh")
        self.assertEqual(len(merged), SEEN_ID_LIMIT)
        self.assertNotIn(f"old-{SEEN_ID_LIMIT - 1}", merged)

    def test_tracking_store_persists_and_removes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracks.json"
            store = TrackingStore(path)
            self.assertTrue(store.add(
                123, 456, "user", "sample", snapshot(SWAPS, TRADES), "buys"
            ))
            self.assertTrue(store.add(123, 456, "other", "Alpha", snapshot(SWAPS, TRADES)))
            self.assertTrue(store.add(999, 456, "elsewhere", "Hidden", snapshot(SWAPS, TRADES)))
            loaded = TrackingStore(path)
            self.assertIn("123:user", loaded.tracks)
            self.assertEqual(loaded.tracks["123:user"]["activityFilters"], ["buys"])
            self.assertEqual(loaded.tracks["123:other"]["activityFilters"], ["all"])
            self.assertEqual(
                [entry["handle"] for entry in loaded.for_channel(123)],
                ["Alpha", "sample"],
            )
            self.assertTrue(loaded.remove(123, "user"))
            saved = json.loads(path.read_text())
            self.assertNotIn("123:user", saved["tracks"])

    def test_tracking_store_updates_filters_without_replacing_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracks.json"
            store = TrackingStore(path)
            store.add(
                1, 2, "user", "handle",
                {"swapIds": ["existing"], "tradeIds": ["trade"]},
                ["buys"],
            )
            self.assertTrue(
                store.set_activity_filters(1, "user", ["sells", "theses"])
            )
            entry = store.tracks[store.key(1, "user")]
            self.assertEqual(entry["activityFilters"], ["sells", "theses"])
            self.assertEqual(entry["swapIds"], ["existing"])
            self.assertEqual(entry["tradeIds"], ["trade"])
            self.assertFalse(
                store.set_activity_filters(1, "missing", ["buys"])
            )

    def test_tracking_store_retries_transient_windows_replace_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracks.json"
            store = TrackingStore(path)
            real_replace = __import__("os").replace
            calls = 0

            def flaky_replace(source: object, target: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError(5, "Access is denied")
                real_replace(source, target)

            with patch("fomo_tracking.os.replace", side_effect=flaky_replace), patch(
                "fomo_tracking.time.sleep"
            ):
                store.add(1, 2, "user", "handle", {"tradeIds": []})
            self.assertEqual(calls, 2)
            self.assertIn("1:user", TrackingStore(path).tracks)

    def test_tracking_store_skips_unchanged_poll_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tracks.json"
            store = TrackingStore(path)
            state = {"tradeIds": ["same"]}
            store.add(1, 2, "user", "handle", state)
            with patch("fomo_tracking.os.replace") as replace:
                store.update_state(store.key(1, "user"), state)
            replace.assert_not_called()

    def test_tracking_activity_filter_is_safe_and_kind_specific(self) -> None:
        self.assertTrue(activity_allowed(None, "buy"))
        self.assertTrue(activity_allowed("buys", "buy"))
        self.assertFalse(activity_allowed("buys", "sell"))
        self.assertTrue(activity_allowed(["buys", "sells"], "sell"))
        self.assertFalse(activity_allowed(["buys", "sells"], "thesis"))
        self.assertTrue(activity_allowed("theses", "thesis"))
        self.assertFalse(activity_allowed("callouts", "buy"))
        self.assertTrue(activity_allowed("unexpected", "sell"))
        self.assertEqual(activity_filter_label("sells"), "sells only")
        self.assertEqual(
            activity_filter_label(["buys", "theses"]), "buys + theses"
        )
        self.assertEqual(activity_filter_label(None), "all activity")
        self.assertEqual(
            normalize_activity_filters("all", ("buys", "sells", "theses")),
            ("buys", "sells", "theses"),
        )


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        return None


class FakeExplorerHttp:
    wallet = "0xfa2b3e71486f75b6b31aa17dbd5e687f253e3111"

    @staticmethod
    def candidate(tx: str, timestamp: str, raw_amount: str) -> dict:
        return {
            "to": {"hash": FakeExplorerHttp.wallet},
            "timestamp": timestamp,
            "transaction_hash": tx,
            "token": {
                "address_hash": "0xc31d45f8a4f319553bb019fd042dfe375bd7d243",
                "symbol": "WALL3", "decimals": "18",
                "total_supply": "1000000000000000000000000000",
            },
            "total": {"decimals": "18", "value": raw_amount},
        }

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        if "/addresses/" in url:
            return FakeResponse({"items": [
                self.candidate("0xbuy2", "2026-08-18T14:40:35Z",
                               "1483700139149756652748921"),
                self.candidate("0xbuy1", "2026-08-18T14:39:36Z",
                               "649684056170721951120255"),
            ]})
        amount = "5688545882" if "0xbuy2" in url else "2370340793"
        return FakeResponse({"items": [{
            "from": {"hash": self.wallet},
            "token": {"address_hash": "0xusdg", "symbol": "USDG",
                      "decimals": "6", "exchange_rate": "1.0"},
            "total": {"decimals": "6", "value": amount},
        }]})


class EvmActivityTests(unittest.IsolatedAsyncioTestCase):
    def test_alchemy_history_pairs_stablecoin_legs_and_ignores_airdrops(self) -> None:
        wallet = FakeExplorerHttp.wallet
        token = "0xc31d45f8a4f319553bb019fd042dfe375bd7d243"
        rows = [
            {"hash": "0xbuy", "from": wallet, "to": "0xrouter", "asset": "USDC",
             "value": 50, "rawContract": {"address": "0xusdc"},
             "metadata": {"blockTimestamp": "2026-08-18T10:00:00Z"}},
            {"hash": "0xbuy", "from": "0xrouter", "to": wallet, "asset": "WALL3",
             "value": 1000, "rawContract": {"address": token},
             "metadata": {"blockTimestamp": "2026-08-18T10:00:00Z"}},
            {"hash": "0xsell", "from": wallet, "to": "0xrouter", "asset": "WALL3",
             "value": 400, "rawContract": {"address": token},
             "metadata": {"blockTimestamp": "2026-08-18T11:00:00Z"}},
            {"hash": "0xsell", "from": "0xrouter", "to": wallet, "asset": "USDC",
             "value": 30, "rawContract": {"address": "0xusdc"},
             "metadata": {"blockTimestamp": "2026-08-18T11:00:00Z"}},
            {"hash": "0xairdrop", "from": "0xstranger", "to": wallet, "asset": "SPAM",
             "value": 1_000_000, "rawContract": {"address": "0xspam"},
             "metadata": {"blockTimestamp": "2026-08-18T12:00:00Z"}},
        ]
        buys, sells = _alchemy_activities(rows, wallet, 56)
        self.assertEqual([(item.symbol, item.usd_value) for item in buys], [("WALL3", 50)])
        self.assertEqual([(item.symbol, item.usd_value) for item in sells], [("WALL3", 30)])
        merged = merge_latest_sells(TraderStats(), tuple(sells))
        self.assertEqual(merged.latest_sells, tuple(sells))

    async def test_robinhood_buys_are_derived_from_verified_wallet_transfers(self) -> None:
        buys = await fetch_robinhood_buys(FakeExplorerHttp(), FakeExplorerHttp.wallet)
        self.assertEqual(len(buys), 2)
        self.assertEqual([buy.symbol for buy in buys], ["WALL3", "WALL3"])
        self.assertAlmostEqual(buys[0].usd_value or 0, 5688.545882)
        self.assertAlmostEqual(buys[1].usd_value or 0, 2370.340793)
        self.assertAlmostEqual((buys[0].market_cap or 0) / 1_000_000, 3.834, places=2)
        self.assertAlmostEqual((buys[1].market_cap or 0) / 1_000_000, 3.649, places=2)
        merged = merge_latest_buys(TraderStats(), buys)
        self.assertEqual(merged.latest_buys, buys)

    async def test_evm_sell_requires_stablecoin_received_by_wallet(self) -> None:
        candidate = {
            "from": {"hash": FakeExplorerHttp.wallet},
            "to": {"hash": "0xrouter"},
            "timestamp": "2026-08-18T15:00:00Z",
            "transaction_hash": "0xsell",
            "token": {
                "address_hash": "0xc31d45f8a4f319553bb019fd042dfe375bd7d243",
                "symbol": "WALL3", "decimals": "18",
            },
            "total": {"decimals": "18", "value": "1000000000000000000"},
        }
        transfers = {"items": [{
            "from": {"hash": "0xrouter"},
            "to": {"hash": FakeExplorerHttp.wallet},
            "token": {"address_hash": "0xusdg", "symbol": "USDG", "decimals": "6"},
            "total": {"decimals": "6", "value": "125000000"},
        }]}
        event = _activity(candidate, transfers, FakeExplorerHttp.wallet, 4663)
        self.assertIsNotNone(event)
        self.assertEqual(event.kind, "sell")  # type: ignore[union-attr]
        self.assertEqual(event.usd_value, 125)  # type: ignore[union-attr]


OPEN_TRADES = {"activeTrades": [
    {"trade": {"id": "sol-1", "tokenAddress": "TOKEN", "networkId": 1399811149,
               "humanTokenAmount": "1200000", "avgEntryPrice": "0.00042",
               "totalCostBasis": 504, "realizedPnlUsd": 0,
               "tokenMetadata": {"symbol": "WALL3", "currentPrice": "0.00058"}}},
    {"trade": {"id": "base-9", "tokenAddress": "0xbasecoin", "networkId": 8453,
               "humanTokenAmount": "500", "totalCostBasis": 2000,
               "tokenMetadata": {"symbol": "BASEY", "currentPrice": "3.1"}}},
    {"trade": {"id": "dust", "tokenAddress": "0xdust", "networkId": 56,
               "humanTokenAmount": "0", "tokenMetadata": {"symbol": "DUST"}}},
], "closedTrades": [{"trade": {"id": "closed", "realizedPnlUsd": 100}}]}


class OpenPositionTests(unittest.TestCase):
    def test_positions_are_priced_from_the_trade_row(self) -> None:
        positions = {item.symbol: item for item in open_positions(OPEN_TRADES)}
        wall3 = positions["WALL3"]
        self.assertAlmostEqual(wall3.entry_price, 0.00042)
        self.assertAlmostEqual(wall3.value_usd, 696.0)
        self.assertAlmostEqual(wall3.pnl_usd, 192.0, places=6)
        self.assertAlmostEqual(wall3.roi, 38.095, places=2)

    def test_pnl_is_per_unit_so_a_partial_sell_cannot_distort_it(self) -> None:
        """totalCostBasis covers sold units too; entry x amount does not."""
        half_sold = {"activeTrades": [{"trade": {
            "id": "p", "tokenAddress": "TOKEN", "networkId": 1399811149,
            "humanTokenAmount": "500", "avgEntryPrice": "1.0",
            "totalCostBasis": 1000, "realizedPnlUsd": 250,
            "tokenMetadata": {"symbol": "HALF", "currentPrice": "1.5"},
        }}]}
        position = open_positions(half_sold)[0]
        self.assertAlmostEqual(position.pnl_usd, 250.0)   # not 1.5*500 - 1000
        self.assertAlmostEqual(position.roi, 50.0)

    def test_entry_falls_back_to_cost_basis_when_the_average_is_absent(self) -> None:
        basey = {item.symbol: item for item in open_positions(OPEN_TRADES)}["BASEY"]
        self.assertAlmostEqual(basey.entry_price, 4.0)

    def test_empty_and_closed_rows_are_excluded(self) -> None:
        symbols = [item.symbol for item in open_positions(OPEN_TRADES)]
        self.assertNotIn("DUST", symbols)
        self.assertEqual(len(symbols), 2)

    def test_largest_position_leads_and_the_limit_holds(self) -> None:
        self.assertEqual(open_positions(OPEN_TRADES)[0].symbol, "BASEY")
        self.assertEqual(len(open_positions(OPEN_TRADES, limit=1)), 1)

    def test_missing_payload_is_not_an_error(self) -> None:
        self.assertEqual(open_positions(None), ())
        self.assertEqual(open_positions({"activeTrades": "nope"}), ())

    def test_stats_expose_the_open_book(self) -> None:
        stats = build_trader_stats(None, None, OPEN_TRADES, None)
        self.assertEqual([item.symbol for item in stats.open_positions],
                         ["BASEY", "WALL3"])

    def test_memecoin_prices_survive_formatting(self) -> None:
        """fmt_usd rounds to cents, which renders every memecoin entry as $0.00."""
        self.assertEqual(fmt_price(0.00042), "$0.00042")
        self.assertEqual(fmt_price(0.00000123), "$0.00000123")
        self.assertEqual(fmt_price(3.1), "$3.1")
        self.assertEqual(fmt_price(1234.5), "$1,234.5")
        self.assertEqual(fmt_price(None), "—")
        self.assertEqual(fmt_price(0), "$0")


if __name__ == "__main__":
    unittest.main()
