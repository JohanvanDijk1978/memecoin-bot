from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fomo_evm import EvmWalletResolver, cached_evm_wallet


ADDRESS = "0x27394168fdcfe5ea4e2042df3949a619238f3627"


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
    def __init__(self, status: str = "verified", code: str = "0x6001") -> None:
        self.status = status
        self.code = code
        self.posts = 0

    async def get(self, *_args, **_kwargs) -> FakeResponse:
        return FakeResponse({
            "user": {"wallets": {"evm": {
                "address": ADDRESS,
                "status": self.status,
                "verifiedAt": "2026-06-06T04:37:57.034Z",
            }}}
        })

    async def post(self, *_args, **_kwargs) -> FakeResponse:
        self.posts += 1
        return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": self.code})


class FailoverHttp(FakeHttp):
    def __init__(self, *, backup_works: bool) -> None:
        super().__init__()
        self.backup_works = backup_works
        self.gets: list[str] = []

    async def get(self, url: str, **_kwargs) -> FakeResponse:
        self.gets.append(url)
        if "primary.invalid" in url or not self.backup_works:
            return FakeResponse({"error": "unavailable"}, 503)
        return await super().get(url)


class BalanceDiscoveryHttp(FakeHttp):
    token = "0x1111111111111111111111111111111111111111"
    amount = "123.456"

    def __init__(self) -> None:
        super().__init__()
        self.index_gets = 0

    async def get(self, *_args, **_kwargs) -> FakeResponse:
        self.index_gets += 1
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


class EvmWalletResolverTests(unittest.IsolatedAsyncioTestCase):
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
                index_url="https://index.invalid",
                rpcs={"base": "https://base.invalid"},
                cache_path=path,
            )
            result = await resolver.resolve(
                SimpleNamespace(handle="BalanceUser"), balances=balances
            )
            self.assertEqual(result, ADDRESS)
            self.assertEqual(http.index_gets, 0)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["balanceuser"]["evmSource"], "balance+rpc")

    async def test_index_uses_backup_after_primary_503(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            http = FailoverHttp(backup_works=True)
            resolver = EvmWalletResolver(
                http,
                index_url=["https://primary.invalid", "https://backup.invalid"],
                rpcs={"base": "https://base.invalid"},
                cache_path=Path(directory) / "wallets.json",
                index_retry_delays=(0.0,),
            )
            result = await resolver.resolve(SimpleNamespace(handle="Konito"))
            self.assertEqual(result, ADDRESS)
            self.assertEqual(len(http.gets), 2)

    async def test_index_circuit_breaker_suppresses_repeat_503_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            http = FailoverHttp(backup_works=False)
            resolver = EvmWalletResolver(
                http,
                index_url="https://primary.invalid",
                rpcs={"base": "https://base.invalid"},
                cache_path=Path(directory) / "wallets.json",
                index_retry_delays=(0.0,),
                index_cooldown=60,
            )
            self.assertIsNone(await resolver.resolve(SimpleNamespace(handle="first")))
            self.assertIsNone(await resolver.resolve(SimpleNamespace(handle="second")))
            self.assertEqual(len(http.gets), 1)

    async def test_verified_deployed_wallet_is_cached_without_losing_solana(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            path.write_text(json.dumps({"konito": {"wallet": "solana-wallet"}}))
            http = FakeHttp()
            resolver = EvmWalletResolver(
                http, rpcs={"base": "https://base.invalid"}, cache_path=path
            )
            result = await resolver.resolve(SimpleNamespace(handle="Konito"))
            self.assertEqual(result, ADDRESS)
            self.assertEqual(cached_evm_wallet("konito", path), ADDRESS)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["konito"]["wallet"], "solana-wallet")
            self.assertEqual(saved["konito"]["evmChains"], ["base"])

    async def test_unverified_index_result_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            http = FakeHttp(status="unverified")
            resolver = EvmWalletResolver(
                http, rpcs={"base": "https://base.invalid"},
                cache_path=Path(directory) / "wallets.json",
            )
            result = await resolver.resolve(SimpleNamespace(handle="unknown"))
            self.assertIsNone(result)
            self.assertEqual(http.posts, 0)

    async def test_verified_but_undeployed_address_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resolver = EvmWalletResolver(
                FakeHttp(code="0x"), rpcs={"base": "https://base.invalid"},
                cache_path=Path(directory) / "wallets.json",
            )
            result = await resolver.resolve(SimpleNamespace(handle="dead"))
            self.assertIsNone(result)

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
