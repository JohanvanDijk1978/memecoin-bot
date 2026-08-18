"""
fomo_bot.py — standalone Discord bot: FOMO trader research.

    /fomo <handle>        rich embed for a fomo.family trader
    /fomosearch <term>    fuzzy handle search
    /fomotop [24h] [n]    leaderboard

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
import logging
import os

from typing import Any

import discord
from discord import app_commands
from dotenv import load_dotenv

# Wallet modules read their RPC/cache settings at import time.
load_dotenv()

from fomo_evm import EvmWalletResolver
from fomo_wallet import WalletResolver
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

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("fomobot")

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional: instant command sync while testing
REFRESH_TOKEN = os.getenv("FOMO_PRIVY_REFRESH_TOKEN", "")
ACCESS_TOKEN = os.getenv("FOMO_PRIVY_ACCESS_TOKEN", "")
# Derive each trader's real wallet on chain. Needs SOLANA_RPC + httpx.
RESOLVE_WALLETS = os.getenv("FOMO_RESOLVE_WALLETS", "1").strip() not in ("0", "false", "no")
RESOLVE_EVM = os.getenv("FOMO_RESOLVE_EVM", "1").strip() not in ("0", "false", "no")

BRAND = 0x4F5EFF  # fomo blue
LOSS = 0xE5484D
WIN = 0x30A46C


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
                evm_wallet: str | None = None) -> discord.Embed:
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
        name="Activity",
        value=(
            f"**{user.num_trades:,}** trades\n"
            f"**{user.swap_count:,}** swaps"
        ),
        inline=True,
    )
    embed.add_field(
        name="Size",
        value=(
            f"**{fmt_usd(user.total_volume)}** volume\n"
            f"**{fmt_duration(user.avg_hold_seconds)}** avg hold"
        ),
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
    if ranks:
        embed.add_field(name="PnL · leaderboard rank", value="\n".join(ranks), inline=False)

    # Never use user.sol_address or user.evm_address: both are synthetic. The
    # real Solana wallet is derived on chain; EVM is accepted only from the
    # verified identity index plus an ERC-4337 deployment probe.
    if wallet:
        embed.add_field(name="Solana wallet", value=f"◎ `{wallet}`", inline=False)
    if evm_wallet:
        embed.add_field(name="EVM wallet", value=f"Ξ `{evm_wallet}`", inline=False)

    links = [f"[fomo]({user.profile_url})"]
    if user.twitter:
        links.append(f"[X]({user.twitter})")
    if wallet:
        links.append(f"[solscan](https://solscan.io/account/{wallet})")
    if evm_wallet:
        links.append(f"[basescan](https://basescan.org/address/{evm_wallet})")
        links.append(f"[bscscan](https://bscscan.com/address/{evm_wallet})")
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


class FomoBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.fomo: FomoClient | None = None
        self.wallets: WalletResolver | None = None
        self.evm_wallets: EvmWalletResolver | None = None
        self._http: Any = None

    async def setup_hook(self) -> None:
        self.fomo = FomoClient(
            refresh_token=REFRESH_TOKEN or None,
            access_token=ACCESS_TOKEN or None,
        )
        await self.fomo.__aenter__()

        # Wallet resolution is optional: without an RPC the embed simply omits
        # the wallet rather than showing an address that has never been used.
        if RESOLVE_WALLETS or RESOLVE_EVM:
            try:
                import httpx

                self._http = httpx.AsyncClient(timeout=60)
                if RESOLVE_WALLETS:
                    self.wallets = WalletResolver(self._http)
                if RESOLVE_EVM:
                    self.evm_wallets = EvmWalletResolver(self._http)
            except ImportError:
                log.warning("httpx not installed - wallet resolution disabled")

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        log.info("🪙 slash commands synced")

    async def close(self) -> None:
        if self.fomo:
            await self.fomo.__aexit__(None, None, None)
        if self._http:
            await self._http.aclose()
        await super().close()


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

    # Cached after the first lookup, so this is free from then on. Never fatal:
    # resolve() swallows its own errors and returns None.
    # Both use wallet_cache.json. Keep first-time writes sequential so one
    # resolver cannot overwrite the other's freshly saved address. Each
    # resolver degrades to None and becomes a permanent cache hit on success.
    wallet = await bot.wallets.resolve(bot.fomo, user) if bot.wallets else None
    evm_wallet = await bot.evm_wallets.resolve(user) if bot.evm_wallets else None
    await interaction.followup.send(embed=build_embed(user, wallet, evm_wallet))


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
