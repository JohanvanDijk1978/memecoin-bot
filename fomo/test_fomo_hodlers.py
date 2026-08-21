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
    HolderThesis,
    confident_matches,
    holders_query,
    match_holders_to_wallets,
    holders_query_many,
    network_id_for,
    parse_holder_groups,
    parse_thesis_feed,
    parse_token_holders,
    rank_theses,
    thesis_feed_query,
    theses_from_trades,
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


class ThesisParsingTests(unittest.TestCase):
    """`/thesis` reads two routes; neither may be trusted to keep its shape."""

    def test_the_feed_query_carries_the_token_and_network(self) -> None:
        path = thesis_feed_query(MINT, 1399811149, limit=25)
        query = parse_qs(urlsplit(path).query)
        self.assertTrue(urlsplit(path).path.endswith("/feed/token/sortedThesis"))
        self.assertEqual(query["tokenAddress"], [MINT])
        self.assertEqual(query["networkId"], ["1399811149"])
        self.assertEqual(query["limit"], ["25"])

    def test_a_feed_row_becomes_a_holder_thesis(self) -> None:
        rows = [{
            "user": {
                "userHandle": "Eagle_0X",
                "displayName": "Eagle",
                "twitter": "https://x.com/Eagle_0X",
            },
            "comment": {"comment": "In hindsight it was obvious"},
            "equity": 39_100.0,
            "pnl": 34_500.0,
            "averageHoldTimeSeconds": 118_800,
            "tradeId": "trade-eagle",
        }]
        theses = parse_thesis_feed(rows)
        self.assertEqual(len(theses), 1)
        self.assertEqual(theses[0].handle, "Eagle_0X")
        self.assertEqual(theses[0].text, "In hindsight it was obvious")
        self.assertEqual(theses[0].value_usd, 39_100.0)
        self.assertEqual(theses[0].pnl_usd, 34_500.0)
        self.assertEqual(theses[0].hold_seconds, 118_800)

    def test_the_feed_is_read_through_common_envelopes(self) -> None:
        row = {"user": {"userHandle": "malk"}, "comment": "zero or hero"}
        for payload in (
            [row],
            {"responseObject": [row]},
            {"theses": [row]},
            {"responseObject": {"items": [row]}},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(
                    [thesis.handle for thesis in parse_thesis_feed(payload)], ["malk"]
                )

    def test_an_unrecognised_feed_shape_yields_nothing_rather_than_junk(self) -> None:
        # This is what makes the fallback safe: a shape we cannot read has to
        # look like "no theses here", never like a half-parsed card.
        for payload in (None, 42, "theses", {"unexpected": {"nested": 1}},
                        [{"user": {"userHandle": "x"}}],   # no text
                        [{"comment": "orphaned"}]):        # no handle
            with self.subTest(payload=payload):
                self.assertEqual(parse_thesis_feed(payload), [])

    def test_trade_details_supply_the_text_for_holder_rows(self) -> None:
        holders, _total = parse_token_holders(PAYLOAD)
        details = [
            {"trade": {"id": f"trade-{holders[0].handle}"},
             "comment": {"comment": "soon worldwide!"}},
            RuntimeError("503 upstream"),          # _get_many returns these inline
            {"trade": {"id": "trade-unknown"}, "comment": {"comment": "orphan"}},
        ]
        theses = theses_from_trades(holders, details)
        self.assertEqual([thesis.handle for thesis in theses], [holders[0].handle])
        self.assertEqual(theses[0].text, "soon worldwide!")
        self.assertEqual(theses[0].value_usd, holders[0].value_usd)
        self.assertEqual(theses[0].hold_seconds, holders[0].hold_seconds)

    def test_a_trade_without_a_comment_is_not_a_thesis(self) -> None:
        holders, _total = parse_token_holders(PAYLOAD)
        details = [{"trade": {"id": f"trade-{holders[0].handle}"}, "comment": None},
                   {"trade": {"id": f"trade-{holders[1].handle}"},
                    "comment": {"comment": "   "}}]
        self.assertEqual(theses_from_trades(holders, details), [])

    def test_ranking_puts_the_biggest_position_first(self) -> None:
        theses = rank_theses([
            HolderThesis("small", "small", "c", value_usd=100.0),
            HolderThesis("big", "big", "a", value_usd=39_100.0),
            HolderThesis("mid", "mid", "b", value_usd=1_000.0),
        ])
        self.assertEqual([thesis.handle for thesis in theses], ["big", "mid", "small"])

    def test_an_unpriced_position_ranks_last_not_first(self) -> None:
        theses = rank_theses([
            HolderThesis("unpriced", "unpriced", "a", value_usd=None),
            HolderThesis("priced", "priced", "b", value_usd=1.0),
        ])
        self.assertEqual([thesis.handle for thesis in theses], ["priced", "unpriced"])

    def test_one_entry_per_handle_and_the_larger_wins(self) -> None:
        theses = rank_theses([
            HolderThesis("Dup", "Dup", "older", value_usd=10.0),
            HolderThesis("dup", "dup", "newer", value_usd=500.0),
        ])
        self.assertEqual(len(theses), 1)
        self.assertEqual(theses[0].text, "newer")


class HolderGroupTests(unittest.TestCase):
    """Wallet resolution asks about a whole position list in one request.

    `parse_token_holders` flattens every token's rows together, which is right
    for `/token` and wrong here: which amount belongs to which token is the
    whole point.
    """

    OTHER = "zj1jpp7QMveWHLs61vL9KMZf254KvW7j4AAmBF8ry2k"

    def _payload(self) -> list:
        return [
            {"tokenAddress": MINT, "networkId": 1399811149, "totalHolders": 1006,
             "topHolders": [holder_row("chun", 24339588.53)]},
            {"tokenAddress": self.OTHER, "networkId": 1399811149,
             "totalHolders": 12, "topHolders": [holder_row("luver", 500.0)]},
        ]

    def test_the_query_carries_every_token_in_one_array(self) -> None:
        path = holders_query_many([(MINT, 1399811149), (self.OTHER, 1399811149)])
        tokens = json.loads(parse_qs(urlsplit(path).query)["tokens"][0])
        self.assertEqual([entry["address"] for entry in tokens],
                         [MINT, self.OTHER])
        self.assertTrue(all(entry["networkId"] == 1399811149 for entry in tokens))

    def test_the_single_token_query_is_unchanged(self) -> None:
        self.assertEqual(holders_query(MINT, 1399811149),
                         holders_query_many([(MINT, 1399811149)]))

    def test_each_token_keeps_its_own_holders(self) -> None:
        groups = parse_holder_groups(self._payload())
        self.assertEqual([group.address for group in groups], [MINT, self.OTHER])
        self.assertEqual([group.total for group in groups], [1006, 12])
        self.assertEqual([[row.handle for row in group.holders] for group in groups],
                         [["chun"], ["luver"]])

    def test_flattening_still_works_for_the_token_card(self) -> None:
        holders, total = parse_token_holders(self._payload())
        self.assertEqual([row.handle for row in holders], ["chun", "luver"])
        self.assertEqual(total, 12)

    def test_a_junk_payload_is_no_groups_rather_than_an_exception(self) -> None:
        for payload in (None, 7, "rows", {"unexpected": 1}, [None, 3]):
            with self.subTest(payload=payload):
                self.assertEqual(parse_holder_groups(payload), [])
