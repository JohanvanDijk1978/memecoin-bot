"""
telegram_scraper.py
───────────────────
Logs in as YOUR Telegram account (not a bot) using Telethon.
Monitors your alpha group and sends instant pings when a CA is dropped.
"""

import os
import re
import logging
import asyncio
import aiohttp
from telethon import TelegramClient, events
from telethon.tl.types import Message, User
from dotenv import load_dotenv
from .mention_store import store, SOL_ADDRESS_RE, ETH_ADDRESS_RE

load_dotenv()
logger = logging.getLogger(__name__)

API_ID    = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH  = os.getenv("TELEGRAM_API_HASH", "")
PHONE     = os.getenv("TELEGRAM_PHONE", "")
GROUPS_RAW = os.getenv("TELEGRAM_ALPHA_GROUP", "")
GROUPS     = [int(g.strip()) for g in GROUPS_RAW.split(",") if g.strip().lstrip("-").isdigit()]
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
YOUR_ID   = os.getenv("YOUR_TELEGRAM_USER_ID", "")

SESSION_FILE = "data/telegram_session"

# Usernames to ignore (bots that repost CAs)
BLOCKED_USERNAMES = {"rickburpbot", "rick", "konitonfbot"}
BLOCKED_NAMES = {"rick"}

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Track pinged addresses: address -> {time, groups: set}
_recent_pings: dict = {}
PING_COOLDOWN = 300  # 5 minutes before resending same CA


def clean_text(text: str) -> str:
    """Strip URLs and markdown links, keep only plain text."""
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\*\*([^\*]+)\*\*', r'\1', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:150]


# send_ping imported from shared module
from .send_ping import send_ping
from .mirror import mirror_message, get_group_link
from .filtered_forward import maybe_forward
from .high_wr_notifier import notify_high_wr_scan
# fetch_token_quick / fetch_ath / the alert body live in ca_enrich now —
# the Discord scraper had its own copy of all three. Re-exported here
# because high_wr_notifier imports fetch_token_quick from this module.
from .ca_enrich import (PENDING_NOTE, build_ca_message, fetch_ath,
                        fetch_token_quick, schedule_recheck)
from .ca_probe import classify_evm


async def fetch_token_age(address: str, chain: str, session: aiohttp.ClientSession) -> str:
    """Fetch true token creation age from Solana RPC or Etherscan."""
    import time as _t
    import os

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
            etherscan_key = os.getenv("ETHERSCAN_API_KEY", "")
            if etherscan_key:
                url = f"https://api.etherscan.io/v2/api?chainid=1&module=account&action=txlist&address={address}&startblock=0&endblock=99999999&page=1&offset=1&sort=asc&apikey={etherscan_key}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        txs = data.get("result", [])
                        if txs and isinstance(txs, list) and len(txs) > 0:
                            ts = int(txs[0].get("timeStamp", 0))
                            if ts:
                                age_secs = _t.time() - ts
                                if age_secs < 3600:
                                    return f"{int(age_secs/60)} minutes"
                                elif age_secs < 86400:
                                    return f"{int(age_secs/3600)} hours"
                                elif age_secs < 2592000:
                                    return f"{int(age_secs/86400)} days"
                                else:
                                    return f"{int(age_secs/2592000)} months"
    except Exception as e:
        logger.warning(f"Age fetch failed for {address}: {e}")

    return "?"


async def handle_ca_ping(text, sender_name, sender_username, group_name, prev_messages, mirror_link="", sender_id=""):
    found = []
    for m in SOL_ADDRESS_RE.finditer(text):
        found.append((m.group(), "SOL"))
    for m in ETH_ADDRESS_RE.finditer(text):
        found.append((m.group().lower(), "ETH"))

    if not found:
        return

    # Only use the first CA found
    found = found[:1]

    import time
    now = time.time()

    for address, chain in found:
        # High-WR caller notification — has its own persistent dedup, so it
        # must see EVERY scan event. Runs BEFORE the ping cooldown (which
        # would swallow first scans by a second caller within 5 min).
        # Fire-and-forget: never blocks or breaks the ping flow.
        asyncio.create_task(notify_high_wr_scan(
            address=address, chain=chain, sender_name=sender_name,
            sender_id=sender_id, group_name=group_name,
        ))

        ping_key = f"{address}:{group_name}"
        existing = _recent_pings.get(address)
        group_last_ping = _recent_pings.get(ping_key, 0)

        # Per-group cooldown: same CA can only ping once per 5 min per group
        if now - group_last_ping < PING_COOLDOWN:
            continue
        _recent_pings[ping_key] = now

        # Fetch token data early so we can use MC in multi-group alert AND
        # know the actual chain (EVM addresses could be any of ethereum, bsc,
        # base, robinhood, etc. — the regex only tells us "0x-shaped").
        token = await fetch_token_quick(address, chain)

        # Resolve chain from Dexscreener's response when available. Fallback
        # to the regex-detected chain if the token lookup failed.
        from .utils import build_trading_links
        actual_chain = (token or {}).get("chain_id") or ("solana" if chain == "SOL" else "ethereum")
        trading_links = build_trading_links(actual_chain, address)

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
                store.add_message(f"CA:{address}", source="telegram", group_name=group_name, sender_name=sender_name, market_cap=mc, ticker=token.get("symbol", "") if token else "", sender_id=sender_id)
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
            # Always fall through to send the full ping too
        else:
            # Store detailed mention with MC for history tracking
            store.add_message(f"CA:{address}", source="telegram", group_name=group_name, sender_name=sender_name, market_cap=mc, ticker=token.get("symbol", "") if token else "", sender_id=sender_id)
            _recent_pings[address] = {"time": now, "groups": {group_name: mc_str}}

        sender = f"*{sender_name}*" if not sender_username else f"*{sender_name}* (@{sender_username})"
        header = f"👤 {sender} in [{group_name}]({mirror_link or get_group_link(group_name)})"

        # No market data. Ask the chains what this address even is, so the
        # alert can say "wallet" rather than "Contract", and so the trading
        # links point at the chain the thing is actually on instead of
        # defaulting to Ethereum.
        probe = None
        if not token and chain == "ETH":
            probe = await classify_evm(address)
            if probe.get("chain"):
                actual_chain = probe["chain"]
                trading_links = build_trading_links(actual_chain, address)

        # A token whose pool has not indexed yet is worth another look. A
        # wallet or a non-ERC-20 contract never will be, so it is not queued.
        will_recheck = not token and (probe or {}).get("kind", "unknown") in ("token", "unknown")

        msg, image_url = build_ca_message(
            header=header, address=address, chain=chain, token=token,
            trading_links=trading_links, probe=probe,
            pending_note=PENDING_NOTE if will_recheck else "",
        )

        sent = await send_ping(msg, image_url)
        if will_recheck:
            schedule_recheck(address, chain, header, sent,
                             source="telegram", group_name=group_name)
        # Side-channel: forward to filtered channel if group + mc match. Fire-and-forget.
        asyncio.create_task(maybe_forward(msg, image_url, group_name, mc, address))


class TelegramScraper:
    def __init__(self):
        self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
        self._group_entities = []

    async def start(self):
        await self.client.start(phone=PHONE)
        logger.info("✅ Telegram user account connected")

        if not GROUPS:
            logger.error("No valid group IDs found in TELEGRAM_ALPHA_GROUP")
            return

        self._group_entities = []
        for group_id in GROUPS:
            try:
                entity = await self.client.get_entity(group_id)
                self._group_entities.append(entity)
                logger.info(f"📡 Monitoring Telegram group: {getattr(entity, 'title', group_id)}")
            except Exception as e:
                logger.error(f"Could not resolve group {group_id}: {e}")

        if not self._group_entities:
            logger.error("No groups could be resolved")
            return

        @self.client.on(events.NewMessage(chats=self._group_entities))
        async def on_new_message(event: events.NewMessage.Event):
            msg: Message = event.message
            text_content = msg.text or msg.message or ""
            has_photo = bool(getattr(msg, "photo", None))
            # Skip only if there's nothing to mirror at all (no text AND no photo).
            if not text_content and not has_photo:
                return
            chat = await event.get_chat()
            group_name = getattr(chat, "title", str(event.chat_id))


            store.add_message(text_content, source="telegram")  # group/sender added after sender resolved

            try:
                sender: User    = await event.get_sender()
                first           = getattr(sender, "first_name", "") or ""
                last            = getattr(sender, "last_name", "") or ""
                sender_name     = f"{first} {last}".strip() or "Unknown"
                sender_username = getattr(sender, "username", "") or ""
                sender_id       = f"tg:{sender.id}" if getattr(sender, "id", None) else ""
            except Exception:
                sender_name     = "Unknown"
                sender_username = ""
                sender_id       = ""

            # Mirror every message to topic channel
            try:
                reply_text = None
                reply_sender = None
                if msg.reply_to_msg_id:
                    try:
                        replied = await self.client.get_messages(event.chat_id, ids=msg.reply_to_msg_id)
                        if replied and replied.text:
                            reply_text = replied.text[:200]
                            rs = await self.client.get_entity(replied.sender_id)
                            first = getattr(rs, "first_name", "") or ""
                            last  = getattr(rs, "last_name", "") or ""
                            reply_sender = f"{first} {last}".strip() or "Unknown"
                    except Exception:
                        pass

                # Download attached photo (if any) so we can upload via Bot API multipart.
                # Telethon photos have no public URL — the Bot API can't fetch them by reference.
                image_bytes = None
                if has_photo:
                    try:
                        image_bytes = await msg.download_media(file=bytes)
                    except Exception as e:
                        logger.warning(f"Photo download failed: {e}")

                mirror_link = await mirror_message(
                    text_content,
                    group_name,
                    sender_name,
                    sender_username,
                    image_bytes=image_bytes,
                    reply_text=reply_text,
                    reply_sender=reply_sender,
                    chat_id=event.chat_id,
                )
            except Exception:
                mirror_link = ""
           

            # Skip blocked bot usernames
            if sender_username.lower() in BLOCKED_USERNAMES or sender_name.lower() in BLOCKED_NAMES:
                return

            prev_messages = []

            # Full detail store call is done inside handle_ca_ping with MC

            await handle_ca_ping(text_content, sender_name, sender_username, group_name, prev_messages, mirror_link=mirror_link, sender_id=sender_id)

        logger.info("👂 Listening — instant CA pings enabled")
        await self.client.run_until_disconnected()

    async def backfill(self, hours: float = 12):
        if not self._group_entities:
            return

        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        total  = 0

        for entity in self._group_entities:
            count = 0
            logger.info(f"⏪ Backfilling {getattr(entity, 'title', entity.id)} last {hours}h...")
            async for msg in self.client.iter_messages(entity, offset_date=None, reverse=False):
                if msg.date < cutoff:
                    break
                if msg.text:
                    store.add_message(msg.text, source="telegram")  # group/sender added after sender resolved
                    count += 1
            total += count

        logger.info(f"✅ Backfilled {total} messages from {len(self._group_entities)} groups")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = TelegramScraper()
    asyncio.run(scraper.start())
