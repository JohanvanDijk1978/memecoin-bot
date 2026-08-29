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
    WalletCandidate,
    WalletResolver,
    choose_unverified_wallets,
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


def transaction(mint: str, amount: float, owner: str = WALLET) -> dict:
    return {
        "transaction": {
            "message": {
                "accountKeys": [
                    {"pubkey": FOMO_SPONSOR, "signer": True},
                    {"pubkey": owner, "signer": True},
                ]
            }
        },
        "meta": {
            "preTokenBalances": [
                {"mint": mint, "owner": owner,
                 "uiTokenAmount": {"uiAmount": 0.0}}
            ],
            "postTokenBalances": [
                {"mint": mint, "owner": owner,
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
            # Two independent tokens agreeing needs no third-party gate, and
            # this path never ran one -- the old label said otherwise.
            self.assertEqual(cached["walletSource"], "balance+helius+2mints")

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


# --------------------------------------------------- the corroboration gate
#
# Every derived route -- exact balances, published holder positions, `/token`
# adoption -- ends at the same question: is this candidate really this
# trader's wallet? `_corroborate` answers it with `verify_wallet` when the
# caller has the trader's swaps and with the older sponsor peek when it does
# not, and the difference between "refuted" and "inconclusive" is what decides
# whether the weaker check gets a turn.

HOLDER_WALLET = "Hodler11111111111111111111111111111111111111"


class HolderHttp:
    """Helius DAS, getTokenSupply, and the two calls the gates make.

    Counts everything, because most of what the holder route is *for* is not
    paying for on-chain queries it does not need.
    """

    def __init__(
        self,
        owners: dict[str, dict[str, int]],
        decimals: int = 6,
        signatures: list[dict] | None = None,
        transactions: dict[str, dict] | None = None,
    ) -> None:
        self.owners = owners
        self.decimals = decimals
        self.signatures = signatures if signatures is not None else []
        self.transactions = transactions or {}
        self.das_mints: list[str] = []
        self.methods: list[str] = []

    async def post(self, _url: str, json: dict) -> FakeResponse:
        if isinstance(json, list):
            results = []
            for call in json:
                signature = call["params"][0]
                results.append({"id": call["id"],
                                "result": self.transactions.get(signature)})
            return FakeResponse(results)
        method = json.get("method")
        self.methods.append(method)
        if method == "getTokenAccounts":
            mint = json.get("params", {}).get("mint")
            self.das_mints.append(mint)
            return FakeResponse({"result": {"token_accounts": [
                {"owner": owner, "mint": mint, "amount": str(amount)}
                for owner, amount in self.owners.get(mint, {}).items()
            ]}})
        if method == "getTokenSupply":
            return FakeResponse({"result": {"value": {"decimals": self.decimals}}})
        if method == "getSignaturesForAddress":
            return FakeResponse({"result": self.signatures})
        if method == "getTransaction":
            signature = json["params"][0]
            return FakeResponse({"result": self.transactions.get(signature)})
        return FakeResponse({"result": None})


class FakeFomoHolders:
    """`/hodlers/top`, batched and per token."""

    def __init__(self, groups: dict[str, list[dict]], batch_limit: int | None = None,
                 batch_error: Exception | None = None) -> None:
        self.groups = groups
        self.batch_limit = batch_limit
        self.batch_error = batch_error
        self.batched: list[list[str]] = []
        self.singles: list[str] = []

    def _entry(self, mint: str) -> dict:
        return {"tokenAddress": mint, "networkId": SOLANA_NETWORK_ID,
                "totalHolders": 900, "topHolders": self.groups.get(mint, [])}

    async def token_holders_many(self, addresses: list, _network: int) -> list:
        self.batched.append(list(addresses))
        if self.batch_error:
            raise self.batch_error
        served = addresses if self.batch_limit is None else addresses[:self.batch_limit]
        return [self._entry(mint) for mint in served]

    async def token_holders(self, address: str, _network: int, **_kw: object) -> list:
        self.singles.append(address)
        return [self._entry(address)]


def holder_row(handle: str, human_amount: float) -> dict:
    return {
        "user": {"id": f"id-{handle}", "userHandle": handle,
                 "displayName": handle, "address": "synthetic"},
        "humanAmount": human_amount,
        "isDev": False,
    }


def sponsored_tx(wallet: str) -> dict:
    return {"transaction": {"message": {"accountKeys": [
        {"pubkey": FOMO_SPONSOR, "signer": True},
        {"pubkey": wallet, "signer": True},
    ]}}}


def resolver_for(http: object, path: Path, **kwargs: object) -> WalletResolver:
    return WalletResolver(
        http, "https://mainnet.helius-rpc.com/?api-key=test",
        cache_path=path, **kwargs,
    )


class CorroborationGateTests(unittest.IsolatedAsyncioTestCase):
    def _balances(self) -> dict:
        return {"balances": [balance_row(SOL_MINT_A, 123456789, 123.456789, 2.0)]}

    async def test_a_candidate_the_traders_own_swaps_confirm_is_accepted(self) -> None:
        when = iso_epoch("2026-08-18T13:05:59.531Z")
        http = HolderHttp(
            {SOL_MINT_A: {WALLET: 123456789}},
            signatures=[{"signature": "sig-1", "blockTime": when, "err": None}],
            transactions={"sig-1": transaction(SOL_MINT_A, 5.0)},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            found = await resolver_for(http, path).resolve_from_balances(
                SimpleNamespace(handle="scrill"), self._balances(),
                swaps=[swap(SOL_MINT_A, 5.0)],
            )
            self.assertEqual(found, WALLET)
            entry = json.loads(path.read_text(encoding="utf-8"))["scrill"]
        # The cache records which gate let the wallet in.
        self.assertEqual(entry["walletSource"], "balance+helius+verify1")
        self.assertEqual(entry["confirmed"], 1)
        self.assertNotIn("getTransaction", http.methods[:1])

    async def test_a_refuted_candidate_never_reaches_the_weaker_gate(self) -> None:
        # verify_wallet looked at this wallet's own history and this trader's
        # swap is not in it. Falling through to "has it ever touched FOMO"
        # after that would throw away the better answer.
        when = iso_epoch("2026-08-18T13:05:59.531Z")
        http = HolderHttp(
            {SOL_MINT_A: {WALLET: 123456789}},
            signatures=[{"signature": "sig-1", "blockTime": when, "err": None}],
            transactions={"sig-1": transaction(SOL_MINT_A, 999.0)},  # wrong amount
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            resolver = resolver_for(http, path)
            with mock.patch.object(
                resolver, "_has_fomo_sponsored_transaction",
                side_effect=AssertionError("the weak gate must not run"),
            ):
                found = await resolver.resolve_from_balances(
                    SimpleNamespace(handle="scrill"), self._balances(),
                    swaps=[swap(SOL_MINT_A, 5.0)],
                )
        self.assertIsNone(found)
        self.assertFalse(path.exists())

    async def test_an_inconclusive_verify_still_lets_the_sponsor_gate_answer(self) -> None:
        # No signature anywhere near the swap: verify never got to look, which
        # is not the same as looking and finding nothing.
        http = HolderHttp({SOL_MINT_A: {WALLET: 123456789}}, signatures=[])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            resolver = resolver_for(http, path)
            with mock.patch.object(
                resolver, "_has_fomo_sponsored_transaction", return_value=True,
            ) as gate:
                found = await resolver.resolve_from_balances(
                    SimpleNamespace(handle="scrill"), self._balances(),
                    swaps=[swap(SOL_MINT_A, 5.0)],
                )
            self.assertEqual(found, WALLET)
            entry = json.loads(path.read_text(encoding="utf-8"))["scrill"]
        gate.assert_awaited_once()
        self.assertEqual(entry["walletSource"], "balance+helius+fomo-sponsor")

    async def test_a_caller_with_no_swaps_keeps_the_old_gate(self) -> None:
        http = HolderHttp({SOL_MINT_A: {WALLET: 123456789}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            resolver = resolver_for(http, path)
            with mock.patch.object(
                resolver, "_has_fomo_sponsored_transaction", return_value=True,
            ):
                found = await resolver.resolve_from_balances(
                    SimpleNamespace(handle="scrill"), self._balances(),
                )
            self.assertEqual(found, WALLET)
            entry = json.loads(path.read_text(encoding="utf-8"))["scrill"]
        self.assertEqual(entry["walletSource"], "balance+helius+fomo-sponsor")

    async def test_adoption_records_the_gate_that_passed(self) -> None:
        http = HolderHttp({})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            resolver = resolver_for(http, path)
            with mock.patch.object(
                resolver, "_has_fomo_sponsored_transaction", return_value=True,
            ):
                written = await resolver.adopt_holder_matches(
                    {WALLET: "quanterty"}, token=SOL_MINT_A
                )
            self.assertEqual(written, {WALLET: "quanterty"})
            entry = json.loads(path.read_text(encoding="utf-8"))["quanterty"]
        # `/token` has no swap rows for the handles it names, so the weaker
        # gate is the honest label there.
        self.assertEqual(entry["walletSource"], "hodlers+amount+fomo-sponsor")

    def test_swap_rows_unwraps_either_shape(self) -> None:
        row = {"createdAt": "2026-08-18T13:05:59.531Z"}
        self.assertEqual(fomo_wallet.swap_rows({"swaps": [row]}), [row])
        self.assertEqual(fomo_wallet.swap_rows([row]), [row])
        self.assertEqual(fomo_wallet.swap_rows(None), [])
        self.assertEqual(fomo_wallet.swap_rows({}), [])
        # The envelope iterated by mistake yields its keys, which would look
        # like usable rows to anything that does not type-check them.
        self.assertEqual(fomo_wallet.swap_rows({"swaps": ["nonsense", row]}), [row])


class HolderRouteTests(unittest.IsolatedAsyncioTestCase):
    """`/hodlers/top`, asked about one trader's own positions.

    The cheapest identity source in the project, and until now it only ran as
    a side effect of somebody typing `/token`.
    """

    def _balances(self, *mints: str) -> dict:
        # Descending USD value, so the order the route sees is deterministic.
        return {"balances": [
            balance_row(mint, 123456789 + index, 123.456789, 3.0 - index)
            for index, mint in enumerate(mints)
        ]}

    async def test_one_batched_request_covers_every_position(self) -> None:
        fomo = FakeFomoHolders({SOL_MINT_A: [], SOL_MINT_B: [], SOL_MINT_C: []})
        http = HolderHttp({})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            found = await resolver_for(http, path).resolve_from_holders(
                fomo, SimpleNamespace(handle="scrill"),
                self._balances(SOL_MINT_A, SOL_MINT_B, SOL_MINT_C),
            )
        self.assertIsNone(found)
        self.assertEqual(fomo.batched, [[SOL_MINT_A, SOL_MINT_B, SOL_MINT_C]])
        self.assertEqual(fomo.singles, [])
        # A token that does not name the trader can never name their wallet,
        # and finding that out costs no on-chain call at all.
        self.assertEqual(http.das_mints, [])

    async def test_only_tokens_naming_the_trader_cost_an_onchain_query(self) -> None:
        fomo = FakeFomoHolders({
            SOL_MINT_A: [holder_row("whale", 5000.0)],
            SOL_MINT_B: [holder_row("scrill", 123.456789),
                         holder_row("whale", 5000.0)],
        })
        http = HolderHttp({SOL_MINT_B: {HOLDER_WALLET: 123456789,
                                        "OtherAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA": 5_000_000_000}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            resolver = resolver_for(http, path)
            with mock.patch.object(
                resolver, "_has_fomo_sponsored_transaction", return_value=True,
            ):
                found = await resolver.resolve_from_holders(
                    fomo, SimpleNamespace(handle="scrill"),
                    self._balances(SOL_MINT_A, SOL_MINT_B),
                )
            self.assertEqual(found, HOLDER_WALLET)
            entry = json.loads(path.read_text(encoding="utf-8"))["scrill"]
        self.assertEqual(http.das_mints, [SOL_MINT_B])
        self.assertEqual(entry["walletSource"], "hodlers+amount+fomo-sponsor")
        self.assertEqual(entry["hodlerToken"], SOL_MINT_B)

    async def test_two_tokens_agreeing_need_no_third_party_gate(self) -> None:
        fomo = FakeFomoHolders({
            SOL_MINT_A: [holder_row("scrill", 123.456789)],
            SOL_MINT_B: [holder_row("scrill", 123.456790)],
        })
        http = HolderHttp({
            SOL_MINT_A: {HOLDER_WALLET: 123456789},
            SOL_MINT_B: {HOLDER_WALLET: 123456790},
        })
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            resolver = resolver_for(http, path)
            with mock.patch.object(
                resolver, "_has_fomo_sponsored_transaction",
                side_effect=AssertionError("two tokens is already the evidence"),
            ):
                found = await resolver.resolve_from_holders(
                    fomo, SimpleNamespace(handle="scrill"),
                    self._balances(SOL_MINT_A, SOL_MINT_B),
                )
            self.assertEqual(found, HOLDER_WALLET)
            entry = json.loads(path.read_text(encoding="utf-8"))["scrill"]
        self.assertEqual(entry["walletSource"], "hodlers+amount+2tokens")
        self.assertEqual(entry["confirmed"], 2)

    async def test_a_single_position_is_verified_against_the_traders_swaps(self) -> None:
        when = iso_epoch("2026-08-18T13:05:59.531Z")
        fomo = FakeFomoHolders({SOL_MINT_A: [holder_row("scrill", 123.456789)]})
        http = HolderHttp(
            {SOL_MINT_A: {HOLDER_WALLET: 123456789}},
            signatures=[{"signature": "sig-1", "blockTime": when, "err": None}],
            transactions={"sig-1": transaction(SOL_MINT_A, 5.0, HOLDER_WALLET)},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            found = await resolver_for(http, path).resolve_from_holders(
                fomo, SimpleNamespace(handle="scrill"),
                self._balances(SOL_MINT_A),
                # The envelope shape the bot actually holds, not a bare list.
                swaps={"swaps": [swap(SOL_MINT_A, 5.0)]},
            )
            entry = json.loads(path.read_text(encoding="utf-8"))["scrill"]
        self.assertEqual(found, HOLDER_WALLET)
        # A holder hit is transaction-backed too, which is what makes running
        # this route ahead of the expensive ones cost no evidence.
        self.assertEqual(entry["walletSource"], "hodlers+amount+verify1")

    async def test_a_near_neighbour_balance_is_refused_not_guessed(self) -> None:
        fomo = FakeFomoHolders({SOL_MINT_A: [holder_row("scrill", 123.456789)]})
        http = HolderHttp({SOL_MINT_A: {
            HOLDER_WALLET: 123456789,
            "Neighbour111111111111111111111111111111111": 123456790,
        }})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            found = await resolver_for(http, path).resolve_from_holders(
                fomo, SimpleNamespace(handle="scrill"), self._balances(SOL_MINT_A),
            )
        self.assertIsNone(found)

    async def test_a_wallet_another_trader_also_matches_is_refused(self) -> None:
        # Uniqueness runs in both directions: the amount must identify one
        # wallet AND that wallet must match one trader.
        fomo = FakeFomoHolders({SOL_MINT_A: [holder_row("scrill", 123.456789),
                                             holder_row("twin", 123.456789)]})
        http = HolderHttp({SOL_MINT_A: {HOLDER_WALLET: 123456789}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            found = await resolver_for(http, path).resolve_from_holders(
                fomo, SimpleNamespace(handle="scrill"), self._balances(SOL_MINT_A),
            )
        self.assertIsNone(found)

    async def test_tokens_the_batch_skips_are_asked_for_individually(self) -> None:
        # The `tokens` array is documented as a list but has only ever been
        # seen with one entry. A server-side cap must cost requests, not the
        # whole route.
        fomo = FakeFomoHolders(
            {SOL_MINT_A: [], SOL_MINT_B: [holder_row("scrill", 123.456790)]},
            batch_limit=1,
        )
        http = HolderHttp({SOL_MINT_B: {HOLDER_WALLET: 123456790}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            resolver = resolver_for(http, path)
            with mock.patch.object(
                resolver, "_has_fomo_sponsored_transaction", return_value=True,
            ):
                found = await resolver.resolve_from_holders(
                    fomo, SimpleNamespace(handle="scrill"),
                    self._balances(SOL_MINT_A, SOL_MINT_B),
                )
        self.assertEqual(found, HOLDER_WALLET)
        self.assertEqual(fomo.singles, [SOL_MINT_B])

    async def test_a_failed_batch_falls_back_to_one_call_per_token(self) -> None:
        fomo = FakeFomoHolders(
            {SOL_MINT_A: [], SOL_MINT_B: []},
            batch_error=RuntimeError("400 from /hodlers/top"),
        )
        http = HolderHttp({})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            found = await resolver_for(http, path).resolve_from_holders(
                fomo, SimpleNamespace(handle="scrill"),
                self._balances(SOL_MINT_A, SOL_MINT_B),
            )
        self.assertIsNone(found)
        self.assertEqual(fomo.singles, [SOL_MINT_A, SOL_MINT_B])

    async def test_a_trader_with_no_positions_costs_nothing(self) -> None:
        fomo = FakeFomoHolders({})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            found = await resolver_for(HolderHttp({}), path).resolve_from_holders(
                fomo, SimpleNamespace(handle="scrill"), {"balances": []},
            )
        self.assertIsNone(found)
        self.assertEqual(fomo.batched, [])

    async def test_a_cached_wallet_short_circuits_the_route(self) -> None:
        fomo = FakeFomoHolders({SOL_MINT_A: [holder_row("scrill", 123.456789)]})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            path.write_text(json.dumps({"scrill": {"wallet": WALLET}}),
                            encoding="utf-8")
            found = await resolver_for(HolderHttp({}), path).resolve_from_holders(
                fomo, SimpleNamespace(handle="scrill"), self._balances(SOL_MINT_A),
            )
        self.assertEqual(found, WALLET)
        self.assertEqual(fomo.batched, [])

    async def test_the_route_never_raises(self) -> None:
        class Broken:
            async def token_holders_many(self, *_a: object, **_k: object) -> list:
                raise RuntimeError("503 upstream")

            async def token_holders(self, *_a: object, **_k: object) -> list:
                raise RuntimeError("503 upstream")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            found = await resolver_for(HolderHttp({}), path).resolve_from_holders(
                Broken(), SimpleNamespace(handle="scrill"),
                self._balances(SOL_MINT_A),
            )
        self.assertIsNone(found)


SECOND_WALLET = "Second11111111111111111111111111111111111111"


class UnverifiedCandidateChoiceTests(unittest.TestCase):
    """`choose_unverified_wallets` -- merge first, rank after."""

    def test_nothing_in_nothing_out(self) -> None:
        self.assertEqual(choose_unverified_wallets([]), [])

    def test_a_lone_candidate_is_the_fallback(self) -> None:
        candidate = WalletCandidate(HOLDER_WALLET, ("hodlers+amount",), (SOL_MINT_A,))
        self.assertEqual(choose_unverified_wallets([candidate]), [candidate])

    def test_the_same_address_from_two_routes_merges_into_one(self) -> None:
        chosen = choose_unverified_wallets([
            WalletCandidate(HOLDER_WALLET, ("hodlers+amount",), (SOL_MINT_A,)),
            WalletCandidate(HOLDER_WALLET, ("balance+helius",), (SOL_MINT_B,)),
        ])
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].address, HOLDER_WALLET)
        self.assertEqual(chosen[0].sources, ("hodlers+amount", "balance+helius"))
        self.assertEqual(chosen[0].evidence, (SOL_MINT_A, SOL_MINT_B))

    def test_the_address_two_routes_agree_on_beats_a_lone_one(self) -> None:
        chosen = choose_unverified_wallets([
            WalletCandidate(HOLDER_WALLET, ("hodlers+amount",), (SOL_MINT_A,)),
            WalletCandidate(HOLDER_WALLET, ("balance+helius",), (SOL_MINT_A,)),
            WalletCandidate(SECOND_WALLET, ("hodlers+amount",), (SOL_MINT_B,)),
        ])
        self.assertEqual([item.address for item in chosen], [HOLDER_WALLET])

    def test_a_tie_is_never_broken_by_guessing(self) -> None:
        chosen = choose_unverified_wallets([
            WalletCandidate(HOLDER_WALLET, ("hodlers+amount",), (SOL_MINT_A,)),
            WalletCandidate(SECOND_WALLET, ("hodlers+amount",), (SOL_MINT_B,)),
        ])
        self.assertEqual(
            sorted(item.address for item in chosen),
            sorted([HOLDER_WALLET, SECOND_WALLET]),
        )


class UnverifiedFallbackTests(unittest.IsolatedAsyncioTestCase):
    """The owners the routes pin down but cannot corroborate.

    They are never returned as a wallet and never cached -- that is the whole
    point of the corroboration gate. They are only carried out to the caller,
    which is the difference between `/fomo` naming a likely wallet under a
    warning and `/fomo` showing nothing at all.
    """

    def _balances(self, *mints: str) -> dict:
        return {"balances": [
            balance_row(mint, 123456789 + index, 123.456789, 3.0 - index)
            for index, mint in enumerate(mints)
        ]}

    async def test_a_verified_holder_hit_offers_no_candidate(self) -> None:
        when = iso_epoch("2026-08-18T13:05:59.531Z")
        fomo = FakeFomoHolders({SOL_MINT_A: [holder_row("scrill", 123.456789)]})
        http = HolderHttp(
            {SOL_MINT_A: {HOLDER_WALLET: 123456789}},
            signatures=[{"signature": "sig-1", "blockTime": when, "err": None}],
            transactions={"sig-1": transaction(SOL_MINT_A, 5.0, HOLDER_WALLET)},
        )
        candidates: list[WalletCandidate] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            found = await resolver_for(http, path).resolve_from_holders(
                fomo, SimpleNamespace(handle="scrill"), self._balances(SOL_MINT_A),
                swaps={"swaps": [swap(SOL_MINT_A, 5.0)]}, candidates=candidates,
            )
        self.assertEqual(found, HOLDER_WALLET)
        self.assertEqual(candidates, [])

    async def test_one_unambiguous_owner_survives_a_failed_gate(self) -> None:
        # The pudgypenguins shape: FOMO names the trader in a published holder
        # list, exactly one on-chain wallet holds that amount, and no gate can
        # confirm it. The route still refuses to call it a wallet.
        fomo = FakeFomoHolders({SOL_MINT_A: [holder_row("pudgypenguins", 123.456789)]})
        http = HolderHttp({SOL_MINT_A: {HOLDER_WALLET: 123456789}})
        candidates: list[WalletCandidate] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            resolver = resolver_for(http, path)
            with mock.patch.object(
                resolver, "_has_fomo_sponsored_transaction", return_value=False,
            ):
                found = await resolver.resolve_from_holders(
                    fomo, SimpleNamespace(handle="pudgypenguins"),
                    self._balances(SOL_MINT_A), candidates=candidates,
                )
            self.assertIsNone(found)
            # Nothing uncorroborated is ever written to the wallet cache.
            self.assertFalse(path.exists())
        self.assertEqual([item.address for item in candidates], [HOLDER_WALLET])
        self.assertEqual(candidates[0].sources, ("hodlers+amount",))
        self.assertEqual(candidates[0].evidence, (SOL_MINT_A,))

    async def test_a_refuted_owner_is_not_offered(self) -> None:
        # verify_wallet looked at this wallet's own history and this trader's
        # swaps are not in it. That is an answer, not a gap, so re-offering it
        # as "likely" would be worse than saying nothing.
        when = iso_epoch("2026-08-18T13:05:59.531Z")
        fomo = FakeFomoHolders({SOL_MINT_A: [holder_row("scrill", 123.456789)]})
        http = HolderHttp(
            {SOL_MINT_A: {HOLDER_WALLET: 123456789}},
            signatures=[{"signature": "sig-1", "blockTime": when, "err": None}],
            transactions={"sig-1": transaction(SOL_MINT_A, 999.0, HOLDER_WALLET)},
        )
        candidates: list[WalletCandidate] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            found = await resolver_for(http, path).resolve_from_holders(
                fomo, SimpleNamespace(handle="scrill"), self._balances(SOL_MINT_A),
                swaps={"swaps": [swap(SOL_MINT_A, 5.0)]}, candidates=candidates,
            )
        self.assertIsNone(found)
        self.assertEqual(candidates, [])

    async def test_two_tokens_naming_two_owners_offer_both(self) -> None:
        fomo = FakeFomoHolders({
            SOL_MINT_A: [holder_row("scrill", 123.456789)],
            SOL_MINT_B: [holder_row("scrill", 123.456790)],
        })
        http = HolderHttp({
            SOL_MINT_A: {HOLDER_WALLET: 123456789},
            SOL_MINT_B: {SECOND_WALLET: 123456790},
        })
        candidates: list[WalletCandidate] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            found = await resolver_for(http, path).resolve_from_holders(
                fomo, SimpleNamespace(handle="scrill"),
                self._balances(SOL_MINT_A, SOL_MINT_B), candidates=candidates,
            )
        self.assertIsNone(found)
        self.assertEqual(
            sorted(item.address for item in candidates),
            sorted([HOLDER_WALLET, SECOND_WALLET]),
        )
        # Two owners, one token each: nothing to prefer, so both are shown.
        self.assertEqual(
            sorted(item.address for item in choose_unverified_wallets(candidates)),
            sorted([HOLDER_WALLET, SECOND_WALLET]),
        )

    async def test_an_ambiguous_token_offers_nothing(self) -> None:
        # Two wallets could hold the published amount, so the route never
        # reached one owner and there is no candidate to carry.
        fomo = FakeFomoHolders({SOL_MINT_A: [holder_row("scrill", 123.456789)]})
        http = HolderHttp({SOL_MINT_A: {
            HOLDER_WALLET: 123456789,
            "Neighbour111111111111111111111111111111111": 123456790,
        }})
        candidates: list[WalletCandidate] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            found = await resolver_for(http, path).resolve_from_holders(
                fomo, SimpleNamespace(handle="scrill"), self._balances(SOL_MINT_A),
                candidates=candidates,
            )
        self.assertIsNone(found)
        self.assertEqual(candidates, [])

    async def test_the_balance_route_offers_its_own_fingerprint_owner(self) -> None:
        http = HolderHttp({SOL_MINT_A: {WALLET: 123456789}})
        candidates: list[WalletCandidate] = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            resolver = resolver_for(http, path)
            with mock.patch.object(
                resolver, "_has_fomo_sponsored_transaction", return_value=False,
            ):
                found = await resolver.resolve_from_balances(
                    SimpleNamespace(handle="pudgypenguins"),
                    self._balances(SOL_MINT_A), candidates=candidates,
                )
            self.assertIsNone(found)
            self.assertFalse(path.exists())
        self.assertEqual([item.address for item in candidates], [WALLET])
        self.assertEqual(candidates[0].sources, ("balance+helius",))

    async def test_a_caller_that_asks_for_no_candidates_is_unaffected(self) -> None:
        fomo = FakeFomoHolders({SOL_MINT_A: [holder_row("scrill", 123.456789)]})
        http = HolderHttp({SOL_MINT_A: {HOLDER_WALLET: 123456789}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wallets.json"
            resolver = resolver_for(http, path)
            with mock.patch.object(
                resolver, "_has_fomo_sponsored_transaction", return_value=False,
            ):
                found = await resolver.resolve_from_holders(
                    fomo, SimpleNamespace(handle="scrill"),
                    self._balances(SOL_MINT_A),
                )
        self.assertIsNone(found)
