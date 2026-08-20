"""Coverage for FOMO's `/hodlers/top` holder list and its wallet matching.

Fixtures are trimmed from a live capture of the token page's own request
(`hunt_out/sniff_hodlers_top_*.json`), so the shapes are real. No network.
"""

from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlsplit

from fomo_hodlers import (
    CACHE_SEPARATION,
    CHAIN_NAMES_BY_ID,
    FomoHolder,
    confident_matches,
    holders_query,
    match_holders_to_wallets,
    network_id_for,
    parse_token_holders,
)

MINT = "E3i7sTY5QYEBh3itepnomZQt7Eh5kzmHFk1vkm2pump"
CHUN_WALLET = "CGrbzqAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAALHA5"
LUVER_WALLET = "4DTFHCAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1Jcg"
QUANT_WALLET = "8f39XhAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAtsEr"


def holder_row(handle: str, amount: float, *, dev: bool = False,
               pnl: float = 100.0, cost: float = 50.0) -> dict:
    return {
        "user": {
            "id": f"id-{handle}",
            # Synthetic; FOMO_API.md section 10. Must never be used as a wallet.
            "address": "DGzQ31Tsg5a4Kgqi5AWTCpTfFxsQjBdevmSPNbGzsXc5",
            "evmAddress": "0x00f38d0000000000000000000000000000005c00",
            "displayName": f"{handle} display",
            "userHandle": handle,
            "twitter": f"https://x.com/{handle}",
        },
        "tradeId": f"trade-{handle}",
        "humanAmount": amount,
        "price": 0.000542771633818,
        "value": 13210.84,
        "pnl": pnl,
        "unrealizedPnl": pnl,
        "realizedPnl": 0,
        "costBasis": cost,
        "averageEntryPrice": 0.000266591,
        "averageHoldTimeSeconds": 378,
        "isDev": dev,
    }


PAYLOAD = [{
    "tokenAddress": MINT,
    "networkId": 1399811149,
    "totalHolders": 1006,
    "topHolders": [
        holder_row("ChunDoohwann", 24339588.53),
        holder_row("luver", 24131746.31),
        holder_row("Quanterty", 16682532.40),
    ],
}]


class QueryTests(unittest.TestCase):
    def test_tokens_parameter_is_a_json_array(self) -> None:
        query = parse_qs(urlsplit(holders_query(MINT, 1399811149)).query)
        self.assertEqual(
            json.loads(query["tokens"][0]),
            [{"address": MINT, "networkId": 1399811149}],
        )

    def test_the_route_is_spelled_hodlers(self) -> None:
        """Every `/holders` spelling 404s; this is not a typo to 'fix'."""
        self.assertTrue(holders_query(MINT, 1).startswith("/hodlers/top?"))

    def test_chain_names_map_to_fomo_network_ids(self) -> None:
        self.assertEqual(network_id_for("Solana"), 1399811149)
        self.assertEqual(network_id_for("Base"), 8453)
        self.assertIsNone(network_id_for("Sui"))


class ParseTests(unittest.TestCase):
    def test_rows_and_total_are_extracted(self) -> None:
        holders, total = parse_token_holders(PAYLOAD)
        self.assertEqual(total, 1006)
        self.assertEqual([item.handle for item in holders],
                         ["ChunDoohwann", "luver", "Quanterty"])

    def test_position_fields_survive(self) -> None:
        holder = parse_token_holders(PAYLOAD)[0][0]
        self.assertAlmostEqual(holder.amount, 24339588.53)
        self.assertAlmostEqual(holder.entry_price, 0.000266591)
        self.assertEqual(holder.hold_seconds, 378)
        self.assertAlmostEqual(holder.roi, 200.0)

    def test_response_object_envelope_is_unwrapped(self) -> None:
        holders, total = parse_token_holders({"responseObject": PAYLOAD})
        self.assertEqual((len(holders), total), (3, 1006))

    def test_rows_without_a_handle_or_amount_are_dropped(self) -> None:
        payload = [{"totalHolders": 2, "topHolders": [
            {"user": {"userHandle": ""}, "humanAmount": 5},
            {"user": {"userHandle": "ghost"}, "humanAmount": None},
            {"user": {"userHandle": "zero"}, "humanAmount": 0},
        ]}]
        self.assertEqual(parse_token_holders(payload)[0], [])

    def test_garbage_payloads_are_not_errors(self) -> None:
        self.assertEqual(parse_token_holders(None), ([], None))
        self.assertEqual(parse_token_holders("nope"), ([], None))


class MatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.holders = parse_token_holders(PAYLOAD)[0]

    def test_exact_positions_name_the_on_chain_owner(self) -> None:
        matched = match_holders_to_wallets(self.holders, [
            (CHUN_WALLET, 24339588.53),
            (LUVER_WALLET, 24131746.31),
            ("StrangerAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", 999.0),
        ])
        self.assertEqual({wallet: item.handle for wallet, item in matched.items()},
                         {CHUN_WALLET: "ChunDoohwann", LUVER_WALLET: "luver"})

    def test_display_rounding_still_matches(self) -> None:
        """FOMO rounds humanAmount; the chain has the full precision."""
        matched = match_holders_to_wallets(self.holders,
                                           [(CHUN_WALLET, 24339588.534117)])
        self.assertEqual(matched[CHUN_WALLET].handle, "ChunDoohwann")

    def test_two_wallets_at_the_same_size_leave_both_unnamed(self) -> None:
        matched = match_holders_to_wallets(self.holders, [
            (CHUN_WALLET, 24339588.53),
            (LUVER_WALLET, 24339588.53),
        ])
        self.assertEqual(matched, {})

    def test_two_traders_at_the_same_size_leave_the_wallet_unnamed(self) -> None:
        holders = [FomoHolder("alice", "Alice", "1", 1000.0),
                   FomoHolder("bob", "Bob", "2", 1000.0)]
        self.assertEqual(match_holders_to_wallets(holders, [(CHUN_WALLET, 1000.0)]), {})

    def test_a_holder_outside_the_top_wallets_matches_nothing(self) -> None:
        matched = match_holders_to_wallets(self.holders, [(QUANT_WALLET, 16682532.40)])
        self.assertEqual(list(matched), [QUANT_WALLET])
        self.assertEqual(matched[QUANT_WALLET].handle, "Quanterty")

    def test_the_synthetic_user_address_is_never_used_as_a_wallet(self) -> None:
        """user.address has no on-chain history — matching is by amount only."""
        synthetic = PAYLOAD[0]["topHolders"][0]["user"]["address"]
        matched = match_holders_to_wallets(self.holders, [(synthetic, 1.0)])
        self.assertEqual(matched, {})

    def test_empty_inputs(self) -> None:
        self.assertEqual(match_holders_to_wallets([], [(CHUN_WALLET, 1.0)]), {})
        self.assertEqual(match_holders_to_wallets(self.holders, []), {})


class ConfidenceTests(unittest.TestCase):
    """A row on a Discord card is reversible; a cache entry is not."""

    def setUp(self) -> None:
        self.holders = parse_token_holders(PAYLOAD)[0]

    def test_a_clean_match_is_persistable(self) -> None:
        onchain = [(CHUN_WALLET, 24339588.53), (QUANT_WALLET, 16682532.40)]
        self.assertEqual(
            {wallet: item.handle
             for wallet, item in confident_matches(self.holders, onchain).items()},
            {CHUN_WALLET: "ChunDoohwann", QUANT_WALLET: "Quanterty"},
        )

    def test_a_near_neighbour_is_displayed_but_not_persisted(self) -> None:
        """Distinguishable enough to label, too close to commit forever.

        Tolerance on a 24.3M position is ~24 tokens, so +100 is a clean match
        for display; the cache margin is 50x that and refuses it.
        """
        nearby = 24339588.53 + 100.0
        onchain = [(CHUN_WALLET, 24339588.53), (LUVER_WALLET, nearby)]
        self.assertIn(CHUN_WALLET, match_holders_to_wallets(self.holders, onchain))
        self.assertEqual(confident_matches(self.holders, onchain), {})

    def test_the_margin_is_a_multiple_of_the_match_tolerance(self) -> None:
        self.assertGreater(CACHE_SEPARATION, 1)

    def test_a_lone_holder_needs_no_margin(self) -> None:
        matched = confident_matches(self.holders, [(CHUN_WALLET, 24339588.53)])
        self.assertEqual(matched[CHUN_WALLET].handle, "ChunDoohwann")


class LeaderboardHoldingsTests(unittest.TestCase):
    """The leaderboard already carries exact positions -- no extra request.

    `/v2/leaderboard?limit=100` returns `topHoldings` per trader with
    tokenAddress, networkId and humanAmount, which is the same fingerprint
    `/hodlers/top` provides. One call seeds the whole bulk mapping.
    """

    def _rows(self) -> list:
        from types import SimpleNamespace
        return [
            SimpleNamespace(raw={
                "id": "u1", "userHandle": "change", "displayName": "change",
                "topHoldings": [
                    {"tokenAddress": MINT, "networkId": 1399811149,
                     "humanAmount": 24339588.53, "value": 13210.84, "pnl": 6722.12},
                    {"tokenAddress": "0xe934", "networkId": 4663,
                     "humanAmount": 26189449.97},
                ],
            }),
            SimpleNamespace(raw={
                "id": "u2", "userHandle": "luver", "displayName": "luver",
                "topHoldings": [{"tokenAddress": MINT, "networkId": 1399811149,
                                 "humanAmount": 24131746.31}],
            }),
        ]

    def test_holdings_group_by_token_and_chain(self) -> None:
        from fomo_map_top import leaderboard_holdings
        grouped = leaderboard_holdings(self._rows())
        self.assertEqual(sorted(grouped), [("0xe934", 4663), (MINT, 1399811149)])
        self.assertEqual(
            sorted(item.handle for item in grouped[(MINT, 1399811149)]),
            ["change", "luver"],
        )

    def test_amounts_survive_as_match_fingerprints(self) -> None:
        from fomo_map_top import leaderboard_holdings
        grouped = leaderboard_holdings(self._rows())
        matched = confident_matches(
            grouped[(MINT, 1399811149)],
            [(CHUN_WALLET, 24339588.53), (LUVER_WALLET, 24131746.31)],
        )
        self.assertEqual({wallet: item.handle for wallet, item in matched.items()},
                         {CHUN_WALLET: "change", LUVER_WALLET: "luver"})

    def test_malformed_holdings_are_skipped_not_fatal(self) -> None:
        from types import SimpleNamespace

        from fomo_map_top import leaderboard_holdings
        rows = [SimpleNamespace(raw={
            "userHandle": "broken",
            "topHoldings": [
                {"tokenAddress": "", "networkId": 1, "humanAmount": 5},
                {"tokenAddress": MINT, "networkId": "nope", "humanAmount": 5},
                {"tokenAddress": MINT, "networkId": 1, "humanAmount": 0},
                "not a dict",
            ],
        }), SimpleNamespace(raw={"userHandle": "", "topHoldings": []})]
        self.assertEqual(leaderboard_holdings(rows), {})

    def test_every_fomo_network_id_has_a_name(self) -> None:
        self.assertEqual(CHAIN_NAMES_BY_ID[1399811149], "Solana")
        self.assertEqual(CHAIN_NAMES_BY_ID[56], "BSC")


if __name__ == "__main__":
    unittest.main()
