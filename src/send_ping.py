"""
send_ping.py
────────────
Shared ping sender used by both Telegram and Discord scrapers.
Sends CA alerts to your Telegram alert group.
"""

import os
import logging
import aiohttp
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALERT_GROUP = os.getenv("TELEGRAM_ALERT_GROUP", os.getenv("YOUR_TELEGRAM_USER_ID", ""))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


async def send_media_group(chat_id: str, image_urls: list, caption: str = "") -> None:
    """Send multiple photos as an album to a Telegram chat. `image_urls` is a
    list of publicly-fetchable URLs (max 10 by Telegram spec). Caption applies
    only to the first photo. Falls back to plain sendMessage if the album
    request fails."""
    if not BOT_TOKEN or not chat_id or not image_urls:
        logger.warning("send_media_group: missing BOT_TOKEN, chat_id, or images")
        return
    media = []
    for i, url in enumerate(image_urls[:10]):
        entry = {"type": "photo", "media": url}
        if i == 0 and caption:
            entry["caption"] = caption[:1024]
            entry["parse_mode"] = "Markdown"
        media.append(entry)
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"{TELEGRAM_API}/sendMediaGroup",
                json={"chat_id": chat_id, "media": media},
                timeout=aiohttp.ClientTimeout(total=20),
            )
            data = await resp.json()
            if not data.get("ok"):
                logger.warning(f"sendMediaGroup not ok: {data}")
                # Fallback to plain text so the alert doesn't get lost entirely.
                await send_ping(caption, chat_id=chat_id)
    except Exception as e:
        logger.warning(f"send_media_group failed: {e}")
        await send_ping(caption, chat_id=chat_id)


async def send_ping(text: str, image_url: str = "", chat_id: str = ""):
    target = chat_id or ALERT_GROUP
    if not BOT_TOKEN or not target:
        logger.warning("BOT_TOKEN or chat_id not set")
        return
    try:
        async with aiohttp.ClientSession() as session:
            if image_url:
                resp = await session.post(
                    f"{TELEGRAM_API}/sendPhoto",
                    json={
                        "chat_id": target,
                        "photo": image_url,
                        "caption": text,
                        "parse_mode": "Markdown",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                # If photo fails (bad URL), fall back to text
                if resp.status != 200:
                    await session.post(
                        f"{TELEGRAM_API}/sendMessage",
                        json={
                            "chat_id": target,
                            "text": text,
                            "parse_mode": "Markdown",
                            "disable_web_page_preview": True,
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
            else:
                await session.post(
                    f"{TELEGRAM_API}/sendMessage",
                    json={
                        "chat_id": target,
                        "text": text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                )
    except Exception as e:
        logger.warning(f"Failed to send ping: {e}")
