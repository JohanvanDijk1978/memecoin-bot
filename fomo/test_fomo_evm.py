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


class EvmWalletResolverTests(unittest.IsolatedAsyncioTestCase):
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
