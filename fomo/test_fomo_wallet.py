from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
