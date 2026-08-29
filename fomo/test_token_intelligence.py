from __future__ import annotations

import unittest

from fomo_bot import _discord_line_chunks
from token_intelligence import (
    EXPLORER_HEADERS,
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


HYPER_TOKEN = "0xb75d5ee14708e7efbea939311090061d72265608"


class FakeHyperEvmHttp:
    """DEX Screener says `hyperevm`; hl.eco answers the holder list.

    Balances come back raw, so the decimals on the payload are what make the
    numbers right -- this fake uses 18, like a real pump.fun launch.
    """

    def __init__(self) -> None:
        self.holder_urls: list[str] = []

    async def get(self, url: str, **_kwargs: object) -> FakeResponse:
        if "/holders" in url:
            self.holder_urls.append(url)
            return FakeResponse({
                "address": HYPER_TOKEN,
                "decimals": 18,
                "symbol": "EGG",
                "totalSupply": "1000000000000000000000000000",
                "holderCount": 2523,
                "holders": [
                    {
                        "holder": "0x4444444444444444444444444444444444444444",
                        "balance": "24568705968933937780679279",
                        "pct": 2.4568705968,
                    },
                    {
                        "holder": "0x5555555555555555555555555555555555555555",
                        "balance": "10000000000000000000000000",
                        "pct": None,
                    },
                    {"holder": "not-an-address", "balance": "1"},
                ],
                "page": {"page": 1, "limit": 50, "reachable": 500, "hasMore": True},
            })
        return FakeResponse({"pairs": [{
            "chainId": "hyperevm",
            "dexId": "hyperswap",
            "baseToken": {"address": HYPER_TOKEN, "name": "egg", "symbol": "EGG"},
            "quoteToken": {"address": "0xquote", "symbol": "WHYPE"},
            "fdv": 5_779_461,
            "liquidity": {"usd": 275_133},
        }]})


class FakeHyperEvmMissing(FakeHyperEvmHttp):
    """An address the index has never seen: 200, but nothing to rank."""

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        if "/holders" in url:
            return FakeResponse({
                "address": HYPER_TOKEN,
                "decimals": None,
                "symbol": "",
                "totalSupply": None,
                "holderCount": None,
                "holders": [],
            })
        return await super().get(url, **kwargs)



RH_TOKEN = "0xcacb0e9caccee63ec4d82952e561a291c68bcb68"


class FakeRobinhoodHttp:
    """robinhoodchain.blockscout.com, answering the way it really does.

    Two things this payload gets right that the old parser assumed away: the
    holders response carries `items` and `next_page_params` and **no** `token`
    object, and the whole host is behind Cloudflare, which 403s a request
    without a browser-ish User-Agent.
    """

    def __init__(self) -> None:
        self.holder_calls: list[dict[str, object]] = []
        self.blocked = 0

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        headers = kwargs.get("headers") or {}
        if "blockscout" in url and not str(headers.get("User-Agent") or "").strip():
            self.blocked += 1
            return FakeResponse({"message": "Forbidden"}, status_code=403)
        if url.endswith("/holders"):
            params = kwargs.get("params") or {}
            self.holder_calls.append(dict(params))
            page = 2 if params else 1
            items = [{
                "address": {"hash": f"0x{index:040x}", "is_contract": False},
                "value": str((100 - index) * 10 ** 18),
            } for index in range(0, 3) if page == 1] or [{
                "address": {"hash": f"0x{index:040x}"},
                "value": str((100 - index) * 10 ** 18),
            } for index in range(3, 5)]
            body: dict[str, object] = {"items": items}
            if page == 1:
                body["next_page_params"] = {
                    "value": "970000000000000000000",
                    "address_hash": "0x" + "3" * 40,
                    "items_count": 50,
                }
            return FakeResponse(body)
        if url.endswith(f"/api/v2/tokens/{RH_TOKEN}"):
            return FakeResponse({
                "decimals": "18",
                "total_supply": str(1000 * 10 ** 18),
                "holders_count": "3127",
                "symbol": "GG",
            })
        return FakeResponse({"pairs": [{
            "chainId": "robinhood",
            "baseToken": {"address": RH_TOKEN, "name": "GG", "symbol": "GG"},
            "quoteToken": {"address": "0xquote", "symbol": "WETH"},
            "marketCap": 5_030_000,
            "priceUsd": "0.005029",
            "liquidity": {"usd": 90_000},
        }]})


class RobinhoodHolderTests(unittest.IsolatedAsyncioTestCase):
    """`/token` reported "Top holders of 0" for every Robinhood token."""

    async def test_explorer_calls_carry_a_user_agent(self) -> None:
        http = FakeRobinhoodHttp()
        client = TokenIntelligenceClient(http, [])
        token = await client.lookup(RH_TOKEN, limit=MAX_HOLDERS)
        self.assertEqual(http.blocked, 0)
        self.assertTrue(EXPLORER_HEADERS["User-Agent"])
        self.assertEqual(token.chain, "Robinhood")
        self.assertTrue(token.holders)

    async def test_percentages_come_from_the_token_route(self) -> None:
        client = TokenIntelligenceClient(FakeRobinhoodHttp(), [])
        token = await client.lookup(RH_TOKEN, limit=MAX_HOLDERS)
        # The holders payload has no `token` object; supply has to come from
        # /api/v2/tokens/{address} or every percentage is None.
        self.assertIsNotNone(token.holders[0].percentage)
        self.assertAlmostEqual(token.holders[0].percentage or 0, 10.0)
        self.assertEqual(float(token.holders[0].balance), 100.0)

    async def test_holders_page_when_the_first_page_is_short(self) -> None:
        http = FakeRobinhoodHttp()
        client = TokenIntelligenceClient(http, [])
        token = await client.lookup(RH_TOKEN, limit=MAX_HOLDERS)
        self.assertEqual(len(http.holder_calls), 2)
        self.assertEqual(http.holder_calls[1]["items_count"], 50)
        self.assertEqual(len(token.holders), 5)

    async def test_a_blocked_explorer_is_an_empty_card_not_a_crash(self) -> None:
        class Blocked(FakeRobinhoodHttp):
            async def get(self, url: str, **kwargs: object) -> FakeResponse:
                if "blockscout" in url:
                    return FakeResponse({"message": "Forbidden"}, status_code=403)
                return await super().get(url, **kwargs)

        client = TokenIntelligenceClient(Blocked(), [])
        token = await client.lookup(RH_TOKEN, limit=MAX_HOLDERS)
        self.assertEqual(token.holders, ())
        self.assertEqual(token.chain, "Robinhood")



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


class HyperliquidHolderTests(unittest.IsolatedAsyncioTestCase):
    """`/token` used to report zero holders for every Hyperliquid token."""

    async def test_hyperevm_pairs_are_labelled_hyperliquid(self) -> None:
        client = TokenIntelligenceClient(FakeHyperEvmHttp(), [])
        token = await client.lookup(HYPER_TOKEN, limit=MAX_HOLDERS)
        self.assertEqual(token.chain, "Hyperliquid")
        self.assertEqual(token.symbol, "EGG")

    async def test_holders_are_scaled_by_the_payload_decimals(self) -> None:
        http = FakeHyperEvmHttp()
        client = TokenIntelligenceClient(http, [])
        token = await client.lookup(HYPER_TOKEN, limit=MAX_HOLDERS)
        # Three rows in, one of them junk: the junk row is dropped rather than
        # rendered as an unlinkable holder.
        self.assertEqual(len(token.holders), 2)
        self.assertEqual(
            token.holders[0].address,
            "0x4444444444444444444444444444444444444444",
        )
        self.assertAlmostEqual(float(token.holders[0].balance), 24_568_705.968933938)
        self.assertAlmostEqual(token.holders[0].percentage or 0, 2.4568705968)
        # `pct: null` still gets a percentage -- from the supply on the payload.
        self.assertAlmostEqual(token.holders[1].percentage or 0, 1.0)
        self.assertTrue(http.holder_urls[0].endswith(f"/holders?limit={MAX_HOLDERS}"))

    async def test_an_unindexed_token_shortens_the_card_rather_than_failing(self) -> None:
        client = TokenIntelligenceClient(FakeHyperEvmMissing(), [])
        token = await client.lookup(HYPER_TOKEN, limit=MAX_HOLDERS)
        self.assertEqual(token.chain, "Hyperliquid")
        self.assertEqual(token.holders, ())


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
