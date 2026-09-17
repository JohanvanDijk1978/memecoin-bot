"""
send_ping.py
────────────
Shared ping sender used by both Telegram and Discord scrapers.
Sends CA alerts to your Telegram alert group.

`send_ping` returns {"chat_id", "message_id"} for the message it sent, so a
caller that may learn more later — see ca_enrich's re-check loop — can edit
that message in place instead of posting the same coin twice. It returns None
when nothing was sent; no caller is obliged to look.
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


def _sent(target, payload) -> dict:
    """Pull chat_id/message_id out of a Bot API response body."""
    result = (payload or {}).get("result") or {}
    message_id = result.get("message_id")
    chat_id = (result.get("chat") or {}).get("id", target)
    return {"chat_id": chat_id, "message_id": message_id} if message_id else None


async def send_ping(text: str, image_url: str = "", chat_id: str = ""):
    target = chat_id or ALERT_GROUP
    if not BOT_TOKEN or not target:
        logger.warning("BOT_TOKEN or chat_id not set")
        return None
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
                if resp.status == 200:
                    return _sent(target, await resp.json())
                # If photo fails (bad URL), fall back to text
                resp = await session.post(
                    f"{TELEGRAM_API}/sendMessage",
                    json={
                        "chat_id": target,
                        "text": text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                return _sent(target, await resp.json()) if resp.status == 200 else None

            resp = await session.post(
                f"{TELEGRAM_API}/sendMessage",
                json={
                    "chat_id": target,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            )
            return _sent(target, await resp.json()) if resp.status == 200 else None
    except Exception as e:
        logger.warning(f"Failed to send ping: {e}")
        return None


async def edit_ping(chat_id, message_id: int, text: str) -> bool:
    """Rewrite an alert we already sent. True if Telegram accepted it.

    Only ever used on text messages (a bare ping never has a photo, because
    the image comes from the token data we did not have). Telegram refuses an
    edit whose text is unchanged; that comes back as a 400 and is a no-op, not
    an error worth retrying.
    """
    if not BOT_TOKEN or not chat_id or not message_id:
        return False
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"{TELEGRAM_API}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if resp.status == 200:
                return True
            body = await resp.text()
            logger.warning("edit_ping %s/%s failed: HTTP %s %s",
                           chat_id, message_id, resp.status, body[:200])
            return False
    except Exception as e:
        logger.warning(f"Failed to edit ping: {e}")
        return False
