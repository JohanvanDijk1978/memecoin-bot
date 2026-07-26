#!/usr/bin/env python3
"""
diag_discord_channel.py
───────────────────────
Live diagnostic for a single Discord channel, run on the VPS.

Pulls the last N messages via REST (no gateway involved, so it works even if
the self-bot's WebSocket is dead) and, for each message, prints exactly which
branch of discord_scraper.on_message() would have handled or dropped it.

Usage (from /root/memecoin-bot or wherever the repo lives):

    python3 tools/diag_discord_channel.py 1246170346948661319 --account 2
    python3 tools/diag_discord_channel.py 1246170346948661319 --account 2 --limit 100

Exits non-zero if the token cannot see the channel at all (kicked / perms).
"""

import os
import re
import sys
import json
import asyncio
import argparse

import aiohttp
from dotenv import load_dotenv

load_dotenv()

API = "https://discord.com/api/v10"

SOL_ADDRESS_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
ETH_ADDRESS_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
BLOCKED_NAMES = {"rickburpbot", "rick"}


def _ids(raw: str):
    return [int(c.strip()) for c in (raw or "").split(",") if c.strip().isdigit()]


def _mirror_map():
    raw = (os.getenv("DISCORD_MIRROR_MAP", "") or "").strip()
    out = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            a, b = pair.split(":", 1)
            try:
                out[int(a.strip())] = int(b.strip())
            except ValueError:
                pass
    if not out:
        cid = int(os.getenv("DISCORD_MIRROR_CHANNEL_ID", "0") or 0)
        tid = int(os.getenv("DISCORD_MIRROR_TOPIC_ID", "0") or 0)
        if cid and tid:
            out[cid] = tid
    return out


def embed_text(msg: dict) -> str:
    """All text Discord renders from embeds — the scraper currently ignores
    every character of this."""
    parts = []
    for e in msg.get("embeds") or []:
        for key in ("title", "description", "url"):
            if e.get(key):
                parts.append(str(e[key]))
        for f in e.get("fields") or []:
            parts.append(f"{f.get('name','')} {f.get('value','')}")
        for key in ("footer", "author"):
            sub = e.get(key) or {}
            for k2 in ("text", "name", "url"):
                if sub.get(k2):
                    parts.append(str(sub[k2]))
    return "\n".join(parts)


def cas(text: str):
    return [m.group() for m in SOL_ADDRESS_RE.finditer(text or "")] + \
           [m.group() for m in ETH_ADDRESS_RE.finditer(text or "")]


def verdict(msg: dict, channel_id: int, monitored, mirror_map):
    author = msg.get("author") or {}
    is_bot = bool(author.get("bot")) or bool(msg.get("webhook_id"))
    name = author.get("global_name") or author.get("username") or "Unknown"
    content = msg.get("content") or ""
    etext = embed_text(msg)

    mirrored = bool(mirror_map.get(channel_id)) and not is_bot

    if channel_id not in monitored:
        v = "DROP  channel not in DISCORD_CHANNEL_IDS[_2]"
    elif not content:
        v = "DROP  `if not message.content` — embed/attachment text never parsed"
    elif is_bot:
        v = "DROP  `if message.author.bot` — DISCORD_BOT_OK_CHANNELS is NOT wired into the code"
    elif name.lower() in BLOCKED_NAMES:
        v = "DROP  BLOCKED_NAMES"
    elif not cas(content):
        v = "DROP  no CA matched in message.content"
    else:
        v = f"PING  {cas(content)[0]}"

    return {
        "id": msg.get("id"),
        "ts": (msg.get("timestamp") or "")[:19],
        "author": name,
        "bot": is_bot,
        "webhook": bool(msg.get("webhook_id")),
        "content_len": len(content),
        "embeds": len(msg.get("embeds") or []),
        "attachments": len(msg.get("attachments") or []),
        "ca_in_content": cas(content),
        "ca_in_embed": cas(etext),
        "mirrored": mirrored,
        "verdict": v,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("channel_id", type=int)
    ap.add_argument("--account", default="2", choices=["1", "2"])
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    token = os.getenv("DISCORD_SELF_TOKEN" if args.account == "1" else "DISCORD_SELF_TOKEN_2", "")
    if not token:
        print(f"✗ no token for account {args.account} in .env")
        return 2

    monitored = _ids(os.getenv("DISCORD_CHANNEL_IDS" if args.account == "1" else "DISCORD_CHANNEL_IDS_2", ""))
    mirror_map = _mirror_map()

    print(f"account          : {args.account}")
    print(f"monitored ids    : {monitored}")
    print(f"target in list   : {args.channel_id in monitored}")
    print(f"mirror topic     : {mirror_map.get(args.channel_id, 0) or '— none —'}")
    print(f"BOT_OK_CHANNELS  : {os.getenv('DISCORD_BOT_OK_CHANNELS','(unset)')}  "
          f"<- read by code? NO (grep src/ returns nothing)")
    print("-" * 100)

    headers = {"Authorization": token, "User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as s:
        # 1. Can this token still see the channel?
        async with s.get(f"{API}/users/@me") as r:
            me = await r.json() if r.status == 200 else {}
            print(f"GET /users/@me            -> {r.status} "
                  f"{me.get('username','')}#{me.get('discriminator','')}")
            if r.status != 200:
                print("✗ token invalid or expired — this alone kills the whole account")
                return 2

        async with s.get(f"{API}/channels/{args.channel_id}") as r:
            body = await r.text()
            print(f"GET /channels/{args.channel_id} -> {r.status}")
            if r.status != 200:
                print(f"✗ channel unreachable: {body[:200]}")
                print("  (kicked from guild, channel deleted, or read perms revoked)")
                return 2
            ch = json.loads(body)
            print(f"    name=#{ch.get('name')} guild={ch.get('guild_id')} type={ch.get('type')}")

        async with s.get(f"{API}/channels/{args.channel_id}/messages",
                         params={"limit": args.limit}) as r:
            body = await r.text()
            print(f"GET .../messages?limit={args.limit} -> {r.status}")
            if r.status != 200:
                print(f"✗ cannot read history: {body[:200]}")
                print("  (READ_MESSAGE_HISTORY revoked — gateway would also deliver nothing)")
                return 2
            msgs = json.loads(body)

    print("-" * 100)
    rows = [verdict(m, args.channel_id, monitored, mirror_map) for m in reversed(msgs)]

    ca_msgs = [r for r in rows if r["ca_in_content"] or r["ca_in_embed"]]
    print(f"{len(msgs)} messages fetched · {len(ca_msgs)} contain a CA somewhere\n")

    for r in rows:
        flag = "🤖" if r["bot"] else "👤"
        tag = "CA" if (r["ca_in_content"] or r["ca_in_embed"]) else "  "
        where = ""
        if r["ca_in_embed"] and not r["ca_in_content"]:
            where = "  [CA is in EMBED only]"
        print(f"{r['ts']} {flag} {tag} {r['author'][:18]:<18} "
              f"len={r['content_len']:<4} emb={r['embeds']} att={r['attachments']} "
              f"mirror={'Y' if r['mirrored'] else 'n'} | {r['verdict']}{where}")

    print("\n" + "=" * 100)
    from collections import Counter
    for v, n in Counter(r["verdict"].split("—")[0].strip() for r in ca_msgs).most_common():
        print(f"{n:>3} CA-bearing message(s): {v}")
    if not ca_msgs:
        print("No CA-bearing messages in this window — widen --limit or wait for a call.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
