from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fomo_wallet
from fomo_wallet import (
    FOMO_SPONSOR,
    SOLANA_NETWORK_ID,
    Rpc,
    RpcInvalidParams,
    SponsorIndex,
    WalletResolver,
    find_tx_via_sponsor,
    find_cached_wallets,
    iso_epoch,
    pick_swaps,
    rpc_display_name,
    solana_balance_positions,
    swap_search_leg,
)


MINT_A = "MintA111111111111111111111111111111111111111"
MINT_B = "MintB111111111111111111111111111111111111111"
WALLET = "Wallet11111111111111111111111111111111111111"
SOL_MINT_A = "E7Kc6aU15bGirh27P6DEgTzuSAQSTtJi7TrKM1wYpump"
SOL_MINT_B = "zj1jpp7QMveWHLs61vL9KMZf254KvW7j4AAmBF8ry2k"
SOL_MINT_C = "CX2v7JSHkVPRZzUGpPTsLpMFHrTSAQSTtJi7TrKMwpum"


def swap(mint: str, amount: float, created: str = "2026-08-18T13:05:59.531Z") -> dict:
    return {
        "createdAt": created,
        "inTokenAddress": "So11111111111111111111111111111111111111112",
        "inHumanAmount": 1.0,
        "outTokenAddress": mint,
        "outHumanAmount": amount,
    }


def transaction(mint: str, amount: float) -> dict:
    return {
        "transaction": {
            "message": {
                "accountKeys": [
                    {"pubkey": FOMO_SPONSOR, "signer": True},
                    {"pubkey": WALLET, "signer": True},
                ]
            }
        },
        "meta": {
            "preTokenBalances": [
                {"mint": mint, "owner": WALLET,
                 "uiTokenAmount": {"uiAmount": 0.0}}
            ],
            "postTokenBalances": [
                {"mint": mint, "owner": WALLET,
                 "uiTokenAmount": {"uiAmount": amount}}
            ],
        },
    }


def sell_transaction(mint: str, amount: float) -> dict:
    tx = transaction(mint, 0)
    tx["meta"]["preTokenBalances"][0]["uiTokenAmount"]["uiAmount"] = amount
    return tx


class FakeRpc:
    def __init__(self, sw: dict, tx: dict) -> None:
        self.when = iso_epoch(sw["createdAt"])
        self.tx = tx
        self.pages = 0

    async def __call__(self, method: str, params: list) -> list:
        self.pages += 1
        if self.pages == 1:
            return [{"signature": "signature-1", "blockTime": self.when, "err": None}]
        return []

    async def batch(self, method: str, param_sets: list[list]) -> list[dict]:
        return [self.tx for _ in param_sets]


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeHeliusHttp:
    def __init__(self, amounts: dict[str, int]) -> None:
        self.amounts = amounts

    async def post(self, _url: str, json: dict) -> FakeResponse:
        mint = json.get("params", {}).get("mint")
        amount = self.amounts.get(mint)
        accounts = [] if amount is None else [{
            "owner": WALLET,
            "mint": mint,
            "amount": str(amount),
        }]
        return FakeResponse({"result": {"token_accounts": accounts}})


class InvalidParamsHttp:
    def __init__(self) -> None:
        self.posts = 0

    async def post(self, _url: str, **_kwargs: object) -> FakeResponse:
        self.posts += 1
        return FakeResponse({
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32602, "message": "Invalid param: Invalid"},
        })


def balance_row(mint: str, raw: int, shifted: float, price: float = 1.0) -> dict:
    return {
        "balance": {
            "tokenAddress": mint,
            "balance": str(raw),
            "shiftedBalance": shifted,
            "tokenId": f"{mint}:1399811149",
        },
        "tokenFilterResult": {
            "priceUSD": str(price),
            "token": {"address": mint, "networkId": 1399811149, "decimals": 6},
        },
    }


class WalletDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_extracts_exact_solana_balance_fingerprints(self) -> None:
        payload = {"balances": [
            balance_row(SOL_MINT_A, 123456789, 123.456789, 2.0),
            balance_row(
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                1000000,
                1.0,
            ),
            {
                "balance": {"tokenAddress": "0x" + "1" * 40, "balance": "50"},
                "tokenFilterResult": {"token": {"networkId": 8453}},
            },
        ]}
        positions = solana_balance_positions(payload)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].mint, SOL_MINT_A)
        self.assertEqual(positions[0].raw_amounts, (123456789,))

    async def test_balance_fallback_resolves_two_exact_tokens_and_preserves_evm(self) -> None:
        balances = {"balances": [
            balance_row(SOL_MINT_A, 123456789, 123.456789, 2.0),
            balance_row(SOL_MINT_B, 987654321, 987.654321, 1.0),
        ]}
        http = FakeHeliusHttp({SOL_MINT_A: 123456789, SOL_MINT_B: 987654321})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            path.write_text(json.dumps({
                "0xforgivable": {
                    "evmWallet": "0x0232b9afb9160fe479f25dade62fa60ef657bdc5",
                    "evmStatus": "verified",
                }
            }), encoding="utf-8")
            resolver = WalletResolver(
                http,
                "https://mainnet.helius-rpc.com/?api-key=test",
                cache_path=path,
            )
            found = await resolver.resolve_from_balances(
                SimpleNamespace(handle="0xforgivable"), balances
            )
            self.assertEqual(found, WALLET)
            cached = json.loads(path.read_text(encoding="utf-8"))["0xforgivable"]
            self.assertEqual(cached["wallet"], WALLET)
            self.assertEqual(
                cached["evmWallet"], "0x0232b9afb9160fe479f25dade62fa60ef657bdc5"
            )
            self.assertEqual(cached["walletSource"], "balance+helius+fomo-sponsor")

    def test_pick_swaps_uses_distinct_mints(self) -> None:
        rows = [swap(MINT_A, 1), swap(MINT_A, 2), swap(MINT_B, 3)]
        self.assertEqual(
            [row["outTokenAddress"] for row in pick_swaps(rows, want=4)],
            [MINT_A, MINT_B, MINT_A],
        )

    def test_pick_swaps_excludes_evm_rows_from_mixed_chain_feed(self) -> None:
        evm = {
            "createdAt": "2026-08-19T10:00:00Z",
            "networkId": 1,
            "inTokenAddress": "0xa0b86991c6218b6c1d19d4a2e9eb0ce3606eb48",
            "inHumanAmount": 100,
            "outTokenAddress": "0xe172e9b6cfbeeb5593bdce3f077356fdb33af904",
            "outHumanAmount": 1000,
        }
        solana = swap(MINT_A, 42.5)
        solana["networkId"] = SOLANA_NETWORK_ID

        self.assertEqual(pick_swaps([evm, solana]), [solana])

    async def test_invalid_rpc_params_do_not_fail_over_or_start_cooldown(self) -> None:
        http = InvalidParamsHttp()
        rpc = Rpc(http, ["https://primary.invalid", "https://backup.invalid"])

        with self.assertRaises(RpcInvalidParams):
            await rpc("getSignaturesForAddress", ["0xnot-solana"])

        self.assertEqual(http.posts, 1)
        self.assertEqual(rpc._cooldown_until, 0.0)

    async def test_sponsor_fallback_requires_exact_wallet_delta(self) -> None:
        row = swap(MINT_A, 42.5)
        rpc = FakeRpc(row, transaction(MINT_A, 42.5))
        signature, tx = await find_tx_via_sponsor(
            rpc, SponsorIndex(rpc, [FOMO_SPONSOR]), MINT_A, 42.5,
            iso_epoch(row["createdAt"]), verbose=False, swap=row,
        )
        self.assertEqual(signature, "signature-1")
        self.assertIsNotNone(tx)

    async def test_sponsor_fallback_rejects_wrong_amount(self) -> None:
        row = swap(MINT_A, 42.5)
        rpc = FakeRpc(row, transaction(MINT_A, 41.0))
        signature, tx = await find_tx_via_sponsor(
            rpc, SponsorIndex(rpc, [FOMO_SPONSOR]), MINT_A, 42.5,
            iso_epoch(row["createdAt"]), verbose=False, swap=row,
        )
        self.assertIsNone(signature)
        self.assertIsNone(tx)

    async def test_sponsor_fallback_matches_sell_token_leg(self) -> None:
        row = {
            "createdAt": "2026-08-18T13:05:59.531Z",
            "inTokenAddress": MINT_A,
            "inHumanAmount": 42.5,
            "outTokenAddress": "So11111111111111111111111111111111111111112",
            "outHumanAmount": 1.0,
        }
        mint, amount, direction = swap_search_leg(row)
        self.assertEqual((mint, amount, direction), (MINT_A, 42.5, -1))
        rpc = FakeRpc(row, sell_transaction(MINT_A, 42.5))
        signature, tx = await find_tx_via_sponsor(
            rpc,
            SponsorIndex(rpc, [FOMO_SPONSOR]),
            mint or "",
            amount,
            iso_epoch(row["createdAt"]),
            verbose=False,
            swap=row,
            direction=direction,
        )
        self.assertEqual(signature, "signature-1")
        self.assertIsNotNone(tx)

    def test_rpc_display_hides_query_credentials(self) -> None:
        shown = rpc_display_name("https://example.invalid/rpc?api-key=secret")
        self.assertEqual(shown, "https://example.invalid")
        self.assertNotIn("secret", shown)

    def test_reverse_wallet_lookup_supports_solana_and_evm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            path.write_text(json.dumps({
                "rowdy": {
                    "wallet": "CzU8MaRcwvwUoNkwJFLbvtFWJugcEXAhDDQqNFE4ybb7",
                    "confirmed": 5,
                    "evmWallet": "0x03ba951f72e59899ac8dab30cb5624dbe5d52bb8",
                    "evmStatus": "verified",
                    "evmSource": "legacy+rpc",
                    "evmChains": ["base", "bsc"],
                }
            }), encoding="utf-8")

            solana = find_cached_wallets(
                "CzU8MaRcwvwUoNkwJFLbvtFWJugcEXAhDDQqNFE4ybb7", path
            )
            self.assertEqual([(match.handle, match.network) for match in solana],
                             [("rowdy", "Solana")])
            self.assertEqual(solana[0].confirmations, 5)

            evm = find_cached_wallets(
                "0x03BA951F72E59899AC8DAB30CB5624DBE5D52BB8", path
            )
            self.assertEqual([(match.handle, match.network) for match in evm],
                             [("rowdy", "EVM")])
            self.assertEqual(evm[0].chains, ("base", "bsc"))

            # Unlike EVM, Solana's base58 addresses are case-sensitive.
            self.assertEqual(find_cached_wallets(
                "czU8MaRcwvwUoNkwJFLbvtFWJugcEXAhDDQqNFE4ybb7", path
            ), [])


class BoundedBlockRouteTests(unittest.IsolatedAsyncioTestCase):
    """The block route runs on the embed path, bounded to the newest swaps.

    Both cheap routes stop at MAX_SIG_PAGES * 1000 signatures. That cap covers
    less wall-clock time every time FOMO's throughput grows, so an active
    trader's day-old swap sits behind it and only the block route still
    reaches. The bot used to run with the block route off entirely, which is
    why such handles resolved an EVM wallet and no Solana one.
    """

    def setUp(self) -> None:
        self.rows = {"swaps": [
            swap(SOL_MINT_A, 1, "2026-08-19T03:08:09.000Z"),
            swap(SOL_MINT_B, 2, "2026-08-18T12:30:13.000Z"),
            swap(SOL_MINT_C, 3, "2026-08-15T18:12:12.000Z"),
        ]}
        self.fomo = SimpleNamespace(_get=lambda *_a, **_k: _async(self.rows))
        self.user = SimpleNamespace(id="uid", handle="397397")

    def _resolver(self, path: Path, **kwargs: object) -> WalletResolver:
        return WalletResolver(
            SimpleNamespace(), "https://rpc.test", verify_targets=0,
            cache_path=path, **kwargs,  # type: ignore[arg-type]
        )

    @staticmethod
    def _recorder(calls: list[tuple[str, bool]], hit_on: str | None = None):
        async def fake_locate(_rpc, sw, _index=None, deep=False, verbose=True):
            mint = sw["outTokenAddress"]
            calls.append((mint, deep))
            if hit_on is not None and mint == hit_on and deep:
                return "sig-1", transaction(mint, sw["outHumanAmount"]), "blocks"
            return None, None, "not found"
        return fake_locate

    async def test_blocks_are_tried_on_the_newest_swap_before_older_ones(self) -> None:
        """Every route per swap, not every cheap route across every swap.

        Draining four mint scans first is the slow way to answer a handle whose
        history is already known to be behind the signature cap.
        """
        calls: list[tuple[str, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            with mock.patch.object(fomo_wallet, "locate_swap",
                                   self._recorder(calls, hit_on=SOL_MINT_A)):
                found = await self._resolver(path, deep=True).resolve(
                    self.fomo, self.user
                )
            self.assertEqual(found, WALLET)
            # One swap, one call -- the newest swap never waited on the others.
            self.assertEqual(calls, [(SOL_MINT_A, True)])
            entry = json.loads(path.read_text(encoding="utf-8"))["397397"]
            self.assertEqual(entry["walletSource"], "fomo-blocks")

    async def test_block_route_is_bounded_to_the_newest_swaps(self) -> None:
        calls: list[tuple[str, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            with mock.patch.object(fomo_wallet, "locate_swap",
                                   self._recorder(calls)):
                resolver = self._resolver(path, deep=True, deep_attempts=2)
                self.assertIsNone(await resolver.resolve(self.fomo, self.user))
            # Blocks on the two newest; the third still gets the cheap routes,
            # because a quiet mint is cheap and might still hit.
            self.assertEqual(calls, [(SOL_MINT_A, True), (SOL_MINT_B, True),
                                     (SOL_MINT_C, False)])

    async def test_disabled_deep_never_asks_for_blocks(self) -> None:
        calls: list[tuple[str, bool]] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            with mock.patch.object(fomo_wallet, "locate_swap",
                                   self._recorder(calls)), \
                    self.assertLogs("fomo.wallet", "INFO") as logs:
                resolver = self._resolver(path, deep=False)
                self.assertIsNone(await resolver.resolve(self.fomo, self.user))
            self.assertEqual([deep for _mint, deep in calls], [False, False, False])
            self.assertIn("block route off", "\n".join(logs.output))

    async def test_cheap_route_hit_never_pays_for_blocks(self) -> None:
        seen: list[str] = []

        async def fake_locate(_rpc, sw, _index=None, deep=False, verbose=True):
            seen.append(sw["outTokenAddress"])
            return "sig-1", transaction(sw["outTokenAddress"],
                                        sw["outHumanAmount"]), "sponsor"

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            with mock.patch.object(fomo_wallet, "locate_swap", fake_locate):
                found = await self._resolver(path, deep=True).resolve(
                    self.fomo, self.user
                )
            self.assertEqual(found, WALLET)
            self.assertEqual(seen, [SOL_MINT_A])
            entry = json.loads(path.read_text(encoding="utf-8"))["397397"]
            self.assertEqual(entry["walletSource"], "fomo-sponsor")

    def test_deep_defaults_to_the_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            self.assertEqual(self._resolver(path).deep, fomo_wallet.DEEP_DEFAULT)
            self.assertIs(self._resolver(path, deep=False).deep, False)
            self.assertIs(self._resolver(path, deep=True).deep, True)


class AdoptHolderMatchesTests(unittest.IsolatedAsyncioTestCase):
    """FOMO's holder list is an identity source, if it survives corroboration.

    `/hodlers/top` states a trader's exact position and `/token` already knows
    every on-chain owner, so an unambiguous amount match is a wallet for free.
    A cached wallet is permanent and feeds `/fomo` and `/wallet`, so the same
    sponsor-signature bar `_resolve_from_balances` applies to a single
    fingerprint applies here.
    """

    def _resolver(self, path: Path, sponsored: bool = True) -> WalletResolver:
        resolver = WalletResolver(SimpleNamespace(), "https://rpc.test",
                                  cache_path=path)
        self.checked: list[str] = []

        async def sponsor_check(wallet: str) -> bool:
            self.checked.append(wallet)
            return sponsored

        resolver._has_fomo_sponsored_transaction = sponsor_check  # type: ignore[method-assign]
        return resolver

    async def test_a_corroborated_match_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            written = await self._resolver(path).adopt_holder_matches(
                {WALLET: "ChunDoohwann"}, token=SOL_MINT_A,
            )
            self.assertEqual(written, {WALLET: "chundoohwann"})
            entry = json.loads(path.read_text(encoding="utf-8"))["chundoohwann"]
            self.assertEqual(entry["wallet"], WALLET)
            self.assertEqual(entry["walletSource"], "hodlers+amount+fomo-sponsor")
            self.assertEqual(entry["hodlerToken"], SOL_MINT_A)
            self.assertEqual(self.checked, [WALLET])

    async def test_a_wallet_with_no_fomo_transaction_is_refused(self) -> None:
        """A whale holding the matching amount is not therefore a FOMO trader."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            written = await self._resolver(path, sponsored=False)\
                .adopt_holder_matches({WALLET: "stranger"})
            self.assertEqual(written, {})
            self.assertFalse(path.exists())

    async def test_an_existing_mapping_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text(json.dumps({"rowdy": {
                "wallet": "OriginalWallet111111111111111111111111111111",
                "confirmed": 5, "walletSource": "fomo-sponsor",
            }}), encoding="utf-8")
            resolver = self._resolver(path)
            self.assertEqual(await resolver.adopt_holder_matches({WALLET: "rowdy"}), {})
            entry = json.loads(path.read_text(encoding="utf-8"))["rowdy"]
            self.assertEqual(entry["wallet"],
                             "OriginalWallet111111111111111111111111111111")
            self.assertEqual(self.checked, [])  # refused before spending RPC

    async def test_a_wallet_already_claimed_by_another_handle_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text(json.dumps({"konito": {
                "wallet": WALLET, "confirmed": 5, "walletSource": "fomo-sponsor",
            }}), encoding="utf-8")
            resolver = self._resolver(path)
            self.assertEqual(
                await resolver.adopt_holder_matches({WALLET: "impostor"}), {}
            )
            self.assertNotIn("impostor",
                             json.loads(path.read_text(encoding="utf-8")))

    async def test_re_adopting_the_same_pair_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            resolver = self._resolver(path)
            await resolver.adopt_holder_matches({WALLET: "chundoohwann"})
            self.checked.clear()
            self.assertEqual(
                await resolver.adopt_holder_matches({WALLET: "ChunDoohwann"}), {}
            )
            self.assertEqual(self.checked, [])  # no repeat RPC cost

    async def test_an_existing_evm_record_survives_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text(json.dumps({"luver": {
                "evmWallet": "0x0232b9afb9160fe479f25dade62fa60ef657bdc5",
                "evmStatus": "verified",
            }}), encoding="utf-8")
            await self._resolver(path).adopt_holder_matches({WALLET: "luver"})
            entry = json.loads(path.read_text(encoding="utf-8"))["luver"]
            self.assertEqual(entry["wallet"], WALLET)
            self.assertEqual(entry["evmWallet"],
                             "0x0232b9afb9160fe479f25dade62fa60ef657bdc5")

    async def test_a_failing_sponsor_check_does_not_stop_the_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            resolver = WalletResolver(SimpleNamespace(), "https://rpc.test",
                                      cache_path=path)

            async def flaky(wallet: str) -> bool:
                if wallet == "BadWallet1111111111111111111111111111111111":
                    raise RuntimeError("all Solana RPCs failed")
                return True

            resolver._has_fomo_sponsored_transaction = flaky  # type: ignore[method-assign]
            written = await resolver.adopt_holder_matches({
                "BadWallet1111111111111111111111111111111111": "broken",
                WALLET: "worked",
            })
            self.assertEqual(written, {WALLET: "worked"})


def _async(value):
    async def run():
        return value
    return run()


if __name__ == "__main__":
    unittest.main()
