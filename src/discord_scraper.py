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
import html as html_mod
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

def _flatten_embeds(message):
    """Return (text, image_url) built from a message's embeds.

    Bot posts (Rick & co) usually carry their whole payload in an embed and
    leave .content empty — without this the mirror would forward a blank line.
    """
    lines = []
    img = ""
    for emb in (getattr(message, "embeds", None) or []):
        try:
            author = getattr(getattr(emb, "author", None), "name", "") or ""
            title  = getattr(emb, "title", "") or ""
            desc   = getattr(emb, "description", "") or ""
            for part in (author, title, desc):
                if part:
                    lines.append(str(part))
            for field in (getattr(emb, "fields", None) or []):
                fname = str(getattr(field, "name", "") or "").strip()
                fval  = str(getattr(field, "value", "") or "").strip()
                if fname and fval:
                    lines.append(f"{fname}: {fval}")
                elif fname or fval:
                    lines.append(fname or fval)
            footer = getattr(getattr(emb, "footer", None), "text", "") or ""
            if footer:
                lines.append(str(footer))
            if not img:
                cand = (getattr(getattr(emb, "image", None), "url", "") or
                        getattr(getattr(emb, "thumbnail", None), "url", ""))
                if cand:
                    img = str(cand)
        except Exception:
            continue
    text = "\n".join(l for l in (x.strip() for x in lines) if l)
    return text[:1500], img


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


# ── Bot card rendering (Rick & co) ───────────────────────────────────────
# A bot's post is an embed, and flattening it to plain text gives an ugly wall
# with raw <:name:id> custom emoji and unrendered Discord markdown. This turns
# it into a Telegram card: banner photo, bold token name + ticker from the
# embed AUTHOR line (Rick puts the name there, not in .title), Discord markdown
# translated to Telegram HTML, and contract addresses tap-to-copy.
_CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")
_MD_LINK_RE      = re.compile(r"\[([^\]\n]{1,60})\]\((https?://[^\s)]+)\)")
_CA_RE           = re.compile(r"\b(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})\b")
_MD_CODE_RE      = re.compile(r"`([^`\n]+)`")
_MD_BOLD_RE      = re.compile(r"\*\*([^*\n]+)\*\*")
_MD_UNDER_RE     = re.compile(r"__([^_\n]+)__")
_MD_ITAL_RE      = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_MD_STRIKE_RE    = re.compile(r"~~([^~\n]+)~~")


def _esc(t: str) -> str:
    return html_mod.escape(t or "", quote=False)


def _code_addresses(t: str) -> str:
    """Wrap contract addresses in <code> — Telegram copies those on tap.

    Skips anything already inside a <code> span so we never nest tags.
    """
    parts = re.split(r"(<code>.*?</code>)", t, flags=re.S)
    for i, part in enumerate(parts):
        if not part.startswith("<code>"):
            parts[i] = _CA_RE.sub(lambda m: f"<code>{m.group(1)}</code>", part)
    return "".join(parts)


def _md_to_html(raw: str) -> str:
    """Discord markdown → Telegram HTML.

    Without this the card shows literal backticks and `**NEW:**` — Telegram's
    HTML parse mode does not understand Discord's markdown. Links are stashed
    behind placeholders first so their URLs never get escaped or matched by the
    inline-formatting patterns.
    """
    raw = _CUSTOM_EMOJI_RE.sub("", raw or "")      # drop <:robinhood:123…>
    links: list = []

    def stash(m):
        links.append((m.group(1), m.group(2)))
        return f"\x00L{len(links) - 1}\x00"

    txt = _MD_LINK_RE.sub(stash, raw)
    txt = _esc(txt)
    # `value` → plain text. Wrapping every stat in <code> made the card noisy
    # and burned the caption budget; only addresses stay tap-to-copy (below).
    txt = _MD_CODE_RE.sub(lambda m: m.group(1), txt)
    txt = _MD_BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", txt)
    txt = _MD_UNDER_RE.sub(lambda m: f"<u>{m.group(1)}</u>", txt)
    txt = _MD_ITAL_RE.sub(lambda m: f"<i>{m.group(1)}</i>", txt)
    txt = _MD_STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", txt)
    txt = _code_addresses(txt)

    def restore(m):
        label, url = links[int(m.group(1))]
        label = _esc(_CUSTOM_EMOJI_RE.sub("", label)).strip() or "link"
        return f'<a href="{_esc(url).replace("&", "&amp;")}">{label}</a>'

    return re.sub(r"\x00L(\d+)\x00", restore, txt).strip()


async def _fetch_bytes(url: str, limit: int = 8_000_000):
    """Download a banner ourselves.

    Telegram's own fetcher often cannot read Discord CDN URLs (they are signed
    and expire), which is why the card arrived with no image. Uploading the
    bytes sidesteps that entirely.
    """
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning(f"banner fetch {resp.status} for {url[:80]}")
                    return None
                data = await resp.read()
        return data if 0 < len(data) <= limit else None
    except Exception as e:
        logger.warning(f"banner fetch failed: {e}")
        return None


def _visible(html: str) -> str:
    """The text Telegram actually counts — tags do not count toward its limits."""
    return html_mod.unescape(re.sub(r"<[^>]+>", "", html or ""))


def _fit_lines(lines: list, limit: int) -> str:
    """Join lines, dropping whole lines from the end once the visible text hits
    `limit`. Slicing raw HTML instead would cut a tag in half and make Telegram
    reject the whole message — that is what truncated the card mid-way."""
    out, used = [], 0
    for line in lines:
        n = len(_visible(line)) + 1
        if used + n > limit:
            break
        out.append(line)
        used += n
    return "\n".join(out)


async def _token_banner(address: str) -> str:
    """Dexscreener's header image for a token, falling back to its logo.

    Rick's embeds carry no image at all, so the card had nothing to show. The
    token's own header is what the other alpha bots use as their banner.
    """
    if not address:
        return ""
    try:
        from .utils import dex_wait
        await dex_wait()
        async with aiohttp.ClientSession() as session:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return ""
        best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
        info = best.get("info") or {}
        return info.get("header") or info.get("imageUrl") or ""
    except Exception as e:
        logger.warning(f"token banner lookup failed: {e}")
        return ""


def _render_bot_card(message):
    """Return (lines, image_url, contract_address) for an embed-carrying bot
    post, else None. Truncation is the caller's job — see _fit_lines."""
    embeds = getattr(message, "embeds", None) or []
    if not embeds:
        return None

    lines: list = []
    img = ""
    content = getattr(message, "clean_content", "") or getattr(message, "content", "") or ""

    # Rick puts the token name + ticker + move in the message CONTENT, above the
    # embed — dropping it is why the card showed neither name nor ticker.
    head_line = content.strip().split("\n")[0].strip() if content.strip() else ""
    if head_line and not _CA_RE.fullmatch(head_line):
        rendered = _md_to_html(head_line)
        if rendered:
            lines.append(f"<b>{rendered}</b>")

    for emb in embeds:
        try:
            emb_author = getattr(emb, "author", None)
            # Rick puts the token name + ticker + move on the embed AUTHOR line.
            # Missing this is why the card had no name or ticker.
            a_name = getattr(emb_author, "name", "") or ""
            a_url = getattr(emb_author, "url", "") or ""
            if a_name and _visible(_md_to_html(a_name)).strip() != _visible(head_line).strip():
                head = _md_to_html(a_name)
                lines.append(f'<b><a href="{_esc(str(a_url)).replace("&", "&amp;")}">{head}</a></b>'
                             if a_url else f"<b>{head}</b>")

            title = _md_to_html(getattr(emb, "title", "") or "")
            if title:
                t_url = getattr(emb, "url", "") or ""
                lines.append(f'<b><a href="{_esc(str(t_url)).replace("&", "&amp;")}">{title}</a></b>'
                             if t_url else f"<b>{title}</b>")

            for raw in (getattr(emb, "description", "") or "").split("\n"):
                rendered = _md_to_html(raw)
                if rendered:
                    lines.append(rendered)

            for field in (getattr(emb, "fields", None) or []):
                fname = _md_to_html(str(getattr(field, "name", "") or ""))
                fval = _md_to_html(str(getattr(field, "value", "") or ""))
                if fname and fval:
                    lines.append(f"<b>{fname}</b> {fval}")
                elif fname or fval:
                    lines.append(fname or fval)

            footer = getattr(getattr(emb, "footer", None), "text", "") or ""
            if footer:
                rendered = _md_to_html(str(footer))
                if rendered:
                    lines.append(f"<i>{rendered}</i>")

            # Banner, best first: big image, then thumbnail, then the author's
            # icon (Rick's token logo) — something is better than a bare card.
            if not img:
                for cand in (
                    getattr(getattr(emb, "image", None), "url", ""),
                    getattr(getattr(emb, "thumbnail", None), "url", ""),
                    getattr(emb_author, "icon_url", ""),
                ):
                    if cand:
                        img = str(cand)
                        break
        except Exception as e:
            logger.warning(f"bot card: embed render failed: {e}")
            continue

    # The CA usually sits in the message the bot REPLIED TO, not in its own
    # card — that reference is the only place to find it for most Rick posts.
    ref = getattr(message, "reference", None)
    ref_text = getattr(getattr(ref, "resolved", None), "content", "") or ""
    ca = ""
    for source in (" ".join(_visible(l) for l in lines), content, ref_text):
        m = _CA_RE.search(source or "")
        if m:
            ca = m.group(1)
            break

    lines = [l for l in lines if l.strip()]
    if not lines and not img:
        return None
    return lines, img, ca


# Messages we saw on a mirrored channel but could not mirror because they
# arrived with no text, no embed and no image. Bots (Rick & co) sometimes post
# an empty shell first and fill it in with an edit a second later — on_message_edit
# below mirrors exactly these, and nothing else, so there is no duplicate risk.
_mirror_skipped_empty: dict = {}  # message_id -> timestamp
_MIRROR_EMPTY_TTL = 300


def _note_skipped_empty(msg_id: int):
    now = import_time.time()
    for k in [k for k, ts in _mirror_skipped_empty.items() if ts < now - _MIRROR_EMPTY_TTL]:
        _mirror_skipped_empty.pop(k, None)
    _mirror_skipped_empty[msg_id] = now


# Track pinged addresses: address -> {time, groups: dict}
_recent_pings: dict = {}
PING_COOLDOWN = 600  # 10 minutes

# Display names to ignore
BLOCKED_NAMES = set()        ##{"rickburpbot", "rick"}

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

        async def _mirror_to_topic(self, message, from_edit: bool = False):
            """Forward one Discord message into its mapped Telegram topic.

            Bots are NOT skipped here. Rick and friends are bots, so the old
            `not message.author.bot` guard dropped them before BLOCKED_NAMES ever
            got a say — emptying BLOCKED_NAMES could not bring them back. The
            mirror filters on BLOCKED_NAMES only; the CA-scan path in on_message
            still skips bots, so pings stay single.
            """
            mirror_topic = _DISCORD_MIRROR_MAP.get(message.channel.id, 0)
            if not mirror_topic:
                return

            author = message.author
            names = {
                (getattr(author, "display_name", "") or "").lower(),
                (getattr(author, "name", "") or "").lower(),
            }
            sender = getattr(author, "display_name", "") or getattr(author, "name", "") or "Unknown"

            img_url = ""
            for att in (getattr(message, "attachments", None) or []):
                if (getattr(att, "content_type", "") or "").startswith("image/"):
                    img_url = att.url
                    break
            # clean_content resolves <@ID>, <#ID>, <@&roleID> to @name / #channel / @role.
            body = getattr(message, "clean_content", "") or getattr(message, "content", "") or ""
            html_body = ""

            card = _render_bot_card(message) if getattr(author, "bot", False) else None
            card_ca = ""
            if card:
                # Bot post (Rick & co) → banner photo + formatted card.
                card_lines, card_img, card_ca = card
                header = f"🤖 <b>{_esc(sender)}</b>"
                ca_line = f"<code>{card_ca}</code>" if card_ca else ""
                # Budget is VISIBLE characters (Telegram ignores the tags): 1024
                # for a photo caption. The CA line is added after the fit so it
                # can never be the line that gets dropped.
                budget = 960 - len(_visible(header)) - len(_visible(ca_line))
                html_body = "\n".join(
                    x for x in (header, _fit_lines(card_lines, budget), ca_line) if x
                )
                img_url = card_img or img_url
                body = html_body
            else:
                embed_text, embed_img = _flatten_embeds(message)
                if embed_text:
                    body = f"{body}\n{embed_text}".strip() if body else embed_text
                if not img_url and embed_img:
                    img_url = embed_img

            blocked = bool(names & BLOCKED_NAMES)
            logger.info(
                f"🪞 MIRRORDBG msg={message.id} chan={message.channel.id} topic={mirror_topic} "
                f"author={sender!r} bot={getattr(author, 'bot', None)} edit={from_edit} "
                f"blocked={blocked} content_len={len(getattr(message, 'content', '') or '')} "
                f"embeds={len(getattr(message, 'embeds', None) or [])} "
                f"atts={len(getattr(message, 'attachments', None) or [])} body_len={len(body)} "
                f"banner={(img_url or '-')[:70]}"
            )

            if blocked:
                return
            if not (body or img_url):
                # Nothing to send yet — remember it so an edit can complete it.
                _note_skipped_empty(message.id)
                return
            if _mirror_dedup(message.id):
                return

            try:
                async def _send(text_body=body, html=html_body, banner=img_url,
                                who=sender, ca=card_ca):
                    # No embed image (Rick never sets one) → use the token's own
                    # Dexscreener header as the banner.
                    if not banner and ca:
                        banner = await _token_banner(ca)
                    # Upload the banner ourselves — Telegram's fetcher usually
                    # cannot read signed Discord CDN URLs, which is why the card
                    # arrived with no image. Fall back to the URL if that fails.
                    data = await _fetch_bytes(banner)
                    await mirror_message(
                        text="" if html else text_body,
                        group_name="",  # unused when topic_id is passed explicitly
                        sender_name=who,
                        image_url="" if data else banner,
                        image_bytes=data,
                        topic_id=mirror_topic,
                        html_text=html,
                    )

                asyncio.create_task(_send())
                _mirror_feed_append(sender, body, img_url)
            except Exception as e:
                logger.warning(f"discord mirror failed for msg {message.id}: {e}")

        async def on_message_edit(self, before, after):
            """Mirror only messages that arrived empty and were filled in by an edit.

            Anything already mirrored is skipped by _mirror_skipped_empty, so a
            normal edit (or a link-preview embed hydrating) never double-posts.
            """
            try:
                if getattr(after, "id", None) not in _mirror_skipped_empty:
                    return
                _mirror_skipped_empty.pop(after.id, None)
                await self._mirror_to_topic(after, from_edit=True)
            except Exception as e:
                logger.warning(f"discord mirror (edit) failed: {e}")

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
            await self._mirror_to_topic(message)

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
