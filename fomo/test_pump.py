from __future__ import annotations

import base64
import json
import struct
import tempfile
import unittest
from pathlib import Path

from pump_api import (
    PumpCallout,
    PumpClient,
    PumpCoin,
    PumpPortfolio,
    PumpUser,
    USDC_MINT,
    WSOL_MINT,
    quote_value_sol,
    quote_value_usd,
)
from pump_chain import (
    AMM_BUY_EVENT,
    PumpChainClient,
    TRADE_EVENT,
    b58encode,
    parse_pump_trades,
)
from pump_evm import PumpEvmResolver
from pump_tracking import PumpAlert, PumpTrackingStore, new_callouts, pump_snapshot
from fomo_bot import (
    _pump_identity,
    build_pump_embed,
    build_pump_track_embed,
)


def pubkey(seed: int) -> bytes:
    return bytes([seed]) * 32


def string(value: str) -> bytes:
    encoded = value.encode()
    return struct.pack("<I", len(encoded)) + encoded


def bonding_event(user_bytes: bytes, mint_bytes: bytes, quote_bytes: bytes) -> bytes:
    fields = [
        TRADE_EVENT,
        mint_bytes,
        struct.pack("<QQ?", 2_000_000_000, 500_000_000, True),
        user_bytes,
        struct.pack("<q", 1_787_083_000),
        struct.pack("<QQQQ", 1, 2, 3, 4),
        pubkey(4),
        struct.pack("<QQ", 100, 20),
        pubkey(5),
        struct.pack("<QQ?QQQq", 50, 10, True, 0, 0, 0, 1_787_083_000),
        string("buy_v2"),
        struct.pack("<?QQQQI", False, 0, 0, 0, 0, 0),
        quote_bytes,
        struct.pack("<QQQ", 2_000_000, 0, 0),
    ]
    return b"".join(fields)


def amm_buy_event(user_bytes: bytes, base_amount: int, quote_amount: int) -> bytes:
    values = [base_amount] + [0] * 11 + [quote_amount]
    return (
        AMM_BUY_EVENT
        + struct.pack("<q", 1_787_083_100)
        + struct.pack("<" + "Q" * len(values), *values)
        + pubkey(7)
        + user_bytes
    )


class PumpModelTests(unittest.TestCase):
    def test_public_models_use_explicit_usd_market_cap(self) -> None:
        user = PumpUser.from_raw({
            "address": "wallet", "username": "rowdy", "followers": 12,
        })
        self.assertEqual(user.profile_url, "https://pump.fun/profile/wallet")
        coin = PumpCoin.from_raw({
            "mint": "mint", "symbol": "TEST", "market_cap": 2,
            "usd_market_cap": 123_456, "quote_decimals": 6,
        })
        self.assertEqual(coin.market_cap_usd, 123_456)
        portfolio = PumpPortfolio.from_raw({"success": True, "data": {
            "total_value": 100, "token_count": 3,
            "portfolioPnL": {"total_unrealized_usd": -5, "total_percentage": -5},
        }})
        self.assertEqual(portfolio.total_value, 100)
        self.assertEqual(portfolio.unrealized_usd, -5)

    def test_pump_profile_renders_plain_username_and_linked_wallet(self) -> None:
        wallet = "5uSNZfK1eLk9j6gR9jhYcfbHd4XtpgHnZP79fVMUcKQH"
        user = PumpUser(address=wallet, username="bubblywhale4907")
        embed = build_pump_embed(user)

        self.assertEqual(embed.title, "@bubblywhale4907")
        self.assertEqual(embed.url, f"https://pump.fun/profile/{wallet}")
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn(
            f"https://pump.fun/profile/{wallet}", fields["Solana wallet"]
        )
        self.assertNotIn("profile/bubblywhale4907", str(embed.to_dict()))

    def test_pump_identity_links_short_wallet_not_username(self) -> None:
        wallet = "9gTHWg123456789012345678901234567890123UF1N"
        rendered = _pump_identity("oldstarfish8933", wallet)

        self.assertIn("**@oldstarfish8933**", rendered)
        self.assertIn("[**@oldstarfish8933**]", rendered)
        self.assertEqual(rendered.count(f"https://pump.fun/profile/{wallet}"), 2)
        self.assertIn("9gTHWg…UF1N", rendered)

    def test_pump_alert_uses_wallet_profile_url(self) -> None:
        wallet = "5uSNZfK1eLk9j6gR9jhYcfbHd4XtpgHnZP79fVMUcKQH"
        embed = build_pump_track_embed(
            "bubblywhale4907",
            wallet,
            PumpAlert(
                id="callout-1",
                kind="callout",
                mint="TokenMint1111111111111111111111111111111",
                symbol="TEST",
                created_at="2026-08-19T10:00:00Z",
                detail="Test callout",
            ),
        )
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(embed.url, f"https://pump.fun/profile/{wallet}")
        self.assertIn(f"https://pump.fun/profile/{wallet}", fields["Pump profile"])
        self.assertNotIn("profile/bubblywhale4907", str(embed.to_dict()))

    def test_quote_values_support_sol_and_stables(self) -> None:
        self.assertEqual(quote_value_usd(WSOL_MINT, 2_000_000_000, 9, 75), 150)
        self.assertEqual(quote_value_usd(USDC_MINT, 10_250_000, 6, 75), 10.25)
        self.assertIsNone(quote_value_usd("unknown", 1, 6, 75))
        self.assertEqual(quote_value_sol(WSOL_MINT, 2_000_000_000, 9, None), 2)
        self.assertAlmostEqual(
            quote_value_sol(USDC_MINT, 10_250_000, 6, 100) or 0,
            0.1025,
        )


class FakeCoinResponse:
    status_code = 200

    def __init__(self, mint: str) -> None:
        self.mint = mint

    def json(self) -> dict[str, str]:
        return {"mint": self.mint, "name": self.mint, "symbol": self.mint.upper()}


class FakeCoinHttp:
    async def get(self, url: str, **_kwargs: object) -> FakeCoinResponse:
        return FakeCoinResponse(url.rsplit("/", 1)[-1])


class PumpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_coin_lookup_supplies_symbols_for_callout_mints(self) -> None:
        coins = await PumpClient(FakeCoinHttp()).coins({"first", "second"})
        self.assertEqual(coins["first"].symbol, "FIRST")
        self.assertEqual(coins["second"].symbol, "SECOND")


class FakeJsonResponse:
    status_code = 200

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def json(self) -> object:
        return self.payload


class FakePumpEvmHttp:
    def __init__(self, raw_balance: int = 12686197783044255065638262) -> None:
        self.raw_balance = raw_balance

    async def get(self, url: str, **_kwargs: object) -> FakeJsonResponse:
        self.last_get = url
        return FakeJsonResponse({
            "positions": [{
                "coinMint": "0x311cdbc8fbe3e5e04602aa688316efca5d327777",
                "chainId": 56,
                "amountHeld": 12686197.783044254,
                "valueUsd": 250,
                "hasTransfers": False,
                "callout": {"calloutId": "call"},
            }],
        })

    async def post(self, url: str, **_kwargs: object) -> FakeJsonResponse:
        self.last_post = url
        request = _kwargs.get("json")
        if isinstance(request, dict) and request.get("method") == "eth_call":
            params = request.get("params")
            call = params[0] if isinstance(params, list) and params else {}
            data = call.get("data") if isinstance(call, dict) else ""
            if data == "0x313ce567":
                return FakeJsonResponse({"jsonrpc": "2.0", "id": 1, "result": "0x12"})
            return FakeJsonResponse({"jsonrpc": "2.0", "id": 2,
                                     "result": hex(self.raw_balance)})
        return FakeJsonResponse({
            "data": {"holders": [
                {"walletAddress": "0x0000000000000000000000000000000000000001",
                 "balance": "42"},
                {"walletAddress": "0x1160079f1463dc5f9f20b1f1b9cf628718649c18",
                 "balance": "12686197.783044255065638262"},
            ]},
        })


class PumpEvmTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_unique_exact_holder_and_caches_reverse_match(self) -> None:
        user = PumpUser(address="sol-wallet", username="1000XCryptoD")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pump_evm.json"
            rpcs = {56: ["https://bsc-rpc.example"]}
            resolver = PumpEvmResolver(FakePumpEvmHttp(), path, rpcs=rpcs)
            match = await resolver.resolve(user)
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(
                match.evm,
                "0x1160079f1463dc5f9f20b1f1b9cf628718649c18",
            )
            self.assertTrue(match.verified_onchain)
            self.assertEqual(resolver.cached(match.evm), match)
            reloaded = PumpEvmResolver(FakePumpEvmHttp(), path, rpcs=rpcs)
            self.assertEqual(reloaded.cached(match.evm), match)

    async def test_rejects_index_match_when_rpc_balance_disagrees(self) -> None:
        user = PumpUser(address="sol-wallet", username="1000XCryptoD")
        with tempfile.TemporaryDirectory() as directory:
            resolver = PumpEvmResolver(
                FakePumpEvmHttp(raw_balance=42),
                Path(directory) / "pump_evm.json",
                rpcs={56: ["https://bsc-rpc.example"]},
            )
            self.assertIsNone(await resolver.resolve(user))


class FakeFallbackHttp:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def post(self, url: str, **_kwargs: object) -> FakeJsonResponse:
        self.urls.append(url)
        if url.endswith("primary"):
            response = FakeJsonResponse({"error": "unavailable"})
            response.status_code = 503
            return response
        return FakeJsonResponse({"jsonrpc": "2.0", "id": 1, "result": [
            {"signature": "sig-backup"},
        ]})


class PumpRpcFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_solana_rpc_uses_backup_after_primary_failure(self) -> None:
        http = FakeFallbackHttp()
        client = PumpChainClient(
            http,
            ["https://rpc.example/primary", "https://rpc.example/backup"],
        )
        self.assertEqual(await client.recent_signature_ids("wallet"), ["sig-backup"])
        self.assertEqual(http.urls, [
            "https://rpc.example/primary",
            "https://rpc.example/backup",
        ])

    async def test_duplicate_wallet_reads_share_one_signature_request(self) -> None:
        http = FakeFallbackHttp()
        client = PumpChainClient(http, ["https://rpc.example/backup"])
        first = await client.recent_signature_ids("wallet")
        second = await client.recent_signature_ids("wallet")
        self.assertEqual(first, second)
        self.assertEqual(http.urls, ["https://rpc.example/backup"])


class PumpEventTests(unittest.TestCase):
    def test_decodes_current_bonding_curve_trade_event(self) -> None:
        user_bytes = pubkey(1)
        mint_bytes = pubkey(2)
        user = b58encode(user_bytes)
        mint = b58encode(mint_bytes)
        payload = bonding_event(user_bytes, mint_bytes, bytes(32))
        tx = {
            "blockTime": 1_787_083_000,
            "transaction": {"signatures": ["sig-bonding"]},
            "meta": {
                "err": None,
                "logMessages": ["Program data: " + base64.b64encode(payload).decode()],
            },
        }
        trades = parse_pump_trades(tx, user)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].kind, "buy")
        self.assertEqual(trades[0].mint, mint)
        self.assertEqual(trades[0].quote_amount, 2_000_000)
        self.assertEqual(trades[0].quote_mint, "11111111111111111111111111111111")

    def test_decodes_pumpswap_event_and_finds_base_mint_from_wallet_delta(self) -> None:
        user_bytes = pubkey(8)
        user = b58encode(user_bytes)
        mint = b58encode(pubkey(9))
        payload = amm_buy_event(user_bytes, 1_000, 500_000_000)
        tx = {
            "blockTime": 1_787_083_100,
            "transaction": {"signatures": ["sig-amm"]},
            "meta": {
                "err": None,
                "logMessages": ["Program data: " + base64.b64encode(payload).decode()],
                "preTokenBalances": [{
                    "accountIndex": 3, "mint": mint, "owner": user,
                    "uiTokenAmount": {"amount": "100"},
                }],
                "postTokenBalances": [{
                    "accountIndex": 3, "mint": mint, "owner": user,
                    "uiTokenAmount": {"amount": "1100"},
                }],
            },
        }
        trades = parse_pump_trades(tx, user)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].source, "PumpSwap")
        self.assertEqual(trades[0].mint, mint)
        self.assertEqual(trades[0].quote_amount, 500_000_000)


class PumpTrackingTests(unittest.TestCase):
    def test_callout_baseline_and_store_are_separate(self) -> None:
        rows = [PumpCallout(
            id="call-1", user_id="wallet", mint="mint", thesis="Looks good",
            created_at="2026-08-18T20:00:00+00:00", market_cap=10_000,
            callout_price_usd=0.0001,
        )]
        state = pump_snapshot(["sig-1"], rows)
        self.assertEqual(new_callouts(rows, state), [])
        self.assertEqual(new_callouts(rows, {}), rows)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pump_tracks.json"
            store = PumpTrackingStore(path)
            store.add(1, 2, "wallet", "rowdy", state)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["tracks"]["1:wallet"]["handle"], "rowdy")


if __name__ == "__main__":
    unittest.main()
