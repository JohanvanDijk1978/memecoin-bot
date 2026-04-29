"""
discord_scraper.py
──────────────────
Discord SELF-BOT that monitors your alpha channel and sends
instant CA pings in the same format as the Telegram scraper.

⚠️  WARNING: Self-bots violate Discord's Terms of Service.
    Use at your own risk.
"""

import os
import re
import logging
import asyncio
import aiohttp
from typing import List, Tuple
from dotenv import load_dotenv
from .mention_store import store, SOL_ADDRESS_RE, ETH_ADDRESS_RE

load_dotenv()
logger = logging.getLogger(__name__)

DISCORD_TOKEN   = os.getenv("DISCORD_SELF_TOKEN", "")
CHANNEL_IDS_RAW = os.getenv("DISCORD_CHANNEL_IDS", "")
CHANNEL_IDS: List[int] = [
    int(cid.strip()) for cid in CHANNEL_IDS_RAW.split(",") if cid.strip().isdigit()
]
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
YOUR_ID   = os.getenv("YOUR_TELEGRAM_USER_ID", "")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Track pinged addresses: address -> {time, groups: set}
_recent_pings: dict = {}
PING_COOLDOWN = 600  # 10 minutes

# Bot usernames/display names to ignore
BLOCKED_NAMES = {"rickburpbot", "rick"}


def clean_text(text: str) -> str:
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\*\*([^\*]+)\*\*', r'\1', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:150]


# send_ping imported from shared module
from .send_ping import send_ping


async def fetch_token_quick(address: str, chain: str) -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()

            pairs = data.get("pairs", [])
            if not pairs:
                return {}

            chain_map = {"SOL": "solana", "ETH": "ethereum"}
            chain_id  = chain_map.get(chain, chain.lower())
            filtered  = [p for p in pairs if p.get("chainId", "").lower() == chain_id] or pairs
            best      = max(filtered, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))

            base = best.get("baseToken", {})
            vol  = best.get("volume", {})
            chg  = best.get("priceChange", {})

            image_url = ""
            info = best.get("info", {})
            if info.get("imageUrl"):
                image_url = info["imageUrl"]

            created_at = best.get("pairCreatedAt", 0) or 0
            if created_at:
                import time as _t2
                age_secs = _t2.time() - created_at / 1000
                if age_secs < 3600:
                    age_str = f"{int(age_secs/60)} minutes"
                elif age_secs < 86400:
                    age_str = f"{int(age_secs/3600)} hours"
                elif age_secs < 2592000:
                    age_str = f"{int(age_secs/86400)} days"
                else:
                    age_str = f"{int(age_secs/2592000)} months"
            else:
                age_str = "?"

            price_usd = float(best.get("priceUsd", 0) or 0)
            fdv_usd   = float(best.get("marketCap", 0) or 0)
            ath_mc, ath_time = await fetch_ath(address, chain, price_usd, fdv_usd, session)

            return {
                "name":       base.get("name", "Unknown"),
                "symbol":     base.get("symbol", "???"),
                "price":      float(best.get("priceUsd", 0) or 0),
                "volume_24h": float(vol.get("h24", 0) or 0),
                "change_24h": float(chg.get("h24", 0) or 0),
                "market_cap": float(best.get("marketCap", 0) or 0),
                "url":        best.get("url", ""),
                "image_url":  image_url,
                "age":        age_str,
                "ath_mc":     ath_mc,
                "ath_time":   ath_time,
            }
    except Exception as e:
        logger.warning(f"Quick fetch failed for {address}: {e}")
        return {}


async def fetch_token_age(address: str, chain: str, session) -> str:
    import time as _t, os
    try:
        if chain == "SOL":
            try:
                payload = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                           "params": [address, {"limit": 1000, "commitment": "finalized"}]}
                async with session.post("https://api.mainnet-beta.solana.com", json=payload,
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        sigs = data.get("result", [])
                        if sigs:
                            block_time = sigs[-1].get("blockTime", 0)
                            if block_time:
                                age_secs = _t.time() - block_time
                                if age_secs < 3600: return f"{int(age_secs/60)} minutes"
                                elif age_secs < 86400: return f"{int(age_secs/3600)} hours"
                                elif age_secs < 2592000: return f"{int(age_secs/86400)} days"
                                else: return f"{int(age_secs/2592000)} months"
            except Exception:
                pass
        elif chain == "ETH":
            key = os.getenv("ETHERSCAN_API_KEY", "")
            if key:
                url = f"https://api.etherscan.io/v2/api?chainid=1&module=account&action=txlist&address={address}&startblock=0&endblock=99999999&page=1&offset=1&sort=asc&apikey={key}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        txs = data.get("result", [])
                        if txs and isinstance(txs, list):
                            ts = int(txs[0].get("timeStamp", 0))
                            if ts:
                                age_secs = _t.time() - ts
                                if age_secs < 3600: return f"{int(age_secs/60)} minutes"
                                elif age_secs < 86400: return f"{int(age_secs/3600)} hours"
                                elif age_secs < 2592000: return f"{int(age_secs/86400)} days"
                                else: return f"{int(age_secs/2592000)} months"
    except Exception as e:
        logger.warning(f"Age fetch failed: {e}")
    return "?"


async def fetch_ath(address: str, chain: str, current_price: float, current_fdv: float, session: aiohttp.ClientSession) -> tuple:
    """Fetch ATH market cap and time from GeckoTerminal. Returns (ath_mc, ath_time)."""
    try:
        network = "solana" if chain == "SOL" else "eth"
        # Get pools for this token
        url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}/pools?page=1"
        headers = {"Accept": "application/json;version=20230302"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return 0, 0
            data = await resp.json()
            pools = data.get("data", [])
            if not pools:
                return 0, 0
            pool_id = pools[0].get("id", "").replace(f"{network}_", "")

        # Get OHLCV data for the top pool
        ohlcv_url = f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool_id}/ohlcv/hour?limit=1000&currency=usd&token=base"
        async with session.get(ohlcv_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return 0, 0
            data = await resp.json()
            candles = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            if not candles:
                return 0, 0

            ath_candle = max(candles, key=lambda c: c[2])
            ath_price = ath_candle[2]
            ath_time  = ath_candle[0]

            # Calculate ATH MC
            if current_price > 0 and current_fdv > 0:
                ath_mc = (ath_price / current_price) * current_fdv
            else:
                ath_mc = 0

            return ath_mc, ath_time
    except Exception as e:
        logger.warning(f"ATH fetch failed for {address}: {e}")
        return 0, 0


import time as import_time

async def handle_ca_ping(text: str, sender_name: str, group_name: str, prev_messages: List[Tuple[str, str]] = None):
    found = []
    for m in SOL_ADDRESS_RE.finditer(text):
        found.append((m.group(), "SOL"))
    for m in ETH_ADDRESS_RE.finditer(text):
        found.append((m.group().lower(), "ETH"))

    if not found:
        return

    found = found[:1]

    import time
    now = time.time()

    chain_emoji = {"SOL": "◎", "ETH": "Ξ"}

    for address, chain in found:
        ping_key = f"{address}:{group_name}"
        existing = _recent_pings.get(address)
        group_last_ping = _recent_pings.get(ping_key, 0)

        # Per-group cooldown: same CA can only ping once per 5 min per group
        if now - group_last_ping < PING_COOLDOWN:
            continue
        _recent_pings[ping_key] = now

        axiom_url  = f"https://axiom.trade/t/{address}" if chain == "SOL" else f"https://axiom.trade/t/{address}?chain=eth"
        padre_url  = f"https://trade.padre.gg/trade/solana/{address}" if chain == "SOL" else f"https://trade.padre.gg/trade/eth/{address}"
        gmgn_url = f"https://gmgn.ai/sol/token/{address}" if chain == "SOL" else f"https://gmgn.ai/eth/token/{address}"

        token = await fetch_token_quick(address, chain)

        def fmt(n):
            if n >= 1_000_000: return f"${n/1_000_000:.1f}M"
            if n >= 1_000: return f"${n/1_000:.0f}K"
            return f"${n:.0f}"

        mc = token.get("market_cap", 0) if token else 0
        mc_str = fmt(mc) if mc else "N/A"

        if existing and now - existing["time"] < PING_COOLDOWN:
            if group_name not in existing["groups"]:
                existing["groups"][group_name] = mc_str
                # Store this group's scan for history
                store.add_message(f"CA:{address}", source="discord", group_name=group_name, sender_name=sender_name, market_cap=mc)
                groups_str = " | ".join(f"{g}({m})" for g, m in existing["groups"].items())
                token_name = token.get("name", "") if token else ""
                ticker = f"${token.get('symbol', '')}" if token else ""
                name_line = f"🪙 *{token_name} {ticker}*\n" if token_name else ""
                await send_ping(
                    f"🔥 *Same CA spotted in multiple groups!*\n\n"
                    f"{name_line}"
                    f"📍 Groups: {groups_str}\n"
                    f"`{address}`"
                )
            # Always fall through to send full ping too
        else:
            # Store detailed mention with MC for history tracking
            store.add_message(f"CA:{address}", source="discord", group_name=group_name, sender_name=sender_name, market_cap=mc)
            _recent_pings[address] = {"time": now, "groups": {group_name: mc_str}}

        icon      = chain_emoji.get(chain, "🔗")

        context_block = ""

        # Get ATH from token data (fetched from GeckoTerminal)
        ath_mc   = token.get("ath_mc", 0) if token else 0
        ath_time = token.get("ath_time", 0) if token else 0
        # Pre-compute ATH suffix for FDV line
        def fmt2(n):
            if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
            if n >= 1_000: return f"{n/1_000:.1f}K"
            return str(n)

        if ath_mc > mc * 1.05 and ath_time:
            ago_secs = import_time.time() - ath_time
            if ago_secs < 3600:
                ath_ago = f"{int(ago_secs/60)}m"
            elif ago_secs < 86400:
                ath_ago = f"{int(ago_secs/3600)}h"
            else:
                ath_ago = f"{int(ago_secs/86400)}d"
            fdv_ath_suffix = f" ⇨ {fmt2(ath_mc)} ATH[{ath_ago}]"
        else:
            fdv_ath_suffix = " ATH" if ath_mc > 0 else ""

        # Get scan stats
        scan_total, scan_groups = store.get_scan_stats(address)
        if scan_total == 0:
            scan_total, scan_groups = 1, 1
        if scan_total <= 1:
            scan_line = "👥 *First scan!*\n"
        else:
            grp_word = "groups" if scan_groups != 1 else "group"
            scan_line = f"👥 Scanned *{scan_total}x* in *{scan_groups}* {grp_word}\n"

        # Build top 3 scanner history
        history = store.get_ca_history(address, limit=3)
        if history:
            medals = ["🥇", "🥈", "🥉"]
            current_mc = token.get("market_cap", 0) if token else 0
            context_block += "\n\n━━━━━━━━━━━━━━━"
            for i, mention in enumerate(history):
                import time as _time
                ago_secs = _time.time() - mention.timestamp
                ago_mins = int(ago_secs / 60)
                if ago_mins < 60:
                    ts = f"{ago_mins}m ago"
                elif ago_mins < 1440:
                    ts = f"{ago_mins // 60}h ago"
                else:
                    ts = f"{ago_mins // 1440}d ago"
                grp = mention.group_name or mention.source
                who = mention.sender_name or "Unknown"
                mc  = mention.market_cap
                if mc >= 1_000_000:
                    mc_str = f"${mc/1_000_000:.1f}M"
                elif mc > 0:
                    mc_str = f"${mc/1_000:.0f}K"
                else:
                    mc_str = "N/A"
                if mc > 0 and current_mc > 0:
                    mult = current_mc / mc
                    mult_str = f"({mult:.1f}x)" if mult >= 1.1 else ""
                else:
                    mult_str = ""
                medal = medals[i] if i < len(medals) else "•"
                # Skip entries with no useful data
                if mc_str == "N/A" and who == "Unknown" and grp in ("discord", "telegram"):
                    continue
                context_block += f"\n{medal} *{grp}* — *{who}* — *{mc_str}{mult_str}* — *{ts}*"

        if token:
            vol    = token["volume_24h"]
            mc     = token["market_cap"]
            chg    = token["change_24h"]
            price  = token["price"]
            ticker = f"${token['symbol']}" if token.get("symbol") else ""
            name   = token["name"]

            def fmt2(n):
                if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
                if n >= 1_000: return f"{n/1_000:.1f}K"
                return str(n)

            chg_icon = "📈" if chg >= 0 else "📉"
            platform = "Pump" if chain == "SOL" else "ETH"

            msg = (
                f"👤 *{sender_name}* in *{group_name}*\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🪙 *{name}*  | *{fmt2(mc)}* | *{ticker}*\n"
                f"💊 {'Solana' if chain == 'SOL' else 'Ethereum'} @ {platform}\n"
                f"🕐 Age: {token.get('age', '?')}\n"
                f"💵 USD: `{price:.8f}`\n"
                f"💎 FDV: *{fmt2(mc)}{fdv_ath_suffix}*\n"
                f"{scan_line}"
                f"\n`{address}`\n"
                f"\n🔗 [Axiom]({axiom_url})" + f" | [Padre]({padre_url})" + f" | [GMGN]({gmgn_url})" +
                f"{context_block}"
            )
            image_url = token.get("image_url", "")
        else:
            msg = (
                f"👤 *{sender_name}* in *{group_name}*\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{'◎ SOL' if chain == 'SOL' else 'Ξ ETH'} Contract\n"
                f"\n`{address}`\n"
                f"\n🔗 [Axiom]({axiom_url})" + f" | [Padre]({padre_url})" + f" | [GMGN]({gmgn_url})" +
                f"{context_block}"
            )
            image_url = ""

        await send_ping(msg, image_url)


try:
    import discord

    class DiscordScraper(discord.Client):
        def __init__(self):
            super().__init__(self_bot=True)
            self._channel_cache = {}

        async def on_ready(self):
            logger.info(f"✅ Discord self-bot connected as: {self.user}")
            logger.info(f"📡 Monitoring {len(CHANNEL_IDS)} Discord channel(s)")

        async def on_message(self, message):
            if message.channel.id not in CHANNEL_IDS:
                return
            if not message.content:
                return

            store.add_message(message.content, source="discord")

            # Skip bots and blocked usernames
            if message.author.bot:
                return
            sender_name = message.author.display_name or message.author.name or "Unknown"
            if sender_name.lower() in BLOCKED_NAMES or (message.author.name or "").lower() in BLOCKED_NAMES:
                return
            group_name  = getattr(message.guild, "name", "Discord") if message.guild else "Discord"
            channel_name = getattr(message.channel, "name", "")
            if channel_name:
                group_name = f"{group_name} #{channel_name}"

            # Wait briefly then grab 2 messages before
            prev_messages = []

            await handle_ca_ping(message.content, sender_name, group_name, prev_messages)

except ImportError:
    logger.warning("discord.py-self not installed — Discord scraper disabled")
    DiscordScraper = None


async def run_discord_scraper():
    if not DISCORD_TOKEN:
        logger.warning("⚠️ DISCORD_SELF_TOKEN not set — Discord scraper disabled")
        return
    if not CHANNEL_IDS:
        logger.warning("⚠️ DISCORD_CHANNEL_IDS not set — Discord scraper disabled")
        return
    if DiscordScraper is None:
        logger.warning("⚠️ discord.py-self not installed — Discord scraper disabled")
        return

    client = DiscordScraper()
    await client.start(DISCORD_TOKEN)
