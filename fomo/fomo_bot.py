"""
fomo_bot.py — standalone Discord bot: FOMO trader research.

    /fomo <handle>          choose a Compact or Wide fomo.family profile
    /pump <handle>          rich Pump.fun profile
    /wallet <address>       find a FOMO or Pump profile by wallet
    /token <address>        market cap and the top 50 holders, refreshable
    /thesis <address>       what this token's biggest holders wrote about it
    /connected <target>     the funding wallet and the wallets a trader
                            actually moves money to and from
    /track <platform> <who> choose and track FOMO or Pump activity
    /tracked                everything tracked here, with Edit and Remove
    /fomotop [24h] [n]      leaderboard

Run this on borz (residential IP). Cloudflare blocks the VPS — see FOMO_API.md §1.

    py -3 -m venv .venv && .venv\\Scripts\\activate
    pip install -r requirements.txt
    copy .env.example .env    # fill in the two tokens
    python fomo_bot.py

NOTE: this needs real `discord.py`, not the `discord.py-self` the main memebot
uses — they both install a package called `discord` and will clobber each
other. Keep this folder on its own venv.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import discord
from discord import app_commands
from dotenv import load_dotenv

# Wallet modules read their RPC/cache settings at import time.
load_dotenv()

from fomo_evm import (
    EvmWalletResolver,
    cached_evm_wallet,
    evm_trade_evidence,
    evm_trade_ids,
)
from fomo_evm_activity import fetch_evm_activity
from connected_wallets import (
    DEFAULT_EVM_CHAINS,
    MIN_EVM_USD,
    MIN_SOL,
    MIN_STABLE,
    ConnectedReport,
    ConnectedWalletAnalyzer,
    Connection,
    Funding,
    address_url,
    explorer_url,
    fmt_day,
)
from fomo_hodlers import (
    FomoHolder,
    HolderThesis,
    confident_matches,
    match_holders_to_wallets,
    network_id_for,
    parse_thesis_feed,
    parse_token_holders,
    rank_theses,
    theses_from_trades,
)
from fomo_features import (
    TraderStats,
    fetch_trader_stats,
    fmt_price,
    iso_to_datetime,
    iso_to_unix,
    merge_latest_buys,
    merge_latest_sells,
)
from fomo_tracking import (
    TrackEvent,
    TrackingStore,
    activity_allowed,
    activity_filter_label,
    chain_name,
    detect_events,
    fmt_native_amount,
    native_currency,
    native_value_from_usd,
    padre_trade_url,
    normalize_activity_filters,
    snapshot,
)
from fomo_wallet import (
    SOLANA_ADDRESS_RE,
    CachedWalletMatch,
    WalletResolver,
    cached_wallet,
    find_cached_wallets,
)
from fomo_api import (
    FomoBlocked,
    FomoClient,
    FomoError,
    FomoNotFound,
    FomoUser,
    fmt_count,
    fmt_duration,
    fmt_usd,
)
from pump_api import (
    PumpCallout,
    PumpClient,
    PumpCoin,
    PumpError,
    PumpHolding,
    PumpPortfolio,
    PumpUser,
    pump_profile_url,
    quote_value_sol,
    quote_value_usd,
)
from pump_chain import PumpChainClient, PumpRpcError
from pump_evm import EVM_RE, PumpEvmMatch, PumpEvmResolver
from pump_profiles import (
    CARD_TTL as PUMP_CARD_TTL,
    PumpProfile,
    PumpProfileResolver,
    UNSUPPORTED as PUMP_UNSUPPORTED,
)
from pump_tracking import (
    PumpAlert,
    PumpTrackingStore,
    callout_alert,
    new_callouts,
    pump_snapshot,
    trade_alert,
)
from rpc_config import env_rpc_urls
from token_intelligence import (
    TokenHolder,
    TokenIntelligence,
    TokenIntelligenceClient,
    TokenIntelligenceError,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
# httpx logs complete request URLs at INFO, including API keys embedded in RPC
# query strings. Keep network request lines quiet; our own diagnostics redact
# endpoints and still report actionable failures.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("fomobot")


LOOP_LAG_WARN = float(os.getenv("FOMO_LOOP_LAG_WARN", "1.0"))

SINGLE_INSTANCE_PORT = int(os.getenv("FOMO_LOCK_PORT", "47821"))
_instance_lock: Any = None


def claim_single_instance() -> bool:
    """Refuse to start beside another copy of this bot.

    Two processes on one bot token both hold a gateway session, Discord hands
    an interaction to one of them, and whichever did not get it fails its
    acknowledgement with 10062 -- no stalled loop, no slow network, just a
    stale process nobody noticed surviving a restart. A bound socket is the
    cheapest way to know: the OS drops it the moment the owner dies, so unlike
    a pid file it cannot go stale.
    """
    global _instance_lock
    if os.getenv("FOMO_SINGLE_INSTANCE", "1").strip() in ("0", "false", "no"):
        return True
    import socket

    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        sock.listen(1)
    except OSError:
        sock.close()
        return False
    _instance_lock = sock
    return True


async def _loop_watchdog(interval: float = 0.25) -> None:
    """Name whatever is holding the event loop.

    A 10062 says only that the loop was busy for three seconds; it never says
    doing what. This sleeps in a tight cycle and measures how late it wakes --
    the overshoot *is* the block -- then prints the tasks that were runnable at
    the time, which is the shortlist of what caused it.

    Set FOMO_LOOP_DEBUG=1 to also turn on asyncio's own slow-callback warning,
    which names the exact coroutine. It costs real overhead, so it is opt-in.
    """
    if os.getenv("FOMO_LOOP_DEBUG", "").strip() in ("1", "true", "yes"):
        loop = asyncio.get_running_loop()
        loop.slow_callback_duration = LOOP_LAG_WARN
        loop.set_debug(True)
        logging.getLogger("asyncio").setLevel(logging.WARNING)
        log.info("loop debug on: callbacks over %.2fs will be named", LOOP_LAG_WARN)

    while True:
        before = time.monotonic()
        await asyncio.sleep(interval)
        lag = time.monotonic() - before - interval
        if lag < LOOP_LAG_WARN:
            continue
        busy = []
        for task in asyncio.all_tasks():
            if task.done():
                continue
            coro = task.get_coro()
            name = getattr(coro, "__qualname__", None) or task.get_name()
            frame = getattr(coro, "cr_frame", None)
            where = ""
            if frame is not None:
                where = f" at {os.path.basename(frame.f_code.co_filename)}:{frame.f_lineno}"
            busy.append(f"{name}{where}")
        log.warning(
            "event loop stalled %.2fs (Discord allows 3s to acknowledge a "
            "command). %d task(s) alive: %s",
            lag, len(busy), "; ".join(sorted(set(busy))[:12]) or "none",
        )


async def _safe_defer(interaction: discord.Interaction, **kwargs: Any) -> bool:
    """Acknowledge the command, or say precisely why the chance was missed.

    Discord expires an interaction token three seconds after *it* issues the
    token -- not three seconds after we see it. So a 10062 has two entirely
    different causes and the traceback distinguishes neither:

    * the event reached us late, and the clock had already run down before
      this handler was even scheduled -- a gateway or network problem;
    * the event arrived in good time and our acknowledgement was what took
      too long -- a REST problem, usually the same network.

    The interaction's snowflake carries its creation time, so both are
    measurable. `_loop_watchdog` covers the third possibility, a stalled loop,
    and reports separately. Raising here would only turn one lost click into a
    two-deep `CommandInvokeError`, so report and let the caller give up.
    """
    name = getattr(interaction.command, "name", "?")
    born = interaction.created_at
    age = (discord.utils.utcnow() - born).total_seconds()
    try:
        await interaction.response.defer(**kwargs)
        if age > 1.5:
            log.warning(
                "/%s was already %.2fs old when it reached us (gateway "
                "latency %.0fms) -- close to Discord's three-second limit.",
                name, age, bot.latency * 1000,
            )
        return True
    except discord.NotFound:
        elapsed = (discord.utils.utcnow() - born).total_seconds()
        if age > 2.5:
            blame = ("the event reached us with the clock already run down, so "
                     "the delay is upstream of the bot -- gateway connection, "
                     "network, or a second bot process holding the session")
        else:
            blame = ("the event arrived in time and the acknowledgement itself "
                     "was too slow -- the REST call to Discord, not our code")
        log.warning(
            "/%s from %s expired (10062): %.2fs old on arrival, %.2fs when "
            "refused, gateway latency %.0fms. Reading: %s.",
            name, interaction.user, age, elapsed, bot.latency * 1000, blame,
        )
        return False
    except discord.HTTPException as exc:
        log.warning("could not acknowledge /%s: %s", name, exc)
        return False

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
REFRESH_TOKEN = os.getenv("FOMO_PRIVY_REFRESH_TOKEN", "")
ACCESS_TOKEN = os.getenv("FOMO_PRIVY_ACCESS_TOKEN", "")
# Derive each trader's real wallet on chain. Needs SOLANA_RPC + httpx.
RESOLVE_WALLETS = os.getenv("FOMO_RESOLVE_WALLETS", "1").strip() not in ("0", "false", "no")
# Write wallet -> handle pairs discovered from FOMO's own holder list into the
# wallet cache, so /fomo and /wallet know them without an on-chain scan.
ADOPT_HOLDER_WALLETS = os.getenv("FOMO_ADOPT_HOLDER_WALLETS", "1").strip() not in (
    "0", "false", "no",
)
RESOLVE_EVM = os.getenv("FOMO_RESOLVE_EVM", "1").strip() not in ("0", "false", "no")
TRACK_FILE = Path(os.getenv("FOMO_TRACK_FILE", "fomo_tracks.json"))
FOMO_TRACK_INTERVAL = max(1.0, float(os.getenv("FOMO_TRACK_INTERVAL", "60")))
LARGE_SWAP_USD = max(0.0, float(os.getenv("FOMO_LARGE_SWAP_USD", "1000")))
FOMO_ENRICH_TIMEOUT = max(
    1.0, float(os.getenv("FOMO_ENRICH_TIMEOUT", "20"))
)
PUMP_TRACK_FILE = Path(os.getenv("PUMP_TRACK_FILE", "pump_tracks.json"))
PUMP_TRACK_INTERVAL = max(
    0.25,
    float(os.getenv("PUMP_TRACK_INTERVAL", os.getenv("FOMO_TRACK_INTERVAL", "60"))),
)
PUMP_EVM_CACHE_FILE = Path(os.getenv("PUMP_EVM_CACHE_FILE", "pump_evm_cache.json"))
PUMP_PROFILE_CACHE_FILE = Path(
    os.getenv("PUMP_PROFILE_CACHE_FILE", "pump_profile_cache.json")
)
PUMP_MIN_TRADE_USD = max(0.0, float(os.getenv("PUMP_MIN_TRADE_USD", "0")))
SOLANA_RPCS = env_rpc_urls(
    "SOLANA_RPC",
    "SOLANA_RPC_FALLBACKS",
    "https://api.mainnet-beta.solana.com",
)

BRAND = 0x4F5EFF  # fomo blue
LOSS = 0xE5484D
WIN = 0x30A46C
THESIS = 0x9B59B6

# `/token` always reads the top 50 and shows ten a page; `/thesis` shows five,
# which is what fits with the thesis text itself under the embed's limits.
TOKEN_HOLDER_LIMIT = 50
TOKEN_HOLDER_PAGE = 10
# A `/token` card may be refreshed in place. The floor is there because the
# rebuild costs a holder query plus identity lookups, and a button anybody in
# the channel can press should not be able to spend those in a loop.
TOKEN_REFRESH_COOLDOWN = 15.0
THESIS_PAGE = 5
THESIS_TEXT_LIMIT = 400

FOMO_ACTIVITY_FILTERS = ("buys", "sells", "theses")
PUMP_ACTIVITY_FILTERS = ("buys", "sells", "callouts")
NATIVE_PRICE_REFERENCES = {
    "SOL": ("Solana", "So11111111111111111111111111111111111111112"),
    "ETH": ("Ethereum", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
    "BNB": ("BSC", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
}


def _entry_activity_filters(entry: dict[str, Any]) -> Any:
    """Read the current list format, falling back to legacy single choices."""
    return entry.get("activityFilters", entry.get("activityFilter", "all"))


class PaginatedEmbedView(discord.ui.View):
    """Previous / Next over a fixed list of already-rendered embeds.

    Both `/token` and `/thesis` render every page before the first one is
    sent. The expensive part -- holder identity, thesis text -- is paid once
    for the whole card, so turning a page is a message edit and nothing else,
    and a page can never disagree with the one before it.

    Unlike the selection views, this one has no requester check: paging carries
    no state and changes nothing, so anyone reading the channel may do it.

    The timeout is an hour rather than the usual few minutes because a card
    that stops paging reports only "interaction failed", and these views hold
    nothing but their own embeds.
    """

    def __init__(self, embeds: list[discord.Embed], *, timeout: float = 3600) -> None:
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.index = 0
        self._sync()

    def _sync(self) -> None:
        self.previous_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(self.embeds) - 1

    async def show(self, interaction: discord.Interaction, index: int) -> None:
        self.index = max(0, min(index, len(self.embeds) - 1))
        self._sync()
        await interaction.response.edit_message(
            embed=self.embeds[self.index], view=self
        )

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self.show(interaction, self.index - 1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self.show(interaction, self.index + 1)


class TokenCardView(PaginatedEmbedView):
    """`/token`'s pager, plus a Refresh that rebuilds the card in place.

    Every holder page is rendered before the card is sent, so turning one is a
    message edit and two pages can never disagree. Refresh re-runs exactly the
    work `/token` did -- the holder query and the identity labelling -- and
    swaps the whole set of pages for the new one, keeping the reader on the
    page they were looking at.

    The rebuild costs provider requests, so it is serialised behind a lock and
    floored by a cooldown: anybody in the channel may press it, but not twice
    in the same breath.
    """

    def __init__(
        self,
        holder_embeds: list[discord.Embed],
        refresh: Callable[[], Awaitable[list[discord.Embed]]],
        *,
        timeout: float = 3600,
    ) -> None:
        self._refresh = refresh
        self._refreshing = asyncio.Lock()
        self._last_refresh = 0.0
        super().__init__(holder_embeds, timeout=timeout)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """Re-read the holders and re-render every page.

        A failed rebuild leaves the card exactly as it was and says so
        privately: stale holders are a better answer than an error card
        replacing data that is still perfectly readable.
        """
        waited = time.monotonic() - self._last_refresh
        if waited < TOKEN_REFRESH_COOLDOWN:
            await interaction.response.send_message(
                f"Just refreshed — try again in "
                f"{TOKEN_REFRESH_COOLDOWN - waited:.0f}s.",
                ephemeral=True,
            )
            return
        if self._refreshing.locked():
            await interaction.response.send_message(
                "A refresh is already running.", ephemeral=True
            )
            return
        # The holder query can outlast Discord's three-second reply budget --
        # acknowledge first, then edit.
        try:
            if not await _safe_defer(interaction):
                return
        except discord.HTTPException as exc:
            log.debug("could not defer the /token refresh: %s", exc)
            return
        async with self._refreshing:
            try:
                embeds = await self._refresh()
            except Exception:
                log.exception("refreshing the /token card failed")
                embeds = []
            self._last_refresh = time.monotonic()
        if not embeds:
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "The refresh could not read the holders, so the card is "
                    "unchanged.", ephemeral=True,
                )
            return
        self.embeds = embeds
        self.index = min(self.index, len(self.embeds) - 1)
        self._sync()
        try:
            await interaction.edit_original_response(
                embed=self.embeds[self.index], view=self
            )
        except discord.HTTPException as exc:
            # An expired or already-answered interaction is the normal end of
            # a card's life, not a failure worth an error card.
            log.debug("could not redraw the refreshed /token card: %s", exc)


class ActivityMultiSelect(discord.ui.Select):
    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, ActivitySelectionView):
            await view.submit(interaction, list(self.values))


class ActivitySelectionView(discord.ui.View):
    """Discord multi-select used to configure one tracking subscription."""

    def __init__(
        self,
        requester_id: int,
        options: tuple[discord.SelectOption, ...],
        on_submit: Callable[[discord.Interaction, list[str]], Awaitable[str]],
    ) -> None:
        super().__init__(timeout=180)
        self.requester_id = requester_id
        self.on_submit = on_submit
        self.selector = ActivityMultiSelect(
            placeholder="Select 1, 2, or all 3 alert types",
            min_values=1,
            max_values=len(options),
            options=list(options),
        )
        self.add_item(self.selector)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who ran this command can choose its alerts.",
            ephemeral=True,
        )
        return False

    async def submit(
        self, interaction: discord.Interaction, selected: list[str]
    ) -> None:
        message = await self.on_submit(interaction, selected)
        self.stop()
        await interaction.response.edit_message(content=message, view=None)


class TrackedManagerSelect(discord.ui.Select):
    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, TrackedManagerView):
            await view.choose(interaction, [int(value) for value in self.values])


class TrackedManagerView(discord.ui.View):
    """`/tracked`'s selector plus the buttons that replaced two commands.

    `/tracksettings` and `/untrack` each opened the same list of subscriptions
    and differed only in what they did with the choice, so the list is shown
    once and the verb is a button: **Edit** opens the alert picker for one
    subscription, **Remove** deletes every selected one.

    The select's callback deliberately only defers. Discord keeps a select's
    visible choice until the message is edited, so acknowledging without
    editing is what lets the buttons read a selection the user can still see.
    """

    def __init__(
        self,
        requester_id: int,
        entries: list[tuple[str, dict[str, Any]]],
        channel_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.channel_id = channel_id
        self.entries = entries[:25]
        self.selected: list[int] = []
        options = [
            discord.SelectOption(
                label=f"{platform} · @{entry.get('handle', 'unknown')}"[:100],
                value=str(index),
                description=activity_filter_label(_entry_activity_filters(entry))[:100],
                emoji="🔵" if platform == "FOMO" else "🟢",
            )
            for index, (platform, entry) in enumerate(self.entries)
        ]
        self.selector = TrackedManagerSelect(
            placeholder="Select tracked profiles, then Edit or Remove",
            min_values=1,
            max_values=len(options),
            options=options,
            row=0,
        )
        self.add_item(self.selector)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who ran this command can use this menu.",
            ephemeral=True,
        )
        return False

    def chosen(self) -> list[tuple[str, dict[str, Any]]]:
        return [self.entries[index] for index in self.selected
                if 0 <= index < len(self.entries)]

    async def choose(
        self, interaction: discord.Interaction, indexes: list[int]
    ) -> None:
        self.selected = indexes
        if not await _safe_defer(interaction):
            return

    @discord.ui.button(
        label="Edit", style=discord.ButtonStyle.primary, emoji="✏️", row=1
    )
    async def edit_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        chosen = self.chosen()
        if len(chosen) != 1:
            await interaction.response.send_message(
                "Select one profile to edit."
                if not chosen else
                "Edit changes one subscription at a time — select just one.",
                ephemeral=True,
            )
            return
        platform, entry = chosen[0]
        handle = str(entry.get("handle") or "unknown")
        self.stop()
        await interaction.response.edit_message(
            content=f"Choose the alerts for {platform} **@{handle}** (select 1–3):",
            embed=None,
            view=_alert_settings_view(
                self.requester_id, self.channel_id, platform, entry
            ),
        )

    @discord.ui.button(
        label="Remove", style=discord.ButtonStyle.danger, emoji="🗑️", row=1
    )
    async def remove_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        chosen = self.chosen()
        if not chosen:
            await interaction.response.send_message(
                "Select at least one profile to remove.", ephemeral=True
            )
            return
        removed = _remove_tracked_entries(self.channel_id, chosen)
        self.stop()
        await interaction.response.edit_message(
            content=("Stopped tracking " + ", ".join(removed) + "."
                     if removed else "No subscriptions were removed."),
            embed=None,
            view=None,
        )


class FomoLayoutSelect(discord.ui.Select):
    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, FomoLayoutSelectionView):
            await view.submit(interaction, self.values[0])


class FomoLayoutSelectionView(discord.ui.View):
    """Choose how one `/fomo` profile is rendered before generating it."""

    def __init__(
        self,
        requester_id: int,
        handle: str,
        on_submit: Callable[[discord.Interaction, str, str], Awaitable[bool]],
    ) -> None:
        super().__init__(timeout=180)
        self.requester_id = requester_id
        self.handle = handle
        self.on_submit = on_submit
        self.selector = FomoLayoutSelect(
            placeholder="Choose profile layout: Compact or Wide",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Compact",
                    value="compact",
                    description="Essential profile information only.",
                    emoji="📇",
                ),
                discord.SelectOption(
                    label="Wide",
                    value="wide",
                    description="Full profile with all available information.",
                    emoji="📊",
                ),
            ],
        )
        self.add_item(self.selector)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who ran this command can choose its layout.",
            ephemeral=True,
        )
        return False

    async def submit(self, interaction: discord.Interaction, layout: str) -> None:
        self.stop()
        label = "Compact" if layout == "compact" else "Wide"
        await interaction.response.edit_message(
            content=f"Generating the **{label}** profile for **@{self.handle.lstrip('@')}**…",
            view=None,
        )
        generated = await self.on_submit(interaction, self.handle, layout)
        status = "Generated" if generated else "Could not generate"
        with contextlib.suppress(discord.HTTPException):
            await interaction.edit_original_response(
                content=(
                    f"{status} the **{label}** profile for "
                    f"**@{self.handle.lstrip('@')}**."
                ),
                view=None,
            )


def _activity_options(
    labels: tuple[tuple[str, str, str], ...],
    selected: tuple[str, ...] = (),
) -> tuple[discord.SelectOption, ...]:
    return tuple(
        discord.SelectOption(
            label=label,
            value=value,
            emoji=emoji,
            default=value in selected,
        )
        for value, label, emoji in labels
    )


def _rank_line(user: FomoUser, period: str, label: str) -> str | None:
    block = user.rank(period)
    if not block:
        return None
    pnl = block.get("pnl")
    rank = block.get("rank")
    arrow = "🟢" if (pnl or 0) >= 0 else "🔴"
    rank_txt = f"#{rank:,}" if isinstance(rank, int) else "—"
    return f"{arrow} **{label}** {fmt_usd(pnl)}  ·  {rank_txt}"


def _fit_field(lines: list[str], limit: int = 1024) -> str:
    """Keep an embed field inside Discord's 1,024-character limit.

    Long Solana mints inside Padre links make these rows wide, and Discord
    rejects the whole message rather than truncating the field.
    """
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        more = len(lines) - index
        tail = f"\n… +{more} more" if more else ""
        if used + len(line) + (1 if kept else 0) + len(tail) > limit:
            kept.append(f"… +{more} more")
            break
        used += len(line) + (1 if kept else 0)
        kept.append(line)
    return "\n".join(kept)


def _token_link(network_id: Any, token_address: str, symbol: str) -> str:
    """`$TICKER` linked to Padre, or bold when Padre has no route for the chain."""
    label = f"${(symbol or '').lstrip('$') or 'TOKEN'}"
    trade_url = padre_trade_url(network_id, token_address or "")
    return f"[{label}]({trade_url})" if trade_url else f"**{label}**"


def build_embed(user: FomoUser, wallet: str | None = None,
                evm_wallet: str | None = None,
                stats: TraderStats | None = None,
                activity_filter: str = "all") -> discord.Embed:
    stats = stats or TraderStats()
    all_time = user.rank("")
    pnl = (all_time or {}).get("pnl")
    colour = BRAND if pnl is None else (WIN if pnl >= 0 else LOSS)

    title = user.display_name
    if user.clan_name:
        title += f"  ·  {user.clan_name}"

    embed = discord.Embed(
        title=title,
        url=user.profile_url,
        description=user.description or None,
        colour=colour,
    )
    embed.set_author(name=f"@{user.handle}", url=user.profile_url)
    if user.avatar:
        embed.set_thumbnail(url=user.avatar)

    embed.add_field(
        name="Social",
        value=(
            f"**{fmt_count(user.followers)}** followers\n"
            f"**{fmt_count(user.following)}** following"
        ),
        inline=True,
    )
    embed.add_field(
        name="Strategy",
        value=(
            f"**{fmt_usd(user.total_volume)}** volume\n"
            f"**{fmt_duration(user.avg_hold_seconds)}** avg hold"
        ),
        inline=True,
    )
    embed.add_field(
        name="Portfolio",
        value=f"**{fmt_usd(stats.portfolio_value)}** current value",
        inline=True,
    )

    ranks = [
        line
        for line in (
            _rank_line(user, "24h", "24h"),
            _rank_line(user, "7d", "7d"),
            _rank_line(user, "30d", "30d"),
            _rank_line(user, "", "All-time"),
        )
        if line
    ]
    if stats.best_trade:
        roi = f" · {stats.best_trade.roi:+.1f}% ROI" if stats.best_trade.roi is not None else ""
        embed.add_field(
            name="Best trade",
            value=f"**{stats.best_trade.symbol}** · {fmt_usd(stats.best_trade.pnl)}{roi}",
            inline=True,
        )
    show_buys = activity_filter in ("all", "buys")
    show_sells = activity_filter in ("all", "sells")
    show_theses = activity_filter in ("all", "theses")
    if show_buys and stats.latest_buys:
        lines = []
        for activity in stats.latest_buys:
            when = iso_to_unix(activity.created_at)
            relative = f" · <t:{when}:R>" if when is not None else ""
            amount = f" · {fmt_usd(activity.usd_value)}" if activity.usd_value is not None else ""
            if activity.market_cap is None:
                market_cap = " · MC —"
            else:
                estimate = "~" if activity.market_cap_estimated else ""
                market_cap = f" · MC {estimate}{fmt_usd(activity.market_cap)}"
            chain = f" · {activity.chain}" if activity.chain else ""
            # Same treatment as sells: a green marker instead of an ordinal,
            # and a ticker that opens the token on Padre.
            lines.append(
                f"🟢 {_token_link(activity.chain, activity.token_address, activity.symbol)}"
                f"{amount}{market_cap}{chain}{relative}"
            )
        embed.add_field(
            name="Latest buys",
            value="\n".join(lines),
            inline=False,
        )
    elif activity_filter == "buys":
        embed.add_field(name="Latest buys", value="No recent buys found.", inline=False)

    if show_sells and stats.latest_sells:
        lines = []
        for event in stats.latest_sells:
            when = iso_to_unix(event.created_at)
            relative = f" · <t:{when}:R>" if when is not None else ""
            amount = f" · {fmt_usd(event.usd_value)}" if event.usd_value is not None else ""
            chain = chain_name(event.network_id)
            token = _token_link(event.network_id, event.token_address, event.symbol)
            lines.append(f"🔴 {token}{amount} · {chain}{relative}")
        embed.add_field(name="Latest sells", value="\n".join(lines), inline=False)
    elif activity_filter == "sells":
        embed.add_field(name="Latest sells", value="No recent sells found.", inline=False)

    if stats.open_positions:
        lines = []
        for position in stats.open_positions:
            token = _token_link(position.network_id, position.token_address,
                                position.symbol)
            entry = f" · entry {fmt_price(position.entry_price)}"
            size = f" · {fmt_count(int(position.amount))} · {fmt_usd(position.value_usd)}"
            if position.pnl_usd is None:
                marker, pnl = "⚪", " · PnL —"
            else:
                marker = "🟢" if position.pnl_usd >= 0 else "🔴"
                sign = "+" if position.pnl_usd >= 0 else ""
                roi = f" ({position.roi:+.1f}%)" if position.roi is not None else ""
                pnl = f" · **{sign}{fmt_usd(position.pnl_usd)}**{roi}"
            lines.append(f"{marker} {token}{entry}{size}{pnl}")
        embed.add_field(name="Open positions", value=_fit_field(lines), inline=False)

    if show_theses and stats.latest_theses:
        lines = []
        for event in stats.latest_theses:
            when = iso_to_unix(event.created_at)
            relative = f" · <t:{when}:R>" if when is not None else ""
            trade_url = padre_trade_url(event.network_id, event.token_address)
            label = f"${event.symbol.lstrip('$') or 'TOKEN'}"
            token = f"[{label}]({trade_url})" if trade_url else f"**{label}**"
            detail = (event.detail or "New thesis posted")[:220]
            lines.append(f"📝 {token}{relative}\n> {detail}")
        embed.add_field(name="Latest theses", value="\n".join(lines), inline=False)
    elif activity_filter == "theses":
        embed.add_field(name="Latest theses", value="No recent theses found.", inline=False)

    # Never use user.sol_address or user.evm_address: both are synthetic. The
    # real Solana wallet is derived on chain; EVM wallets are accepted only
    # from corroborated on-chain evidence or an explicitly verified mapping.
    if wallet:
        embed.add_field(name="Solana wallet", value=f"◎ `{wallet}`", inline=False)
    if evm_wallet:
        embed.add_field(name="EVM wallet", value=f"Ξ `{evm_wallet}`", inline=False)

    if ranks:
        embed.add_field(name="PnL · leaderboard rank", value="\n".join(ranks), inline=False)

    links = [f"[fomo]({user.profile_url})"]
    if user.twitter:
        links.append(f"[X]({user.twitter})")
    embed.add_field(name="Links", value=" · ".join(links), inline=False)

    flags = []
    if user.is_private:
        flags.append("🔒 private")
    if user.is_restricted:
        flags.append("⚠️ restricted")
    footer = " · ".join(flags) or "fomo.family"
    if user.created_at:
        footer += f" · joined {user.created_at[:10]}"
    embed.set_footer(text=footer)
    return embed


def build_compact_embed(
    user: FomoUser,
    wallet: str | None = None,
    evm_wallet: str | None = None,
    stats: TraderStats | None = None,
    *,
    wallets_pending: bool = False,
) -> discord.Embed:
    """Render only the essential identity and portfolio profile sections."""
    stats = stats or TraderStats()
    embed = discord.Embed(
        title=user.display_name,
        url=user.profile_url,
        colour=BRAND,
    )
    embed.set_author(name=f"@{user.handle}", url=user.profile_url)
    if user.avatar:
        embed.set_thumbnail(url=user.avatar)

    embed.add_field(
        name="Social",
        value=(
            f"**{fmt_count(user.followers)}** followers\n"
            f"**{fmt_count(user.following)}** following"
        ),
        inline=True,
    )
    embed.add_field(
        name="Strategy",
        value=(
            f"**{fmt_usd(user.total_volume)}** volume\n"
            f"**{fmt_duration(user.avg_hold_seconds)}** avg hold"
        ),
        inline=True,
    )
    embed.add_field(
        name="Portfolio",
        value=f"**{fmt_usd(stats.portfolio_value)}** current value",
        inline=True,
    )

    twitter = str(user.twitter or "").strip()
    if twitter:
        twitter_url = twitter if twitter.startswith(("https://", "http://")) else (
            f"https://x.com/{twitter.lstrip('@')}"
        )
        twitter_value = f"[Open linked account]({twitter_url})"
    else:
        twitter_value = "Not linked"
    embed.add_field(name="X / Twitter", value=twitter_value, inline=False)

    wallets = []
    if wallet:
        wallets.append(f"◎ **Solana** · `{wallet}`")
    if evm_wallet:
        wallets.append(f"Ξ **EVM** · `{evm_wallet}`")
    embed.add_field(
        name="Linked wallets",
        value=(
            "\n".join(wallets)
            if wallets
            else ("Querying ⏳" if wallets_pending else "No verified wallets found.")
        ),
        inline=False,
    )
    return embed


def build_profile_embed(
    user: FomoUser,
    wallet: str | None = None,
    evm_wallet: str | None = None,
    stats: TraderStats | None = None,
    *,
    layout: str = "wide",
    wallets_pending: bool = False,
) -> discord.Embed:
    if layout == "compact":
        return build_compact_embed(
            user,
            wallet,
            evm_wallet,
            stats,
            wallets_pending=wallets_pending,
        )
    return build_embed(user, wallet, evm_wallet, stats)


def build_track_embed(
    handle: str,
    event: TrackEvent,
    market_cap: float | None = None,
    native_value: float | None = None,
    native_symbol: str | None = None,
) -> discord.Embed:
    """Render one tracking event as a focused, tradeable Discord card."""
    clean_handle = handle.strip().lstrip("@")
    profile = f"https://fomo.family/profile/{clean_handle}"
    presentation = {
        "buy": ("🟢", "bought", WIN),
        "sell": ("🔴", "sold", LOSS),
        "thesis": ("📝", "wrote a thesis", THESIS),
    }
    icon, action, colour = presentation.get(event.kind, ("🔔", "updated", BRAND))
    token_label = f"${event.symbol.lstrip('$') or 'TOKEN'}"
    trade_url = padre_trade_url(event.network_id, event.token_address)
    # discord.utils.parse_time delegates directly to datetime.fromisoformat in
    # discord.py 2.4. Under Python 3.10 that rejects FOMO's standard trailing
    # "Z", so normalize it ourselves before handing the value to the embed.
    timestamp = iso_to_datetime(event.created_at)

    chain = chain_name(event.network_id)
    if event.kind == "thesis":
        token_line = f"[{token_label}]({trade_url})" if trade_url else f"**{token_label}**"
        embed = discord.Embed(
            title=f"{icon} @{clean_handle} {action}",
            url=profile,
            description=f"## {token_line}",
            colour=colour,
            timestamp=timestamp,
        )
        embed.add_field(
            name="✨ Thesis",
            value=f">>> {event.detail or 'New thesis posted'}",
            inline=False,
        )
        if event.token_address:
            embed.add_field(
                name="Contract address",
                value=f"`{event.token_address}`",
                inline=False,
            )
    else:
        amount = fmt_native_amount(
            event.native_value if native_value is None else native_value,
            native_symbol or event.native_symbol or native_currency(event.network_id),
        )
        cap = fmt_usd(market_cap) if market_cap is not None else "—"
        contract = f"\n\n`{event.token_address}`" if event.token_address else ""
        embed = discord.Embed(
            title=f"{icon} @{clean_handle} {action} {token_label}",
            url=trade_url or profile,
            description=f"💰 **{amount}** · MC **{cap}**{contract}",
            colour=colour,
            timestamp=timestamp,
        )
    if event.image_url:
        embed.set_thumbnail(url=event.image_url)
    embed.set_footer(text=f"FOMO tracker • {chain}")
    return embed


def _market_cap_value(data: Any) -> float | None:
    if not isinstance(data, dict):
        return None
    for key in ("marketCap", "fdv"):
        try:
            value = float(data.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _price_usd_value(data: Any) -> float | None:
    if not isinstance(data, dict):
        return None
    try:
        value = float(data.get("priceUsd"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def build_pump_embed(
    user: PumpUser,
    portfolio: PumpPortfolio | None = None,
    holdings: list[PumpHolding] | None = None,
    callouts: list[PumpCallout] | None = None,
    created_count: int = 0,
    created_coins: list[PumpCoin] | None = None,
    callout_coins: dict[str, PumpCoin] | None = None,
    evm_match: PumpEvmMatch | None = None,
    activities: list[PumpAlert] | None = None,
    activity_filter: str = "all",
) -> discord.Embed:
    """Render a compact Pump profile without exposing internal API details."""
    portfolio = portfolio or PumpPortfolio()
    holdings = holdings or []
    callouts = callouts or []
    created_coins = created_coins or []
    callout_coins = callout_coins or {}
    pnl = portfolio.unrealized_usd
    colour = BRAND if pnl is None else (WIN if pnl >= 0 else LOSS)
    embed = discord.Embed(
        title=f"@{user.username}",
        url=user.profile_url,
        description=user.bio,
        colour=colour,
    )
    if user.profile_image:
        embed.set_thumbnail(url=user.profile_image)
    embed.add_field(
        name="Social",
        value=(f"**{fmt_count(user.followers)}** followers\n"
               f"**{fmt_count(user.following)}** following"),
        inline=True,
    )
    embed.add_field(
        name="Portfolio",
        value=(f"**{fmt_usd(portfolio.total_value)}** value\n"
               f"**{portfolio.token_count:,}** tokens"),
        inline=True,
    )
    pnl_percent = (f" · {portfolio.unrealized_percent:+.1f}%"
                   if portfolio.unrealized_percent is not None else "")
    embed.add_field(
        name="Unrealized PnL",
        value=f"**{fmt_usd(portfolio.unrealized_usd)}**{pnl_percent}",
        inline=True,
    )
    if holdings:
        lines = []
        for holding in holdings[:5]:
            link = padre_trade_url("solana", holding.mint)
            token = f"[${holding.symbol}]({link})" if link else f"**${holding.symbol}**"
            pnl_text = f" · PnL {fmt_usd(holding.pnl_usd)}" if holding.pnl_usd is not None else ""
            lines.append(f"{token} — **{fmt_usd(holding.value_usd)}**{pnl_text}")
        embed.add_field(name="Top holdings", value="\n".join(lines), inline=False)
    if activities is None:
        activities = [callout_alert(item, callout_coins.get(item.mint))
                      for item in callouts]
    activity_sections = (
        ("buy", "Latest buys", "No recent buys found."),
        ("callout", "Latest callouts", "No recent callouts found."),
        ("sell", "Latest sells", "No recent sells found."),
    )
    selected_kind = {"buys": "buy", "callouts": "callout", "sells": "sell"}.get(
        activity_filter
    )
    rendered_activity = False
    for kind, field_name, empty_text in activity_sections:
        if selected_kind and selected_kind != kind:
            continue
        rows = sorted(
            (event for event in activities if event.kind == kind),
            key=lambda event: event.created_at or "",
            reverse=True,
        )[:3]
        if not rows:
            if selected_kind == kind:
                embed.add_field(name=field_name, value=empty_text, inline=False)
                rendered_activity = True
            continue
        lines = []
        for event in rows:
            when = iso_to_unix(event.created_at)
            relative = f" · <t:{when}:R>" if when is not None else ""
            link = padre_trade_url("solana", event.mint)
            token = f"[${event.symbol}]({link})" if link else f"**${event.symbol}**"
            cap = f" · MC {fmt_usd(event.market_cap)}" if event.market_cap is not None else ""
            if kind == "callout":
                lines.append(f"📝 {token}{cap}{relative}\n> {(event.detail or 'New callout')[:220]}")
            else:
                icon = "🟢" if kind == "buy" else "🔴"
                amount = f" · {fmt_usd(event.usd_value)}" if event.usd_value is not None else ""
                lines.append(f"{icon} {token}{amount}{cap}{relative}")
        embed.add_field(name=field_name, value="\n".join(lines), inline=False)
        rendered_activity = True
    if not rendered_activity and activity_filter == "all":
        embed.add_field(name="Latest activity", value="No recent activity found.", inline=False)
    if created_count:
        labels = ", ".join(f"${coin.symbol}" for coin in created_coins[:3])
        suffix = f" · {labels}" if labels else ""
        embed.add_field(
            name="Coins created",
            value=f"**{created_count:,}**{suffix}",
            inline=False,
        )
    embed.add_field(
        name="Solana wallet",
        value=f"◎ {_pump_wallet_link(user.address)}",
        inline=False,
    )
    if evm_match:
        embed.add_field(name="EVM wallet", value=f"Ξ `{evm_match.evm}`", inline=False)
    if user.x_url:
        embed.add_field(name="Links", value=f"[X]({user.x_url})", inline=False)
    embed.set_footer(text="pump.fun")
    return embed


def build_pump_track_embed(
    handle: str, wallet: str, event: PumpAlert
) -> discord.Embed:
    clean_handle = handle.strip().lstrip("@")
    profile = pump_profile_url(wallet)
    trade_url = padre_trade_url("solana", event.mint)
    token = f"${event.symbol.lstrip('$') or 'TOKEN'}"
    timestamp = iso_to_datetime(event.created_at)
    if event.kind == "callout":
        token_line = f"[{token}]({trade_url})" if trade_url else f"**{token}**"
        embed = discord.Embed(
            title=f"📝 @{clean_handle} wrote a thesis",
            url=profile,
            description=f"## {token_line}",
            colour=THESIS,
            timestamp=timestamp,
        )
        embed.add_field(
            name="✨ Thesis",
            value=f">>> {event.detail or 'New Pump callout'}",
            inline=False,
        )
        if event.market_cap is not None:
            embed.add_field(
                name="Market cap at callout",
                value=f"**{fmt_usd(event.market_cap)}**",
                inline=True,
            )
        if event.mint:
            embed.add_field(name="Contract address", value=f"`{event.mint}`", inline=False)
    else:
        icon, action, colour = (("🟢", "bought", WIN) if event.kind == "buy"
                                else ("🔴", "sold", LOSS))
        amount = fmt_native_amount(event.native_value, "SOL")
        cap = fmt_usd(event.market_cap) if event.market_cap is not None else "—"
        embed = discord.Embed(
            title=f"{icon} @{clean_handle} {action} {token}",
            url=profile,
            description=f"💰 **{amount}** · MC **{cap}**\n\n`{event.mint}`",
            colour=colour,
            timestamp=timestamp,
        )
    embed.add_field(
        name="Pump profile",
        value=_pump_wallet_link(wallet),
        inline=False,
    )
    if event.image_url:
        embed.set_thumbnail(url=event.image_url)
    embed.set_footer(text="Pump tracker")
    return embed


async def _clear_guild_commands(tree: Any, guild: Any) -> list[Any]:
    """Delete legacy guild registrations while preserving global commands."""
    tree.clear_commands(guild=guild)
    return await tree.sync(guild=guild)


class FomoBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.fomo: FomoClient | None = None
        self.wallets: WalletResolver | None = None
        self.evm_wallets: EvmWalletResolver | None = None
        self.pump: PumpClient | None = None
        self.pump_evm: PumpEvmResolver | None = None
        self.pump_profiles: PumpProfileResolver | None = None
        self.pump_chain: PumpChainClient | None = None
        self.tokens: TokenIntelligenceClient | None = None
        self.connected: ConnectedWalletAnalyzer | None = None
        self._http: Any = None
        self.tracking = TrackingStore(TRACK_FILE)
        self.pump_tracking = PumpTrackingStore(PUMP_TRACK_FILE)
        self._tracking_tasks: list[asyncio.Task[None]] = []
        self._enrichment_tasks: set[asyncio.Task[None]] = set()
        self._guild_commands_cleared = False

    def create_enrichment_task(
        self, awaitable: Awaitable[None], *, name: str
    ) -> asyncio.Task[None]:
        """Keep a strong reference to a bounded profile-enrichment task."""

        async def guarded() -> None:
            try:
                await awaitable
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("background profile enrichment failed")

        task = asyncio.create_task(guarded(), name=name)
        self._enrichment_tasks.add(task)
        task.add_done_callback(self._enrichment_tasks.discard)
        return task

    async def setup_hook(self) -> None:
        self.fomo = FomoClient(
            refresh_token=REFRESH_TOKEN or None,
            access_token=ACCESS_TOKEN or None,
        )
        await self.fomo.__aenter__()

        # Pump's public profile adapters and Solana event reader share this
        # transport with the optional FOMO wallet enrichment.
        try:
            import httpx

            self._http = httpx.AsyncClient(timeout=60)
            self.pump = PumpClient(self._http)
            self.pump_evm = PumpEvmResolver(self._http, PUMP_EVM_CACHE_FILE)
            # One request per wallet, ever: /token, /wallet and /pump all go
            # through this instead of calling PumpClient.resolve() directly.
            self.pump_profiles = PumpProfileResolver(
                self.pump, PUMP_PROFILE_CACHE_FILE, evm=self.pump_evm
            )
            self.pump_chain = PumpChainClient(self._http, SOLANA_RPCS)
            self.tokens = TokenIntelligenceClient(self._http, SOLANA_RPCS)
            self.connected = ConnectedWalletAnalyzer(
                self._http, SOLANA_RPCS, identify=_connected_identity
            )
            if RESOLVE_WALLETS:
                self.wallets = WalletResolver(self._http, SOLANA_RPCS)
            if RESOLVE_EVM:
                self.evm_wallets = EvmWalletResolver(self._http)
        except ImportError:
            log.warning("httpx not installed - Pump and wallet resolution disabled")

        self.create_enrichment_task(_loop_watchdog(), name="loop-watchdog")

        synced = await self.tree.sync()
        self._tracking_tasks = [
            asyncio.create_task(
                self._tracking_loop("FOMO", FOMO_TRACK_INTERVAL, self._poll_tracking),
                name="fomo-tracking",
            ),
            asyncio.create_task(
                self._tracking_loop("Pump", PUMP_TRACK_INTERVAL, self._poll_pump_tracking),
                name="pump-tracking",
            ),
        ]
        log.info(
            "tracking intervals: FOMO %.2fs, Pump %.2fs",
            FOMO_TRACK_INTERVAL,
            PUMP_TRACK_INTERVAL,
        )
        names = ", ".join(command.name for command in synced)
        log.info("🪙 global slash commands synced: %s", names)

    async def on_ready(self) -> None:
        # Older releases copied every global command into each guild, which made
        # Discord show duplicate global and server-specific entries. Sync an
        # empty guild command tree once to delete those legacy registrations.
        if self._guild_commands_cleared:
            return
        all_cleared = bool(self.guilds)
        for guild in self.guilds:
            try:
                synced = await _clear_guild_commands(self.tree, guild)
                log.info(
                    "cleared guild-specific slash commands for guild %s (%d remain)",
                    guild.id,
                    len(synced),
                )
            except discord.HTTPException as exc:
                all_cleared = False
                log.warning("guild command cleanup failed for %s: %s", guild.id, exc)
        self._guild_commands_cleared = all_cleared

    async def close(self) -> None:
        enrichment_tasks = list(self._enrichment_tasks)
        for task in enrichment_tasks:
            task.cancel()
        for task in enrichment_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._enrichment_tasks.clear()
        for task in self._tracking_tasks:
            task.cancel()
        for task in self._tracking_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tracking_tasks.clear()
        if self.fomo:
            await self.fomo.__aexit__(None, None, None)
        if self._http:
            await self._http.aclose()
        await super().close()

    async def _tracking_loop(
        self,
        label: str,
        interval: float,
        poller: Callable[[], Awaitable[None]],
    ) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(interval)
            try:
                await poller()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s tracking poll failed", label)

    async def _poll_tracking(self) -> None:
        if not self.fomo:
            return
        for key, entry in list(self.tracking.tracks.items()):
            try:
                swaps_data, trades_data = await asyncio.gather(
                    self.fomo.swaps(
                        str(entry["userId"]), limit=25, fresh=True,
                        background=True,
                    ),
                    self.fomo.trades(
                        str(entry["userId"]), fresh=True, background=True
                    ),
                )
                events = detect_events(swaps_data, trades_data, entry, LARGE_SWAP_USD)
                state = snapshot(swaps_data, trades_data, entry)
                alerts = [
                    event for event in events
                    if activity_allowed(_entry_activity_filters(entry), event.kind)
                ]
                try:
                    if alerts:
                        await self._send_track_alert(entry, alerts)
                finally:
                    # Baseline every observed event even when delivery fails.
                    # Leaving the baseline behind replays this whole batch on
                    # the next poll, and on every poll after it.
                    self.tracking.update_state(key, state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("tracking @%s failed: %s", entry.get("handle", "?"), exc)

    async def _send_track_alert(self, entry: dict[str, Any], events: list[TrackEvent]) -> None:
        channel_id = int(entry["channelId"])
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        handle = str(entry["handle"])
        markets: dict[tuple[str, str], dict[str, Any]] = {}
        if self.fomo:
            tokens = [
                (chain_name(event.network_id), event.token_address)
                for event in events
                if event.kind != "thesis" and event.token_address
            ]
            native_symbols = {
                event.native_symbol or native_currency(event.network_id)
                for event in events
                if event.kind != "thesis" and event.native_value is None
            }
            tokens.extend(
                NATIVE_PRICE_REFERENCES[symbol]
                for symbol in native_symbols
                if symbol in NATIVE_PRICE_REFERENCES
            )
            if tokens:
                try:
                    markets = await self.fomo.token_market_data(tokens)
                except Exception as exc:
                    log.debug("tracking market-cap lookup failed: %s", exc)
        for event in events:
            key = (chain_name(event.network_id), event.token_address.lower())
            market_cap = _market_cap_value(markets.get(key))
            native_symbol = event.native_symbol or native_currency(event.network_id)
            native_value = event.native_value
            reference = NATIVE_PRICE_REFERENCES.get(native_symbol or "")
            if native_value is None and reference:
                native_price = _price_usd_value(
                    markets.get((reference[0], reference[1].lower()))
                )
                native_value = native_value_from_usd(event.usd_value, native_price)
            try:
                await channel.send(  # type: ignore[union-attr]
                    embed=build_track_embed(
                        handle, event, market_cap, native_value, native_symbol
                    )
                )
            except Exception as exc:
                # One malformed event must not abandon the rest of the batch.
                log.warning(
                    "could not deliver %s alert for @%s: %s", event.kind, handle, exc
                )

    async def _poll_pump_tracking(self) -> None:
        if not self.pump:
            return
        for key, entry in list(self.pump_tracking.tracks.items()):
            try:
                wallet = str(entry["userId"])
                callouts_result, chain_result = await asyncio.gather(
                    self.pump.callouts(wallet, limit=30),
                    self._pump_chain_updates(wallet, entry),
                    return_exceptions=True,
                )
                alerts: list[PumpAlert] = []
                current_callouts: list[PumpCallout] = []
                observed_signatures: list[str] = []
                trades = []
                changed_callouts: list[PumpCallout] = []

                if isinstance(callouts_result, Exception):
                    log.debug("Pump callouts @%s failed: %s", entry.get("handle"), callouts_result)
                else:
                    current_callouts = callouts_result
                    if entry.get("calloutBaselineReady"):
                        changed_callouts = new_callouts(current_callouts, entry)
                    else:
                        changed_callouts = []
                        entry["calloutBaselineReady"] = True

                if isinstance(chain_result, Exception):
                    log.debug("Pump chain @%s failed: %s", entry.get("handle"), chain_result)
                else:
                    trades, observed_signatures = chain_result
                    entry["signatureBaselineReady"] = True

                mints = {trade.mint for trade in trades} | {item.mint for item in changed_callouts}
                coins = await self.pump.coins(mints) if mints else {}
                sol_price = None
                if trades:
                    try:
                        sol_price = await self.pump.sol_price()
                    except PumpError as exc:
                        # The exact on-chain alert remains useful without a USD
                        # conversion, so a price outage must not suppress it.
                        log.debug("Pump SOL price lookup failed: %s", exc)

                for trade in trades:
                    coin = coins.get(trade.mint)
                    quote_mint = trade.quote_mint or (coin.quote_mint if coin else None)
                    decimals = coin.quote_decimals if coin else 9
                    usd_value = quote_value_usd(
                        quote_mint, trade.quote_amount, decimals, sol_price
                    )
                    if (usd_value is not None and PUMP_MIN_TRADE_USD > 0
                            and usd_value < PUMP_MIN_TRADE_USD):
                        continue
                    native_value = quote_value_sol(
                        quote_mint,
                        trade.quote_amount,
                        decimals,
                        sol_price,
                    )
                    alerts.append(trade_alert(trade, coin, usd_value, native_value))
                alerts.extend(callout_alert(item, coins.get(item.mint))
                              for item in changed_callouts)
                alerts.sort(key=lambda event: event.created_at or "")

                selected_alerts = [
                    event for event in alerts
                    if activity_allowed(_entry_activity_filters(entry), event.kind)
                ]
                if selected_alerts:
                    await self._send_pump_alerts(entry, selected_alerts)
                state = pump_snapshot(observed_signatures, current_callouts, entry)
                state["signatureBaselineReady"] = bool(entry.get("signatureBaselineReady"))
                state["calloutBaselineReady"] = bool(entry.get("calloutBaselineReady"))
                self.pump_tracking.update_state(key, state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Pump tracking @%s failed: %s", entry.get("handle", "?"), exc)

    async def _pump_chain_updates(
        self, wallet: str, entry: dict[str, Any]
    ) -> tuple[list[Any], list[str]]:
        if not self.pump_chain:
            raise PumpRpcError("Solana RPC is unavailable")
        if not entry.get("signatureBaselineReady"):
            baseline = await self.pump_chain.recent_signature_ids(wallet)
            return [], baseline
        known = set(str(value) for value in (entry.get("signatureIds") or []))
        return await self.pump_chain.new_trades(wallet, known)

    async def _send_pump_alerts(
        self, entry: dict[str, Any], alerts: list[PumpAlert]
    ) -> None:
        channel_id = int(entry["channelId"])
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        handle = str(entry["handle"])
        wallet = str(entry["userId"])
        for alert in alerts:
            await channel.send(  # type: ignore[union-attr]
                embed=build_pump_track_embed(handle, wallet, alert)
            )


bot = FomoBot()


async def _reply_error(interaction: discord.Interaction, exc: Exception, term: str) -> None:
    if isinstance(exc, FomoNotFound):
        msg = f"No FOMO trader called **{term}**."
    elif isinstance(exc, FomoBlocked):
        # Don't guess at the cause — describe_403() already classified it.
        log.warning("403 on %s: %s", term, exc)
        msg = f"FOMO returned 403.\n```\n{str(exc)[:1600]}\n```"
    else:
        log.exception("lookup failed for %s", term)
        msg = f"FOMO lookup failed: `{str(exc)[:180]}`"
    await interaction.followup.send(msg, ephemeral=True)


async def _resolve_fomo_enrichment(
    client: FomoBot,
    user: FomoUser,
    stats: TraderStats,
    wallet: str | None,
    evm_wallet: str | None,
) -> tuple[str | None, str | None, TraderStats]:
    """Complete optional wallet/activity panels after the base reply exists."""
    async def resolve_solana() -> str | None:
        """Three routes, cheapest first.

        The holder route runs before the transaction routes, which is a change
        of order rather than a change of standard. It costs one FOMO request
        plus an on-chain query for each token that actually publishes a row
        naming this trader, where `resolve()` pays a 12-page sponsor index and
        up to four mint scans before its first answer -- and the enrichment
        budget is a wall clock (`FOMO_ENRICH_TIMEOUT`) that cancels whatever is
        still running. A handle the expensive route cannot reach used to spend
        the whole budget proving it. Evidence quality does not drop, because
        the holder route's own gate is `verify_wallet` against this trader's
        swaps: a hit is transaction-backed either way.
        """
        if wallet is not None or not client.wallets or not client.fomo:
            return wallet
        try:
            result = None
            if stats.raw_balances is not None:
                result = await client.wallets.resolve_from_holders(
                    client.fomo, user, stats.raw_balances, swaps=stats.raw_swaps or ()
                )
            if result is None:
                result = await client.wallets.resolve(client.fomo, user)
            if result is None and stats.raw_balances is not None:
                result = await client.wallets.resolve_from_balances(
                    user, stats.raw_balances, swaps=stats.raw_swaps or ()
                )
            return result
        except Exception as exc:
            log.warning("Solana wallet lookup failed for @%s: %s", user.handle, exc)
            return None

    async def resolve_evm() -> str | None:
        if evm_wallet is not None or not client.evm_wallets:
            return evm_wallet
        try:
            trade_details: tuple[Any, ...] = ()
            existing_evidence = evm_trade_evidence(
                stats.raw_swaps, stats.raw_trades
            )
            exact_evidence = sum(not item.aggregate for item in existing_evidence)
            if (exact_evidence < 2 and client.fomo
                    and hasattr(client.fomo, "trade_details")):
                detail_ids = evm_trade_ids(stats.raw_trades)
                if detail_ids:
                    results = await client.fomo.trade_details(
                        detail_ids, background=True
                    )
                    trade_details = tuple(
                        item for item in results if not isinstance(item, Exception)
                    )
            return await client.evm_wallets.resolve(
                user,
                balances=stats.raw_balances,
                swaps=stats.raw_swaps,
                trades=stats.raw_trades,
                trade_details=trade_details,
            )
        except Exception as exc:
            log.warning("EVM wallet lookup failed for @%s: %s", user.handle, exc)
            return None

    async def resolve_evm_with_activity() -> tuple[str | None, TraderStats]:
        resolved_wallet = await resolve_evm()
        resolved_stats = stats
        if resolved_wallet and client._http:
            try:
                evm_buys, evm_sells = await fetch_evm_activity(
                    client._http, resolved_wallet
                )
                resolved_stats = merge_latest_buys(resolved_stats, evm_buys)
                resolved_stats = merge_latest_sells(resolved_stats, evm_sells)
            except Exception as exc:
                log.warning("EVM activity lookup failed for @%s: %s", user.handle, exc)
        return resolved_wallet, resolved_stats

    wallet, evm_result = await asyncio.gather(
        resolve_solana(), resolve_evm_with_activity()
    )
    evm_wallet, stats = evm_result
    return wallet, evm_wallet, stats


async def _enrich_fomo_message(
    client: FomoBot,
    message: Any,
    user: FomoUser,
    stats: TraderStats,
    wallet: str | None,
    evm_wallet: str | None,
    *,
    timeout: float = FOMO_ENRICH_TIMEOUT,
    layout: str = "wide",
    wallets_pending: bool = False,
) -> None:
    """Bound optional enrichment and edit the already-visible profile card."""
    initial = (wallet, evm_wallet, stats)
    try:
        enriched = await asyncio.wait_for(
            _resolve_fomo_enrichment(
                client, user, stats, wallet, evm_wallet
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # A resolver may have completed and cached one identity before a later
        # stage consumed the remaining budget. Preserve that partial progress.
        handle = user.handle.lower()
        enriched = (
            cached_wallet(handle) if client.wallets else wallet,
            cached_evm_wallet(handle) if client.evm_wallets else evm_wallet,
            stats,
        )
        log.info(
            "on-chain enrichment for @%s reached the %.1fs background deadline",
            user.handle,
            timeout,
        )
    if enriched == initial and not wallets_pending:
        return
    new_wallet, new_evm_wallet, new_stats = enriched
    try:
        await message.edit(
            embed=build_profile_embed(
                user,
                new_wallet,
                new_evm_wallet,
                new_stats,
                layout=layout,
                wallets_pending=False,
            )
        )
    except discord.HTTPException as exc:
        log.debug("could not update enriched /fomo card for @%s: %s", user.handle, exc)


async def _generate_fomo_profile(
    interaction: discord.Interaction, handle: str, layout: str
) -> bool:
    """Fetch one profile through the shared path, then apply its chosen layout."""
    assert bot.fomo
    try:
        user = await bot.fomo.resolve(handle)
    except (FomoError, asyncio.TimeoutError) as exc:
        await _reply_error(interaction, exc, handle)
        return False

    # Core FOMO panels are the response critical path. Wallet derivation and
    # cross-chain activity are optional and can be much slower than the profile
    # itself, so show cached identities immediately and enrich the same card in
    # a bounded background task.
    stats = await fetch_trader_stats(bot.fomo, user.id)
    normalized_handle = user.handle.lower()
    wallet = cached_wallet(normalized_handle) if bot.wallets else None
    evm_wallet = cached_evm_wallet(normalized_handle) if bot.evm_wallets else None
    wallets_pending = bool(
        layout == "compact"
        and ((wallet is None and bot.wallets) or (evm_wallet is None and bot.evm_wallets))
    )
    message = await interaction.followup.send(
        embed=build_profile_embed(
            user,
            wallet,
            evm_wallet,
            stats,
            layout=layout,
            wallets_pending=wallets_pending,
        ),
        wait=True,
    )
    if bot.wallets or bot.evm_wallets or (evm_wallet and bot._http):
        bot.create_enrichment_task(
            _enrich_fomo_message(
                bot,
                message,
                user,
                stats,
                wallet,
                evm_wallet,
                layout=layout,
                wallets_pending=wallets_pending,
            ),
            name=f"fomo-enrich:{user.id}",
        )
    return True


@bot.tree.command(name="fomo", description="Look up a fomo.family trader")
@app_commands.describe(handle="FOMO username, e.g. Binkieee")
async def fomo_cmd(interaction: discord.Interaction, handle: str) -> None:
    clean_handle = handle.strip().lstrip("@")
    view = FomoLayoutSelectionView(
        interaction.user.id,
        clean_handle,
        _generate_fomo_profile,
    )
    await interaction.response.send_message(
        f"Choose a profile layout for **@{clean_handle}**:\n"
        "📇 **Compact** — Essential profile information only.\n"
        "📊 **Wide** — Full profile with all available information.",
        view=view,
        ephemeral=True,
    )


async def _resolve_pump_user(
    interaction: discord.Interaction, term: str, *, max_age: float | None = None
) -> PumpUser | None:
    """The cached Pump profile for a command term, or an explained refusal.

    Every Pump command goes through this so the profile is fetched at most
    once per wallet per TTL, and so a wallet Pump has no profile for is
    answered from the negative cache rather than by asking again. `max_age`
    lets a card demand fresher numbers than holder labelling needs.
    """
    if not bot.pump_profiles:
        await interaction.followup.send(
            "Pump support is unavailable: httpx is not installed.", ephemeral=True
        )
        return None
    result = await bot.pump_profiles.lookup(term, max_age=max_age)
    if result.profile is not None:
        return result.profile.to_user()
    if (result.status == PUMP_UNSUPPORTED
            and EVM_RE.fullmatch(term.strip().strip("`").strip().lower())):
        await interaction.followup.send(
            "That EVM wallet has not been discovered yet. Run `/pump <handle>` "
            "once to discover and cache the profile\u2019s EVM wallet.",
            ephemeral=True,
        )
        return None
    if result.definitive_miss:
        await interaction.followup.send(
            f"No Pump.fun profile found for **{term}**.", ephemeral=True
        )
        return None
    log.warning("Pump lookup failed for %s: %s", term, result.error)
    await interaction.followup.send(
        f"Pump.fun lookup failed: `{str(result.error or 'unavailable')[:180]}`",
        ephemeral=True,
    )
    return None


@bot.tree.command(name="wallet", description="Find FOMO and Pump profiles by wallet")
@app_commands.describe(address="Solana or EVM wallet address")
async def wallet_cmd(interaction: discord.Interaction, address: str) -> None:
    if not await _safe_defer(interaction):
        return
    query = address.strip().strip("`").strip()
    fomo_matches = find_cached_wallets(query)

    users: dict[str, FomoUser] = {}
    if bot.fomo:
        for match in fomo_matches:
            try:
                users[match.handle] = await bot.fomo.user_by_handle(
                    match.handle, with_ranks=False
                )
            except (FomoError, asyncio.TimeoutError):
                # The verified cache remains useful if FOMO is temporarily
                # unavailable or a handle has changed capitalization.
                pass

    pump_profile: PumpProfile | None = None
    pump_evm = bot.pump_evm.cached(query) if bot.pump_evm else None
    if bot.pump_profiles:
        # The resolver already translates a discovered EVM wallet to its Pump
        # profile, caches the answer and remembers a definitive "no profile".
        pump_profile = await bot.pump_profiles.resolve(
            pump_evm.solana if pump_evm else query
        )

    lines: list[str] = []
    for match in fomo_matches:
        user = users.get(match.handle)
        handle = user.handle if user else match.handle
        display = f" — {user.display_name}" if user and user.display_name != handle else ""
        verification = _wallet_match_verification(match)
        lines.append(
            f"🔵 **FOMO** · [@{handle}](https://fomo.family/profile/{handle}){display}\n"
            f"{verification}"
        )

    if pump_profile:
        network = "EVM + Solana" if pump_evm else "Solana"
        lines.append(
            f"🟢 **Pump.fun** · "
            f"{_pump_username_link(pump_profile.username, pump_profile.address)} · "
            f"{_pump_wallet_link(pump_profile.address)}\n"
            f"◎ **{network}** · Public Pump profile mapping"
        )

    if not lines:
        detail = (
            "Pump Solana wallets are checked live. FOMO and Pump EVM matches "
            "appear after their verified identity has been discovered."
        )
        await interaction.followup.send(
            f"No FOMO or Pump profile was found for `{query[:100]}`.\n{detail}"
        )
        return

    noun = "match" if len(lines) == 1 else "matches"
    embed = discord.Embed(
        title=f"🪙 Wallet {noun}",
        description="\n\n".join(lines),
        colour=BRAND,
    )
    embed.add_field(name="Wallet", value=f"`{query}`", inline=False)
    if len(lines) == 1 and fomo_matches:
        user = users.get(fomo_matches[0].handle)
        if user and user.avatar:
            embed.set_thumbnail(url=user.avatar)
    elif len(lines) == 1 and pump_profile and pump_profile.profile_image:
        embed.set_thumbnail(url=pump_profile.profile_image)
    embed.set_footer(text=f"FOMO + Pump identity search • {len(lines)} {noun}")
    await interaction.followup.send(embed=embed)


def _wallet_match_verification(match: CachedWalletMatch) -> str:
    if match.network == "Solana":
        count = match.confirmations or 0
        evidence = f"{count} on-chain confirmation{'s' if count != 1 else ''}"
        return f"◎ **Solana** · {evidence}"
    chains = ", ".join(chain.upper() for chain in match.chains)
    chain_text = f" · {chains}" if chains else ""
    return f"Ξ **EVM**{chain_text} · Verified mapping"


def _short_wallet(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if len(address) > 13 else address


def _pump_wallet_link(address: str) -> str:
    """Display a readable wallet while linking with its complete address."""
    return f"[{_short_wallet(address)}]({pump_profile_url(address)})"


def _pump_username_link(handle: str, address: str) -> str:
    clean_handle = handle.strip().lstrip("@")
    return f"[**@{clean_handle}**]({pump_profile_url(address)})"


def _pump_identity(handle: str, address: str) -> str:
    return (
        f"🟢 {_pump_username_link(handle, address)} · "
        f"{_pump_wallet_link(address)}"
    )


def _holder_explorer(chain: str, address: str) -> str | None:
    bases = {
        "Solana": "https://solscan.io/account/",
        "Ethereum": "https://etherscan.io/address/",
        "BSC": "https://bscscan.com/address/",
        "Base": "https://basescan.org/address/",
        "Robinhood": "https://robinhoodchain.blockscout.com/address/",
        "Hyperliquid": "https://hyperscan.com/address/",
    }
    base = bases.get(chain)
    return f"{base}{address}" if base else None


def _fmt_holder_balance(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return "—"
    for cutoff, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= cutoff:
            return f"{number / cutoff:.2f}{suffix}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _discord_line_chunks(lines: list[str], limit: int = 1024) -> list[str]:
    """Pack complete lines into Discord-safe field values."""
    chunks: list[list[str]] = [[]]
    for line in lines:
        safe = line if len(line) <= limit else line[: limit - 1] + "…"
        candidate = "\n".join(chunks[-1] + [safe])
        if chunks[-1] and len(candidate) > limit:
            chunks.append([safe])
        else:
            chunks[-1].append(safe)
    return ["\n".join(chunk) for chunk in chunks if chunk]


async def _fomo_holder_matches(
    token: TokenIntelligence,
) -> dict[str, FomoHolder]:
    """Name this token's on-chain holders from FOMO's own Holders tab.

    Identity used to come only from `wallet_cache.json`, so a FOMO trader who
    had never been through `/fomo` showed as a bare address. `/hodlers/top`
    reports every top holder's handle and exact position; matching that
    position against the owners `/token` already computed names them without
    any cached mapping.
    """
    network_id = network_id_for(token.chain)
    if not bot.fomo or not network_id or not token.holders:
        return {}
    try:
        payload = await bot.fomo.token_holders(token.address, network_id)
    except (FomoError, asyncio.TimeoutError) as exc:
        log.debug("FOMO holder lookup failed for %s: %s", token.address, exc)
        return {}
    holders, total = parse_token_holders(payload)
    onchain = [(row.address, float(row.balance)) for row in token.holders]
    matches = match_holders_to_wallets(holders, onchain)
    log.info(
        "FOMO holders for %s: %d of %s named %d on-chain wallet(s)",
        token.address, len(holders), total, len(matches),
    )

    # Every confident pair is an identity /fomo and /wallet would otherwise pay
    # a sponsor index or a block scan for. Adopting runs in the background: it
    # costs RPC calls for corroboration and must not delay the token card.
    if ADOPT_HOLDER_WALLETS and (bot.wallets or bot.evm_wallets):
        strong = {
            wallet: holder.handle
            for wallet, holder in confident_matches(holders, onchain).items()
        }
        if strong:
            bot.create_enrichment_task(
                _adopt_holder_wallets(strong, token.address, token.chain),
                name=f"hodler-adopt:{token.address[:12]}",
            )
    return matches


async def _adopt_holder_wallets(
    matches: dict[str, str], token: str, chain: str
) -> None:
    """Background: corroborate and cache holder-derived wallet identities.

    The chain decides both the destination field and the corroboration. A BSC
    holder's `0x…` address is an EVM smart wallet, not a Solana one: writing it
    to the Solana field would be wrong, and a Solana sponsor-signature probe
    against it is a JSON-RPC error rather than a check.
    """
    solana = chain == "Solana"
    resolver = bot.wallets if solana else bot.evm_wallets
    if resolver is None:
        return
    try:
        if solana:
            written = await resolver.adopt_holder_matches(matches, token=token)
        else:
            written = await resolver.adopt_holder_matches(
                matches, token=token, chain=chain.lower()
            )
    except Exception as exc:
        log.warning("adopting holder wallets for %s failed: %s", token, exc)
        return
    if written:
        log.info("cached %d new %s identity(ies) from %s's holder list",
                 len(written), "Solana" if solana else "EVM", token)


async def _wallet_identity(address: str, chain: str,
                           fomo_match: FomoHolder | None = None) -> str:
    """`@handle · wallet`, or the bare wallet when nothing names it.

    Shared by the holder rows and `/connected`'s cards so no two lists can
    disagree about who a wallet is; the row-specific figures are appended by
    the callers.
    """
    identities: list[str] = []
    pump_wallet: str | None = None
    named = {match.handle.lower() for match in find_cached_wallets(address)}
    for match in find_cached_wallets(address):
        identities.append(
            f"🔵 [@{match.handle}](https://fomo.family/profile/{match.handle})"
        )
    if fomo_match and fomo_match.handle.lower() not in named:
        dev = " 🛠️" if fomo_match.is_dev else ""
        identities.append(
            f"🔵 [@{fomo_match.handle}]"
            f"(https://fomo.family/profile/{fomo_match.handle}){dev}"
        )

    pump_match = bot.pump_evm.cached(address) if bot.pump_evm else None
    if pump_match:
        pump_wallet = pump_match.solana
        identities.append(_pump_identity(pump_match.handle, pump_wallet))
    elif chain == "Solana" and bot.pump_profiles:
        # `token_cmd` has already prefetched the whole holder list, so this is
        # normally a cache read. A wallet with no Pump profile is remembered as
        # such, so the next /token does not re-ask Pump for a known 404.
        pump_profile = await bot.pump_profiles.resolve(address)
        if pump_profile:
            pump_wallet = pump_profile.address
            identities.append(_pump_identity(pump_profile.username, pump_wallet))

    explorer = _holder_explorer(chain, address)
    wallet = _short_wallet(address)
    wallet_text = f"[{wallet}]({explorer})" if explorer else f"`{wallet}`"
    identity = " + ".join(dict.fromkeys(identities)) if identities else wallet_text
    if identities and (
        not pump_wallet or pump_wallet.casefold() != address.casefold()
    ):
        identity += f" · {wallet_text}"
    return identity


async def _holder_label(holder: TokenHolder, chain: str,
                        fomo_match: FomoHolder | None = None) -> str:
    identity = await _wallet_identity(holder.address, chain, fomo_match)
    percentage = (
        f" · **{holder.percentage:.2f}%**"
        if holder.percentage is not None else ""
    )
    return f"{identity}{percentage} · {_fmt_holder_balance(holder.balance)}"


def _token_trade_url(token: TokenIntelligence) -> str | None:
    network = {
        "Solana": "solana",
        "Ethereum": "ethereum",
        "BSC": "bsc",
        "Base": "base",
    }.get(token.chain)
    return padre_trade_url(network, token.address) if network else token.dex_url


def _token_page_embed(
    token: TokenIntelligence, numbered: list[str], page: int, pages: int,
    *, refreshed: bool = False,
) -> discord.Embed:
    """One page of `/token`: the same header, a different slice of holders.

    `refreshed` only changes the footer. Discord renders `embed.timestamp` in
    the reader's own timezone, so the card can say when these holders were
    read without the bot having to guess where anybody is.
    """
    market_cap = fmt_usd(token.market_cap) if token.market_cap is not None else "—"
    description = f"**Market cap:** {market_cap}"
    if token.price_usd is not None:
        description += f"\n**Price:** {fmt_price(token.price_usd)}"
    if token.fdv is not None and token.market_cap is None:
        description += f"\n**FDV:** {fmt_usd(token.fdv)}"

    embed = discord.Embed(
        title=f"🪙 ${token.symbol} · {token.chain}",
        url=_token_trade_url(token),
        description=description,
        colour=BRAND,
    )
    embed.add_field(name="Contract address", value=f"`{token.address}`", inline=False)

    start = (page - 1) * TOKEN_HOLDER_PAGE
    rows = numbered[start:start + TOKEN_HOLDER_PAGE]
    holder_chunks = _discord_line_chunks(rows)
    if not holder_chunks:
        holder_chunks = ["Holder data is currently unavailable."]
        rank = ""
    else:
        rank = f" · {start + 1}-{start + len(rows)}"
    for index, chunk in enumerate(holder_chunks, 1):
        suffix = f" · {index}/{len(holder_chunks)}" if len(holder_chunks) > 1 else ""
        embed.add_field(
            name=f"Top holders{rank} of {len(numbered)}{suffix}",
            value=chunk,
            inline=False,
        )
    if token.image_url:
        embed.set_thumbnail(url=token.image_url)
    embed.timestamp = datetime.now(tz=timezone.utc)
    embed.set_footer(
        text=f"Page {page} of {pages} · 🔵 FOMO · 🟢 Pump.fun • "
             + ("refreshed" if refreshed else "read")
    )
    return embed


@bot.tree.command(
    name="token",
    description="Show token market cap and its top 50 holders",
)
@app_commands.describe(address="Solana or EVM token contract address")
async def token_cmd(interaction: discord.Interaction, address: str) -> None:
    """Market data plus the top 50 holders, ten to a page.

    The holder count used to be a choice between 5 and 10. It is now always
    50: every page is rendered up front, so paging costs a message edit rather
    than another round of identity lookups.

    That is also why the card carries a Refresh button rather than asking the
    reader to run the command again. `_render_token_card` below is the whole
    of the work, so refreshing repeats exactly it -- the market data, the
    holder query and the identity labelling -- and nothing else.
    """
    if not await _safe_defer(interaction):
        return
    if not bot.tokens:
        await interaction.followup.send(
            "Token intelligence is unavailable because the HTTP client did not start.",
            ephemeral=True,
        )
        return

    clean = address.strip().strip("`").strip()
    try:
        embeds = await _render_token_card(clean)
    except TokenIntelligenceError as exc:
        await interaction.followup.send(
            f"Token lookup failed: `{str(exc)[:180]}`", ephemeral=True
        )
        return

    async def refresh() -> list[discord.Embed]:
        # A refresh that cannot read the token leaves the card standing; the
        # view turns an empty list into a private note rather than an error.
        try:
            return await _render_token_card(clean, refreshed=True)
        except TokenIntelligenceError as exc:
            log.info("refreshing /token %s failed: %s", clean, exc)
            return []

    await interaction.followup.send(
        embed=embeds[0], view=TokenCardView(embeds, refresh)
    )


async def _render_token_card(
    address: str, *, refreshed: bool = False
) -> list[discord.Embed]:
    """Every page of a `/token` card, read fresh from the providers.

    Called once when the command runs and again for each Refresh, so the two
    can never drift apart. Identity caches are deliberately *not* bypassed: a
    wallet's handle does not go stale in the way a holder list does, and
    re-asking Pump for fifty profiles is the expensive half of this.
    """
    pump_coin: PumpCoin | None = None
    if bot.pump and not EVM_RE.fullmatch(address):
        try:
            pump_coin = await bot.pump.coin(address)
        except (PumpError, asyncio.TimeoutError):
            pass
    token = await bot.tokens.lookup(
        address, limit=TOKEN_HOLDER_LIMIT, pump_coin=pump_coin
    )

    fomo_matches = await _fomo_holder_matches(token)
    # One deduplicated, bounded batch for every Solana holder before the rows
    # render, so the labelling below costs no requests at all. A holder already
    # named by the Pump EVM cache is skipped -- its profile is known.
    if token.chain == "Solana" and bot.pump_profiles:
        pending = [
            holder.address for holder in token.holders
            if not (bot.pump_evm and bot.pump_evm.cached(holder.address))
        ]
        try:
            await bot.pump_profiles.prefetch(pending)
        except Exception as exc:
            # Identity is never load-bearing: the card renders without it.
            log.debug("pump holder prefetch failed for %s: %s", token.address, exc)
    holder_lines = await asyncio.gather(
        *(_holder_label(holder, token.chain, fomo_matches.get(holder.address))
          for holder in token.holders)
    )
    numbered = [f"`{index}.` {line}" for index, line in enumerate(holder_lines, 1)]
    pages = max(1, -(-len(numbered) // TOKEN_HOLDER_PAGE))
    return [_token_page_embed(token, numbered, page, pages, refreshed=refreshed)
            for page in range(1, pages + 1)]


async def _token_theses(token: TokenIntelligence) -> list[HolderThesis]:
    """This token's holder theses, cheapest route first.

    `/feed/token/sortedThesis` answers the whole question in one request but
    has never been probed live, so an error, an empty body or a shape this
    build does not recognise all fall through to the verified pair of routes:
    `/hodlers/top` names the trade behind each position and `/trades/{id}`
    carries the comment that *is* the thesis. The fallback is one request per
    holder, which is why it is second rather than first.
    """
    network_id = network_id_for(token.chain)
    if not bot.fomo or not network_id:
        return []

    try:
        feed = rank_theses(parse_thesis_feed(
            await bot.fomo.token_theses(token.address, network_id)
        ))
        if feed:
            return feed
        log.info("thesis feed had no usable rows for %s; using the holder route",
                 token.address)
    except (FomoError, asyncio.TimeoutError) as exc:
        log.info("thesis feed unavailable for %s: %s", token.address, exc)

    try:
        payload = await bot.fomo.token_holders(token.address, network_id)
    except (FomoError, asyncio.TimeoutError) as exc:
        log.info("holder lookup for theses failed on %s: %s", token.address, exc)
        return []
    holders, _total = parse_token_holders(payload)
    ranked = sorted(
        holders,
        key=lambda holder: holder.value_usd if holder.value_usd is not None else -1.0,
        reverse=True,
    )[:TOKEN_HOLDER_LIMIT]
    trade_ids = [holder.trade_id for holder in ranked if holder.trade_id]
    if not trade_ids:
        return []
    details = await bot.fomo.trade_details(trade_ids)
    return rank_theses(theses_from_trades(ranked, details))


def _x_profile_url(twitter: str) -> str:
    """FOMO stores `twitter` as a handle on some rows and a full URL on others."""
    value = str(twitter).strip()
    if value.startswith(("http://", "https://")):
        return value
    return f"https://x.com/{value.lstrip('@')}"


def _thesis_quote(text: str) -> str:
    """Render a thesis as a Discord quote without swallowing the next entry.

    `>>>` quotes everything that follows it, so a multi-line thesis is quoted
    line by line instead.
    """
    clipped = text if len(text) <= THESIS_TEXT_LIMIT else text[:THESIS_TEXT_LIMIT - 1] + "…"
    lines = [line.strip() for line in clipped.splitlines()]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _thesis_entry(index: int, thesis: HolderThesis) -> str:
    handle = thesis.handle.lstrip("@")
    parts = [f"[**{index}. {handle}**](https://fomo.family/profile/{handle})"]
    if thesis.is_dev:
        parts.append("🛠️")
    if thesis.twitter:
        parts.append(f"[[X]]({_x_profile_url(thesis.twitter)})")
    # Position and PnL read as one figure -- "$39.1K (🟢 +$34.5K)" -- because
    # the PnL is a property of that position, not a separate fact about it.
    position = fmt_usd(thesis.value_usd) if thesis.value_usd is not None else ""
    if thesis.pnl_usd is not None:
        marker = "🟢" if thesis.pnl_usd >= 0 else "🔴"
        sign = "+" if thesis.pnl_usd >= 0 else ""
        position = (position + " " if position else "") + \
            f"({marker} {sign}{fmt_usd(thesis.pnl_usd)})"
    meta = [position] if position else []
    if thesis.hold_seconds:
        meta.append(fmt_duration(thesis.hold_seconds))
    header = " ".join(parts)
    if meta:
        header += " · " + " · ".join(meta)
    return f"{header}\n{_thesis_quote(thesis.text)}"


def _thesis_page_embed(
    token: TokenIntelligence, theses: list[HolderThesis], page: int, pages: int
) -> discord.Embed:
    start = (page - 1) * THESIS_PAGE
    rows = theses[start:start + THESIS_PAGE]
    header = (
        f"**Token:** [{token.name}]({_token_trade_url(token) or ''}) "
        f"(`{_short_wallet(token.address)}`)\n"
        f"**{len(theses)} holder{'s' if len(theses) != 1 else ''} with a thesis**"
    )
    entries = [_thesis_entry(start + offset, thesis)
               for offset, thesis in enumerate(rows, 1)]
    description = header + "\n\n" + "\n\n".join(entries)
    embed = discord.Embed(
        title=f"📝 Holder theses for ${token.symbol}",
        description=description[:4096],
        colour=THESIS,
    )
    if token.image_url:
        embed.set_thumbnail(url=token.image_url)
    embed.set_footer(text=f"Page {page} of {pages} · fomo.family holders by position")
    return embed


@bot.tree.command(
    name="thesis", description="What this token's biggest holders wrote about it"
)
@app_commands.describe(address="Solana or EVM token contract address")
async def thesis_cmd(interaction: discord.Interaction, address: str) -> None:
    if not await _safe_defer(interaction):
        return
    if not bot.tokens or not bot.fomo:
        await interaction.followup.send(
            "Thesis lookup is unavailable because the HTTP client did not start.",
            ephemeral=True,
        )
        return

    clean = address.strip().strip("`").strip()
    pump_coin: PumpCoin | None = None
    if bot.pump and not EVM_RE.fullmatch(clean):
        try:
            pump_coin = await bot.pump.coin(clean)
        except (PumpError, asyncio.TimeoutError):
            pass
    try:
        # Only the token's name, symbol, chain and image are wanted here, so
        # this asks for the smallest holder list the lookup will return.
        token = await bot.tokens.lookup(clean, limit=1, pump_coin=pump_coin)
    except TokenIntelligenceError as exc:
        await interaction.followup.send(
            f"Token lookup failed: `{str(exc)[:180]}`", ephemeral=True
        )
        return

    theses = await _token_theses(token)
    if not theses:
        await interaction.followup.send(
            f"No FOMO holder has written a thesis on **${token.symbol}** yet."
        )
        return

    pages = max(1, -(-len(theses) // THESIS_PAGE))
    embeds = [_thesis_page_embed(token, theses, page, pages)
              for page in range(1, pages + 1)]
    extra = {"view": PaginatedEmbedView(embeds)} if pages > 1 else {}
    await interaction.followup.send(embed=embeds[0], **extra)


CONNECTED_PAGE = 4
# The whole bar, spelled out on every page, because a reader who does not know
# what was excluded cannot tell an empty answer from a broken one.
CONNECTED_RULE = (
    f"Direct transfers only — {MIN_SOL:g}+ SOL / {MIN_STABLE:g}+ USDC on "
    f"Solana, ${MIN_EVM_USD:,.0f}+ native or {MIN_STABLE:g}+ stablecoin on EVM."
)
CONNECTED_DISCLAIMER = (
    "Swaps, pools, DEX routers, exchanges and program accounts are excluded. "
    "A shared transfer history is not proof of shared ownership."
)


def _connected_identity(address: str) -> str | None:
    """The handle the wallet cache already knows for an address, if any.

    `ConnectedWalletAnalyzer` takes this as a callable so the module stays
    independent of FOMO's cache; here it is the same reverse lookup `/wallet`
    and `/token` use, plus Pump's own mapping.
    """
    matches = find_cached_wallets(address)
    if matches:
        return matches[0].handle
    pump_match = bot.pump_evm.cached(address) if bot.pump_evm else None
    if pump_match:
        return pump_match.handle
    profile = bot.pump_profiles.cached(address) if bot.pump_profiles else None
    return profile.username if profile else None


def _connected_targets(
    wallet: str | None, evm_wallet: str | None
) -> list[tuple[str, str]]:
    """Which (address, chain) pairs a run should cover.

    An EVM wallet is checked on the chains the cache has already seen it
    deployed on, because that is the cheapest true answer available; with no
    record it falls back to FOMO's own chains rather than every configured one.
    """
    pairs: list[tuple[str, str]] = []
    if wallet:
        pairs.append((wallet, "solana"))
    if evm_wallet:
        chains = []
        for match in find_cached_wallets(evm_wallet):
            chains.extend(match.chains)
        chains = [chain for chain in dict.fromkeys(chains) if chain] or list(
            DEFAULT_EVM_CHAINS
        )
        pairs.extend((evm_wallet, chain) for chain in chains)
    return pairs


async def _connected_wallets_for(
    interaction: discord.Interaction, target: str
) -> tuple[list[tuple[str, str]], str] | None:
    """Resolve `/connected`'s argument into wallets, or explain why it could not.

    A raw address is taken at face value -- it is already the thing being
    analysed. A handle goes through exactly the path `/fomo` uses: the cache
    first, then the same bounded enrichment, so `/connected` can never resolve
    a wallet `/fomo` would disagree with.
    """
    clean = target.strip().strip("`").lstrip("@").strip()
    # An address is analysed as itself. A 32-character base58 string is not a
    # handle anybody has, so the two cases never collide.
    if EVM_RE.fullmatch(clean):
        label = _connected_identity(clean)
        return _connected_targets(None, clean), f"@{label}" if label else clean
    if SOLANA_ADDRESS_RE.fullmatch(clean):
        label = _connected_identity(clean)
        return _connected_targets(clean, None), f"@{label}" if label else clean

    if bot.fomo:
        try:
            user = await bot.fomo.resolve(clean)
        except (FomoError, asyncio.TimeoutError) as exc:
            await _reply_error(interaction, exc, clean)
            return None
    else:
        await interaction.followup.send(
            "FOMO is unavailable, so a handle cannot be resolved. Pass a wallet "
            "address instead.", ephemeral=True,
        )
        return None

    handle = user.handle.lower()
    wallet = cached_wallet(handle) if bot.wallets else None
    evm_wallet = cached_evm_wallet(handle) if bot.evm_wallets else None
    if not wallet and not evm_wallet and (bot.wallets or bot.evm_wallets):
        # Same resolution `/fomo` runs, under the same wall clock. A handle
        # nobody has looked up yet is the common case here.
        try:
            stats = await fetch_trader_stats(bot.fomo, user.id)
            wallet, evm_wallet, _stats = await asyncio.wait_for(
                _resolve_fomo_enrichment(bot, user, stats, wallet, evm_wallet),
                timeout=FOMO_ENRICH_TIMEOUT,
            )
        except Exception as exc:
            log.info("connected: wallet resolution for @%s did not finish: %s",
                     user.handle, exc)
            wallet = wallet or (cached_wallet(handle) if bot.wallets else None)
            evm_wallet = evm_wallet or (
                cached_evm_wallet(handle) if bot.evm_wallets else None
            )

    pairs = _connected_targets(wallet, evm_wallet)
    if not pairs:
        await interaction.followup.send(
            f"No wallet is known for **@{user.handle}** yet, so there is nothing "
            "to analyse. Run `/fomo` on the handle first — it resolves and "
            "caches the wallet, and `/connected` reads the same cache.",
            ephemeral=True,
        )
        return None
    return pairs, f"@{user.handle}"


def _connected_amount(amount: float, symbol: str, usd: float | None) -> str:
    """`2.5 SOL ($412)` — the figure first, the dollars only if they are real."""
    text = f"{amount:,.4f}".rstrip("0").rstrip(".") or "0"
    text = f"{text} {symbol}".strip()
    return f"{text} ({fmt_usd(usd)})" if usd else text


def _funding_field(item: Funding) -> tuple[str, str]:
    """The funding wallet, as its own field at the top of the first page."""
    link = address_url(item.chain, item.address)
    address = _short_wallet(item.address)
    address_text = f"[`{address}`]({link})" if link else f"`{address}`"
    who = f"🔵 [@{item.identity}](https://fomo.family/profile/{item.identity})" \
        if item.identity else address_text
    lines = [who]
    if item.identity:
        lines[0] += f" · {address_text}"
    if item.label:
        lines.append(f"Labelled **{item.label}** — a service, not a person.")
    lines.append(
        f"First funded {_short_wallet(item.wallet)} with "
        f"**{_connected_amount(item.amount, item.symbol, item.usd)}**"
        + (f" on {fmt_day(item.timestamp)}" if item.timestamp else "")
    )
    tx = explorer_url(item.chain, item.reference)
    if tx:
        lines.append(f"[View the funding transaction]({tx})")
    return f"💰 Funding wallet · {item.chain.title()}", _fit_field(lines)


def _connected_entry(index: int, item: Connection) -> tuple[str, str]:
    """One connected wallet as a Discord field: who, how much, how often."""
    record = item.relationship
    link = address_url(record.chain, record.address)
    address = _short_wallet(record.address)
    address_text = f"[`{address}`]({link})" if link else f"`{address}`"
    marker = "💰 " if item.funder else ""
    name = (f"{index}. {marker}@{record.identity}" if record.identity
            else f"{index}. {marker}{address}")

    lines = [
        f"Wallet: {address_text} · {record.chain.title()}",
        f"Qualifying transfers: **{record.transfers}** "
        f"({record.sent_count} out / {record.received_count} in)",
    ]
    if record.total_usd:
        lines.append(
            f"Value moved: **{fmt_usd(record.total_usd)}** "
            f"(sent {fmt_usd(record.sent_usd)} / received "
            f"{fmt_usd(record.received_usd)})"
        )
    if record.unpriced:
        lines.append(
            f"{record.unpriced} of those could not be priced, so the total "
            "understates them."
        )
    lines.append(
        f"First: {fmt_day(record.first_seen)} · "
        f"Latest: {fmt_day(record.last_seen)} · "
        f"{record.active_days} separate dates"
    )
    if item.funder:
        lines.append("This wallet funded the analysed one.")
    if record.is_contract and not record.identity:
        lines.append("Contract code, and no known identity — treat with care.")
    return name, _fit_field(lines)


def _connected_embeds(report: ConnectedReport, label: str) -> list[discord.Embed]:
    """Every page of one `/connected` run, rendered before the first is sent."""
    items = list(report.connections)
    scope = ", ".join(
        f"{chain.title()} `{_short_wallet(address)}`"
        for address, chain in report.wallets
    ) or "—"
    header = f"**{label}** · analysed {scope}"
    if report.transactions:
        header += f"\n{report.transactions:,} transactions read"
    header += f"\n{CONNECTED_RULE}"

    pages = max(1, -(-len(items) // CONNECTED_PAGE))
    embeds: list[discord.Embed] = []
    for page in range(1, pages + 1):
        embed = discord.Embed(
            title="🔗 Connected wallets",
            description=header,
            colour=WIN if items else BRAND,
        )
        # The funder belongs on the first page whether or not anything else
        # cleared the bar -- it is usually the answer somebody came for.
        if page == 1:
            for item in report.funding[:2]:
                name, value = _funding_field(item)
                embed.add_field(name=name[:256], value=value, inline=False)
            if not report.funding:
                embed.add_field(
                    name="💰 Funding wallet",
                    value="Not determined — see the notes below.",
                    inline=False,
                )
        if items:
            start = (page - 1) * CONNECTED_PAGE
            for offset, item in enumerate(
                items[start:start + CONNECTED_PAGE], start + 1
            ):
                name, value = _connected_entry(offset, item)
                embed.add_field(name=name[:256], value=value, inline=False)
        elif page == 1:
            embed.add_field(
                name="Connected wallets",
                value=(
                    "**No wallet cleared the transfer bar.** That is the "
                    "intended answer when a trader only ever swaps: buying and "
                    "selling on Jupiter, Raydium or Meteora connects them to a "
                    "pool, not to a person, so none of it is counted here."
                ),
                inline=False,
            )
        for warning in report.warnings[:2]:
            embed.add_field(name="Note", value=warning[:1024], inline=False)
        embed.set_footer(
            text=f"Page {page} of {pages} · {CONNECTED_DISCLAIMER}"
        )
        embeds.append(embed)
    return embeds


def _connected_evidence_embed(item: Connection) -> discord.Embed:
    """The transactions behind one wallet, so the claim can be checked."""
    record = item.relationship
    embed = discord.Embed(
        title=f"🔍 Evidence · {_short_wallet(record.address)}",
        description=(
            f"{record.chain.title()} · **{record.transfers}** qualifying "
            f"transfers between `{_short_wallet(record.known_wallet)}` and "
            f"`{_short_wallet(record.address)}`"
        ),
        colour=BRAND,
    )
    links = []
    for reference in record.references[:10]:
        url = explorer_url(record.chain, reference)
        short = f"{reference[:10]}…{reference[-6:]}" if len(reference) > 20 else reference
        links.append(f"• [{short}]({url})" if url else f"• `{short}`")
    embed.add_field(
        name=f"Transactions ({len(record.references)} kept)",
        value=_fit_field(links) if links else "No transaction reference was kept.",
        inline=False,
    )
    embed.add_field(
        name="What was counted",
        value=_fit_field([
            f"• {CONNECTED_RULE}",
            f"• Sent {fmt_usd(record.sent_usd)} in {record.sent_count} "
            f"transfers, received {fmt_usd(record.received_usd)} in "
            f"{record.received_count}",
            f"• {fmt_day(record.first_seen)} – {fmt_day(record.last_seen)}, "
            f"on {record.active_days} separate dates",
        ]),
        inline=False,
    )
    embed.set_footer(text=CONNECTED_DISCLAIMER)
    return embed


class ConnectedEvidenceSelect(discord.ui.Select):
    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, ConnectedView):
            await view.show_evidence(interaction, self.values[0])


class ConnectedView(PaginatedEmbedView):
    """`/connected`'s pager and its evidence drawer.

    Every page is rendered up front -- the analysis is already paid for by the
    time the card exists -- so paging is a message edit. Evidence is sent
    ephemerally rather than replacing the card, because it is a drill-down on
    one row, not another page.
    """

    def __init__(
        self, report: ConnectedReport, label: str, *, timeout: float = 3600
    ) -> None:
        self.report = report
        super().__init__(_connected_embeds(report, label), timeout=timeout)
        self._install_select()

    def _install_select(self) -> None:
        for child in list(self.children):
            if isinstance(child, ConnectedEvidenceSelect):
                self.remove_item(child)
        items = list(self.report.connections)[:25]
        if not items:
            return
        options = [
            discord.SelectOption(
                label=(f"@{item.relationship.identity}"
                       if item.relationship.identity
                       else _short_wallet(item.relationship.address))[:100],
                value=f"{item.chain}:{item.address}"[:100],
                description=(
                    f"{item.relationship.transfers} transfers · "
                    f"{fmt_usd(item.relationship.total_usd)}"
                )[:100],
            )
            for item in items
        ]
        self.add_item(ConnectedEvidenceSelect(
            placeholder="Inspect the transactions behind a wallet",
            options=options, min_values=1, max_values=1, row=1,
        ))

    async def show_evidence(
        self, interaction: discord.Interaction, value: str
    ) -> None:
        match = next(
            (item for item in self.report.connections
             if f"{item.chain}:{item.address}"[:100] == value), None
        )
        if match is None:
            await interaction.response.send_message(
                "That wallet is no longer on this card.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=_connected_evidence_embed(match), ephemeral=True
        )


@bot.tree.command(
    name="connected",
    description="The funding wallet, and wallets a trader moves real money with",
)
@app_commands.describe(
    target="FOMO username, or a Solana / EVM wallet address",
    fresh="Re-read the chain instead of using the cached run",
)
async def connected_cmd(
    interaction: discord.Interaction, target: str, fresh: bool = False,
) -> None:
    """Who funded this wallet, and who it actually sends money to.

    Only direct transfers count, and only ones big enough to mean something:
    1+ SOL or 50+ USDC on Solana, $200+ on EVM. Swaps are not connections --
    a Jupiter, Raydium or Meteora trade ties a wallet to a liquidity pool, not
    to a person -- so they never reach this card, and neither do exchanges,
    routers, program accounts or high-degree service wallets.
    """
    if not await _safe_defer(interaction):
        return
    if not bot.connected:
        await interaction.followup.send(
            "Wallet connection analysis is unavailable because the HTTP client "
            "did not start.", ephemeral=True,
        )
        return

    resolved = await _connected_wallets_for(interaction, target)
    if resolved is None:
        return
    pairs, label = resolved

    try:
        report = await bot.connected.analyse(pairs, fresh=fresh)
    except Exception as exc:
        log.exception("connected analysis failed for %s", label)
        await interaction.followup.send(
            f"Connection analysis failed: `{str(exc)[:180]}`", ephemeral=True
        )
        return

    log.info(
        "connected: %s -> %d wallet(s), %d funder(s), from %d transactions",
        label, len(report.connections), len(report.funding), report.transactions,
    )
    view = ConnectedView(report, label)
    await interaction.followup.send(embed=view.embeds[0], view=view)


def _tracked_entries(channel_id: int) -> list[tuple[str, dict[str, Any]]]:
    entries = [
        *(('FOMO', entry) for entry in bot.tracking.for_channel(channel_id)),
        *(('Pump', entry) for entry in bot.pump_tracking.for_channel(channel_id)),
    ]
    return sorted(
        entries,
        key=lambda item: (str(item[1].get("handle") or "").casefold(), item[0]),
    )


def _tracked_line(index: int, platform: str, entry: dict[str, Any]) -> str:
    """One row of `/tracked`, in each platform's own link shape."""
    handle = str(entry.get("handle") or "unknown")
    filters = activity_filter_label(_entry_activity_filters(entry))
    if platform == "FOMO":
        return (
            f"`{index:>2}.` 🔵 [@{handle}](https://fomo.family/profile/{handle})"
            f" · {filters}"
        )
    address = str(entry.get("userId") or "")
    return (
        f"`{index:>2}.` 🟢 {_pump_username_link(handle, address)} · "
        f"{_pump_wallet_link(address)} · {filters}"
    )


def _tracked_embeds(entries: list[tuple[str, dict[str, Any]]]) -> list[discord.Embed]:
    """The combined list `/fomotracked` and `/pumptracked` used to show apart."""
    lines = [_tracked_line(index, platform, entry)
             for index, (platform, entry) in enumerate(entries, 1)]
    chunks: list[list[str]] = [[]]
    for line in lines:
        if chunks[-1] and len("\n".join(chunks[-1] + [line])) > 3800:
            chunks.append([])
        chunks[-1].append(line)

    threshold = (f" • minimum Pump trade {fmt_usd(PUMP_MIN_TRADE_USD)}"
                 if PUMP_MIN_TRADE_USD > 0 else "")
    embeds = []
    for index, chunk in enumerate(chunks, 1):
        suffix = f" · {index}/{len(chunks)}" if len(chunks) > 1 else ""
        embed = discord.Embed(
            title=f"🔔 Tracked in this channel · {len(entries)}{suffix}",
            description="\n".join(chunk) or "Nothing is being tracked.",
            colour=BRAND,
        )
        embed.set_footer(
            text=f"🔵 FOMO · 🟢 Pump.fun • large swaps ≥ "
                 f"{fmt_usd(LARGE_SWAP_USD)}{threshold}"
        )
        embeds.append(embed)
    return embeds


def _remove_tracked_entries(
    channel_id: int, selected: list[tuple[str, dict[str, Any]]]
) -> list[str]:
    """Drop each selected subscription, returning what was actually removed."""
    removed: list[str] = []
    for platform, entry in selected:
        user_id = str(entry.get("userId") or "")
        store = bot.tracking if platform == "FOMO" else bot.pump_tracking
        if user_id and store.remove(channel_id, user_id):
            removed.append(f"{platform} **@{entry.get('handle', 'unknown')}**")
    return removed


def _alert_settings_view(
    requester_id: int, channel_id: int, platform: str, entry: dict[str, Any]
) -> ActivitySelectionView:
    """The alert picker behind `/tracked`'s Edit button.

    Each platform offers a different third alert type -- theses on FOMO,
    callouts on Pump -- so the option list and the store both follow from the
    entry's platform rather than from which command opened the picker.
    """
    is_fomo = platform == "FOMO"
    store = bot.tracking if is_fomo else bot.pump_tracking
    allowed = FOMO_ACTIVITY_FILTERS if is_fomo else PUMP_ACTIVITY_FILTERS
    labels = (
        (("buys", "Buys", "🟢"), ("sells", "Sells", "🔴"), ("theses", "Theses", "📝"))
        if is_fomo else
        (("buys", "Buys", "🟢"), ("sells", "Sells", "🔴"), ("callouts", "Callouts", "📣"))
    )
    defaults = normalize_activity_filters(_entry_activity_filters(entry), allowed)
    user_id = str(entry.get("userId") or "")
    handle = str(entry.get("handle") or "unknown")

    async def save_settings(
        _activity_interaction: discord.Interaction, selected_filters: list[str]
    ) -> str:
        updated = store.set_activity_filters(channel_id, user_id, selected_filters)
        if not updated:
            return f"The {platform} subscription for **@{handle}** no longer exists."
        return (
            f"Updated {platform} alerts for **@{handle}** · "
            f"**{activity_filter_label(selected_filters)}**."
        )

    return ActivitySelectionView(
        requester_id, _activity_options(labels, defaults), save_settings
    )


@bot.tree.command(
    name="tracked", description="Everything tracked in this channel"
)
async def tracked_cmd(interaction: discord.Interaction) -> None:
    entries = _tracked_entries(interaction.channel_id)
    if not entries:
        await interaction.response.send_message(
            "Nothing is being tracked in this channel yet. "
            "Use `/track` to add a FOMO trader or a Pump profile."
        )
        return

    embeds = _tracked_embeds(entries)
    view = TrackedManagerView(interaction.user.id, entries, interaction.channel_id)
    if len(entries) > 25:
        view.selector.placeholder = (
            "Select from the first 25 tracked profiles"
        )
    await interaction.response.send_message(embed=embeds[0], view=view)
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed)


async def _track_fomo(interaction: discord.Interaction, handle: str) -> None:
    """The FOMO half of `/track`. The interaction is already deferred."""
    if not bot.fomo:
        await interaction.followup.send(
            "FOMO support is unavailable.", ephemeral=True
        )
        return
    try:
        user = await bot.fomo.resolve(handle)
        swaps_data, trades_data = await asyncio.gather(
            bot.fomo.swaps(user.id, limit=25, fresh=True),
            bot.fomo.trades(user.id, fresh=True),
        )
    except (FomoError, asyncio.TimeoutError) as exc:
        await _reply_error(interaction, exc, handle)
        return
    state = snapshot(swaps_data, trades_data)
    existing = bot.tracking.tracks.get(
        bot.tracking.key(interaction.channel_id, user.id), {}
    )
    defaults = normalize_activity_filters(
        _entry_activity_filters(existing), FOMO_ACTIVITY_FILTERS
    ) if existing else ()

    async def save_selection(
        _menu_interaction: discord.Interaction, selected: list[str]
    ) -> str:
        added = bot.tracking.add(
            interaction.channel_id,
            interaction.guild_id,
            user.id,
            user.handle,
            state,
            selected,
        )
        verb = "Now tracking" if added else "Tracking updated for"
        return (
            f"{verb} **@{user.handle}** in this channel · "
            f"**{activity_filter_label(selected)}**."
        )

    view = ActivitySelectionView(
        interaction.user.id,
        _activity_options(
            (
                ("buys", "Buys", "🟢"),
                ("sells", "Sells", "🔴"),
                ("theses", "Theses", "📝"),
            ),
            defaults,
        ),
        save_selection,
    )
    await interaction.followup.send(
        f"Choose which alerts to receive for **@{user.handle}** (select 1–3):",
        view=view,
    )


@bot.tree.command(name="pump", description="Look up a Pump.fun profile")
@app_commands.describe(handle="Pump username or Solana wallet")
async def pump_cmd(interaction: discord.Interaction, handle: str) -> None:
    if not await _safe_defer(interaction):
        return
    if not bot.pump:
        await interaction.followup.send("Pump support is unavailable: httpx is not installed.", ephemeral=True)
        return
    # The card shows follower counts and an avatar, which move; holder
    # labelling does not care. One store, a shorter freshness bar here.
    user = await _resolve_pump_user(interaction, handle, max_age=PUMP_CARD_TTL)
    if user is None:
        return

    results = await asyncio.gather(
        bot.pump.portfolio(user.address),
        bot.pump.holdings(user.address, limit=8),
        bot.pump.callouts(user.address, limit=8),
        bot.pump.created_coins(user.address, limit=5),
        (bot.pump_evm.resolve(user) if bot.pump_evm else asyncio.sleep(0, result=None)),
        return_exceptions=True,
    )
    portfolio = results[0] if isinstance(results[0], PumpPortfolio) else PumpPortfolio()
    holdings = results[1] if isinstance(results[1], list) else []
    callouts = results[2] if isinstance(results[2], list) else []
    created_count, created_coins = results[3] if isinstance(results[3], tuple) else (0, [])
    evm_match = results[4] if isinstance(results[4], PumpEvmMatch) else None
    mints = {item.mint for item in callouts}
    callout_coins = await bot.pump.coins(mints)
    embed = build_pump_embed(
        user,
        portfolio,
        holdings,
        callouts,
        created_count,
        created_coins,
        callout_coins=callout_coins,
        evm_match=evm_match,
    )
    await interaction.followup.send(embed=embed)


async def _track_pump(interaction: discord.Interaction, handle: str) -> None:
    """The Pump half of `/track`. The interaction is already deferred."""
    if not bot.pump:
        await interaction.followup.send("Pump support is unavailable.", ephemeral=True)
        return
    user = await _resolve_pump_user(interaction, handle)
    if user is None:
        return

    callout_result, signature_result = await asyncio.gather(
        bot.pump.callouts(user.address, limit=30),
        (bot.pump_chain.recent_signature_ids(user.address)
         if bot.pump_chain else asyncio.sleep(0, result=[])),
        return_exceptions=True,
    )
    callouts = callout_result if isinstance(callout_result, list) else []
    signatures = signature_result if isinstance(signature_result, list) else []
    state = pump_snapshot(signatures, callouts)
    state["calloutBaselineReady"] = not isinstance(callout_result, Exception)
    state["signatureBaselineReady"] = not isinstance(signature_result, Exception)
    pending = []
    if not state["signatureBaselineReady"]:
        pending.append("trade RPC will baseline when it recovers")
    if not state["calloutBaselineReady"]:
        pending.append("callouts will baseline when Pump recovers")
    suffix = f" ({'; '.join(pending)})" if pending else ""
    existing = bot.pump_tracking.tracks.get(
        bot.pump_tracking.key(interaction.channel_id, user.address), {}
    )
    defaults = normalize_activity_filters(
        _entry_activity_filters(existing), PUMP_ACTIVITY_FILTERS
    ) if existing else ()

    async def save_selection(
        _menu_interaction: discord.Interaction, selected: list[str]
    ) -> str:
        added = bot.pump_tracking.add(
            interaction.channel_id,
            interaction.guild_id,
            user.address,
            user.username,
            state,
            selected,
        )
        verb = "Now tracking" if added else "Tracking updated for"
        return (
            f"{verb} {_pump_username_link(user.username, user.address)} "
            "in this channel · "
            f"**{activity_filter_label(selected)}**.{suffix}"
        )

    view = ActivitySelectionView(
        interaction.user.id,
        _activity_options(
            (
                ("buys", "Buys", "🟢"),
                ("sells", "Sells", "🔴"),
                ("callouts", "Callouts", "📣"),
            ),
            defaults,
        ),
        save_selection,
    )
    await interaction.followup.send(
        f"Choose which alerts to receive for "
        f"{_pump_username_link(user.username, user.address)} (select 1–3):",
        view=view,
    )


@bot.tree.command(
    name="track", description="Alert this channel about a FOMO or Pump profile"
)
@app_commands.describe(
    platform="Which platform the profile lives on",
    target="FOMO username, or a Pump username or Solana wallet",
)
@app_commands.choices(platform=[
    app_commands.Choice(name="FOMO", value="fomo"),
    app_commands.Choice(name="Pump.fun", value="pump"),
])
async def track_cmd(
    interaction: discord.Interaction,
    platform: app_commands.Choice[str],
    target: str,
) -> None:
    """One entry point for both trackers.

    `/fomotrack` and `/pumptrack` differed only in which profile they resolved
    and which third alert type they offered, so the platform is an argument
    rather than a command name. Each half still owns its own resolution,
    baseline snapshot and alert picker.
    """
    if not await _safe_defer(interaction):
        return
    if platform.value == "pump":
        await _track_pump(interaction, target)
    else:
        await _track_fomo(interaction, target)


@bot.tree.command(name="fomotop", description="FOMO leaderboard")
@app_commands.describe(period="24h or all-time", count="How many (1-25)")
@app_commands.choices(
    period=[
        app_commands.Choice(name="24h", value="24h"),
        app_commands.Choice(name="all-time", value="all"),
    ]
)
async def fomo_top_cmd(
    interaction: discord.Interaction,
    period: app_commands.Choice[str] | None = None,
    count: int = 10,
) -> None:
    if not await _safe_defer(interaction):
        return
    assert bot.fomo
    value = period.value if period else "24h"
    count = max(1, min(count, 25))
    try:
        users = await bot.fomo.leaderboard(None if value == "all" else value, limit=count)
    except (FomoError, asyncio.TimeoutError) as exc:
        await _reply_error(interaction, exc, "leaderboard")
        return
    lines = []
    for i, u in enumerate(users, 1):
        pnl = u.raw.get("totalPnL", u.raw.get("pnl24h"))
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`{i:>2}.`")
        lines.append(
            f"{medal} [{u.display_name}]({u.profile_url}) — {fmt_usd(pnl)} · "
            f"{fmt_usd(u.total_volume)} vol"
        )
    embed = discord.Embed(
        title=f"🪙 FOMO leaderboard · {'all-time' if value == 'all' else value}",
        description="\n".join(lines) or "empty",
        colour=BRAND,
    )
    await interaction.followup.send(embed=embed)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN missing from .env")
    if not claim_single_instance():
        raise SystemExit(
            f"another fomo_bot is already running (port "
            f"{SINGLE_INSTANCE_PORT} is held). Two processes on one bot token "
            f"both receive interactions and one of them always fails to "
            f"acknowledge with 10062. Close the other one, or set "
            f"FOMO_SINGLE_INSTANCE=0 if you really mean to run two."
        )
    # `basicConfig` above already owns the root logger. Left to itself
    # `run()` adds a second handler to it, which is why every record --
    # tracebacks included -- was printed twice in two different formats.
    bot.run(DISCORD_TOKEN, log_handler=None)
