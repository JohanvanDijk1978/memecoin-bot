from __future__ import annotations

import unittest

from fomo_bot import _discord_line_chunks
from token_intelligence import TokenIntelligenceClient


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


if __name__ == "__main__":
    unittest.main()
