from __future__ import annotations

import unittest

from fomo_wallet import (
    FOMO_SPONSOR,
    SponsorIndex,
    find_tx_via_sponsor,
    iso_epoch,
    pick_swaps,
    rpc_display_name,
)


MINT_A = "MintA111111111111111111111111111111111111111"
MINT_B = "MintB111111111111111111111111111111111111111"
WALLET = "Wallet11111111111111111111111111111111111111"


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


class WalletDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_pick_swaps_uses_distinct_mints(self) -> None:
        rows = [swap(MINT_A, 1), swap(MINT_A, 2), swap(MINT_B, 3)]
        self.assertEqual(
            [row["outTokenAddress"] for row in pick_swaps(rows, want=4)],
            [MINT_A, MINT_B, MINT_A],
        )

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

    def test_rpc_display_hides_query_credentials(self) -> None:
        shown = rpc_display_name("https://example.invalid/rpc?api-key=secret")
        self.assertEqual(shown, "https://example.invalid")
        self.assertNotIn("secret", shown)


if __name__ == "__main__":
    unittest.main()
