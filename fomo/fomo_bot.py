"""
fomo_bot.py — standalone Discord bot: FOMO trader research.

    /fomo <handle>        rich embed for a fomo.family trader
    /wallet <address>     find a FOMO trader by verified wallet
    /fomotrack <handle>   choose and track trader activity
    /fomotracked          list traders tracked in this channel
    /fomountrack <handle> stop alerts in this channel
    /untrack                interactively remove FOMO or Pump tracking
    /tracksettings          change an existing subscription's alerts
    /token <address>        token market cap, image and top holders
    /fomosearch <term>    fuzzy handle search
    /fomotop [24h] [n]    leaderboard
    /pump <handle>         rich Pump.fun profile
    /pumptrack <handle>   choose and track Pump activity
    /pumptracked           list Pump profiles tracked in this channel
    /pumpuntrack <handle>  stop Pump alerts in this channel
    /pumpwallet <address>  find a Pump profile by wallet

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

from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

import discord
from discord import app_commands
from dotenv import load_dotenv

# Wallet modules read their RPC/cache settings at import time.
load_dotenv()

from fomo_evm import EvmWalletResolver
from fomo_evm_activity import fetch_evm_activity
from fomo_features import (
    TraderStats,
    fetch_trader_stats,
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
from fomo_wallet import CachedWalletMatch, WalletResolver, find_cached_wallets
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
    PumpNotFound,
    PumpPortfolio,
    PumpUser,
    quote_value_sol,
    quote_value_usd,
)
from pump_chain import PumpChainClient, PumpRpcError
from pump_evm import EVM_RE, PumpEvmMatch, PumpEvmResolver
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

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional: instant command sync while testing
REFRESH_TOKEN = os.getenv("FOMO_PRIVY_REFRESH_TOKEN", "")
ACCESS_TOKEN = os.getenv("FOMO_PRIVY_ACCESS_TOKEN", "")
# Derive each trader's real wallet on chain. Needs SOLANA_RPC + httpx.
RESOLVE_WALLETS = os.getenv("FOMO_RESOLVE_WALLETS", "1").strip() not in ("0", "false", "no")
RESOLVE_EVM = os.getenv("FOMO_RESOLVE_EVM", "1").strip() not in ("0", "false", "no")
TRACK_FILE = Path(os.getenv("FOMO_TRACK_FILE", "fomo_tracks.json"))
FOMO_TRACK_INTERVAL = max(1.0, float(os.getenv("FOMO_TRACK_INTERVAL", "60")))
LARGE_SWAP_USD = max(0.0, float(os.getenv("FOMO_LARGE_SWAP_USD", "1000")))
PUMP_TRACK_FILE = Path(os.getenv("PUMP_TRACK_FILE", "pump_tracks.json"))
PUMP_TRACK_INTERVAL = max(
    0.25,
    float(os.getenv("PUMP_TRACK_INTERVAL", os.getenv("FOMO_TRACK_INTERVAL", "60"))),
)
PUMP_EVM_CACHE_FILE = Path(os.getenv("PUMP_EVM_CACHE_FILE", "pump_evm_cache.json"))
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


class TrackedEntrySelect(discord.ui.Select):
    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, TrackedEntrySelectionView):
            await view.submit(interaction, [int(value) for value in self.values])


class TrackedEntrySelectionView(discord.ui.View):
    """Select existing subscriptions without retyping exact handles."""

    def __init__(
        self,
        requester_id: int,
        entries: list[tuple[str, dict[str, Any]]],
        on_submit: Callable[
            [discord.Interaction, list[tuple[str, dict[str, Any]]]], Awaitable[None]
        ],
        *,
        multiple: bool,
        placeholder: str,
    ) -> None:
        super().__init__(timeout=180)
        self.requester_id = requester_id
        self.entries = entries[:25]
        self.on_submit = on_submit
        options = [
            discord.SelectOption(
                label=f"{platform} · @{entry.get('handle', 'unknown')}"[:100],
                value=str(index),
                description=activity_filter_label(_entry_activity_filters(entry))[:100],
                emoji="🔵" if platform == "FOMO" else "🟢",
            )
            for index, (platform, entry) in enumerate(self.entries)
        ]
        self.add_item(TrackedEntrySelect(
            placeholder=placeholder,
            min_values=1,
            max_values=len(options) if multiple else 1,
            options=options,
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who ran this command can use this menu.",
            ephemeral=True,
        )
        return False

    async def submit(self, interaction: discord.Interaction, indexes: list[int]) -> None:
        selected = [self.entries[index] for index in indexes if 0 <= index < len(self.entries)]
        self.stop()
        await self.on_submit(interaction, selected)


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
        for index, activity in enumerate(stats.latest_buys, 1):
            when = iso_to_unix(activity.created_at)
            relative = f" · <t:{when}:R>" if when is not None else ""
            amount = f" · {fmt_usd(activity.usd_value)}" if activity.usd_value is not None else ""
            if activity.market_cap is None:
                market_cap = " · MC —"
            else:
                estimate = "~" if activity.market_cap_estimated else ""
                market_cap = f" · MC {estimate}{fmt_usd(activity.market_cap)}"
            chain = f" · {activity.chain}" if activity.chain else ""
            lines.append(
                f"`{index}.` **{activity.symbol}**{amount}{market_cap}{chain}{relative}"
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
            trade_url = padre_trade_url(event.network_id, event.token_address)
            label = f"${event.symbol.lstrip('$') or 'TOKEN'}"
            token = f"[{label}]({trade_url})" if trade_url else f"**{label}**"
            lines.append(f"🔴 {token}{amount} · {chain}{relative}")
        embed.add_field(name="Latest sells", value="\n".join(lines), inline=False)
    elif activity_filter == "sells":
        embed.add_field(name="Latest sells", value="No recent sells found.", inline=False)

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
    # real Solana wallet is derived on chain; EVM is accepted only from the
    # verified identity index plus an ERC-4337 deployment probe.
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
    embed.add_field(name="Solana wallet", value=f"◎ `{user.address}`", inline=False)
    if evm_match:
        embed.add_field(name="EVM wallet", value=f"Ξ `{evm_match.evm}`", inline=False)
    links = [f"[Pump]({user.profile_url})"]
    if user.x_url:
        links.append(f"[X]({user.x_url})")
    embed.add_field(name="Links", value=" · ".join(links), inline=False)
    embed.set_footer(text="pump.fun")
    return embed


def build_pump_track_embed(handle: str, event: PumpAlert) -> discord.Embed:
    clean_handle = handle.strip().lstrip("@")
    profile = f"https://pump.fun/profile/{clean_handle}"
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
            url=trade_url or profile,
            description=f"💰 **{amount}** · MC **{cap}**\n\n`{event.mint}`",
            colour=colour,
            timestamp=timestamp,
        )
    if event.image_url:
        embed.set_thumbnail(url=event.image_url)
    embed.set_footer(text="Pump tracker")
    return embed


class FomoBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.fomo: FomoClient | None = None
        self.wallets: WalletResolver | None = None
        self.evm_wallets: EvmWalletResolver | None = None
        self.pump: PumpClient | None = None
        self.pump_evm: PumpEvmResolver | None = None
        self.pump_chain: PumpChainClient | None = None
        self.tokens: TokenIntelligenceClient | None = None
        self._http: Any = None
        self.tracking = TrackingStore(TRACK_FILE)
        self.pump_tracking = PumpTrackingStore(PUMP_TRACK_FILE)
        self._tracking_tasks: list[asyncio.Task[None]] = []
        self._guild_commands_synced = False

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
            self.pump_chain = PumpChainClient(self._http, SOLANA_RPCS)
            self.tokens = TokenIntelligenceClient(self._http, SOLANA_RPCS)
            if RESOLVE_WALLETS:
                self.wallets = WalletResolver(self._http, SOLANA_RPCS)
            if RESOLVE_EVM:
                self.evm_wallets = EvmWalletResolver(self._http)
        except ImportError:
            log.warning("httpx not installed - Pump and wallet resolution disabled")

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
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
        names = ", ".join(command.name for command in self.tree.get_commands())
        log.info("🪙 slash commands synced: %s", names)

    async def on_ready(self) -> None:
        # Global application commands can take a while to propagate. Copy them
        # into every connected guild once per process so new commands such as
        # /fomotrack appear immediately after restart.
        if self._guild_commands_synced:
            return
        all_synced = bool(self.guilds)
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                names = ", ".join(command.name for command in synced)
                log.info("synced %d command(s) instantly to guild %s: %s",
                         len(synced), guild.id, names)
            except discord.HTTPException as exc:
                all_synced = False
                log.warning("guild command sync failed for %s: %s", guild.id, exc)
        self._guild_commands_synced = all_synced

    async def close(self) -> None:
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
                    self.fomo.swaps(str(entry["userId"]), limit=25, fresh=True),
                    self.fomo.trades(str(entry["userId"]), fresh=True),
                )
                events = detect_events(swaps_data, trades_data, entry, LARGE_SWAP_USD)
                state = snapshot(swaps_data, trades_data, entry)
                alerts = [
                    event for event in events
                    if activity_allowed(_entry_activity_filters(entry), event.kind)
                ]
                if alerts:
                    await self._send_track_alert(entry, alerts)
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
            await channel.send(  # type: ignore[union-attr]
                embed=build_track_embed(
                    handle, event, market_cap, native_value, native_symbol
                )
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
        for alert in alerts:
            await channel.send(  # type: ignore[union-attr]
                embed=build_pump_track_embed(handle, alert)
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


async def _reply_pump_error(
    interaction: discord.Interaction, exc: Exception, term: str
) -> None:
    if isinstance(exc, PumpNotFound):
        message = f"No Pump.fun profile found for **{term}**."
    else:
        log.warning("Pump lookup failed for %s: %s", term, exc)
        message = f"Pump.fun lookup failed: `{str(exc)[:180]}`"
    await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(name="fomo", description="Look up a fomo.family trader")
@app_commands.describe(handle="FOMO username, e.g. Binkieee")
async def fomo_cmd(interaction: discord.Interaction, handle: str) -> None:
    await interaction.response.defer()
    assert bot.fomo
    try:
        user = await bot.fomo.resolve(handle)
    except (FomoError, asyncio.TimeoutError) as exc:
        await _reply_error(interaction, exc, handle)
        return

    # Start the profile panels while wallet enrichment runs. Browser API calls
    # serialize inside BrowserTransport; independent RPC work does not.
    stats_task = asyncio.create_task(fetch_trader_stats(bot.fomo, user.id))

    # Cached after the first lookup, so this is free from then on. Never fatal:
    # resolve() swallows its own errors and returns None.
    # Both use wallet_cache.json. Keep first-time writes sequential so one
    # resolver cannot overwrite the other's freshly saved address. Each
    # resolver degrades to None and becomes a permanent cache hit on success.
    wallet = await bot.wallets.resolve(bot.fomo, user) if bot.wallets else None
    stats = await stats_task
    if wallet is None and bot.wallets and stats.raw_balances is not None:
        wallet = await bot.wallets.resolve_from_balances(user, stats.raw_balances)
    evm_wallet = (
        await bot.evm_wallets.resolve(user, balances=stats.raw_balances)
        if bot.evm_wallets else None
    )
    evm_activity_task = asyncio.create_task(fetch_evm_activity(bot._http, evm_wallet)) \
        if evm_wallet and bot._http else None
    if evm_activity_task:
        try:
            evm_buys, evm_sells = await evm_activity_task
            stats = merge_latest_buys(stats, evm_buys)
            stats = merge_latest_sells(stats, evm_sells)
        except Exception as exc:
            log.warning("EVM activity lookup failed for @%s: %s", user.handle, exc)
    embed = build_embed(user, wallet, evm_wallet, stats)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="wallet", description="Find FOMO and Pump profiles by wallet")
@app_commands.describe(address="Solana or EVM wallet address")
async def wallet_cmd(interaction: discord.Interaction, address: str) -> None:
    await interaction.response.defer()
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

    pump_user: PumpUser | None = None
    pump_evm = bot.pump_evm.cached(query) if bot.pump_evm else None
    if bot.pump:
        try:
            pump_user = await bot.pump.resolve(pump_evm.solana if pump_evm else query)
        except (PumpError, asyncio.TimeoutError):
            pass

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

    if pump_user:
        network = "EVM + Solana" if pump_evm else "Solana"
        lines.append(
            f"🟢 **Pump.fun** · [@{pump_user.username}]({pump_user.profile_url})\n"
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
    elif len(lines) == 1 and pump_user and pump_user.profile_image:
        embed.set_thumbnail(url=pump_user.profile_image)
    embed.set_footer(text=f"FOMO + Pump identity search • {len(lines)} {noun}")
    await interaction.followup.send(embed=embed)


def _wallet_match_verification(match: CachedWalletMatch) -> str:
    if match.network == "Solana":
        count = match.confirmations or 0
        evidence = f"{count} on-chain confirmation{'s' if count != 1 else ''}"
        return f"◎ **Solana** · {evidence}"
    chains = ", ".join(chain.upper() for chain in match.chains)
    chain_text = f" · {chains}" if chains else ""
    source = "FomoScan verified" if match.source == "fomoscan" else "Verified mapping"
    return f"Ξ **EVM**{chain_text} · {source}"


def _short_wallet(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if len(address) > 13 else address


def _holder_explorer(chain: str, address: str) -> str | None:
    bases = {
        "Solana": "https://solscan.io/account/",
        "Ethereum": "https://etherscan.io/address/",
        "BSC": "https://bscscan.com/address/",
        "Base": "https://basescan.org/address/",
        "Robinhood": "https://robinhoodchain.blockscout.com/address/",
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


async def _holder_label(holder: TokenHolder, chain: str) -> str:
    identities: list[str] = []
    for match in find_cached_wallets(holder.address):
        identities.append(
            f"🔵 [@{match.handle}](https://fomo.family/profile/{match.handle})"
        )

    pump_match = bot.pump_evm.cached(holder.address) if bot.pump_evm else None
    pump_user: PumpUser | None = None
    if pump_match:
        identities.append(
            f"🟢 [@{pump_match.handle}](https://pump.fun/profile/{pump_match.handle})"
        )
    elif chain == "Solana" and bot.pump:
        try:
            pump_user = await bot.pump.resolve(holder.address)
        except (PumpError, asyncio.TimeoutError):
            pass
        if pump_user:
            identities.append(f"🟢 [@{pump_user.username}]({pump_user.profile_url})")

    explorer = _holder_explorer(chain, holder.address)
    wallet = _short_wallet(holder.address)
    wallet_text = f"[{wallet}]({explorer})" if explorer else f"`{wallet}`"
    identity = " + ".join(dict.fromkeys(identities)) if identities else wallet_text
    if identities:
        identity += f" · {wallet_text}"
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


@bot.tree.command(name="token", description="Show token market cap and top holders")
@app_commands.describe(
    address="Solana or EVM token contract address",
    holders="Show the top 5 or top 10 holder wallets",
)
@app_commands.choices(holders=[
    app_commands.Choice(name="Top 5", value=5),
    app_commands.Choice(name="Top 10", value=10),
])
async def token_cmd(
    interaction: discord.Interaction,
    address: str,
    holders: app_commands.Choice[int] | None = None,
) -> None:
    await interaction.response.defer()
    if not bot.tokens:
        await interaction.followup.send(
            "Token intelligence is unavailable because the HTTP client did not start.",
            ephemeral=True,
        )
        return

    clean = address.strip().strip("`").strip()
    count = holders.value if holders else 5
    pump_coin: PumpCoin | None = None
    if bot.pump and not EVM_RE.fullmatch(clean):
        try:
            pump_coin = await bot.pump.coin(clean)
        except (PumpError, asyncio.TimeoutError):
            pass
    try:
        token = await bot.tokens.lookup(clean, limit=count, pump_coin=pump_coin)
    except TokenIntelligenceError as exc:
        await interaction.followup.send(
            f"Token lookup failed: `{str(exc)[:180]}`", ephemeral=True
        )
        return

    holder_lines = await asyncio.gather(
        *(_holder_label(holder, token.chain) for holder in token.holders)
    )
    numbered = [f"`{index}.` {line}" for index, line in enumerate(holder_lines, 1)]
    market_cap = fmt_usd(token.market_cap) if token.market_cap is not None else "—"
    description = f"**Market cap:** {market_cap}"
    if token.price_usd is not None:
        description += f"\n**Price:** {fmt_usd(token.price_usd)}"
    if token.fdv is not None and token.market_cap is None:
        description += f"\n**FDV:** {fmt_usd(token.fdv)}"

    embed = discord.Embed(
        title=f"🪙 ${token.symbol} · {token.chain}",
        url=_token_trade_url(token),
        description=description,
        colour=BRAND,
    )
    embed.add_field(name="Contract address", value=f"`{token.address}`", inline=False)
    holder_chunks = _discord_line_chunks(numbered)
    if not holder_chunks:
        holder_chunks = ["Holder data is currently unavailable."]
    for index, chunk in enumerate(holder_chunks, 1):
        suffix = f" · {index}/{len(holder_chunks)}" if len(holder_chunks) > 1 else ""
        embed.add_field(
            name=f"Top holders · {len(token.holders)}{suffix}",
            value=chunk,
            inline=False,
        )
    if token.image_url:
        embed.set_thumbnail(url=token.image_url)
    embed.set_footer(
        text="🔵 FOMO · 🟢 Pump.fun • identities do not need to be tracked"
    )
    await interaction.followup.send(embed=embed)


def _tracked_entries(channel_id: int) -> list[tuple[str, dict[str, Any]]]:
    entries = [
        *(('FOMO', entry) for entry in bot.tracking.for_channel(channel_id)),
        *(('Pump', entry) for entry in bot.pump_tracking.for_channel(channel_id)),
    ]
    return sorted(
        entries,
        key=lambda item: (str(item[1].get("handle") or "").casefold(), item[0]),
    )


@bot.tree.command(name="untrack", description="Select tracked profiles to remove")
async def untrack_cmd(interaction: discord.Interaction) -> None:
    entries = _tracked_entries(interaction.channel_id)
    if not entries:
        await interaction.response.send_message(
            "Nothing is being tracked in this channel.", ephemeral=True
        )
        return

    async def remove_selected(
        menu_interaction: discord.Interaction,
        selected: list[tuple[str, dict[str, Any]]],
    ) -> None:
        removed: list[str] = []
        for platform, entry in selected:
            user_id = str(entry.get("userId") or "")
            store = bot.tracking if platform == "FOMO" else bot.pump_tracking
            if user_id and store.remove(interaction.channel_id, user_id):
                removed.append(f"{platform} **@{entry.get('handle', 'unknown')}**")
        message = "Stopped tracking " + ", ".join(removed) + "." if removed else "No subscriptions were removed."
        await menu_interaction.response.edit_message(content=message, view=None)

    visible = entries[:25]
    suffix = " Only the first 25 are shown." if len(entries) > 25 else ""
    view = TrackedEntrySelectionView(
        interaction.user.id,
        visible,
        remove_selected,
        multiple=True,
        placeholder="Select one or more profiles to untrack",
    )
    await interaction.response.send_message(
        f"Choose who to stop tracking in this channel.{suffix}",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(name="tracksettings", description="Change alerts for a tracked profile")
async def track_settings_cmd(interaction: discord.Interaction) -> None:
    entries = _tracked_entries(interaction.channel_id)
    if not entries:
        await interaction.response.send_message(
            "Nothing is being tracked in this channel.", ephemeral=True
        )
        return

    async def choose_entry(
        menu_interaction: discord.Interaction,
        selected: list[tuple[str, dict[str, Any]]],
    ) -> None:
        platform, entry = selected[0]
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
            updated = store.set_activity_filters(
                interaction.channel_id, user_id, selected_filters
            )
            if not updated:
                return f"The {platform} subscription for **@{handle}** no longer exists."
            return (
                f"Updated {platform} alerts for **@{handle}** · "
                f"**{activity_filter_label(selected_filters)}**."
            )

        activity_view = ActivitySelectionView(
            interaction.user.id,
            _activity_options(labels, defaults),
            save_settings,
        )
        await menu_interaction.response.edit_message(
            content=f"Choose the alerts for {platform} **@{handle}** (select 1–3):",
            view=activity_view,
        )

    visible = entries[:25]
    suffix = " Only the first 25 are shown." if len(entries) > 25 else ""
    view = TrackedEntrySelectionView(
        interaction.user.id,
        visible,
        choose_entry,
        multiple=False,
        placeholder="Select a tracked profile",
    )
    await interaction.response.send_message(
        f"Choose whose alert settings to change.{suffix}",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(name="fomotrack", description="Alert this channel about a FOMO trader")
@app_commands.describe(handle="FOMO username to track")
async def fomo_track_cmd(interaction: discord.Interaction, handle: str) -> None:
    await interaction.response.defer()
    assert bot.fomo
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


@bot.tree.command(name="fomotracked", description="Show traders tracked in this channel")
async def fomo_tracked_cmd(interaction: discord.Interaction) -> None:
    entries = bot.tracking.for_channel(interaction.channel_id)
    if not entries:
        await interaction.response.send_message(
            "No FOMO traders are being tracked in this channel yet."
        )
        return

    lines = [
        f"`{index:>2}.` [@{entry['handle']}]"
        f"(https://fomo.family/profile/{entry['handle']}) · "
        f"{activity_filter_label(_entry_activity_filters(entry))}"
        for index, entry in enumerate(entries, 1)
    ]
    chunks: list[list[str]] = [[]]
    for line in lines:
        if chunks[-1] and len("\n".join(chunks[-1] + [line])) > 3800:
            chunks.append([])
        chunks[-1].append(line)

    embeds = []
    for index, chunk in enumerate(chunks, 1):
        suffix = f" · {index}/{len(chunks)}" if len(chunks) > 1 else ""
        embed = discord.Embed(
            title=f"🔔 Tracked FOMO traders · {len(entries)}{suffix}",
            description="\n".join(chunk),
            colour=BRAND,
        )
        embed.set_footer(
            text=f"This channel • large swaps ≥ {fmt_usd(LARGE_SWAP_USD)}"
        )
        embeds.append(embed)

    await interaction.response.send_message(embed=embeds[0])
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="fomountrack", description="Stop FOMO trader alerts in this channel")
@app_commands.describe(handle="FOMO username to stop tracking")
async def fomo_untrack_cmd(interaction: discord.Interaction, handle: str) -> None:
    await interaction.response.defer(ephemeral=True)
    assert bot.fomo
    try:
        user = await bot.fomo.resolve(handle)
    except (FomoError, asyncio.TimeoutError) as exc:
        await _reply_error(interaction, exc, handle)
        return
    removed = bot.tracking.remove(interaction.channel_id, user.id)
    message = (f"Stopped tracking **@{user.handle}** in this channel."
               if removed else f"**@{user.handle}** was not tracked in this channel.")
    await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(name="pump", description="Look up a Pump.fun profile")
@app_commands.describe(handle="Pump username or Solana wallet")
async def pump_cmd(interaction: discord.Interaction, handle: str) -> None:
    await interaction.response.defer()
    if not bot.pump:
        await interaction.followup.send("Pump support is unavailable: httpx is not installed.", ephemeral=True)
        return
    try:
        user = await bot.pump.resolve(handle)
    except (PumpError, asyncio.TimeoutError) as exc:
        await _reply_pump_error(interaction, exc, handle)
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


@bot.tree.command(name="pumpwallet", description="Find a Pump.fun profile by wallet")
@app_commands.describe(address="Solana or cached EVM wallet address")
async def pump_wallet_cmd(interaction: discord.Interaction, address: str) -> None:
    await interaction.response.defer()
    if not bot.pump:
        await interaction.followup.send("Pump support is unavailable.", ephemeral=True)
        return
    query = address.strip().strip("`").strip()
    cached_evm = bot.pump_evm.cached(query) if bot.pump_evm else None
    try:
        user = await bot.pump.resolve(cached_evm.solana if cached_evm else query)
    except (PumpError, asyncio.TimeoutError) as exc:
        if EVM_RE.fullmatch(query.lower()) and not cached_evm:
            await interaction.followup.send(
                "That EVM wallet has not been discovered yet. Run `/pump <handle>` "
                "once to discover and cache the profile's EVM wallet.",
                ephemeral=True,
            )
            return
        await _reply_pump_error(interaction, exc, query)
        return
    embed = discord.Embed(
        title="🟩 Pump wallet match",
        description=f"[**@{user.username}**]({user.profile_url})",
        colour=WIN,
    )
    if cached_evm:
        embed.add_field(name="EVM wallet", value=f"`{cached_evm.evm}`", inline=False)
        embed.add_field(name="Solana wallet", value=f"`{user.address}`", inline=False)
    else:
        embed.add_field(name="Solana wallet", value=f"`{user.address}`", inline=False)
    if user.profile_image:
        embed.set_thumbnail(url=user.profile_image)
    embed.set_footer(text="Pump profile mapping")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="pumptrack", description="Alert this channel about a Pump.fun profile")
@app_commands.describe(handle="Pump username or Solana wallet to track")
async def pump_track_cmd(interaction: discord.Interaction, handle: str) -> None:
    await interaction.response.defer()
    if not bot.pump:
        await interaction.followup.send("Pump support is unavailable.", ephemeral=True)
        return
    try:
        user = await bot.pump.resolve(handle)
    except (PumpError, asyncio.TimeoutError) as exc:
        await _reply_pump_error(interaction, exc, handle)
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
            f"{verb} **@{user.username}** in this channel · "
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
        f"Choose which alerts to receive for **@{user.username}** (select 1–3):",
        view=view,
    )


@bot.tree.command(name="pumptracked", description="Show Pump profiles tracked in this channel")
async def pump_tracked_cmd(interaction: discord.Interaction) -> None:
    entries = bot.pump_tracking.for_channel(interaction.channel_id)
    if not entries:
        await interaction.response.send_message(
            "No Pump.fun profiles are being tracked in this channel yet."
        )
        return
    lines = [
        f"`{index:>2}.` [@{entry['handle']}]"
        f"(https://pump.fun/profile/{entry['handle']}) · "
        f"{activity_filter_label(_entry_activity_filters(entry))}"
        for index, entry in enumerate(entries, 1)
    ]
    chunks: list[list[str]] = [[]]
    for line in lines:
        if chunks[-1] and len("\n".join(chunks[-1] + [line])) > 3800:
            chunks.append([])
        chunks[-1].append(line)
    embeds = []
    for index, chunk in enumerate(chunks, 1):
        suffix = f" · {index}/{len(chunks)}" if len(chunks) > 1 else ""
        embed = discord.Embed(
            title=f"🔔 Tracked Pump profiles · {len(entries)}{suffix}",
            description="\n".join(chunk),
            colour=WIN,
        )
        threshold = (f" • minimum trade {fmt_usd(PUMP_MIN_TRADE_USD)}"
                     if PUMP_MIN_TRADE_USD > 0 else "")
        embed.set_footer(text=f"This channel{threshold}")
        embeds.append(embed)
    await interaction.response.send_message(embed=embeds[0])
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="pumpuntrack", description="Stop Pump alerts in this channel")
@app_commands.describe(handle="Pump username or Solana wallet to stop tracking")
async def pump_untrack_cmd(interaction: discord.Interaction, handle: str) -> None:
    await interaction.response.defer(ephemeral=True)
    if not bot.pump:
        await interaction.followup.send("Pump support is unavailable.", ephemeral=True)
        return
    try:
        user = await bot.pump.resolve(handle)
    except (PumpError, asyncio.TimeoutError) as exc:
        await _reply_pump_error(interaction, exc, handle)
        return
    removed = bot.pump_tracking.remove(interaction.channel_id, user.address)
    message = (f"Stopped tracking **@{user.username}** in this channel."
               if removed else f"**@{user.username}** was not tracked in this channel.")
    await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(name="fomosearch", description="Fuzzy-search FOMO traders")
@app_commands.describe(term="Part of a handle or display name")
async def fomo_search_cmd(interaction: discord.Interaction, term: str) -> None:
    await interaction.response.defer()
    assert bot.fomo
    try:
        hits = await bot.fomo.search(term, limit=8)
    except (FomoError, asyncio.TimeoutError) as exc:
        await _reply_error(interaction, exc, term)
        return
    if not hits:
        await interaction.followup.send(f"Nothing matching **{term}**.", ephemeral=True)
        return
    lines = [
        f"`{i:>2}.` [{u.display_name}]({u.profile_url}) — @{u.handle} · "
        f"{fmt_count(u.followers)} followers · {fmt_usd(u.total_volume)} vol"
        for i, u in enumerate(hits, 1)
    ]
    embed = discord.Embed(
        title=f"FOMO search · {term}", description="\n".join(lines), colour=BRAND
    )
    await interaction.followup.send(embed=embed)


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
    await interaction.response.defer()
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
    bot.run(DISCORD_TOKEN)
