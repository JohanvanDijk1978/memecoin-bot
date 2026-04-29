"""
mirror.py
─────────
Mirrors all messages from alpha groups to topic channels using the bot.
"""

import os
import re
import logging
import aiohttp
from dotenv import load_dotenv

SOL_ADDRESS_RE = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')
ETH_ADDRESS_RE = re.compile(r'\b0x[a-fA-F0-9]{40}\b')


"""Wrap any CA addresses in backticks so they are tap-to-copy in Telegram. Dont need for now, if we need it for 
later also add wrap_cas_in_backticks{text}"""
"""def wrap_cas_in_backticks(text: str) -> str:
    result = text
    seen = set()
    for m in SOL_ADDRESS_RE.finditer(text):
        addr = m.group()
        if addr not in seen:
            seen.add(addr)
            result = result.replace(addr, f"`{addr}`")
    for m in ETH_ADDRESS_RE.finditer(text):
        addr = m.group()
        if addr not in seen:
            seen.add(addr)
            result = result.replace(addr, f"`{addr}`")
    return result
"""

load_dotenv()
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
MIRROR_GROUP = os.getenv("MIRROR_GROUP_ID", "")
TOPIC_MAIN   = int(os.getenv("MIRROR_TOPIC_MAIN", "1"))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Map group names to topic IDs (lowercase)
GROUP_TOPIC_MAP = {
    "fantom troupe":             2,
    "the great locked in penis": 4,
    "alphadao":                  6,
    "tiktokfnf":                 8,
    "underground":               2096,
}

MIRROR_BASE_URL = "https://t.me/c/3963742680"

GROUP_LINK_MAP = {
    "fantom troupe":             f"{MIRROR_BASE_URL}/2",
    "the great locked in penis": f"{MIRROR_BASE_URL}/4",
    "alphadao":                  f"{MIRROR_BASE_URL}/6",
    "tiktokfnf":                 f"{MIRROR_BASE_URL}/8",
    "underground":               f"{MIRROR_BASE_URL}/2096",
}

def get_group_link(group_name: str) -> str:
    return GROUP_LINK_MAP.get(group_name.lower().strip(), "")



def get_topic_id(group_name: str) -> int:
    return GROUP_TOPIC_MAP.get(group_name.lower().strip(), TOPIC_MAIN)


async def mirror_message(text: str, group_name: str, sender_name: str, sender_username: str = "", image_url: str = "", reply_text: str = None, reply_sender: str = None) -> str:
    if not BOT_TOKEN or not MIRROR_GROUP:
        return ""

    topic_id = get_topic_id(group_name)

    if sender_username:
        sender_display = f"*{sender_name}* (@{sender_username})"
    else:
        sender_display = f"*{sender_name}*"
    reply_block = ""
    if reply_text and reply_sender:
        clean_reply = reply_text[:150].replace("\n", " ")
        reply_block = f"┌ *{reply_sender}:* {clean_reply}\n"
    formatted_text = f"{reply_block}👤 {sender_display} — {text}"

    async with aiohttp.ClientSession() as session:
        try:
            if image_url:
                resp = await session.post(
                    f"{TELEGRAM_API}/sendPhoto",
                    json={
                        "chat_id": MIRROR_GROUP,
                        "message_thread_id": topic_id,
                        "photo": image_url,
                        "caption": formatted_text[:1024],
                        "parse_mode": "Markdown",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                )
            else:
                resp = await session.post(
                    f"{TELEGRAM_API}/sendMessage",
                    json={
                        "chat_id": MIRROR_GROUP,
                        "message_thread_id": topic_id,
                        "text": formatted_text[:4096],
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                )
            data = await resp.json()
            msg_id = data.get("result", {}).get("message_id")
            if msg_id:
                return f"https://t.me/c/3963742680/{topic_id}/{msg_id}"
        except Exception as e:
            logger.warning(f"Mirror send failed: {e}")
    return ""
