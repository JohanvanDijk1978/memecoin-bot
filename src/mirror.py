"""
mirror.py
─────────
Mirrors all messages from alpha groups to topic channels using the bot.
"""

import os
import re
import json
import html as html_mod
import logging
import aiohttp
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
MIRROR_GROUP = os.getenv("MIRROR_GROUP_ID", "")
TOPIC_MAIN   = int(os.getenv("MIRROR_TOPIC_MAIN", "1"))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Map chat IDs to topic IDs — checked first, immune to title changes/mismatches
CHAT_TOPIC_MAP = {
    -1003755416055: 105846,  # casino
}

# Map group names to topic IDs (lowercase)
GROUP_TOPIC_MAP = {
    "fantom troupe":             2,
    "the great locked in penis": 4,
    "alphadao":                  6,
    "tiktokfnf":                 8,
    "casino":                    105846,
    "underground":               2096,
}

MIRROR_BASE_URL = "https://t.me/c/3963742680"

GROUP_LINK_MAP = {
    "fantom troupe":             f"{MIRROR_BASE_URL}/2",
    "the great locked in penis": f"{MIRROR_BASE_URL}/4",
    "alphadao":                  f"{MIRROR_BASE_URL}/6",
    "tiktokfnf":                 f"{MIRROR_BASE_URL}/8",
    "casino":                    f"{MIRROR_BASE_URL}/105846",
    "underground":               f"{MIRROR_BASE_URL}/2096",
}

def get_group_link(group_name: str) -> str:
    return GROUP_LINK_MAP.get(group_name.lower().strip(), "")



def get_topic_id(group_name: str, chat_id: int = None) -> int:
    if chat_id is not None and chat_id in CHAT_TOPIC_MAP:
        return CHAT_TOPIC_MAP[chat_id]
    return GROUP_TOPIC_MAP.get(group_name.lower().strip(), TOPIC_MAIN)


async def mirror_message(
    text: str,
    group_name: str,
    sender_name: str,
    sender_username: str = "",
    image_url: str = "",
    image_bytes: bytes = None,
    reply_text: str = None,
    reply_sender: str = None,
    topic_id: int = None,
    chat_id: int = None,
    html_text: str = "",
    buttons: list = None,
) -> str:
    """Mirror a message to the topic channel.

    image_url   — public URL the Bot API can fetch (used by CA ping flow that pulls
                  token logos from Dexscreener, and by Discord mirror for CDN URLs).
    image_bytes — raw image bytes (used by the Telegram scraper to mirror photos
                  attached to alpha-group messages — those have no public URL).
    html_text   — a fully rendered, already-escaped HTML body. When given it is
                  sent verbatim (parse_mode=HTML) instead of the default
                  "👤 sender — text" line; used for Discord bot cards (Rick).
    buttons     — [(label, url), …] rendered as a Telegram inline keyboard,
                  4 per row. Link buttons need no callback handler.
    topic_id    — override the group-name → topic lookup. Used by the Discord
                  channel mirror where the source isn't a Telegram group name.
    """
    if not BOT_TOKEN or not MIRROR_GROUP:
        return ""

    if topic_id is None:
        topic_id = get_topic_id(group_name, chat_id)

    if sender_username:
        sender_display = f"*{sender_name}* (@{sender_username})"
    else:
        sender_display = f"*{sender_name}*"
    reply_block = ""
    if reply_text and reply_sender:
        clean_reply = reply_text[:150].replace("\n", " ")
        reply_block = f"┌ *{reply_sender}:* {clean_reply}\n"
    if html_text:
        formatted_text = html_text
        # Fallback if Telegram rejects the entities: tags out, entities back.
        plain_text = html_mod.unescape(re.sub(r"<[^>]+>", "", html_text))
        parse_modes = ("HTML", None)
    else:
        formatted_text = f"{reply_block}👤 {sender_display} — {text}"
        plain_text = formatted_text
        parse_modes = ("Markdown", None)

    reply_markup = None
    if buttons:
        rows, row = [], []
        for label, url in buttons[:8]:
            row.append({"text": label, "url": url})
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        if rows:
            reply_markup = {"inline_keyboard": rows}

    def _body(parse: str) -> str:
        return formatted_text if parse else plain_text

    # Pick endpoint + request builder. `parse` is the parse_mode to use (or None
    # for plain text). Builders take `parse` so we can re-send the exact same
    # message without Markdown if the first attempt fails to parse.
    if image_bytes:
        # Multipart upload — required because Telethon-sourced photos have
        # no public URL the Bot API can fetch on its own.
        method, timeout = "sendPhoto", 20
        def build(parse):
            form = aiohttp.FormData()
            form.add_field("chat_id", str(MIRROR_GROUP))
            if topic_id > 1:
                # thread_id=1 (General) is rejected with "message thread not
                # found" — omit it so unmapped groups land in General instead
                # of being dropped.
                form.add_field("message_thread_id", str(topic_id))
            form.add_field("caption", _body(parse)[:1024])
            if parse:
                form.add_field("parse_mode", parse)
            if reply_markup:
                form.add_field("reply_markup", json.dumps(reply_markup))
            form.add_field(
                "photo", image_bytes,
                filename="photo.jpg",
                content_type="image/jpeg",
            )
            return {"data": form}
    elif image_url:
        method, timeout = "sendPhoto", 10
        def build(parse):
            payload = {
                "chat_id": MIRROR_GROUP,
                "photo": image_url,
                "caption": _body(parse)[:1024],
            }
            if topic_id > 1:
                payload["message_thread_id"] = topic_id
            if parse:
                payload["parse_mode"] = parse
            if reply_markup:
                payload["reply_markup"] = reply_markup
            return {"json": payload}
    else:
        method, timeout = "sendMessage", 10
        def build(parse):
            payload = {
                "chat_id": MIRROR_GROUP,
                "text": _body(parse)[:4096],
                "disable_web_page_preview": True,
            }
            if topic_id > 1:
                payload["message_thread_id"] = topic_id
            if parse:
                payload["parse_mode"] = parse
            if reply_markup:
                payload["reply_markup"] = reply_markup
            return {"json": payload}

    async with aiohttp.ClientSession() as session:
        # Try Markdown first; if the Bot API can't parse entities (a stray *, _,
        # [ or ` in group chatter), re-send as plain text instead of silently
        # dropping the whole message.
        for parse in parse_modes:
            try:
                resp = await session.post(
                    f"{TELEGRAM_API}/{method}",
                    **build(parse),
                    timeout=aiohttp.ClientTimeout(total=timeout),
                )
                data = await resp.json()
            except Exception as e:
                logger.warning(f"Mirror send failed: {e}")
                return ""
            if data.get("ok"):
                msg_id = data.get("result", {}).get("message_id")
                if msg_id:
                    return f"https://t.me/c/3963742680/{topic_id}/{msg_id}"
                return ""
            logger.warning(f"Mirror send not ok (parse_mode={parse}, group={group_name!r}, topic={topic_id}): {data}")
            desc = str(data.get("description", "")).lower()
            # A photo card whose banner URL Telegram cannot fetch: retry as text
            # rather than losing the card entirely.
            if method == "sendPhoto" and image_url and ("wrong file identifier" in desc
                                                        or "failed to get http url content" in desc
                                                        or "wrong type of the web page content" in desc):
                logger.warning("Mirror: banner unusable, resending as text")
                return await mirror_message(
                    text=text, group_name=group_name, sender_name=sender_name,
                    sender_username=sender_username, reply_text=reply_text,
                    reply_sender=reply_sender, topic_id=topic_id, chat_id=chat_id,
                    html_text=html_text, buttons=buttons,
                )
            # Only a parse failure is worth retrying as plain text; anything else
            # (bot not admin, chat not found, rate limit) would just fail again.
            if parse is None or "parse" not in str(data.get("description", "")).lower():
                break
    return ""
