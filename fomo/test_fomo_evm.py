from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from decimal import Decimal

from fomo_evm import (
    TRANSFER_TOPIC,
    evidence_windows,
    select_evidence_groups,
    EvmTransfer,
    EvmWalletResolver,
    _relays_amount,
    cached_evm_wallet,
    evm_trade_evidence,
    evm_trade_ids,
)


ADDRESS = "0x27394168fdcfe5ea4e2042df3949a619238f3627"
OTHER_ADDRESS = "0x1111111111111111111111111111111111111111"
TOKEN_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
OUROBOROS_TOKENS = {
    "0xe172e9b6cfbeeb5593bdce3f077356fdb33af904": 1,
    "0x4e8fc9e5a6d2b9c6e7ca8b923661ca4e78087777": 56,
    "0xb9972ca7188e511174947e3936a5315ac7073277": 4663,
}


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, code: str = "0x6001") -> None:
        self.code = code
        self.posts = 0
        self.gets = 0

    async def get(self, *_args, **_kwargs) -> FakeResponse:
        self.gets += 1
        return FakeResponse({"message": "not found"}, 404)

    async def post(self, *_args, **_kwargs) -> FakeResponse:
        self.posts += 1
        return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": self.code})


class BalanceDiscoveryHttp(FakeHttp):
    token = "0x1111111111111111111111111111111111111111"
    amount = "123.456"

    def __init__(self) -> None:
        super().__init__()

    async def get(self, *_args, **_kwargs) -> FakeResponse:
        self.gets += 1
        return FakeResponse({"error": "unavailable"}, 503)

    async def post(self, url: str, **kwargs) -> FakeResponse:
        request = kwargs.get("json")
        if "coinmarketcap" in url:
            return FakeResponse({"data": {"holders": [
                {"walletAddress": ADDRESS, "balance": self.amount},
                {"walletAddress": "0x0000000000000000000000000000000000000001",
                 "balance": "1"},
            ]}})
        if isinstance(request, dict) and request.get("method") == "eth_call":
            data = request["params"][0]["data"]
            if data == "0x313ce567":
                return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": "0x12"})
            raw = int(self.amount.replace(".", "")) * 10 ** 15
            return FakeResponse({"jsonrpc": "2.0", "id": 2, "result": hex(raw)})
        return FakeResponse({"jsonrpc": "2.0", "id": 3, "result": "0x6001"})


class TransactionDiscoveryHttp(FakeHttp):
    def __init__(self, recipients: dict[str, str] | None = None) -> None:
        super().__init__()
        self.recipients = recipients or {TOKEN_A: ADDRESS, TOKEN_B: ADDRESS}
        self.unrelated_gets = 0

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        if "/transactions/0xtx-a/token-transfers" in url:
            return FakeResponse({"items": [{
                "token": {"symbol": "USDC", "decimals": 6},
                "total": {"value": "100000000", "decimals": 6},
            }]})
        self.unrelated_gets += 1
        return FakeResponse({"message": "not found"}, 404)

    async def post(self, url: str, **kwargs) -> FakeResponse:
        request = kwargs.get("json") or {}
        method = request.get("method")
        if method == "alchemy_getAssetTransfers":
            token = request["params"][0]["contractAddresses"][0]
            if token == TOKEN_A:
                timestamp, amount, transaction = (
                    "2026-08-19T10:00:00Z", "1000", "0xtx-a"
                )
            else:
                timestamp, amount, transaction = (
                    "2026-08-19T09:00:00Z", "2000", "0xtx-b"
                )
            return FakeResponse({"result": {"transfers": [{
                "hash": transaction,
                "from": "0x2222222222222222222222222222222222222222",
                "to": self.recipients[token],
                "value": amount,
                "rawContract": {"address": token},
                "metadata": {"blockTimestamp": timestamp},
            }]}})
        if method == "eth_getTransactionReceipt":
            return FakeResponse({"result": {"logs": [{
                "address": "0x55d398326f99059ff775485246999027b3197955",
                "topics": [TRANSFER_TOPIC],
                "data": hex(200 * 10**18),
            }]}})
        if method == "eth_getCode":
            return FakeResponse({"result": "0x6001"})
        return FakeResponse({"result": "0x"})


class TransactionFailoverHttp(TransactionDiscoveryHttp):
    def __init__(self) -> None:
        super().__init__()
        self.failed_primaries = 0

    async def post(self, url: str, **kwargs) -> FakeResponse:
        request = kwargs.get("json") or {}
        if (request.get("method") == "alchemy_getAssetTransfers"
                and "primary.g.alchemy.com" in url):
            self.failed_primaries += 1
            raise RuntimeError("primary unavailable")
        return await super().post(url, **kwargs)


class TiedTransferHttp(TransactionDiscoveryHttp):
    async def post(self, url: str, **kwargs) -> FakeResponse:
        request = kwargs.get("json") or {}
        if request.get("method") == "alchemy_getAssetTransfers":
            token = request["params"][0]["contractAddresses"][0]
            return FakeResponse({"result": {"transfers": [
                {
                    "hash": "0xtx-tie-a",
                    "from": OTHER_ADDRESS,
                    "to": ADDRESS,
                    "value": "1000",
                    "rawContract": {"address": token},
                    "metadata": {"blockTimestamp": "2026-08-19T10:00:00Z"},
                },
                {
                    "hash": "0xtx-tie-b",
                    "from": OTHER_ADDRESS,
                    "to": "0x3333333333333333333333333333333333333333",
                    "value": "1000",
                    "rawContract": {"address": token},
                    "metadata": {"blockTimestamp": "2026-08-19T10:00:00Z"},
                },
            ]}})
        return await super().post(url, **kwargs)


def transaction_swaps(second: bool = True) -> dict:
    rows = [{
        "id": "swap-a",
        "outTradeId": "trade-a",
        "outTokenAddress": TOKEN_A,
        "outNetworkId": 1,
        "outHumanAmount": "1000",
        "humanUsdAmountIn": "100",
        "createdAt": "2026-08-19T10:00:00Z",
    }]
    if second:
        rows.append({
            "id": "swap-b",
            "outTradeId": "trade-b",
            "outTokenAddress": TOKEN_B,
            "outNetworkId": 56,
            "outHumanAmount": "2000",
            "humanUsdAmountIn": "200",
            "createdAt": "2026-08-19T09:00:00Z",
        })
    return {"swaps": rows}


# chieftom17's real Robinhood Chain trades. Every swap is relayed: the token
# moves pool -> router(s) -> trader on a buy and trader -> router(s) -> pool on
# a sell, so each hop carries the exact traded amount and matches FOMO's
# fingerprint as strongly as the trader does.
CHIEF_WALLET = "0x79e2c27aaf07704932037e8abd50112e0c66742b"
CHIEF_ROUTER = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"
CHIEF_RELAY = "0x8f10b468b06c6fd214b65f87778827f7d113f996"
CATS = "0x88eeef5d676d7eb5363df6002381adafe7488455"
CATS_POOL = "0xb61955186db38c3dbad9c5bf727bb8ebc5e34a6f"
DJT = "0xf793b93c2479a7dd71d3254c42be3a3e34ad1e18"
DJT_POOL = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
DJT_HOP = "0x39b38686a19836ac10162c490e4558e120cbbe5f"
CATS_BUY = "20716330.002154"
CATS_SELL = "19396388.1994"
DJT_AMOUNT = "20013820.875862"


def _transfer(token: str, tx: str, sender: str, recipient: str,
              amount: str, timestamp: str) -> dict:
    return {
        "hash": tx, "from": sender, "to": recipient, "value": amount,
        "rawContract": {"address": token},
        "metadata": {"blockTimestamp": timestamp},
    }


ROUTED_TRANSFERS = {
    CATS: [
        # buy: pool -> router -> trader
        _transfer(CATS, "0xcats-buy", CATS_POOL, CHIEF_ROUTER,
                  CATS_BUY, "2026-08-18T00:18:05Z"),
        _transfer(CATS, "0xcats-buy", CHIEF_ROUTER, CHIEF_WALLET,
                  CATS_BUY, "2026-08-18T00:18:05Z"),
        # sell: trader -> router -> relay -> pool
        _transfer(CATS, "0xcats-sell", CHIEF_WALLET, CHIEF_ROUTER,
                  CATS_SELL, "2026-08-18T01:20:28Z"),
        _transfer(CATS, "0xcats-sell", CHIEF_ROUTER, CHIEF_RELAY,
                  CATS_SELL, "2026-08-18T01:20:28Z"),
        _transfer(CATS, "0xcats-sell", CHIEF_RELAY, CATS_POOL,
                  CATS_SELL, "2026-08-18T01:20:28Z"),
    ],
    DJT: [
        # buy: pool -> hop -> router -> trader
        _transfer(DJT, "0xdjt-buy", DJT_POOL, DJT_HOP,
                  DJT_AMOUNT, "2026-08-19T15:35:05Z"),
        _transfer(DJT, "0xdjt-buy", DJT_HOP, CHIEF_ROUTER,
                  DJT_AMOUNT, "2026-08-19T15:35:05Z"),
        _transfer(DJT, "0xdjt-buy", CHIEF_ROUTER, CHIEF_WALLET,
                  DJT_AMOUNT, "2026-08-19T15:35:05Z"),
        # sell 94s later, same amount, so the buy window also covers it
        _transfer(DJT, "0xdjt-sell", CHIEF_WALLET, CHIEF_ROUTER,
                  DJT_AMOUNT, "2026-08-19T15:36:39Z"),
        _transfer(DJT, "0xdjt-sell", CHIEF_ROUTER, DJT_POOL,
                  DJT_AMOUNT, "2026-08-19T15:36:39Z"),
    ],
}


class RoutedTransferHttp(FakeHttp):
    """Robinhood Chain transfers with the real multi-hop router legs."""

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        return FakeResponse({"message": "not found"}, 404)

    async def post(self, url: str, **kwargs) -> FakeResponse:
        request = kwargs.get("json") or {}
        method = request.get("method")
        if method == "alchemy_getAssetTransfers":
            token = request["params"][0]["contractAddresses"][0]
            return FakeResponse(
                {"result": {"transfers": ROUTED_TRANSFERS.get(token, [])}}
            )
        if method == "eth_getTransactionReceipt":
            return FakeResponse({"result": {"logs": []}})
        if method == "eth_getCode":
            return FakeResponse({"result": "0x6001"})
        return FakeResponse({"result": "0x"})


def routed_swaps() -> dict:
    return {"swaps": [
        {
            "id": "cats-buy", "outTradeId": "trade-cats",
            "outTokenAddress": CATS, "outNetworkId": 4663,
            "outHumanAmount": CATS_BUY, "humanUsdAmountIn": "497.33107",
            "createdAt": "2026-08-18T00:18:03Z",
        },
        {
            "id": "cats-sell", "inTradeId": "trade-cats",
            "inTokenAddress": CATS, "inNetworkId": 4663,
            "inHumanAmount": CATS_SELL, "humanUsdAmountOut": "528.73893",
            "createdAt": "2026-08-18T01:20:29Z",
        },
        {
            "id": "djt-buy", "outTradeId": "trade-djt",
            "outTokenAddress": DJT, "outNetworkId": 4663,
            "outHumanAmount": DJT_AMOUNT, "humanUsdAmountIn": "497.271574",
            "createdAt": "2026-08-19T15:35:02Z",
        },
        {
            "id": "djt-sell", "inTradeId": "trade-djt",
            "inTokenAddress": DJT, "inNetworkId": 4663,
            "inHumanAmount": DJT_AMOUNT, "humanUsdAmountOut": "480.200075",
            "createdAt": "2026-08-19T15:36:40Z",
        },
    ]}


# A busy BSC token: thousands of transfers per minute, so a descending scan
# from the chain head only ever sees the last few minutes. insentos' trades
# were up to four days old and none were reachable that way.
HOT_HEAD = 60_000_000
HOT_RATE = 0.75
HOT_HEAD_TIME = 1_787_200_000
HOT_TOKEN = "0x789d83d2881c439695dbd8dfebc7cf1093c97777"
HOT_TRADE_TIME = HOT_HEAD_TIME - 43_200
HOT_TRADE_BLOCK = HOT_HEAD - int(43_200 / HOT_RATE)
HOT_WALLET = "0x93c006f2051cb72168cf8c27cafe0fb2d71682c8"


class HotTokenHttp(FakeHttp):
    def __init__(self) -> None:
        super().__init__()
        self.transfer_requests: list[dict] = []
        self.block_probes = 0

    @staticmethod
    def block_time(number: int) -> int:
        return int(HOT_HEAD_TIME - (HOT_HEAD - number) * HOT_RATE)

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        return FakeResponse({"message": "not found"}, 404)

    async def post(self, url: str, **kwargs) -> FakeResponse:
        request = kwargs.get("json") or {}
        method = request.get("method")
        if method == "eth_blockNumber":
            return FakeResponse({"result": hex(HOT_HEAD)})
        if method == "eth_getBlockByNumber":
            self.block_probes += 1
            number = int(request["params"][0], 16)
            return FakeResponse(
                {"result": {"timestamp": hex(self.block_time(number))}}
            )
        if method == "alchemy_getAssetTransfers":
            params = request["params"][0]
            self.transfer_requests.append(params)
            if params["fromBlock"] == "0x0":
                # unbounded descending scan: only the newest minutes are in reach
                return FakeResponse({"result": {"transfers": [
                    _transfer(HOT_TOKEN, f"0xnoise-{index}", OTHER_ADDRESS,
                              OTHER_ADDRESS, "123",
                              _iso(HOT_HEAD_TIME - index))
                    for index in range(50)
                ]}})
            low = int(params["fromBlock"], 16)
            high = int(params["toBlock"], 16)
            if not low <= HOT_TRADE_BLOCK <= high:
                return FakeResponse({"result": {"transfers": []}})
            return FakeResponse({"result": {"transfers": [
                _transfer(HOT_TOKEN, "0xhot-buy", OTHER_ADDRESS, HOT_WALLET,
                          "5267132.976472", _iso(HOT_TRADE_TIME)),
            ]}})
        if method == "eth_getCode":
            return FakeResponse({"result": "0x6001"})
        return FakeResponse({"result": "0x"})


class TokenChoiceHttp(FakeHttp):
    """Records which tokens the resolver decided to search."""

    def __init__(self) -> None:
        super().__init__()
        self.searched: list[str] = []

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        return FakeResponse({"message": "not found"}, 404)

    async def post(self, url: str, **kwargs) -> FakeResponse:
        request = kwargs.get("json") or {}
        if request.get("method") == "alchemy_getAssetTransfers":
            self.searched.append(request["params"][0]["contractAddresses"][0])
            return FakeResponse({"result": {"transfers": []}})
        return FakeResponse({"result": "0x"})


def _iso(epoch: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def crowded_swaps() -> dict:
    """One recent five-swap token behind many older single-swap tokens."""
    rows = []
    for index in range(8):
        token = f"0x{index:040x}"
        rows.append({
            "id": f"old-{index}", "outTradeId": f"trade-old-{index}",
            "outTokenAddress": token, "outNetworkId": 56,
            "outHumanAmount": "1000", "humanUsdAmountIn": "100",
            "createdAt": _iso(HOT_HEAD_TIME - 400_000 + index),
        })
    for index in range(5):
        rows.append({
            "id": f"hot-{index}", "outTradeId": "trade-hot",
            "outTokenAddress": HOT_TOKEN, "outNetworkId": 56,
            "outHumanAmount": "5267132.976472", "humanUsdAmountIn": "996.21",
            "createdAt": _iso(HOT_TRADE_TIME + index * 60),
        })
    return {"swaps": rows}


class EvmWalletResolverTests(unittest.IsolatedAsyncioTestCase):
    def test_separate_trade_clusters_become_separate_windows(self) -> None:
        """One span per token would leave a day-long gap unsearched."""
        swaps = {"swaps": [
            {
                "id": f"swap-{index}", "outTradeId": "trade-a",
                "outTokenAddress": TOKEN_A, "outNetworkId": 56,
                "outHumanAmount": "1000", "humanUsdAmountIn": "100",
                "createdAt": stamp,
            }
            for index, stamp in enumerate((
                "2026-08-15T23:14:22Z", "2026-08-15T23:16:10Z",
                "2026-08-17T07:30:03Z",
            ))
        ]}
        groups = select_evidence_groups(evm_trade_evidence(swaps=swaps))
        windows = evidence_windows(groups)

        self.assertEqual(len(windows), 2)
        spans = sorted((end - start) for _, start, end in windows)
        self.assertLess(max(spans), 3600)

    async def test_block_search_locates_a_timestamp_in_few_probes(self) -> None:
        http = HotTokenHttp()
        resolver = EvmWalletResolver(
            http,
            rpcs={"bsc": "https://bnb-mainnet.g.alchemy.com/v2/test"},
        )

        block = await resolver._block_number_at(56, HOT_TRADE_TIME)

        self.assertIsNotNone(block)
        self.assertLessEqual(http.block_time(block), HOT_TRADE_TIME)
        self.assertLessEqual(HOT_TRADE_TIME - http.block_time(block), 4)
        self.assertLess(http.block_probes, 12)
        # a second lookup reuses the cached samples
        probes = http.block_probes
        await resolver._block_number_at(56, HOT_TRADE_TIME)
        self.assertLess(http.block_probes - probes, 12)

    async def test_hot_token_is_searched_by_block_range_not_from_the_head(self) -> None:
        """A descending scan from the head cannot reach an older trade."""
        http = HotTokenHttp()
        resolver = EvmWalletResolver(
            http,
            rpcs={"bsc": "https://bnb-mainnet.g.alchemy.com/v2/test"},
        )

        transfers = await resolver._transfers_for_token(
            56, HOT_TOKEN, HOT_TRADE_TIME, HOT_TRADE_TIME
        )

        self.assertEqual([row.transaction for row in transfers], ["0xhot-buy"])
        params = http.transfer_requests[0]
        self.assertEqual(params["order"], "asc")
        self.assertNotEqual(params["fromBlock"], "0x0")
        self.assertNotEqual(params["toBlock"], "latest")
        self.assertLessEqual(int(params["fromBlock"], 16), HOT_TRADE_BLOCK)
        self.assertGreaterEqual(int(params["toBlock"], 16), HOT_TRADE_BLOCK)

    async def test_token_search_prefers_most_evidence_over_evidence_order(self) -> None:
        """Evidence order alone picked the oldest tokens and skipped the rest."""
        with tempfile.TemporaryDirectory() as directory:
            http = TokenChoiceHttp()
            resolver = EvmWalletResolver(
                http,
                rpcs={"bsc": "https://bnb-mainnet.g.alchemy.com/v2/test"},
                cache_path=Path(directory) / "wallets.json",
            )
            evidence = evm_trade_evidence(swaps=crowded_swaps())

            await resolver._resolve_from_transactions("insentos", evidence)

            self.assertIn(HOT_TOKEN, http.searched)
            self.assertLessEqual(len(set(http.searched)), 6)

    async def test_routed_swaps_resolve_the_trader_not_the_router(self) -> None:
        """A relay carries the same amount as the trader and used to tie."""
        with tempfile.TemporaryDirectory() as directory:
            resolver = EvmWalletResolver(
                RoutedTransferHttp(),
                rpcs={"robinhood": "https://robinhood-mainnet.g.alchemy.com/v2/test"},
                cache_path=Path(directory) / "wallets.json",
            )
            evidence = evm_trade_evidence(swaps=routed_swaps())
            self.assertEqual(len(evidence), 4)

            result = await resolver._resolve_from_transactions("chieftom17", evidence)

            self.assertEqual(result, CHIEF_WALLET)
            cached = json.loads(
                (Path(directory) / "wallets.json").read_text(encoding="utf-8")
            )
            entry = cached["chieftom17"]
            self.assertEqual(entry["evmWallet"], CHIEF_WALLET)
            self.assertEqual(entry["evmSource"], "transactions+rpc")
            self.assertEqual(entry["evmConfirmed"], 4)
            self.assertEqual(entry["evmEvidenceTokens"], sorted([CATS, DJT]))

    async def test_relayed_amount_is_not_a_wallet_candidate(self) -> None:
        buy = _transfer(CATS, "0xcats-buy", CHIEF_ROUTER, CHIEF_WALLET,
                        CATS_BUY, "2026-08-18T00:18:05Z")
        hop = _transfer(CATS, "0xcats-buy", CATS_POOL, CHIEF_ROUTER,
                        CATS_BUY, "2026-08-18T00:18:05Z")
        legs = [
            EvmTransfer(CATS, 4663, row["hash"], row["from"], row["to"],
                        0, Decimal(row["value"]))
            for row in (buy, hop)
        ]
        amount = Decimal(CATS_BUY)
        tolerance = amount * Decimal("0.01")

        # the router receives the amount and forwards it in the same transaction
        self.assertTrue(
            _relays_amount(legs, CHIEF_ROUTER, amount, tolerance, "buy")
        )
        # the trader only receives it
        self.assertFalse(
            _relays_amount(legs, CHIEF_WALLET, amount, tolerance, "buy")
        )
        # an unrelated amount in the same transaction is not a relay
        self.assertFalse(
            _relays_amount(legs, CHIEF_ROUTER, amount * 3, tolerance, "buy")
        )

    async def test_equal_time_transfer_candidates_are_sorted_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resolver = EvmWalletResolver(
                TiedTransferHttp(),
                rpcs={"ethereum": "https://eth-mainnet.g.alchemy.com/v2/test"},
                cache_path=Path(directory) / "wallets.json",
            )
            evidence = evm_trade_evidence(swaps=transaction_swaps(False))

            result = await resolver._resolve_from_transactions("tied", evidence)

            self.assertIsNone(result)

    async def test_mixed_chain_profile_retains_ouroboros_evm_evidence(self) -> None:
        trades = {"activeTrades": [
            {"trade": {
                "id": f"trade-{chain_id}",
                "tokenAddress": token,
                "networkId": chain_id,
                "createdAt": "2026-08-19T10:00:00Z",
                "humanTokenAmount": "1000",
            }}
            for token, chain_id in OUROBOROS_TOKENS.items()
        ]}
        swaps = {"swaps": [
            {
                "id": f"swap-{chain_id}",
                "outTradeId": f"trade-{chain_id}",
                "outTokenAddress": token,
                "outNetworkId": chain_id,
                "outHumanAmount": "1000",
                "humanUsdAmountIn": "500",
                "createdAt": "2026-08-19T10:00:00Z",
            }
            for token, chain_id in OUROBOROS_TOKENS.items()
        ] + [{
            "id": "solana-noise",
            "outTradeId": "solana-trade",
            "outTokenAddress": "So11111111111111111111111111111111111111112",
            "outNetworkId": 1399811149,
            "outHumanAmount": "1",
            "humanUsdAmountIn": "100",
            "createdAt": "2026-08-19T10:00:00Z",
        }]}

        evidence = evm_trade_evidence(swaps, trades)

        self.assertEqual({item.token for item in evidence}, set(OUROBOROS_TOKENS))
        self.assertEqual({item.chain_id for item in evidence}, {1, 56, 4663})
        self.assertTrue(all(not item.aggregate for item in evidence))

    async def test_two_historical_transactions_discover_and_cache_wallet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            http = TransactionDiscoveryHttp()
            resolver = EvmWalletResolver(
                http,
                rpcs={
                    "ethereum": "https://eth-mainnet.g.alchemy.com/v2/test",
                    "bsc": "https://bnb-mainnet.g.alchemy.com/v2/test",
                },
                cache_path=path,
            )
            result = await resolver.resolve(
                SimpleNamespace(handle="Ouroboros"), swaps=transaction_swaps()
            )
            self.assertEqual(result, ADDRESS)
            saved = json.loads(path.read_text())["ouroboros"]
            self.assertEqual(saved["evmSource"], "transactions+rpc")
            self.assertEqual(saved["evmConfirmed"], 2)
            self.assertEqual(set(saved["evmEvidenceTokens"]), {TOKEN_A, TOKEN_B})

    async def test_transaction_search_uses_backup_alchemy_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            http = TransactionFailoverHttp()
            resolver = EvmWalletResolver(
                http,
                rpcs={
                    "ethereum": [
                        "https://primary.g.alchemy.com/v2/test",
                        "https://eth-mainnet.g.alchemy.com/v2/test",
                    ],
                    "bsc": [
                        "https://primary.g.alchemy.com/v2/test",
                        "https://bnb-mainnet.g.alchemy.com/v2/test",
                    ],
                },
                cache_path=Path(directory) / "wallets.json",
            )
            result = await resolver.resolve(
                SimpleNamespace(handle="failover"), swaps=transaction_swaps()
            )
            self.assertEqual(result, ADDRESS)
            self.assertEqual(http.failed_primaries, 2)

    async def test_one_historical_transaction_is_not_enough(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            http = TransactionDiscoveryHttp()
            resolver = EvmWalletResolver(
                http,
                rpcs={"ethereum": "https://eth-mainnet.g.alchemy.com/v2/test"},
                cache_path=Path(directory) / "wallets.json",
            )
            result = await resolver.resolve(
                SimpleNamespace(handle="single"), swaps=transaction_swaps(False)
            )
            self.assertIsNone(result)
            self.assertEqual(http.unrelated_gets, 0)

    async def test_different_wallets_across_trades_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            http = TransactionDiscoveryHttp({TOKEN_A: ADDRESS, TOKEN_B: OTHER_ADDRESS})
            resolver = EvmWalletResolver(
                http,
                rpcs={
                    "ethereum": "https://eth-mainnet.g.alchemy.com/v2/test",
                    "bsc": "https://bnb-mainnet.g.alchemy.com/v2/test",
                },
                cache_path=Path(directory) / "wallets.json",
            )
            result = await resolver.resolve(
                SimpleNamespace(handle="ambiguous"), swaps=transaction_swaps()
            )
            self.assertIsNone(result)

    async def test_trade_details_supply_swaps_missing_from_profile_feed(self) -> None:
        details = [{"trade": {
            "id": "trade-a", "tokenAddress": TOKEN_A, "networkId": 1,
            "createdAt": "2026-08-19T10:00:00Z", "humanTokenAmount": "1000",
        }, "swaps": transaction_swaps(False)["swaps"]}]
        evidence = evm_trade_evidence(details=details)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].token, TOKEN_A)
        self.assertFalse(evidence[0].aggregate)

    async def test_trade_detail_selection_prefers_low_liquidity_then_oldest(self) -> None:
        trades = {"activeTrades": [
            {"trade": {"id": "high", "tokenAddress": TOKEN_A, "networkId": 1,
                       "createdAt": "2026-08-19T08:00:00Z",
                       "tokenMetadata": {"liquidity": 500000}}},
            {"trade": {"id": "low", "tokenAddress": TOKEN_B, "networkId": 56,
                       "createdAt": "2026-08-19T10:00:00Z",
                       "tokenMetadata": {"liquidity": 10000}}},
        ]}
        self.assertEqual(evm_trade_ids(trades), ["low", "high"])

    async def test_exact_fomo_balance_discovers_wallet_without_index(self) -> None:
        balances = {"balances": [{
            "balance": {
                "tokenAddress": BalanceDiscoveryHttp.token,
                "shiftedBalance": BalanceDiscoveryHttp.amount,
            },
            "tokenFilterResult": {"priceUSD": "2"},
            "userToken": {"networkId": 8453},
        }]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            http = BalanceDiscoveryHttp()
            resolver = EvmWalletResolver(
                http,
                rpcs={"base": "https://base.invalid"},
                cache_path=path,
            )
            result = await resolver.resolve(
                SimpleNamespace(handle="BalanceUser"), balances=balances
            )
            self.assertEqual(result, ADDRESS)
            self.assertEqual(http.gets, 0)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["balanceuser"]["evmSource"], "balance+rpc")

    async def test_no_evidence_returns_none_without_identity_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            http = FakeHttp()
            resolver = EvmWalletResolver(
                http,
                rpcs={"base": "https://base.invalid"},
                cache_path=Path(directory) / "wallets.json",
            )
            result = await resolver.resolve(SimpleNamespace(handle="Konito"))
            self.assertIsNone(result)
            self.assertEqual(http.gets, 0)
            self.assertEqual(http.posts, 0)

    async def test_cached_wallet_is_returned_without_network_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            path.write_text(json.dumps({"konito": {
                "wallet": "solana-wallet",
                "evmWallet": ADDRESS,
                "evmSource": "legacy+rpc",
            }}))
            http = FakeHttp()
            resolver = EvmWalletResolver(
                http, rpcs={"base": "https://base.invalid"}, cache_path=path
            )
            result = await resolver.resolve(SimpleNamespace(handle="Konito"))
            self.assertEqual(result, ADDRESS)
            self.assertEqual(http.gets, 0)
            self.assertEqual(http.posts, 0)
            self.assertEqual(json.loads(path.read_text())["konito"]["wallet"], "solana-wallet")

    async def test_manual_deployed_wallet_is_cached_with_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            path.write_text(json.dumps({"onmycheck": {"wallet": "solana-wallet"}}))
            resolver = EvmWalletResolver(
                FakeHttp(), rpcs={"base": "https://base.invalid"}, cache_path=path
            )
            result = await resolver.verify_and_cache("OnMyCheck", ADDRESS.upper())
            self.assertEqual(result, ADDRESS)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["onmycheck"]["wallet"], "solana-wallet")
            self.assertEqual(saved["onmycheck"]["evmSource"], "manual+rpc")

    async def test_manual_undeployed_wallet_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resolver = EvmWalletResolver(
                FakeHttp(code="0x"), rpcs={"base": "https://base.invalid"},
                cache_path=Path(directory) / "wallets.json",
            )
            result = await resolver.verify_and_cache("onmycheck", ADDRESS)
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
