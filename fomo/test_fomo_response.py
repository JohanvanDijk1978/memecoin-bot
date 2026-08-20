from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fomo_api import FomoUser
from fomo_bot import (
    FomoLayoutSelectionView,
    _clear_guild_commands,
    _enrich_fomo_message,
    _fit_field,
    build_compact_embed,
    build_embed,
    build_profile_embed,
)
from fomo_features import LatestActivity, OpenPosition, TraderStats
from fomo_tracking import TrackEvent


SOLANA_WALLET = "GxpWcYRz2nLcNxshch7yAhseZGtdgQz1nG8p5XxwQVdD"
EVM_WALLET = "0x0232b9afb9160fe479f25dade62fa60ef657bdc5"


def user(handle: str = "latencytest") -> FomoUser:
    return FomoUser({
        "id": "user-id",
        "userHandle": handle,
        "displayName": "Latency Test",
        "followers": 10,
        "following": 2,
        "totalVolume": 1000,
        "averageHoldTimeSeconds": 60,
        "description": "This biography must not appear in Compact.",
        "profilePictureLink": "https://example.com/avatar.png",
        "twitter": "https://x.com/latencytest",
    })


class WalletResolver:
    async def resolve(self, _fomo: object, _user: FomoUser) -> str:
        return SOLANA_WALLET

    async def resolve_from_balances(
        self, _user: FomoUser, _balances: object
    ) -> str:
        raise AssertionError("transaction resolver already returned a wallet")


class SlowWalletResolver:
    async def resolve(self, _fomo: object, _user: FomoUser) -> str:
        await asyncio.sleep(1)
        return SOLANA_WALLET


class EmptyWalletResolver:
    async def resolve(self, _fomo: object, _user: FomoUser) -> None:
        return None


class EvmResolver:
    async def resolve(self, _user: FomoUser, **_kwargs: object) -> str:
        return EVM_WALLET


class ConcurrentWalletResolver(WalletResolver):
    def __init__(self, own: asyncio.Event, other: asyncio.Event) -> None:
        self.own = own
        self.other = other

    async def resolve(self, _fomo: object, _user: FomoUser) -> str:
        self.own.set()
        await asyncio.wait_for(self.other.wait(), timeout=0.2)
        return SOLANA_WALLET


class ConcurrentEvmResolver:
    def __init__(self, own: asyncio.Event, other: asyncio.Event) -> None:
        self.own = own
        self.other = other

    async def resolve(self, _user: FomoUser, **_kwargs: object) -> str:
        self.own.set()
        await asyncio.wait_for(self.other.wait(), timeout=0.2)
        return EVM_WALLET


class ActivityAwareWalletResolver(WalletResolver):
    def __init__(self, activity_started: asyncio.Event) -> None:
        self.activity_started = activity_started

    async def resolve(self, _fomo: object, _user: FomoUser) -> str:
        await asyncio.wait_for(self.activity_started.wait(), timeout=0.2)
        return SOLANA_WALLET


class Client:
    def __init__(self, wallets: object, evm_wallets: object = None) -> None:
        self.fomo = object()
        self.wallets = wallets
        self.evm_wallets = evm_wallets
        self._http = None


class Message:
    def __init__(self) -> None:
        self.embeds = []

    async def edit(self, *, embed: object) -> None:
        self.embeds.append(embed)


class GuildCommandTree:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def clear_commands(self, *, guild: object) -> None:
        self.calls.append(("clear", guild))

    async def sync(self, *, guild: object) -> list[object]:
        self.calls.append(("sync", guild))
        return []


class FomoResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_guild_commands_are_cleared_without_copying_globals(self) -> None:
        tree = GuildCommandTree()
        guild = object()
        remaining = await _clear_guild_commands(tree, guild)
        self.assertEqual(remaining, [])
        self.assertEqual(tree.calls, [("clear", guild), ("sync", guild)])

    async def test_layout_selector_has_clear_compact_and_wide_descriptions(self) -> None:
        async def submit(_interaction: object, _handle: str, _layout: str) -> bool:
            return True

        view = FomoLayoutSelectionView(123, "latencytest", submit)
        options = view.selector.options
        self.assertEqual([option.label for option in options], ["Compact", "Wide"])
        self.assertEqual([option.value for option in options], ["compact", "wide"])
        self.assertEqual(
            [option.description for option in options],
            [
                "Essential profile information only.",
                "Full profile with all available information.",
            ],
        )

    async def test_compact_embed_contains_only_essential_sections(self) -> None:
        embed = build_compact_embed(
            user(),
            SOLANA_WALLET,
            EVM_WALLET,
            TraderStats(portfolio_value=250),
        )
        self.assertEqual(
            [field.name for field in embed.fields],
            ["Social", "Strategy", "Portfolio", "X / Twitter", "Linked wallets"],
        )
        self.assertEqual(embed.title, "Latency Test")
        self.assertEqual(embed.author.name, "@latencytest")
        self.assertEqual(embed.thumbnail.url, "https://example.com/avatar.png")
        self.assertIsNone(embed.description)
        self.assertIsNone(embed.footer.text)
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("https://x.com/latencytest", fields["X / Twitter"])
        self.assertIn(SOLANA_WALLET, fields["Linked wallets"])
        self.assertIn(EVM_WALLET, fields["Linked wallets"])

    async def test_compact_embed_shows_querying_while_wallets_are_pending(self) -> None:
        embed = build_compact_embed(
            user(),
            stats=TraderStats(portfolio_value=250),
            wallets_pending=True,
        )
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["Linked wallets"], "Querying ⏳")

    async def test_wide_layout_uses_the_existing_full_renderer_unchanged(self) -> None:
        profile = user()
        stats = TraderStats(portfolio_value=250)
        expected = build_embed(profile, SOLANA_WALLET, EVM_WALLET, stats)
        actual = build_profile_embed(
            profile, SOLANA_WALLET, EVM_WALLET, stats, layout="wide"
        )
        self.assertEqual(actual.to_dict(), expected.to_dict())

    async def test_evm_activity_does_not_wait_for_solana_discovery(self) -> None:
        activity_started = asyncio.Event()
        client = Client(ActivityAwareWalletResolver(activity_started), EvmResolver())
        client._http = object()
        message = Message()

        async def activity(_http: object, _wallet: str):
            activity_started.set()
            return (), ()

        with patch("fomo_bot.fetch_evm_activity", side_effect=activity):
            await _enrich_fomo_message(
                client,
                message,
                user("activity-concurrency-test"),
                TraderStats(),
                None,
                None,
                timeout=1,
            )
        self.assertEqual(len(message.embeds), 1)

    async def test_solana_and_evm_discovery_start_concurrently(self) -> None:
        sol_started = asyncio.Event()
        evm_started = asyncio.Event()
        message = Message()
        await _enrich_fomo_message(
            Client(
                ConcurrentWalletResolver(sol_started, evm_started),
                ConcurrentEvmResolver(evm_started, sol_started),
            ),
            message,
            user("concurrent-test"),
            TraderStats(),
            None,
            None,
            timeout=1,
        )
        self.assertEqual(len(message.embeds), 1)

    async def test_background_enrichment_edits_existing_profile(self) -> None:
        message = Message()
        await _enrich_fomo_message(
            Client(WalletResolver(), EvmResolver()),
            message,
            user(),
            TraderStats(raw_balances={"balances": []}),
            None,
            None,
            timeout=1,
        )
        self.assertEqual(len(message.embeds), 1)
        fields = {field.name: field.value for field in message.embeds[0].fields}
        self.assertIn(SOLANA_WALLET, fields["Solana wallet"])
        self.assertIn(EVM_WALLET, fields["EVM wallet"])

    async def test_background_enrichment_preserves_compact_layout(self) -> None:
        message = Message()
        await _enrich_fomo_message(
            Client(WalletResolver(), EvmResolver()),
            message,
            user("compact-enrichment-test"),
            TraderStats(raw_balances={"balances": []}),
            None,
            None,
            timeout=1,
            layout="compact",
        )
        self.assertEqual(len(message.embeds), 1)
        self.assertEqual(
            [field.name for field in message.embeds[0].fields],
            ["Social", "Strategy", "Portfolio", "X / Twitter", "Linked wallets"],
        )
        wallets = message.embeds[0].fields[-1].value
        self.assertIn(SOLANA_WALLET, wallets)
        self.assertIn(EVM_WALLET, wallets)

    async def test_completed_empty_wallet_search_replaces_querying_state(self) -> None:
        message = Message()
        await _enrich_fomo_message(
            Client(EmptyWalletResolver()),
            message,
            user("empty-wallet-test"),
            TraderStats(),
            None,
            None,
            timeout=1,
            layout="compact",
            wallets_pending=True,
        )
        self.assertEqual(len(message.embeds), 1)
        fields = {field.name: field.value for field in message.embeds[0].fields}
        self.assertEqual(fields["Linked wallets"], "No verified wallets found.")

    async def test_background_deadline_does_not_delay_or_edit_without_results(self) -> None:
        message = Message()
        await _enrich_fomo_message(
            Client(SlowWalletResolver()),
            message,
            user("unique-timeout-test"),
            TraderStats(),
            None,
            None,
            timeout=0.01,
        )
        self.assertEqual(message.embeds, [])


SOL_MINT = "TOKmint1111111111111111111111111111111111111"


def field(embed: object, name: str) -> str:
    return next((f.value for f in embed.fields if f.name == name), "")  # type: ignore[attr-defined]


class WideActivityTests(unittest.TestCase):
    """Wide buys read like wide sells: a marker, then a tradeable ticker."""

    def _stats(self, **kwargs: object) -> TraderStats:
        return TraderStats(
            latest_buys=(LatestActivity(
                "Bought", "WALL3", 504.0, "2026-08-19T13:00:00Z", "swap-1",
                "Solana", SOL_MINT, 42000.0, False,
            ),),
            latest_sells=(TrackEvent(
                kind="sell", symbol="BASEY", token_address="0xbasecoin",
                network_id=8453, created_at="2026-08-19T14:00:00Z",
                usd_value=120.0,
            ),),
            **kwargs,  # type: ignore[arg-type]
        )

    def test_buys_use_a_green_marker_not_an_ordinal(self) -> None:
        value = field(build_embed(user(), stats=self._stats()), "Latest buys")
        self.assertTrue(value.startswith("🟢 "))
        self.assertNotIn("`1.`", value)

    def test_buy_tickers_link_to_padre_like_sells_do(self) -> None:
        embed = build_embed(user(), stats=self._stats())
        buys, sells = field(embed, "Latest buys"), field(embed, "Latest sells")
        self.assertIn(f"[$WALL3](https://trade.padre.gg/trade/solana/{SOL_MINT})", buys)
        self.assertIn("[$BASEY](https://trade.padre.gg/trade/base/0xbasecoin)", sells)

    def test_buy_keeps_its_usd_market_cap_and_chain_detail(self) -> None:
        value = field(build_embed(user(), stats=self._stats()), "Latest buys")
        self.assertIn("$504.00", value)
        self.assertIn("MC", value)
        self.assertIn("Solana", value)

    def test_a_chain_padre_cannot_route_falls_back_to_bold(self) -> None:
        stats = TraderStats(latest_buys=(LatestActivity(
            "Bought", "WALL3", 10.0, None, None, "Robinhood", "0xrh",
        ),))
        value = field(build_embed(user(), stats=stats), "Latest buys")
        self.assertIn("**$WALL3**", value)
        self.assertNotIn("padre.gg", value)


class OpenPositionsFieldTests(unittest.TestCase):
    def _stats(self) -> TraderStats:
        return TraderStats(open_positions=(
            OpenPosition("WALL3", SOL_MINT, 1399811149, 1_200_000, 0.00042,
                         0.00058, 696.0, 192.0, 38.1, "a"),
            OpenPosition("BASEY", "0xbasecoin", 8453, 500, 4.0, 3.1,
                         1550.0, -450.0, -22.5, "b"),
        ))

    def test_every_requested_column_is_present(self) -> None:
        value = field(build_embed(user(), stats=self._stats()), "Open positions")
        line = value.splitlines()[0]
        self.assertIn(f"[$WALL3](https://trade.padre.gg/trade/solana/{SOL_MINT})", line)
        self.assertIn("entry $0.00042", line)   # avg entry, not rounded to cents
        self.assertIn("1.2M", line)             # position size
        self.assertIn("$696.00", line)          # position value
        self.assertIn("+$192.00", line)         # PnL
        self.assertIn("(+38.1%)", line)

    def test_a_losing_position_is_red_and_signed(self) -> None:
        value = field(build_embed(user(), stats=self._stats()), "Open positions")
        losing = value.splitlines()[1]
        self.assertTrue(losing.startswith("🔴 "))
        self.assertIn("-$450.00", losing)
        self.assertIn("(-22.5%)", losing)

    def test_an_unpriced_position_still_lists_without_inventing_pnl(self) -> None:
        stats = TraderStats(open_positions=(
            OpenPosition("QUIET", SOL_MINT, 1399811149, 10, None, None,
                         None, None, None, "c"),
        ))
        value = field(build_embed(user(), stats=stats), "Open positions")
        self.assertIn("⚪", value)
        self.assertIn("PnL —", value)
        self.assertIn("entry —", value)

    def test_no_open_positions_adds_no_field(self) -> None:
        embed = build_embed(user(), stats=TraderStats())
        self.assertNotIn("Open positions", [f.name for f in embed.fields])

    def test_rows_are_packed_inside_the_discord_field_limit(self) -> None:
        """Session 14's holder-row bug: Discord rejects the whole message."""
        rows = [f"{'x' * 300} row{index}" for index in range(5)]
        packed = _fit_field(rows)
        self.assertLessEqual(len(packed), 1024)
        self.assertTrue(packed.splitlines()[-1].startswith("… +"))
        self.assertEqual(_fit_field(["a", "b"]), "a\nb")


class TokenHolderIdentityTests(unittest.IsolatedAsyncioTestCase):
    """`/token` names FOMO holders from /hodlers/top, not just the cache."""

    def _token(self) -> object:
        from decimal import Decimal
        from token_intelligence import TokenHolder, TokenIntelligence
        return TokenIntelligence(
            "E3i7sTY5QYEBh3itepnomZQt7Eh5kzmHFk1vkm2pump", "Solana", "cc", "cc",
            591430.0, None, 0.00054, None, None,
            (TokenHolder("CGrbzqAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAALHA5",
                         Decimal("24339588.53"), 2.54),
             TokenHolder("StrangerAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                         Decimal("63850000"), 6.66)),
        )

    def _payload(self) -> list:
        return [{"tokenAddress": "E3i7", "networkId": 1399811149,
                 "totalHolders": 1006, "topHolders": [{
                     "user": {"id": "u1", "userHandle": "ChunDoohwann",
                              "displayName": "Chun", "address": "synthetic"},
                     "humanAmount": 24339588.53, "isDev": False}]}]

    async def test_an_uncached_fomo_holder_is_named(self) -> None:
        import fomo_bot

        class Fomo:
            async def token_holders(self, _address: str, network_id: int) -> list:
                assert network_id == 1399811149
                return self_payload

        self_payload = self._payload()
        token = self._token()
        with patch.object(fomo_bot, "bot",
                          type("B", (), {"fomo": Fomo(), "pump": None, "pump_profiles": None,
                                         "pump_evm": None, "wallets": None,
                                         "evm_wallets": None})()):
            matches = await fomo_bot._fomo_holder_matches(token)
            self.assertEqual(matches[token.holders[0].address].handle,
                             "ChunDoohwann")
            line = await fomo_bot._holder_label(
                token.holders[0], "Solana", matches[token.holders[0].address]
            )
        self.assertIn("[@ChunDoohwann](https://fomo.family/profile/ChunDoohwann)", line)
        self.assertIn("2.54%", line)

    async def test_a_holder_fomo_does_not_know_keeps_its_address(self) -> None:
        import fomo_bot
        token = self._token()
        with patch.object(fomo_bot, "bot",
                          type("B", (), {"fomo": None, "pump": None, "pump_profiles": None,
                                         "pump_evm": None, "wallets": None,
                                         "evm_wallets": None})()):
            self.assertEqual(await fomo_bot._fomo_holder_matches(token), {})
            line = await fomo_bot._holder_label(token.holders[1], "Solana", None)
        self.assertNotIn("fomo.family/profile", line)
        self.assertIn("6.66%", line)


    async def test_a_confident_match_is_queued_for_the_wallet_cache(self) -> None:
        """The identity /fomo would otherwise pay a sponsor or block scan for."""
        import fomo_bot

        adopted: dict[str, str] = {}

        class Wallets:
            async def adopt_holder_matches(self, matches: dict, token: str = "") -> dict:
                adopted.update(matches)
                return matches

        class Fomo:
            async def token_holders(self, _address: str, _network: int) -> list:
                return payload

        payload = self._payload()
        scheduled: list[object] = []
        stub = type("B", (), {"fomo": Fomo(), "pump": None, "pump_evm": None, "pump_profiles": None,
                              "wallets": Wallets(), "evm_wallets": None})()
        stub.create_enrichment_task = lambda coro, name: scheduled.append(  # type: ignore[attr-defined]
            asyncio.get_event_loop().create_task(coro)
        )
        with patch.object(fomo_bot, "bot", stub):
            await fomo_bot._fomo_holder_matches(self._token())
            await asyncio.gather(*scheduled)
        self.assertEqual(adopted,
                         {"CGrbzqAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAALHA5":
                          "ChunDoohwann"})

    async def test_adoption_is_skipped_when_the_resolver_is_off(self) -> None:
        import fomo_bot

        class Fomo:
            async def token_holders(self, _address: str, _network: int) -> list:
                return payload

        payload = self._payload()
        stub = type("B", (), {"fomo": Fomo(), "pump": None, "pump_evm": None, "pump_profiles": None,
                              "wallets": None, "evm_wallets": None})()
        stub.create_enrichment_task = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[attr-defined]
            AssertionError("nothing to adopt into")
        )
        with patch.object(fomo_bot, "bot", stub):
            matches = await fomo_bot._fomo_holder_matches(self._token())
        self.assertEqual(len(matches), 1)


    def _bsc_token(self) -> object:
        from decimal import Decimal
        from token_intelligence import TokenHolder, TokenIntelligence
        return TokenIntelligence(
            "0xb0c2ab5af4028461ace3f6e1c33a4ee1404e7777", "BSC", "CETS", "CETS",
            12110000.0, None, 0.01, None, None,
            (TokenHolder("0x11631d8299aa8385c95769dfa95d968248c2202a",
                         Decimal("22370000"), 2.24),),
        )

    async def test_an_evm_holder_is_adopted_as_an_evm_wallet(self) -> None:
        """A BSC holder's 0x address is an EVM smart wallet, not a Solana one.

        Routing it to the Solana resolver would write it to the `wallet` field
        and probe it with getSignaturesForAddress, which is a JSON-RPC error —
        so EVM identities never reached the cache and `/wallet` could not find
        a trader `/token` had just named.
        """
        import fomo_bot

        evm_adopted: dict[str, str] = {}
        chains: list[str] = []

        class EvmWallets:
            async def adopt_holder_matches(self, matches: dict, token: str = "",
                                           chain: str = "") -> dict:
                evm_adopted.update(matches)
                chains.append(chain)
                return matches

        class SolanaWallets:
            async def adopt_holder_matches(self, *_a: object, **_k: object) -> dict:
                raise AssertionError("an EVM holder must not go to the Solana cache")

        class Fomo:
            async def token_holders(self, _address: str, network_id: int) -> list:
                assert network_id == 56, network_id
                return [{"tokenAddress": "0xb0c2", "networkId": 56,
                         "totalHolders": 900, "topHolders": [{
                             "user": {"id": "u", "userHandle": "Drillpig_",
                                      "displayName": "Drillpig"},
                             "humanAmount": 22370000, "isDev": False}]}]

        scheduled: list[object] = []
        stub = type("B", (), {"fomo": Fomo(), "pump": None, "pump_evm": None, "pump_profiles": None,
                              "wallets": SolanaWallets(),
                              "evm_wallets": EvmWallets()})()
        stub.create_enrichment_task = lambda coro, name: scheduled.append(  # type: ignore[attr-defined]
            asyncio.get_event_loop().create_task(coro)
        )
        with patch.object(fomo_bot, "bot", stub):
            matches = await fomo_bot._fomo_holder_matches(self._bsc_token())
            await asyncio.gather(*scheduled)
        self.assertEqual(matches["0x11631d8299aa8385c95769dfa95d968248c2202a"].handle,
                         "Drillpig_")
        self.assertEqual(evm_adopted,
                         {"0x11631d8299aa8385c95769dfa95d968248c2202a": "Drillpig_"})
        self.assertEqual(chains, ["bsc"])

    async def test_a_failed_holder_lookup_never_breaks_the_card(self) -> None:
        import fomo_bot
        from fomo_api import FomoError

        class Broken:
            async def token_holders(self, *_a: object, **_k: object) -> list:
                raise FomoError("503 upstream")

        with patch.object(fomo_bot, "bot",
                          type("B", (), {"fomo": Broken(), "pump": None, "pump_profiles": None,
                                         "pump_evm": None, "wallets": None,
                                         "evm_wallets": None})()):
            self.assertEqual(await fomo_bot._fomo_holder_matches(self._token()), {})


if __name__ == "__main__":
    unittest.main()
