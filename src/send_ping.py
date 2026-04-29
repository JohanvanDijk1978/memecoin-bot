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

load_dotenv('/root/memecoin-bot/.env')
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALERT_GROUP = os.getenv("TELEGRAM_ALERT_GROUP", os.getenv("YOUR_TELEGRAM_USER_ID", ""))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


async def send_ping(text: str, image_url: str = ""):
    if not BOT_TOKEN or not ALERT_GROUP:
        logger.warning("BOT_TOKEN or ALERT_GROUP not set")
        return
    try:
        async with aiohttp.ClientSession() as session:
            if image_url:
                resp = await session.post(
                    f"{TELEGRAM_API}/sendPhoto",
                    json={
                        "chat_id": ALERT_GROUP,
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
                            "chat_id": ALERT_GROUP,
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
                        "chat_id": ALERT_GROUP,
                        "text": text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                )
    except Exception as e:
        logger.warning(f"Failed to send ping: {e}")
