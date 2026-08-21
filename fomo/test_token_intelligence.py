from __future__ import annotations

import unittest

from fomo_bot import _discord_line_chunks
from token_intelligence import (
    LARGEST_ACCOUNTS_CAP,
    MAX_HOLDERS,
    TokenIntelligenceClient,
)


class FakeResponse:
    def __init__(self, value: object, status_code: int = 200) -> None:
        self.value = value
        self.status_code = status_code

    def json(self) -> object:
        return self.value


class FakeSolanaHttp:
    async def get(self, _url: str, **_kwargs: object) -> FakeResponse:
        return FakeResponse({"pairs": [{
            "chainId": "solana",
            "url": "https://dex.example/token",
            "baseToken": {"address": "mint", "name": "Test Coin", "symbol": "TEST"},
            "quoteToken": {"address": "quote", "name": "SOL", "symbol": "SOL"},
            "marketCap": 123_456,
            "priceUsd": "0.00123",
            "liquidity": {"usd": 50_000},
            "info": {"imageUrl": "https://img.example/token.png"},
        }]})

    async def post(self, _url: str, **kwargs: object) -> FakeResponse:
        request = kwargs.get("json")
        method = request.get("method") if isinstance(request, dict) else None
        if method == "getTokenSupply":
            result = {"value": {"uiAmountString": "1000"}}
        elif method == "getTokenLargestAccounts":
            result = {"value": [
                {"address": "account1", "uiAmountString": "200"},
                {"address": "account2", "uiAmountString": "100"},
                {"address": "account3", "uiAmountString": "50"},
            ]}
        else:
            result = {"value": [
                {"data": {"parsed": {"info": {"owner": "walletA"}}}},
                {"data": {"parsed": {"info": {"owner": "walletB"}}}},
                {"data": {"parsed": {"info": {"owner": "walletA"}}}},
            ]}
        return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": result})


class FakeEvmHttp:
    async def get(self, _url: str, **_kwargs: object) -> FakeResponse:
        return FakeResponse({"pairs": [{
            "chainId": "base",
            "baseToken": {
                "address": "0x1111111111111111111111111111111111111111",
                "name": "Base Coin",
                "symbol": "BASE",
            },
            "quoteToken": {"address": "0xquote"},
            "fdv": 2_000_000,
            "liquidity": {"usd": 10_000},
        }]})

    async def post(self, _url: str, **_kwargs: object) -> FakeResponse:
        return FakeResponse({"data": {"holders": [
            {
                "walletAddress": "0x2222222222222222222222222222222222222222",
                "balance": "900",
                "percentage": "9",
            },
            {
                "walletAddress": "0x3333333333333333333333333333333333333333",
                "balance": "100",
                "percentage": "1",
            },
        ]}})


HELIUS_RPC = "https://mainnet.helius-rpc.com/?api-key=test"


class FakeDasHttp(FakeSolanaHttp):
    """Helius DAS, which pages past the 20-account cap `/token` used to hit."""

    def __init__(self, owners: int = 42, fail: bool = False) -> None:
        self.owners = owners
        self.fail = fail
        self.das_calls = 0
        self.largest_calls = 0

    async def post(self, _url: str, **kwargs: object) -> FakeResponse:
        request = kwargs.get("json")
        method = request.get("method") if isinstance(request, dict) else None
        if method == "getTokenAccounts":
            self.das_calls += 1
            if self.fail:
                return FakeResponse({"error": {"message": "DAS unavailable"}})
            accounts = [
                {"owner": f"owner{index:02d}", "amount": str((self.owners - index) * 10)}
                for index in range(self.owners)
            ]
            return FakeResponse({"result": {"token_accounts": accounts}})
        if method == "getTokenSupply":
            return FakeResponse({"result": {"value": {
                "uiAmountString": "1000", "decimals": 1,
            }}})
        if method == "getTokenLargestAccounts":
            self.largest_calls += 1
        return await super().post(_url, **kwargs)


class TokenIntelligenceTests(unittest.IsolatedAsyncioTestCase):
    def test_holder_lines_are_split_at_discord_field_limit(self) -> None:
        lines = [f"`{index}.` " + ("x" * 190) for index in range(1, 11)]
        chunks = _discord_line_chunks(lines)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1024 for chunk in chunks))
        self.assertEqual(sum(chunk.count("`") // 2 for chunk in chunks), 10)

    async def test_solana_metadata_and_owner_aggregation(self) -> None:
        client = TokenIntelligenceClient(FakeSolanaHttp(), ["https://rpc.example"])
        token = await client.lookup("mint", limit=5)
        self.assertEqual(token.chain, "Solana")
        self.assertEqual(token.symbol, "TEST")
        self.assertEqual(token.market_cap, 123_456)
        self.assertEqual(token.image_url, "https://img.example/token.png")
        self.assertEqual([holder.address for holder in token.holders], ["walletA", "walletB"])
        self.assertEqual(float(token.holders[0].balance), 250)
        self.assertAlmostEqual(token.holders[0].percentage or 0, 25)

    async def test_evm_holders_use_detected_chain(self) -> None:
        token_address = "0x1111111111111111111111111111111111111111"
        client = TokenIntelligenceClient(FakeEvmHttp(), [])
        token = await client.lookup(token_address, limit=10)
        self.assertEqual(token.chain, "Base")
        self.assertIsNone(token.market_cap)
        self.assertEqual(token.fdv, 2_000_000)
        self.assertEqual(len(token.holders), 2)
        self.assertEqual(token.holders[0].percentage, 9)


class DeepHolderTests(unittest.IsolatedAsyncioTestCase):
    """`/token` asks for 50 holders; `getTokenLargestAccounts` cannot answer."""

    async def test_a_top_50_request_pages_helius_das(self) -> None:
        http = FakeDasHttp(owners=42)
        client = TokenIntelligenceClient(http, [HELIUS_RPC])
        token = await client.lookup("mint", limit=MAX_HOLDERS)
        self.assertEqual(len(token.holders), 42)
        self.assertGreater(len(token.holders), LARGEST_ACCOUNTS_CAP)
        self.assertEqual(http.largest_calls, 0)
        self.assertEqual(token.holders[0].address, "owner00")
        # amount 420 raw / 10**1 decimals = 42 tokens of a 1000 supply
        self.assertEqual(float(token.holders[0].balance), 42)
        self.assertAlmostEqual(token.holders[0].percentage or 0, 4.2)

    async def test_holders_come_back_largest_first(self) -> None:
        client = TokenIntelligenceClient(FakeDasHttp(owners=30), [HELIUS_RPC])
        token = await client.lookup("mint", limit=MAX_HOLDERS)
        balances = [holder.balance for holder in token.holders]
        self.assertEqual(balances, sorted(balances, reverse=True))

    async def test_a_small_request_never_pays_for_das(self) -> None:
        http = FakeDasHttp()
        client = TokenIntelligenceClient(http, [HELIUS_RPC])
        await client.lookup("mint", limit=10)
        self.assertEqual(http.das_calls, 0)
        self.assertEqual(http.largest_calls, 1)

    async def test_das_failure_falls_back_rather_than_emptying_the_card(self) -> None:
        # No Helius, or a Helius that will not answer, still has to produce the
        # holders the old path could reach -- a shorter card, not a blank one.
        http = FakeDasHttp(fail=True)
        client = TokenIntelligenceClient(http, [HELIUS_RPC])
        token = await client.lookup("mint", limit=MAX_HOLDERS)
        self.assertEqual(http.largest_calls, 1)
        self.assertEqual([holder.address for holder in token.holders],
                         ["walletA", "walletB"])

    async def test_no_helius_endpoint_skips_das_entirely(self) -> None:
        http = FakeDasHttp()
        client = TokenIntelligenceClient(http, ["https://api.mainnet-beta.solana.com"])
        token = await client.lookup("mint", limit=MAX_HOLDERS)
        self.assertEqual(http.das_calls, 0)
        self.assertEqual(len(token.holders), 2)

    async def test_the_holder_limit_is_clamped_not_snapped_to_5_or_10(self) -> None:
        client = TokenIntelligenceClient(FakeDasHttp(owners=60), [HELIUS_RPC])
        self.assertEqual(len(
            (await client.lookup("mint", limit=MAX_HOLDERS + 25)).holders
        ), MAX_HOLDERS)
        self.assertEqual(len((await client.lookup("mint", limit=25)).holders), 25)
        self.assertEqual(len((await client.lookup("mint", limit=1)).holders), 1)


if __name__ == "__main__":
    unittest.main()
