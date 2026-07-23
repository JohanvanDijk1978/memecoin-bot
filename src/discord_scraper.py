"""
discord_scraper.py
──────────────────
Discord SELF-BOT that monitors your alpha channel and sends
instant CA pings in the same format as the Telegram scraper.

⚠️  WARNING: Self-bots violate Discord's Terms of Service.
    Use at your own risk.
"""

import os
import logging
import asyncio
import aiohttp
import time as import_time
from collections import OrderedDict
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
DISCORD_TOKEN_2    = os.getenv("DISCORD_SELF_TOKEN_2", "")
CHANNEL_IDS_2_RAW  = os.getenv("DISCORD_CHANNEL_IDS_2", "")
CHANNEL_IDS_2: List[int] = [
    int(cid.strip()) for cid in CHANNEL_IDS_2_RAW.split(",") if cid.strip().isdigit()
]

# ── Mirror config: pipe Discord channels → Telegram topics ───────────────
# Preferred format (new): DISCORD_MIRROR_MAP="CHANNEL_ID:TOPIC_ID,CHANNEL_ID:TOPIC_ID"
#   e.g. DISCORD_MIRROR_MAP="1374034315985293384:93008,1246170346948661319:98698"
# Legacy (still honored if MAP is unset): DISCORD_MIRROR_CHANNEL_ID + DISCORD_MIRROR_TOPIC_ID
# Silent no-op if nothing is configured.
def _parse_mirror_map() -> dict:
    raw = (os.getenv("DISCORD_MIRROR_MAP", "") or "").strip()
    if raw:
        result: dict = {}
        for pair in raw.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            cid_str, tid_str = pair.split(":", 1)
            try:
                result[int(cid_str.strip())] = int(tid_str.strip())
            except ValueError:
                logger.warning(f"discord mirror: bad entry in DISCORD_MIRROR_MAP: {pair!r}")
        return result
    # Legacy single-mapping fallback.
    cid = int(os.getenv("DISCORD_MIRROR_CHANNEL_ID", "0") or 0)
    tid = int(os.getenv("DISCORD_MIRROR_TOPIC_ID",   "0") or 0)
    if cid and tid:
        return {cid: tid}
    return {}


_DISCORD_MIRROR_MAP: dict = _parse_mirror_map()

# ── Backfill config: catch messages the WebSocket gateway missed ──────────
# Discord self-bot gateway connections drop and RESUME frequently. During
# reconnect gaps, on_message never fires for messages sent in that window.
# The backfill loop polls channel.history() via REST (which doesn't depend on
# the gateway) and replays anything new through on_message. Bounded dedup
# prevents double-processing when the gateway also delivers the message.
DISCORD_BACKFILL_INTERVAL_SECS = int(os.getenv("DISCORD_BACKFILL_INTERVAL_SECS", "60"))
DISCORD_BACKFILL_LIMIT         = int(os.getenv("DISCORD_BACKFILL_LIMIT", "20"))
_SEEN_MSG_ID_MAX               = 5000  # bound the per-instance dedup dict


def _mirror_feed_append(sender: str, text: str, image_url: str):
    """Also record mirrored messages to data/mirror_feed.jsonl so the
    dashboard can show the mirror next to its live CA feed."""
    import json as _json, time as _time
    try:
        os.makedirs("data", exist_ok=True)
        path = "data/mirror_feed.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps({"ts": _time.time(), "sender": sender,
                                 "text": (text or "")[:600], "image": image_url}) + "\n")
        if os.path.getsize(path) > 2_000_000:  # cap file size, keep newest 500
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-500:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
    except Exception as e:
        logger.warning(f"mirror feed append failed: {e}")

# Dedup — if both Discord accounts happen to see the same channel, both
# on_message handlers fire. Without this we'd double-post to Telegram.
_mirror_seen: dict = {}  # message_id -> timestamp
_MIRROR_DEDUP_TTL = 600  # 10 min is plenty; message IDs never repeat


def _mirror_dedup(msg_id: int) -> bool:
    """Return True if this message ID was already mirrored (within TTL)."""
    now = import_time.time()
    stale = [k for k, ts in _mirror_seen.items() if ts < now - _MIRROR_DEDUP_TTL]
    for k in stale:
        _mirror_seen.pop(k, None)
    if msg_id in _mirror_seen:
        return True
    _mirror_seen[msg_id] = now
    return False


# Track pinged addresses: address -> {time, groups: dict}
_recent_pings: dict = {}
PING_COOLDOWN = 600  # 10 minutes

# Display names to ignore
BLOCKED_NAMES = {"rickburpbot", "rick"}

from .send_ping import send_ping
from .filtered_forward import maybe_forward
from .mirror import mirror_message
from .high_wr_notifier import notify_high_wr_scan


async def fetch_token_quick(address: str, chain: str) -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
            from .utils import dex_wait
            await dex_wait()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()

            pairs = data.get("pairs", [])
            if not pairs:
                return {}

            # Restrict to solana pairs when the address is base58 (SOL). For 0x
            # addresses, let Dexscreener return whichever EVM chain the token
            # actually lives on and pick the highest-liquidity pair.
            if chain == "SOL":
                filtered = [p for p in pairs if p.get("chainId", "").lower() == "solana"] or pairs
            else:
                filtered = pairs
            best = max(filtered, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
            actual_chain_id = (best.get("chainId") or "").lower()
            dex_id          = (best.get("dexId") or "").lower()

            base = best.get("baseToken", {})
            vol  = best.get("volume", {})
            chg  = best.get("priceChange", {})

            image_url = ""
            info = best.get("info", {})
            if info.get("imageUrl"):
                image_url = info["imageUrl"]

            created_at = best.get("pairCreatedAt", 0) or 0
            if created_at:
                age_secs = import_time.time() - created_at / 1000
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
                "price":      price_usd,
                "volume_24h": float(vol.get("h24", 0) or 0),
                "change_24h": float(chg.get("h24", 0) or 0),
                "market_cap": fdv_usd,
                "url":        best.get("url", ""),
                "image_url":  image_url,
                "age":        age_str,
                "ath_mc":     ath_mc,
                "ath_time":   ath_time,
                "chain_id":   actual_chain_id,
                "dex_id":     dex_id,
            }
    except Exception as e:
        logger.warning(f"Quick fetch failed for {address}: {e}")
        return {}


async def fetch_ath(address: str, chain: str, current_price: float, current_fdv: float, session: aiohttp.ClientSession) -> tuple:
    """Fetch ATH market cap and time from GeckoTerminal."""
    try:
        network = "solana" if chain == "SOL" else "eth"
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

        ohlcv_url = f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool_id}/ohlcv/hour?limit=1000&currency=usd&token=base"
        async with session.get(ohlcv_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return 0, 0
            data = await resp.json()
            candles = data.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            if not candles:
                return 0, 0

            ath_candle = max(candles, key=lambda c: c[2])
            ath_price  = ath_candle[2]
            ath_time   = ath_candle[0]

            if current_price > 0 and current_fdv > 0:
                ath_mc = (ath_price / current_price) * current_fdv
            else:
                ath_mc = 0

            return ath_mc, ath_time
    except Exception as e:
        logger.warning(f"ATH fetch failed for {address}: {e}")
        return 0, 0


async def handle_ca_ping(text: str, sender_name: str, group_name: str, sender_id: str = ""):
    found = []
    for m in SOL_ADDRESS_RE.finditer(text):
        found.append((m.group(), "SOL"))
    for m in ETH_ADDRESS_RE.finditer(text):
        found.append((m.group().lower(), "ETH"))

    if not found:
        return

    found = found[:1]
    now = import_time.time()

    def fmt(n):
        if n >= 1_000_000: return f"${n/1_000_000:.1f}M"
        if n >= 1_000: return f"${n/1_000:.0f}K"
        return f"${n:.0f}"

    def fmt2(n):
        if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
        if n >= 1_000: return f"{n/1_000:.1f}K"
        return str(n)

    for address, chain in found:
        # High-WR caller notification — own persistent dedup, must see EVERY
        # scan event, so it runs BEFORE the ping cooldown. Fire-and-forget.
        asyncio.create_task(notify_high_wr_scan(
            address=address, chain=chain, sender_name=sender_name,
            sender_id=sender_id, group_name=group_name,
        ))

        ping_key = f"{address}:{group_name}"
        existing = _recent_pings.get(address)
        group_last_ping = _recent_pings.get(ping_key, 0)

        if now - group_last_ping < PING_COOLDOWN:
            continue
        _recent_pings[ping_key] = now

        token  = await fetch_token_quick(address, chain)
        mc     = token.get("market_cap", 0) if token else 0
        mc_str = fmt(mc) if mc else "N/A"

        # Resolve actual chain from Dexscreener's response for correct link
        # construction (EVM addresses could be on any EVM chain).
        from .utils import build_trading_links, chain_display_name
        actual_chain = (token or {}).get("chain_id") or ("solana" if chain == "SOL" else "ethereum")
        trading_links = build_trading_links(actual_chain, address)

        if existing and now - existing["time"] < PING_COOLDOWN:
            if group_name not in existing["groups"]:
                existing["groups"][group_name] = mc_str
                store.add_message(f"CA:{address}", source="discord", group_name=group_name, sender_name=sender_name, market_cap=mc, sender_id=sender_id)
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
        else:
            store.add_message(f"CA:{address}", source="discord", group_name=group_name, sender_name=sender_name, market_cap=mc, sender_id=sender_id)
            _recent_pings[address] = {"time": now, "groups": {group_name: mc_str}}

        # ATH suffix
        ath_mc   = token.get("ath_mc", 0) if token else 0
        ath_time = token.get("ath_time", 0) if token else 0
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

        # Scan stats
        scan_total, scan_groups = store.get_scan_stats(address)
        if scan_total == 0:
            scan_total, scan_groups = 1, 1
        if scan_total <= 1:
            scan_line = "👥 *First scan!*\n"
        else:
            grp_word = "groups" if scan_groups != 1 else "group"
            scan_line = f"👥 Scanned *{scan_total}x* in *{scan_groups}* {grp_word}\n"

        # History block
        context_block = ""
        history = store.get_ca_history(address, limit=3)
        if history:
            medals = ["🥇", "🥈", "🥉"]
            current_mc = mc
            context_block += "\n\n━━━━━━━━━━━━━━━"
            for i, mention in enumerate(history):
                ago_secs = import_time.time() - mention.timestamp
                ago_mins = int(ago_secs / 60)
                if ago_mins < 60:
                    ts = f"{ago_mins}m ago"
                elif ago_mins < 1440:
                    ts = f"{ago_mins // 60}h ago"
                else:
                    ts = f"{ago_mins // 1440}d ago"
                grp  = mention.group_name or mention.source
                who  = mention.sender_name or "Unknown"
                mca  = mention.market_cap

                # Use peak_mc from store for multiplier
                stored_entries = store._ca_history.get(address, [])
                peak_mc_stored = max((e.get("peak_mc", 0) for e in stored_entries), default=0)
                best_mc = peak_mc_stored if peak_mc_stored > 0 else current_mc

                if mca >= 1_000_000:
                    mca_str = f"${mca/1_000_000:.1f}M"
                elif mca > 0:
                    mca_str = f"${mca/1_000:.0f}K"
                else:
                    mca_str = "N/A"

                if mca > 0 and best_mc > 0:
                    mult = best_mc / mca
                    mult_str = f"({mult:.1f}x)" if mult >= 1.1 else ""
                else:
                    mult_str = ""

                medal = medals[i] if i < len(medals) else "•"
                if mca_str == "N/A" and who == "Unknown" and grp in ("discord", "telegram"):
                    continue
                context_block += f"\n{medal} *{grp}* — *{who}* — *{mca_str}{mult_str}* — *{ts}*"

        if token:
            price  = token["price"]
            ticker = f"${token['symbol']}" if token.get("symbol") else ""
            name   = token["name"]
            dex_id = token.get("dex_id") or ""
            platform_label = dex_id.title() if dex_id else chain_display_name(actual_chain)

            msg = (
                f"👤 *{sender_name}* in *{group_name}*\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🪙 *{name}*  | *{fmt2(mc)}* | *{ticker}*\n"
                f"💊 {chain_display_name(actual_chain)} @ {platform_label}\n"
                f"🕐 Age: {token.get('age', '?')}\n"
                f"💵 USD: `{price:.8f}`\n"
                f"💎 FDV: *{fmt2(mc)}{fdv_ath_suffix}*\n"
                f"{scan_line}"
                f"\n`{address}`\n"
                f"\n🔗 {trading_links}"
                f"{context_block}"
            )
            image_url = token.get("image_url", "")
        else:
            chain_lbl = "◎ SOL" if chain == "SOL" else "Ξ EVM"
            msg = (
                f"👤 *{sender_name}* in *{group_name}*\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{chain_lbl} Contract\n"
                f"\n`{address}`\n"
                f"\n🔗 {trading_links}"
                f"{context_block}"
            )
            image_url = ""

        await send_ping(msg, image_url)
        # Side-channel: forward to filtered channel if group + mc match. Fire-and-forget.
        asyncio.create_task(maybe_forward(msg, image_url, group_name, mc, address))


try:
    import discord

    class DiscordScraper(discord.Client):
        def __init__(self, channel_ids: List[int] = None):
            super().__init__(self_bot=True, chunk_guilds_at_startup=False)
            self._channel_ids = channel_ids or CHANNEL_IDS
            self._channel_cache = {}
            # Per-instance bounded dedup so on_message and _backfill_loop don't
            # double-process the same Discord message.
            self._seen_msg_ids: "OrderedDict[int, None]" = OrderedDict()
            self._backfill_task = None

        def _mark_seen(self, msg_id: int) -> bool:
            """Return True if this msg_id was newly added, False if already seen.
            Evicts oldest entries when the dict exceeds _SEEN_MSG_ID_MAX."""
            if msg_id in self._seen_msg_ids:
                # move to end so recently-seen stays warm
                self._seen_msg_ids.move_to_end(msg_id)
                return False
            self._seen_msg_ids[msg_id] = None
            while len(self._seen_msg_ids) > _SEEN_MSG_ID_MAX:
                self._seen_msg_ids.popitem(last=False)
            return True

        async def _backfill_loop(self):
            """Every DISCORD_BACKFILL_INTERVAL_SECS, pull the last N messages
            from each monitored channel via REST. Anything not already seen
            (i.e. missed by the WebSocket gateway during a reconnect) gets
            replayed through on_message.

            Uses REST, so it works even when the gateway is disconnected.
            """
            # Let the gateway settle before the first pass so we don't race
            # on_ready and end up processing every recent message on startup.
            await asyncio.sleep(DISCORD_BACKFILL_INTERVAL_SECS)
            while True:
                for channel_id in list(self._channel_ids):
                    try:
                        ch = self.get_channel(channel_id)
                        if ch is None:
                            try:
                                ch = await self.fetch_channel(channel_id)
                            except Exception as e:
                                logger.warning(f"backfill: cannot resolve channel {channel_id}: {e}")
                                ch = None
                        if ch is None:
                            logger.warning(f"backfill: channel {channel_id} unresolved — skipping (lost access?)")
                            continue

                        messages = []
                        async for msg in ch.history(limit=DISCORD_BACKFILL_LIMIT):
                            messages.append(msg)
                        # Oldest first, so per-group cooldowns and any other
                        # ordering-sensitive logic still runs chronologically.
                        for msg in reversed(messages):
                            if msg.id not in self._seen_msg_ids:
                                try:
                                    await self.on_message(msg)
                                except Exception as e:
                                    logger.warning(f"backfill on_message failed for {msg.id}: {e}")
                    except Exception as e:
                        logger.warning(f"backfill: channel {channel_id} failed: {e}")
                await asyncio.sleep(DISCORD_BACKFILL_INTERVAL_SECS)

        async def on_ready(self):
            logger.info(f"✅ Discord self-bot connected as: {self.user}")
            logger.info(f"📡 Monitoring {len(self._channel_ids)} Discord channel(s)")
            # Start the REST-based backfill loop exactly once per client instance.
            # on_ready can fire multiple times across gateway reconnects — the
            # guard prevents spawning duplicate loops.
            if self._backfill_task is None or self._backfill_task.done():
                self._backfill_task = asyncio.create_task(self._backfill_loop())
                logger.info(
                    f"🩹 Backfill loop armed — interval={DISCORD_BACKFILL_INTERVAL_SECS}s, "
                    f"limit={DISCORD_BACKFILL_LIMIT}/channel"
                )

        async def on_message(self, message):
            # Dedup at the door: the same message can arrive via WebSocket
            # (on_message) and via _backfill_loop. Whoever gets here first wins;
            # the second call short-circuits so we don't ping / mirror twice.
            if not self._mark_seen(message.id):
                return

            # TEMP DEBUG (remove after 1246 diagnosis): trace inbound messages
            # on monitored channels — reveals whether the channel delivers at all
            # and whether its poster is a bot (bot authors are skipped below).
            if message.channel.id in self._channel_ids:
                logger.info(
                    f"🔎 DBG chan={message.channel.id} author={message.author!r} "
                    f"bot={getattr(message.author, 'bot', None)} "
                    f"content={(message.content or '')[:80]!r}"
                )

            # Mirror path: forward EVERY message from any configured Discord
            # channel into its mapped Telegram topic. Runs independently of the
            # scraper path so it fires even for image-only messages.
            mirror_topic = _DISCORD_MIRROR_MAP.get(message.channel.id, 0)
            if (
                mirror_topic
                and not message.author.bot
                and not _mirror_dedup(message.id)
            ):
                try:
                    sender = message.author.display_name or message.author.name or "Unknown"
                    img_url = ""
                    for att in (message.attachments or []):
                        if (att.content_type or "").startswith("image/"):
                            img_url = att.url
                            break
                    # Use clean_content so <@ID>, <#ID>, <@&roleID> get resolved
                    # to @displayname / #channel / @role instead of raw IDs.
                    asyncio.create_task(mirror_message(
                        text=message.clean_content or "",
                        group_name="",  # unused when topic_id is passed explicitly
                        sender_name=sender,
                        image_url=img_url,
                        topic_id=mirror_topic,
                    ))
                    _mirror_feed_append(sender, message.clean_content or "", img_url)
                except Exception as e:
                    logger.warning(f"discord mirror failed for msg {message.id}: {e}")

            if message.channel.id not in self._channel_ids:
                return
            if not message.content:
                return

            store.add_message(message.content, source="discord")

            if message.author.bot:
                return

            sender_name  = message.author.display_name or message.author.name or "Unknown"
            if sender_name.lower() in BLOCKED_NAMES or (message.author.name or "").lower() in BLOCKED_NAMES:
                return

            group_name   = getattr(message.guild, "name", "Discord") if message.guild else "Discord"
            channel_name = getattr(message.channel, "name", "")
            if channel_name:
                group_name = f"{group_name} #{channel_name}"

            sender_id = f"dc:{message.author.id}" if getattr(message.author, "id", None) else ""
            await handle_ca_ping(message.content, sender_name, group_name, sender_id=sender_id)

except ImportError:
    logger.warning("discord.py-self not installed — Discord scraper disabled")
    DiscordScraper = None


async def run_discord_scraper():
    if DiscordScraper is None:
        logger.warning("⚠️ discord.py-self not installed — Discord scraper disabled")
        return

    tasks = []

    if DISCORD_TOKEN and CHANNEL_IDS:
        client1 = DiscordScraper(CHANNEL_IDS)
        tasks.append(client1.start(DISCORD_TOKEN))
        logger.info(f"🤖 Account 1: monitoring {len(CHANNEL_IDS)} channel(s)")
    else:
        logger.warning("⚠️ DISCORD_SELF_TOKEN or DISCORD_CHANNEL_IDS not set — account 1 skipped")

    if DISCORD_TOKEN_2 and CHANNEL_IDS_2:
        client2 = DiscordScraper(CHANNEL_IDS_2)
        tasks.append(client2.start(DISCORD_TOKEN_2))
        logger.info(f"🤖 Account 2: monitoring {len(CHANNEL_IDS_2)} channel(s)")
    else:
        logger.warning("⚠️ DISCORD_SELF_TOKEN_2 or DISCORD_CHANNEL_IDS_2 not set — account 2 skipped")

    if tasks:
        await asyncio.gather(*tasks)
