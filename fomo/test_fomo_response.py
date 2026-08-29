from __future__ import annotations

import asyncio
import logging
import unittest
from collections.abc import Sequence
from unittest.mock import patch

from fomo_api import FomoUser
from fomo_bot import (
    UNVERIFIED_WALLET_NOTE,
    UNVERIFIED_WALLETS_NOTE,
    FomoLayoutSelectionView,
    _clear_guild_commands,
    _enrich_fomo_message,
    _fit_field,
    build_compact_embed,
    build_embed,
    build_profile_embed,
)
from fomo_wallet import WalletCandidate
from fomo_features import LatestActivity, OpenPosition, TraderStats
from fomo_tracking import TrackEvent


SOLANA_WALLET = "GxpWcYRz2nLcNxshch7yAhseZGtdgQz1nG8p5XxwQVdD"
EVM_WALLET = "0x0232b9afb9160fe479f25dade62fa60ef657bdc5"
LIKELY_WALLET = "Likely11111111111111111111111111111111111111"
OTHER_LIKELY_WALLET = "Likely22222222222222222222222222222222222222"
SOL_MINT_1 = "MintA111111111111111111111111111111111111111"
SOL_MINT_2 = "MintB111111111111111111111111111111111111111"


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


class BaseWalletResolver:
    """The three Solana routes `_resolve_fomo_enrichment` drives, in order.

    Each double overrides the one route it is about. The defaults return None
    so a test that cares about the transaction route is not silently answered
    by the holder route in front of it.
    """

    async def resolve_from_holders(
        self, _fomo: object, _user: FomoUser, _balances: object,
        **_kwargs: object,
    ) -> str | None:
        return None

    async def resolve(self, _fomo: object, _user: FomoUser) -> str | None:
        return None

    async def resolve_from_balances(
        self, _user: FomoUser, _balances: object, **_kwargs: object
    ) -> str | None:
        return None


class WalletResolver(BaseWalletResolver):
    async def resolve(self, _fomo: object, _user: FomoUser) -> str:
        return SOLANA_WALLET

    async def resolve_from_balances(
        self, _user: FomoUser, _balances: object, **_kwargs: object
    ) -> str:
        raise AssertionError("transaction resolver already returned a wallet")


class SlowWalletResolver(BaseWalletResolver):
    async def resolve(self, _fomo: object, _user: FomoUser) -> str:
        await asyncio.sleep(1)
        return SOLANA_WALLET


class EmptyWalletResolver(BaseWalletResolver):
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




class FakeResponseChannel:
    """Records what a view did instead of talking to Discord."""

    def __init__(self) -> None:
        self.edits: list[dict] = []
        self.messages: list[dict] = []
        self.deferred = 0

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)

    async def send_message(self, content: str = "", **kwargs: object) -> None:
        self.messages.append({"content": content, **kwargs})

    async def defer(self, **_kwargs: object) -> None:
        self.deferred += 1


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, content: str = "", **kwargs: object) -> None:
        self.messages.append({"content": content, **kwargs})


class FakeInteraction:
    def __init__(self, user_id: int = 1, channel_id: int = 99) -> None:
        self.response = FakeResponseChannel()
        self.followup = FakeFollowup()
        self.user = type("U", (), {"id": user_id})()
        self.channel_id = channel_id
        self.guild_id = 7
        self.followup_edits: list[dict] = []

    async def edit_original_response(self, **kwargs: object) -> None:
        self.followup_edits.append(kwargs)


def token_fixture(symbol: str = "CC") -> object:
    from decimal import Decimal
    from token_intelligence import TokenHolder, TokenIntelligence
    return TokenIntelligence(
        "E3i7sTY5QYEBh3itepnomZQt7Eh5kzmHFk1vkm2pump", "Solana", "Chiikawa", symbol,
        591_430.0, None, 0.00054, "https://img.example/cc.png", None,
        tuple(TokenHolder(f"wallet{index:02d}", Decimal(1000 - index), 1.0)
              for index in range(50)),
    )


class TokenPaginationTests(unittest.TestCase):
    """`/token` is always the top 50, ten a page, five pages."""

    def _lines(self, count: int = 50) -> list[str]:
        return [f"`{index}.` holder-{index}" for index in range(1, count + 1)]

    @staticmethod
    def _holder_body(embed: object) -> str:
        """Everything below the contract-address field."""
        return "\n".join(field.value for field in embed.fields[1:])

    def test_page_two_starts_at_the_eleventh_holder(self) -> None:
        from fomo_bot import _token_page_embed
        embed = _token_page_embed(token_fixture(), self._lines(), 2, 5)
        body = self._holder_body(embed)
        self.assertIn("`11.` holder-11", body)
        self.assertIn("`20.` holder-20", body)
        self.assertNotIn("holder-10\n", body)
        self.assertNotIn("holder-21", body)
        self.assertIn("11-20 of 50", "".join(field.name for field in embed.fields))
        self.assertIn("Page 2 of 5", embed.footer.text)

    def test_every_holder_appears_exactly_once_across_the_five_pages(self) -> None:
        from fomo_bot import TOKEN_HOLDER_PAGE, _token_page_embed
        lines = self._lines()
        pages = -(-len(lines) // TOKEN_HOLDER_PAGE)
        self.assertEqual(pages, 5)
        seen = []
        for page in range(1, pages + 1):
            embed = _token_page_embed(token_fixture(), lines, page, pages)
            seen.extend(self._holder_body(embed).splitlines())
        self.assertEqual(seen, lines)

    def test_a_short_holder_list_is_one_page(self) -> None:
        from fomo_bot import TOKEN_HOLDER_PAGE, _token_page_embed
        lines = self._lines(7)
        pages = max(1, -(-len(lines) // TOKEN_HOLDER_PAGE))
        self.assertEqual(pages, 1)
        embed = _token_page_embed(token_fixture(), lines, 1, pages)
        self.assertIn("1-7 of 7", "".join(field.name for field in embed.fields))

    def test_the_header_repeats_on_every_page(self) -> None:
        from fomo_bot import _token_page_embed
        for page in (1, 3, 5):
            embed = _token_page_embed(token_fixture(), self._lines(), page, 5)
            self.assertIn("Market cap", embed.description)
            self.assertEqual(embed.fields[0].name, "Contract address")
            self.assertEqual(embed.thumbnail.url, "https://img.example/cc.png")

    def test_an_empty_holder_list_still_renders(self) -> None:
        from fomo_bot import _token_page_embed
        embed = _token_page_embed(token_fixture(), [], 1, 1)
        self.assertIn("Holder data is currently unavailable.",
                      self._holder_body(embed))


class PaginatedEmbedViewTests(unittest.IsolatedAsyncioTestCase):
    def _view(self, pages: int = 3):
        import discord
        from fomo_bot import PaginatedEmbedView
        return PaginatedEmbedView(
            [discord.Embed(title=f"page {index}") for index in range(pages)]
        )

    async def test_the_first_page_cannot_go_back(self) -> None:
        view = self._view()
        self.assertTrue(view.previous_button.disabled)
        self.assertFalse(view.next_button.disabled)

    async def test_next_advances_and_re_enables_previous(self) -> None:
        view = self._view()
        interaction = FakeInteraction()
        await view.next_button.callback(interaction)
        self.assertEqual(view.index, 1)
        self.assertFalse(view.previous_button.disabled)
        self.assertEqual(interaction.response.edits[0]["embed"].title, "page 1")

    async def test_the_last_page_cannot_go_forward(self) -> None:
        view = self._view()
        interaction = FakeInteraction()
        await view.next_button.callback(interaction)
        await view.next_button.callback(interaction)
        self.assertEqual(view.index, 2)
        self.assertTrue(view.next_button.disabled)
        self.assertFalse(view.previous_button.disabled)

    async def test_paging_never_walks_off_either_end(self) -> None:
        view = self._view(2)
        interaction = FakeInteraction()
        for _ in range(4):
            await view.next_button.callback(interaction)
        self.assertEqual(view.index, 1)
        for _ in range(4):
            await view.previous_button.callback(interaction)
        self.assertEqual(view.index, 0)


class ThesisCardTests(unittest.TestCase):
    """`/thesis` — the holder theses card and how one entry reads."""

    def _thesis(self, **kwargs):
        from fomo_hodlers import HolderThesis
        base = dict(
            handle="Eagle_0X", display_name="Eagle",
            text="In hindsight it was obvious", value_usd=39_100.0,
            pnl_usd=34_500.0, hold_seconds=118_800,
            twitter="https://x.com/Eagle_0X",
        )
        base.update(kwargs)
        return HolderThesis(**base)

    def test_an_entry_carries_position_pnl_and_hold_time(self) -> None:
        from fomo_bot import _thesis_entry
        entry = _thesis_entry(1, self._thesis())
        self.assertIn("**1. Eagle_0X**", entry)
        self.assertIn("https://fomo.family/profile/Eagle_0X", entry)
        self.assertIn("[[X]](https://x.com/Eagle_0X)", entry)
        self.assertIn("$39.10K (🟢 +$34.50K)", entry)
        self.assertIn("1d 9h", entry)
        self.assertIn("> In hindsight it was obvious", entry)

    def test_a_losing_position_is_marked_red(self) -> None:
        from fomo_bot import _thesis_entry
        entry = _thesis_entry(4, self._thesis(pnl_usd=-3_600.0))
        self.assertIn("🔴 -$3.60K", entry)
        self.assertNotIn("🟢", entry)

    def test_a_dev_is_marked_and_a_missing_twitter_is_not_linked(self) -> None:
        from fomo_bot import _thesis_entry
        entry = _thesis_entry(2, self._thesis(is_dev=True, twitter=None))
        self.assertIn("🛠️", entry)
        self.assertNotIn("[X]", entry)

    def test_a_multi_line_thesis_quotes_every_line(self) -> None:
        from fomo_bot import _thesis_quote
        quoted = _thesis_quote("first line\n\nsecond line")
        self.assertEqual(quoted.splitlines(), ["> first line", ">", "> second line"])
        # `>>>` would quote the entries that follow it, so it must not appear.
        self.assertNotIn(">>>", quoted)

    def test_a_very_long_thesis_is_clipped(self) -> None:
        from fomo_bot import THESIS_TEXT_LIMIT, _thesis_quote
        quoted = _thesis_quote("x" * (THESIS_TEXT_LIMIT * 2))
        self.assertTrue(quoted.endswith("…"))
        self.assertLessEqual(len(quoted), THESIS_TEXT_LIMIT + 2)

    def test_the_card_counts_the_theses_and_numbers_pages(self) -> None:
        from fomo_bot import THESIS_PAGE, _thesis_page_embed
        theses = [self._thesis(handle=f"holder{index}", value_usd=1000.0 - index)
                  for index in range(30)]
        pages = -(-len(theses) // THESIS_PAGE)
        self.assertEqual(pages, 6)
        first = _thesis_page_embed(token_fixture(), theses, 1, pages)
        self.assertIn("Holder theses for $CC", first.title)
        self.assertIn("30 holders with a thesis", first.description)
        self.assertIn("**1. holder0**", first.description)
        self.assertIn("**5. holder4**", first.description)
        self.assertNotIn("**6. holder5**", first.description)
        self.assertIn("Page 1 of 6", first.footer.text)

        second = _thesis_page_embed(token_fixture(), theses, 2, pages)
        self.assertIn("**6. holder5**", second.description)
        self.assertIn("Page 2 of 6", second.footer.text)

    def test_the_description_stays_inside_the_embed_limit(self) -> None:
        from fomo_bot import _thesis_page_embed
        theses = [self._thesis(handle=f"h{index}", text="y" * 5000)
                  for index in range(5)]
        embed = _thesis_page_embed(token_fixture(), theses, 1, 1)
        self.assertLessEqual(len(embed.description), 4096)


class ThesisSourceTests(unittest.IsolatedAsyncioTestCase):
    """The feed route is tried first; the verified holder route is the floor."""

    def _bot(self, fomo: object) -> object:
        return type("B", (), {"fomo": fomo})()

    def _holder_payload(self) -> list:
        return [{"tokenAddress": "E3i7", "networkId": 1399811149,
                 "totalHolders": 3, "topHolders": [
                     {"user": {"id": "u1", "userHandle": "malk"},
                      "tradeId": "t-malk", "humanAmount": 10.0, "value": 21_600.0},
                     {"user": {"id": "u2", "userHandle": "Ili"},
                      "tradeId": "t-ili", "humanAmount": 5.0, "value": 19_300.0},
                 ]}]

    async def test_the_feed_answers_in_one_request(self) -> None:
        import fomo_bot

        class Fomo:
            calls: list[str] = []

            async def token_theses(self, *_a: object, **_k: object) -> list:
                self.calls.append("feed")
                return [{"user": {"userHandle": "Eagle_0X"},
                         "comment": {"comment": "100m is coming"},
                         "equity": 39_100.0}]

            async def token_holders(self, *_a: object, **_k: object) -> list:
                self.calls.append("hodlers")
                return []

        fomo = Fomo()
        with patch.object(fomo_bot, "bot", self._bot(fomo)):
            theses = await fomo_bot._token_theses(token_fixture())
        self.assertEqual([thesis.handle for thesis in theses], ["Eagle_0X"])
        self.assertEqual(fomo.calls, ["feed"])

    async def test_an_empty_feed_falls_back_to_holders_and_trades(self) -> None:
        import fomo_bot

        class Fomo:
            def __init__(self) -> None:
                self.trade_ids: list[str] = []

            async def token_theses(self, *_a: object, **_k: object) -> list:
                return []

            async def token_holders(self, *_a: object, **_k: object) -> list:
                return ThesisSourceTests._holder_payload(self)  # type: ignore[arg-type]

            async def trade_details(self, trade_ids: list, **_k: object) -> tuple:
                self.trade_ids = trade_ids
                return (
                    {"trade": {"id": "t-malk"}, "comment": {"comment": "zero or hero"}},
                    {"trade": {"id": "t-ili"}, "comment": {"comment": "soon worldwide!"}},
                )

        fomo = Fomo()
        with patch.object(fomo_bot, "bot", self._bot(fomo)):
            theses = await fomo_bot._token_theses(token_fixture())
        self.assertEqual([thesis.handle for thesis in theses], ["malk", "Ili"])
        self.assertEqual(theses[0].text, "zero or hero")
        self.assertEqual(sorted(fomo.trade_ids), ["t-ili", "t-malk"])

    async def test_a_broken_feed_route_is_not_an_error(self) -> None:
        import fomo_bot
        from fomo_api import FomoError

        class Fomo:
            async def token_theses(self, *_a: object, **_k: object) -> list:
                raise FomoError("404 from /feed/token/sortedThesis")

            async def token_holders(self, *_a: object, **_k: object) -> list:
                return ThesisSourceTests._holder_payload(self)  # type: ignore[arg-type]

            async def trade_details(self, trade_ids: list, **_k: object) -> tuple:
                return ({"trade": {"id": "t-malk"},
                         "comment": {"comment": "zero or hero"}},)

        with patch.object(fomo_bot, "bot", self._bot(Fomo())):
            theses = await fomo_bot._token_theses(token_fixture())
        self.assertEqual([thesis.handle for thesis in theses], ["malk"])

    async def test_both_routes_failing_yields_no_theses_rather_than_raising(self) -> None:
        import fomo_bot
        from fomo_api import FomoError

        class Fomo:
            async def token_theses(self, *_a: object, **_k: object) -> list:
                raise FomoError("503")

            async def token_holders(self, *_a: object, **_k: object) -> list:
                raise FomoError("503")

        with patch.object(fomo_bot, "bot", self._bot(Fomo())):
            self.assertEqual(await fomo_bot._token_theses(token_fixture()), [])

    async def test_an_unsupported_chain_never_asks_fomo(self) -> None:
        import fomo_bot
        from dataclasses import replace

        class Fomo:
            async def token_theses(self, *_a: object, **_k: object) -> list:
                raise AssertionError("must not be called for an unknown chain")

        token = replace(token_fixture(), chain="Unknown")
        with patch.object(fomo_bot, "bot", self._bot(Fomo())):
            self.assertEqual(await fomo_bot._token_theses(token), [])


class TrackedManagerTests(unittest.IsolatedAsyncioTestCase):
    """`/tracked` absorbed `/fomotracked`, `/pumptracked`, `/untrack`
    and `/tracksettings`."""

    PUMP_WALLET = "8f39Xh1111111111111111111111111111111tsEr"

    def _entries(self) -> list:
        return [
            ("FOMO", {"userId": "fomo-1", "handle": "Binkieee",
                      "activityFilters": ["buys", "theses"]}),
            ("Pump", {"userId": self.PUMP_WALLET, "handle": "zinc",
                      "activityFilters": ["sells"]}),
        ]

    class Store:
        def __init__(self) -> None:
            self.removed: list[tuple[int, str]] = []
            self.updated: list[tuple[int, str, list]] = []
            self.exists = True

        def remove(self, channel_id: int, user_id: str) -> bool:
            self.removed.append((channel_id, user_id))
            return self.exists

        def set_activity_filters(self, channel_id: int, user_id: str,
                                 filters: list) -> bool:
            self.updated.append((channel_id, user_id, filters))
            return self.exists

    def _bot(self, fomo_store: object, pump_store: object) -> object:
        return type("B", (), {"tracking": fomo_store, "pump_tracking": pump_store})()

    async def test_both_platforms_share_one_list(self) -> None:
        from fomo_bot import _tracked_embeds
        embeds = _tracked_embeds(self._entries())
        self.assertEqual(len(embeds), 1)
        body = embeds[0].description
        self.assertIn("Tracked in this channel · 2", embeds[0].title)
        self.assertIn("🔵 [@Binkieee](https://fomo.family/profile/Binkieee)", body)
        self.assertIn("🟢", body)
        self.assertIn("zinc", body)
        self.assertIn(self.PUMP_WALLET, body)
        self.assertIn("buys + theses", body)
        self.assertIn("sells only", body)

    def test_removal_reports_only_what_was_actually_removed(self) -> None:
        import fomo_bot
        store = self.Store()
        pump = self.Store()
        pump.exists = False
        with patch.object(fomo_bot, "bot", self._bot(store, pump)):
            removed = fomo_bot._remove_tracked_entries(99, self._entries())
        self.assertEqual(removed, ["FOMO **@Binkieee**"])
        self.assertEqual(store.removed, [(99, "fomo-1")])
        self.assertEqual(pump.removed, [(99, self.PUMP_WALLET)])

    async def test_the_edit_picker_offers_each_platform_its_own_third_alert(self) -> None:
        import fomo_bot
        with patch.object(fomo_bot, "bot", self._bot(self.Store(), self.Store())):
            fomo_view = fomo_bot._alert_settings_view(1, 99, "FOMO", self._entries()[0][1])
            pump_view = fomo_bot._alert_settings_view(1, 99, "Pump", self._entries()[1][1])
        self.assertEqual(
            [option.value for option in fomo_view.selector.options],
            ["buys", "sells", "theses"],
        )
        self.assertEqual(
            [option.value for option in pump_view.selector.options],
            ["buys", "sells", "callouts"],
        )
        # the current subscription comes back pre-selected
        self.assertEqual(
            [option.value for option in fomo_view.selector.options if option.default],
            ["buys", "theses"],
        )

    async def test_saving_settings_writes_to_that_platforms_store(self) -> None:
        import fomo_bot
        pump = self.Store()
        with patch.object(fomo_bot, "bot", self._bot(self.Store(), pump)):
            view = fomo_bot._alert_settings_view(1, 99, "Pump", self._entries()[1][1])
            message = await view.on_submit(FakeInteraction(), ["buys", "callouts"])
        self.assertEqual(pump.updated, [(99, self.PUMP_WALLET, ["buys", "callouts"])])
        self.assertIn("Updated Pump alerts for **@zinc**", message)

    async def test_remove_without_a_selection_asks_for_one(self) -> None:
        import fomo_bot
        store = self.Store()
        with patch.object(fomo_bot, "bot", self._bot(store, self.Store())):
            view = fomo_bot.TrackedManagerView(1, self._entries(), 99)
            interaction = FakeInteraction()
            await view.remove_button.callback(interaction)
        self.assertEqual(store.removed, [])
        self.assertIn("Select at least one",
                      interaction.response.messages[0]["content"])
        self.assertTrue(interaction.response.messages[0]["ephemeral"])

    async def test_remove_drops_every_selected_subscription(self) -> None:
        import fomo_bot
        store = self.Store()
        pump = self.Store()
        with patch.object(fomo_bot, "bot", self._bot(store, pump)):
            view = fomo_bot.TrackedManagerView(1, self._entries(), 99)
            await view.choose(FakeInteraction(), [0, 1])
            interaction = FakeInteraction()
            await view.remove_button.callback(interaction)
        self.assertEqual(store.removed, [(99, "fomo-1")])
        self.assertEqual(pump.removed, [(99, self.PUMP_WALLET)])
        edit = interaction.response.edits[0]
        self.assertIn("Stopped tracking", edit["content"])
        self.assertIsNone(edit["view"])

    async def test_edit_changes_one_subscription_at_a_time(self) -> None:
        import fomo_bot
        with patch.object(fomo_bot, "bot", self._bot(self.Store(), self.Store())):
            view = fomo_bot.TrackedManagerView(1, self._entries(), 99)
            await view.choose(FakeInteraction(), [0, 1])
            interaction = FakeInteraction()
            await view.edit_button.callback(interaction)
            self.assertIn("select just one",
                          interaction.response.messages[0]["content"])

            await view.choose(FakeInteraction(), [1])
            chosen = FakeInteraction()
            await view.edit_button.callback(chosen)
        edit = chosen.response.edits[0]
        self.assertIn("Choose the alerts for Pump **@zinc**", edit["content"])
        self.assertIsInstance(edit["view"], fomo_bot.ActivitySelectionView)

    async def test_the_selector_only_defers_so_the_choice_stays_visible(self) -> None:
        import fomo_bot
        with patch.object(fomo_bot, "bot", self._bot(self.Store(), self.Store())):
            view = fomo_bot.TrackedManagerView(1, self._entries(), 99)
            interaction = FakeInteraction()
            await view.selector.callback(interaction)
        self.assertEqual(interaction.response.deferred, 1)
        self.assertEqual(interaction.response.edits, [])

    async def test_another_user_cannot_operate_the_menu(self) -> None:
        import fomo_bot
        with patch.object(fomo_bot, "bot", self._bot(self.Store(), self.Store())):
            view = fomo_bot.TrackedManagerView(1, self._entries(), 99)
            interaction = FakeInteraction(user_id=2)
            self.assertFalse(await view.interaction_check(interaction))
        self.assertIn("Only the person who ran this command",
                      interaction.response.messages[0]["content"])

    async def test_only_the_first_25_can_be_selected(self) -> None:
        import fomo_bot
        entries = [("FOMO", {"userId": f"u{index}", "handle": f"h{index}"})
                   for index in range(40)]
        with patch.object(fomo_bot, "bot", self._bot(self.Store(), self.Store())):
            view = fomo_bot.TrackedManagerView(1, entries, 99)
        self.assertEqual(len(view.entries), 25)
        self.assertEqual(len(view.selector.options), 25)




class TokenCardRefreshTests(unittest.IsolatedAsyncioTestCase):
    """`/token`'s Refresh: what it rebuilds, and what it refuses to break."""

    def _view(self, refresh, pages: int = 5):
        import discord
        from fomo_bot import TokenCardView
        return TokenCardView(
            [discord.Embed(title=f"holders {index}") for index in range(pages)],
            refresh,
        )

    async def test_refresh_replaces_every_page(self) -> None:
        import discord
        calls = []

        async def refresh() -> list:
            calls.append(1)
            return [discord.Embed(title=f"fresh {index}") for index in range(5)]

        view = self._view(refresh)
        interaction = FakeInteraction()
        await view.refresh_button.callback(interaction)
        self.assertEqual(len(calls), 1)
        self.assertEqual(interaction.response.deferred, 1)
        self.assertEqual(view.embeds[0].title, "fresh 0")
        self.assertEqual(
            interaction.followup_edits[0]["embed"].title, "fresh 0"
        )

    async def test_the_reader_stays_on_the_page_they_were_reading(self) -> None:
        import discord

        async def refresh() -> list:
            return [discord.Embed(title=f"fresh {index}") for index in range(5)]

        view = self._view(refresh)
        interaction = FakeInteraction()
        await view.next_button.callback(interaction)
        await view.next_button.callback(interaction)
        self.assertEqual(view.index, 2)
        await view.refresh_button.callback(interaction)
        self.assertEqual(view.index, 2)
        self.assertEqual(view.embeds[2].title, "fresh 2")

    async def test_a_shorter_list_clamps_rather_than_crashing(self) -> None:
        import discord

        async def refresh() -> list:
            return [discord.Embed(title="fresh 0")]

        view = self._view(refresh)
        interaction = FakeInteraction()
        for _ in range(4):
            await view.next_button.callback(interaction)
        self.assertEqual(view.index, 4)
        await view.refresh_button.callback(interaction)
        self.assertEqual(view.index, 0)
        self.assertTrue(view.next_button.disabled)

    async def test_a_failed_refresh_leaves_the_card_standing(self) -> None:
        async def refresh() -> list:
            raise RuntimeError("provider down")

        view = self._view(refresh)
        interaction = FakeInteraction()
        await view.refresh_button.callback(interaction)
        self.assertEqual(view.embeds[0].title, "holders 0")
        self.assertTrue(interaction.followup.messages[0]["ephemeral"])
        self.assertIn("unchanged", interaction.followup.messages[0]["content"])

    async def test_an_empty_refresh_is_treated_as_a_failure(self) -> None:
        async def refresh() -> list:
            return []

        view = self._view(refresh)
        interaction = FakeInteraction()
        await view.refresh_button.callback(interaction)
        self.assertEqual(view.embeds[0].title, "holders 0")
        self.assertTrue(interaction.followup.messages)

    async def test_the_cooldown_stops_a_second_press(self) -> None:
        import discord
        calls = []

        async def refresh() -> list:
            calls.append(1)
            return [discord.Embed(title="fresh")]

        view = self._view(refresh)
        await view.refresh_button.callback(FakeInteraction())
        second = FakeInteraction()
        await view.refresh_button.callback(second)
        self.assertEqual(len(calls), 1)
        self.assertTrue(second.response.messages[0]["ephemeral"])
        self.assertEqual(second.response.deferred, 0)

    async def test_the_card_no_longer_offers_top_traders(self) -> None:
        import fomo_bot
        view = self._view(lambda: None)
        labels = {getattr(child, "label", "") for child in view.children}
        self.assertNotIn("Top Traders", labels)
        self.assertIn("Refresh", labels)
        for gone in ("_token_trader_embeds", "_trader_rows",
                     "_token_traders_embed", "_sample_window"):
            with self.subTest(attribute=gone):
                self.assertFalse(hasattr(fomo_bot, gone))


class ConnectedCardTests(unittest.IsolatedAsyncioTestCase):
    """`/connected` — the funding wallet, the list, and what an empty run says."""

    KNOWN = "KnownAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
    FRIEND = "FriendAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"
    FUNDER = "FunderAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA6"

    def _connection(self, **overrides):
        from connected_wallets import Connection, Relationship
        funder = overrides.pop("funder", False)
        record = Relationship(
            address=self.FRIEND, chain="solana", known_wallet=self.KNOWN,
            sent_count=24, received_count=19,
            sent_usd=120_000.0, received_usd=64_200.0,
            first_seen=1_735_689_600, last_seen=1_755_600_000,
            first_direction="out",
        )
        record.days = set(range(40))
        record.references = ["sig-a", "sig-b", "sig-c"]
        for key, value in overrides.items():
            setattr(record, key, value)
        return Connection(record, funder=funder)

    def _funding(self, **overrides):
        from connected_wallets import Funding
        base = dict(
            wallet=self.KNOWN, chain="solana", address=self.FUNDER,
            amount=0.05, symbol="SOL", timestamp=1_730_000_000,
            reference="fund-sig", exact=True,
        )
        base.update(overrides)
        return Funding(**base)

    def _report(self, connections=(), funding=(), **kwargs):
        from connected_wallets import ConnectedReport
        base = dict(
            wallets=((self.KNOWN, "solana"),),
            funding=tuple(funding), connections=tuple(connections),
            transactions=500, warnings=(), generated_at=0,
        )
        base.update(kwargs)
        return ConnectedReport(**base)

    def test_an_entry_carries_every_requested_field(self) -> None:
        from fomo_bot import _connected_entry
        name, value = _connected_entry(1, self._connection())
        self.assertIn("Solana", value)
        self.assertIn("Qualifying transfers: **43**", value)
        self.assertIn("Value moved:", value)
        self.assertIn("First:", value)
        self.assertIn("Latest:", value)
        self.assertTrue(name.startswith("1."))
        # No scores, no bands, no confidence language.
        self.assertNotIn("/100", value)
        for word in ("Very High", "Possible", "Evidence:"):
            self.assertNotIn(word, value)

    def test_a_known_identity_titles_the_entry(self) -> None:
        from fomo_bot import _connected_entry
        name, _value = _connected_entry(2, self._connection(identity="rowdy"))
        self.assertEqual(name, "2. @rowdy")

    def test_the_funder_is_marked_in_the_list(self) -> None:
        from fomo_bot import _connected_entry
        name, value = _connected_entry(1, self._connection(funder=True))
        self.assertIn("💰", name)
        self.assertIn("funded the analysed one", value)

    def test_the_funding_wallet_leads_the_first_page(self) -> None:
        from fomo_bot import _connected_embeds
        embeds = _connected_embeds(
            self._report([self._connection()], [self._funding()]), "@rowdy")
        first = embeds[0].fields[0]
        self.assertIn("Funding wallet", first.name)
        self.assertIn("0.05 SOL", first.value)
        self.assertIn("solscan.io/tx/fund-sig", first.value)

    def test_a_named_funder_reads_as_a_handle(self) -> None:
        from fomo_bot import _connected_embeds
        embeds = _connected_embeds(
            self._report([], [self._funding(identity="banker")]), "@rowdy")
        self.assertIn("@banker", embeds[0].fields[0].value)

    def test_a_labelled_funder_is_called_what_it_is(self) -> None:
        from fomo_bot import _connected_embeds
        embeds = _connected_embeds(
            self._report([], [self._funding(label="Binance")]), "@rowdy")
        self.assertIn("Binance", embeds[0].fields[0].value)

    def test_an_undetermined_funder_says_so(self) -> None:
        from fomo_bot import _connected_embeds
        embeds = _connected_embeds(self._report([self._connection()]), "@rowdy")
        self.assertIn("Not determined", embeds[0].fields[0].value)

    def test_every_page_says_what_the_bar_was(self) -> None:
        from fomo_bot import CONNECTED_RULE, _connected_embeds
        embeds = _connected_embeds(
            self._report([self._connection() for _ in range(9)]), "@rowdy")
        self.assertGreater(len(embeds), 1)
        self.assertTrue(all(CONNECTED_RULE in e.description for e in embeds))

    def test_every_page_repeats_what_was_excluded(self) -> None:
        from fomo_bot import CONNECTED_DISCLAIMER, _connected_embeds
        embeds = _connected_embeds(
            self._report([self._connection()]), "@rowdy")
        self.assertTrue(all(CONNECTED_DISCLAIMER in e.footer.text for e in embeds))
        self.assertIn("Swaps", CONNECTED_DISCLAIMER)

    def test_finding_nothing_names_the_reason(self) -> None:
        from fomo_bot import _connected_embeds
        embeds = _connected_embeds(self._report(), "@rowdy")
        self.assertEqual(len(embeds), 1)
        body = "".join(field.value for field in embeds[0].fields)
        self.assertIn("No wallet cleared the transfer bar", body)
        self.assertIn("Jupiter", body)

    def test_warnings_reach_the_card(self) -> None:
        from fomo_bot import _connected_embeds
        report = self._report(warnings=("solana: needs a Helius endpoint",))
        embeds = _connected_embeds(report, "@rowdy")
        self.assertIn(
            "Helius",
            "".join(field.value for field in embeds[0].fields),
        )

    def test_the_evidence_card_links_the_transactions(self) -> None:
        from fomo_bot import _connected_evidence_embed
        embed = _connected_evidence_embed(self._connection())
        body = "".join(field.value for field in embed.fields)
        self.assertIn("solscan.io/tx/sig-a", body)
        self.assertIn("separate dates", body)

    async def test_the_evidence_picker_lists_the_connections(self) -> None:
        import discord
        from fomo_bot import ConnectedView
        view = ConnectedView(self._report([self._connection()]), "@rowdy")
        selects = [child for child in view.children
                   if isinstance(child, discord.ui.Select)]
        self.assertEqual(len(selects), 1)
        self.assertEqual(selects[0].options[0].value,
                         f"solana:{self.FRIEND}"[:100])

    async def test_an_empty_run_still_renders_a_card(self) -> None:
        from fomo_bot import ConnectedView
        view = ConnectedView(self._report(funding=[self._funding()]), "@rowdy")
        self.assertEqual(len(view.embeds), 1)
        self.assertTrue(view.next_button.disabled)

    async def test_inspecting_a_wallet_answers_privately(self) -> None:
        from fomo_bot import ConnectedView
        view = ConnectedView(self._report([self._connection()]), "@rowdy")
        interaction = FakeInteraction()
        await view.show_evidence(interaction, f"solana:{self.FRIEND}"[:100])
        self.assertTrue(interaction.response.messages[0]["ephemeral"])
        self.assertIn("Evidence",
                      interaction.response.messages[0]["embed"].title)


class CommandSurfaceTests(unittest.IsolatedAsyncioTestCase):
    """The tree is the contract: a global sync deletes whatever is not here."""

    RETIRED = (
        "pumpwallet", "fomosearch", "fomotrack", "pumptrack", "fomotracked",
        "pumptracked", "fomountrack", "pumpuntrack", "untrack", "tracksettings",
    )

    def _names(self) -> set:
        import fomo_bot
        return {command.name for command in fomo_bot.bot.tree.get_commands()}

    def test_the_command_set_is_exactly_what_was_asked_for(self) -> None:
        self.assertEqual(
            self._names(),
            {"fomo", "pump", "wallet", "token", "thesis", "track", "tracked",
             "fomotop", "connected"},
        )

    def test_every_retired_command_is_gone(self) -> None:
        for name in self.RETIRED:
            with self.subTest(command=name):
                self.assertNotIn(name, self._names())

    def test_track_takes_a_platform_and_a_target(self) -> None:
        import fomo_bot
        command = fomo_bot.bot.tree.get_command("track")
        self.assertEqual([p.name for p in command.parameters], ["platform", "target"])
        self.assertTrue(all(p.required for p in command.parameters))
        self.assertEqual(
            [choice.value for choice in command.parameters[0].choices],
            ["fomo", "pump"],
        )

    def test_connected_takes_a_target_and_an_optional_fresh_flag(self) -> None:
        import fomo_bot
        command = fomo_bot.bot.tree.get_command("connected")
        names = [parameter.name for parameter in command.parameters]
        self.assertEqual(names, ["target", "fresh"])
        required = {p.name: p.required for p in command.parameters}
        self.assertTrue(required["target"])
        self.assertFalse(required["fresh"])

    def test_token_no_longer_offers_a_holder_count(self) -> None:
        import fomo_bot
        command = fomo_bot.bot.tree.get_command("token")
        self.assertEqual([p.name for p in command.parameters], ["address"])
        self.assertEqual(fomo_bot.TOKEN_HOLDER_LIMIT, 50)
        self.assertEqual(fomo_bot.TOKEN_HOLDER_PAGE, 10)

    async def test_track_dispatches_on_the_chosen_platform(self) -> None:
        import fomo_bot
        from discord import app_commands

        called: list[tuple[str, str]] = []

        async def fake_fomo(_interaction: object, handle: str) -> None:
            called.append(("fomo", handle))

        async def fake_pump(_interaction: object, handle: str) -> None:
            called.append(("pump", handle))

        command = fomo_bot.bot.tree.get_command("track")
        with patch.object(fomo_bot, "_track_fomo", fake_fomo), \
             patch.object(fomo_bot, "_track_pump", fake_pump):
            interaction = FakeInteraction()
            await command.callback(
                interaction, app_commands.Choice(name="Pump.fun", value="pump"), "zinc"
            )
            await command.callback(
                interaction, app_commands.Choice(name="FOMO", value="fomo"), "Binkieee"
            )
        self.assertEqual(called, [("pump", "zinc"), ("fomo", "Binkieee")])
        # Both halves expect an already-deferred interaction.
        self.assertEqual(interaction.response.deferred, 2)




class SolanaRouteOrderTests(unittest.IsolatedAsyncioTestCase):
    """`/fomo` runs the cheap published-position route before the scans.

    The enrichment budget is a wall clock that CANCELS what is still running,
    so a handle the expensive route cannot reach used to spend the whole
    budget proving it and never reach the cheap one.
    """

    class Recorder(BaseWalletResolver):
        def __init__(self, answers: dict) -> None:
            self.calls: list[str] = []
            self.answers = answers
            self.swaps_seen: list[object] = []

        async def resolve_from_holders(self, _fomo, _user, _balances, **kwargs):
            self.calls.append("holders")
            self.swaps_seen.append(kwargs.get("swaps"))
            return self.answers.get("holders")

        async def resolve(self, _fomo, _user):
            self.calls.append("transactions")
            return self.answers.get("transactions")

        async def resolve_from_balances(self, _user, _balances, **kwargs):
            self.calls.append("balances")
            self.swaps_seen.append(kwargs.get("swaps"))
            return self.answers.get("balances")

    async def _run(self, resolver: object, stats: TraderStats) -> Message:
        message = Message()
        await _enrich_fomo_message(
            Client(resolver, EvmResolver()),
            message, user("route-order-test"), stats, None, None, timeout=1,
        )
        return message

    def _stats(self) -> TraderStats:
        return TraderStats(
            raw_balances={"balances": []},
            raw_swaps={"swaps": [{"createdAt": "2026-08-18T13:05:59.531Z"}]},
        )

    async def test_the_holder_route_runs_first_and_stops_the_rest(self) -> None:
        resolver = self.Recorder({"holders": SOLANA_WALLET})
        message = await self._run(resolver, self._stats())
        self.assertEqual(resolver.calls, ["holders"])
        fields = {field.name: field.value for field in message.embeds[0].fields}
        self.assertIn(SOLANA_WALLET, fields["Solana wallet"])

    async def test_a_holder_miss_still_pays_for_the_transaction_routes(self) -> None:
        resolver = self.Recorder({"transactions": SOLANA_WALLET})
        await self._run(resolver, self._stats())
        self.assertEqual(resolver.calls, ["holders", "transactions"])

    async def test_the_balance_fingerprint_is_still_the_last_resort(self) -> None:
        resolver = self.Recorder({"balances": SOLANA_WALLET})
        await self._run(resolver, self._stats())
        self.assertEqual(resolver.calls, ["holders", "transactions", "balances"])

    async def test_both_derived_routes_are_handed_the_traders_swaps(self) -> None:
        # Without them the corroboration gate silently drops to the weaker
        # "has this wallet ever touched FOMO" check.
        resolver = self.Recorder({})
        stats = self._stats()
        await self._run(resolver, stats)
        self.assertEqual(resolver.swaps_seen, [stats.raw_swaps, stats.raw_swaps])

    async def test_a_trader_with_no_balances_skips_both_derived_routes(self) -> None:
        resolver = self.Recorder({"transactions": SOLANA_WALLET})
        await self._run(resolver, TraderStats())
        self.assertEqual(resolver.calls, ["transactions"])


class UnverifiedWalletCardTests(unittest.IsolatedAsyncioTestCase):
    """What `/fomo` shows when no route could corroborate a wallet.

    The pudgypenguins case: FOMO publishes the trader as a top holder, the
    chain names exactly one owner of that position, and no swap ties the two
    together. Showing "No verified wallets found." threw that owner away.
    """

    class CandidateResolver(BaseWalletResolver):
        """Answers nothing and offers `count` uncorroborated owners.

        `resolve()` returning None is the "FOMO returned no swaps" leg of the
        real log; the candidates arrive through the sink the derived routes
        take.
        """

        def __init__(self, addresses: Sequence[str] = (LIKELY_WALLET,)) -> None:
            self.addresses = tuple(addresses)
            self.calls: list[str] = []

        async def resolve_from_holders(self, _fomo, _user, _balances, **kwargs):
            self.calls.append("holders")
            sink = kwargs.get("candidates")
            if sink is not None:
                for index, address in enumerate(self.addresses):
                    sink.append(WalletCandidate(
                        address, ("hodlers+amount",),
                        ((SOL_MINT_1,) if index == 0 else (SOL_MINT_2,)),
                    ))
            return None

        async def resolve(self, _fomo, _user):
            self.calls.append("transactions")
            return None

        async def resolve_from_balances(self, _user, _balances, **kwargs):
            self.calls.append("balances")
            return None

    def _stats(self) -> TraderStats:
        return TraderStats(
            raw_balances={"balances": []},
            raw_swaps={"swaps": [{"createdAt": "2026-08-18T13:05:59.531Z"}]},
        )

    async def _card(self, resolver: object, layout: str = "compact") -> Message:
        message = Message()
        await _enrich_fomo_message(
            Client(resolver), message, user("pudgypenguins"), self._stats(),
            None, None, timeout=1, layout=layout, wallets_pending=True,
        )
        return message

    # ---------------------------------------------------------- rendering

    def test_a_verified_wallet_is_never_downgraded_or_warned_about(self) -> None:
        embed = build_compact_embed(
            user(), SOLANA_WALLET, EVM_WALLET, TraderStats(),
            unverified_wallets=(WalletCandidate(LIKELY_WALLET),),
        )
        wallets = {field.name: field.value for field in embed.fields}["Linked wallets"]
        self.assertIn(SOLANA_WALLET, wallets)
        self.assertNotIn(LIKELY_WALLET, wallets)
        self.assertNotIn("⚠️", wallets)

    def test_one_candidate_is_shown_copyable_with_the_singular_warning(self) -> None:
        embed = build_compact_embed(
            user(), None, None, TraderStats(),
            unverified_wallets=(WalletCandidate(LIKELY_WALLET, ("hodlers+amount",)),),
        )
        wallets = {field.name: field.value for field in embed.fields}["Linked wallets"]
        # Same inline-code row a linked wallet gets, so it stays selectable.
        self.assertIn(f"◎ **Solana** · `{LIKELY_WALLET}`", wallets)
        self.assertIn(UNVERIFIED_WALLET_NOTE, wallets)
        self.assertNotIn("No verified wallets found.", wallets)

    def test_several_candidates_are_all_shown_under_the_plural_warning(self) -> None:
        embed = build_compact_embed(
            user(), None, None, TraderStats(),
            unverified_wallets=(
                WalletCandidate(LIKELY_WALLET),
                WalletCandidate(OTHER_LIKELY_WALLET),
            ),
        )
        wallets = {field.name: field.value for field in embed.fields}["Linked wallets"]
        self.assertIn(LIKELY_WALLET, wallets)
        self.assertIn(OTHER_LIKELY_WALLET, wallets)
        self.assertIn(UNVERIFIED_WALLETS_NOTE, wallets)

    def test_a_verified_evm_wallet_keeps_its_row_beside_a_candidate(self) -> None:
        embed = build_compact_embed(
            user(), None, EVM_WALLET, TraderStats(),
            unverified_wallets=(WalletCandidate(LIKELY_WALLET),),
        )
        wallets = {field.name: field.value for field in embed.fields}["Linked wallets"]
        self.assertIn(f"Ξ **EVM** · `{EVM_WALLET}`", wallets)
        self.assertIn(LIKELY_WALLET, wallets)
        self.assertIn(UNVERIFIED_WALLET_NOTE, wallets)

    def test_no_candidate_keeps_the_existing_empty_state(self) -> None:
        embed = build_compact_embed(user(), None, None, TraderStats())
        wallets = {field.name: field.value for field in embed.fields}["Linked wallets"]
        self.assertEqual(wallets, "No verified wallets found.")

    def test_the_wide_layout_gets_its_own_clearly_named_field(self) -> None:
        embed = build_embed(
            user(), None, None, TraderStats(),
            unverified_wallets=(WalletCandidate(LIKELY_WALLET),),
        )
        fields = {field.name: field.value for field in embed.fields}
        self.assertNotIn("Solana wallet", fields)
        self.assertIn(LIKELY_WALLET, fields["Solana wallet · unverified"])
        self.assertIn(UNVERIFIED_WALLET_NOTE, fields["Solana wallet · unverified"])

    def test_the_wide_layout_leaves_a_verified_wallet_exactly_as_it_was(self) -> None:
        embed = build_embed(
            user(), SOLANA_WALLET, None, TraderStats(),
            unverified_wallets=(WalletCandidate(LIKELY_WALLET),),
        )
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["Solana wallet"], f"◎ `{SOLANA_WALLET}`")
        self.assertNotIn("Solana wallet · unverified", fields)

    # ------------------------------------------------------ the decision

    async def test_the_pudgypenguins_path_shows_the_single_likely_wallet(self) -> None:
        resolver = self.CandidateResolver()
        message = await self._card(resolver)
        # Every route still ran, and none of them returned a wallet.
        self.assertEqual(resolver.calls, ["holders", "transactions", "balances"])
        wallets = {f.name: f.value for f in message.embeds[0].fields}["Linked wallets"]
        self.assertIn(LIKELY_WALLET, wallets)
        self.assertIn(UNVERIFIED_WALLET_NOTE, wallets)

    async def test_two_tied_candidates_are_both_offered_rather_than_guessed(self) -> None:
        message = await self._card(
            self.CandidateResolver((LIKELY_WALLET, OTHER_LIKELY_WALLET))
        )
        wallets = {f.name: f.value for f in message.embeds[0].fields}["Linked wallets"]
        self.assertIn(LIKELY_WALLET, wallets)
        self.assertIn(OTHER_LIKELY_WALLET, wallets)
        self.assertIn(UNVERIFIED_WALLETS_NOTE, wallets)

    async def test_a_verified_wallet_suppresses_the_candidates_entirely(self) -> None:
        class Verified(self.CandidateResolver):
            async def resolve(self, _fomo, _user):
                self.calls.append("transactions")
                return SOLANA_WALLET

        message = await self._card(Verified())
        wallets = {f.name: f.value for f in message.embeds[0].fields}["Linked wallets"]
        self.assertIn(SOLANA_WALLET, wallets)
        self.assertNotIn(LIKELY_WALLET, wallets)
        self.assertNotIn("⚠️", wallets)

    async def test_no_candidate_anywhere_still_says_no_verified_wallets(self) -> None:
        message = await self._card(self.CandidateResolver(()))
        wallets = {f.name: f.value for f in message.embeds[0].fields}["Linked wallets"]
        self.assertEqual(wallets, "No verified wallets found.")

    async def test_the_fallback_says_so_in_the_log(self) -> None:
        # The one line that separates "we found nothing" from "we found
        # something we cannot prove" when reading a live log.
        with self.assertLogs("fomobot", level="INFO") as captured:
            await self._card(self.CandidateResolver())
        self.assertTrue(
            any(f"using unverified fallback wallet for pudgypenguins: "
                f"{LIKELY_WALLET}" in line for line in captured.output),
            captured.output,
        )

    async def test_a_verified_wallet_logs_no_fallback(self) -> None:
        class Verified(self.CandidateResolver):
            async def resolve(self, _fomo, _user):
                self.calls.append("transactions")
                return SOLANA_WALLET

        with self.assertLogs("fomobot", level="INFO") as captured:
            logging.getLogger("fomobot").info("probe")
            await self._card(Verified())
        self.assertFalse(any("unverified fallback" in line
                             for line in captured.output), captured.output)



if __name__ == "__main__":
    unittest.main()
