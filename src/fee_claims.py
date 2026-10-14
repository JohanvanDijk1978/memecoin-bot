"""
fee_claims.py
─────────────
Discord alert when a GitHub account claims pump.fun creator fees for the first time.

pump.fun coins can route creator fees to a GitHub account ("fee sharing"). When the account withdraws them the
Pump Fees program emits SocialFeePdaClaimed (all its coins at once). Every such claim is signed by one
social_claim_authority, so subscribing to that address gives a stream of only claims — cheap on the paid Alchemy
socket. Layouts are from https://github.com/pump-fun/pump-public-docs (idl/pump.json, idl/pump_fees.json).
Ported from SolanaVolume/fee_claims.py.

Env: FEE_CLAIM_DISCORD_WEBHOOK (required), FEE_CLAIM_MIN_SOL (1), FEE_CLAIM_MIN_USDC (200),
FEE_CLAIM_BLACKLIST (recipient wallets / numeric GitHub ids, comma-separated), FEE_CLAIM_WS_URL (override).
"""

import asyncio
import base64
import json
import logging
import os
import struct
import time

import aiohttp

from src.multiwallet_sources import solana_rpcs, solana_wss, _to_wss  # also loads fomo/.env for the RPC keys

logger = logging.getLogger(__name__)

FEE_GLOBAL = "CHqnuTkj6sXDFknM652aEFPECZh9qVsBXWkhPohmV9dA"  # Pump Fees FeeProgramGlobal: holds social_claim_authority
DISTRIBUTE = bytes.fromhex("a537817004b3ca28")
SOCIAL_CLAIM = bytes.fromhex("3212c141edd2eaec")
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TRADE_URL = "https://trade.padre.gg/trade/solana/{mint}"
COIN_IMAGE = "https://images.pump.fun/coin-image/{mint}?variant=256x256"
PUMP_HEADERS = {"Origin": "https://pump.fun", "Referer": "https://pump.fun/", "User-Agent": "Mozilla/5.0"}

WEBHOOK = os.getenv("FEE_CLAIM_DISCORD_WEBHOOK", "").strip()
MIN_SOL = float(os.getenv("FEE_CLAIM_MIN_SOL") or 1)
MIN_USDC = float(os.getenv("FEE_CLAIM_MIN_USDC") or 200)
BLACKLIST = {w.strip() for w in (os.getenv("FEE_CLAIM_BLACKLIST") or "").split(",") if w.strip()}

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def ws_url() -> str:
    """The Alchemy socket when an Alchemy RPC is configured (paid, steady), else the bot's usual Solana socket."""
    alchemy = next((u for u in solana_rpcs() if "alchemy.com" in u), "")
    return os.getenv("FEE_CLAIM_WS_URL", "").strip() or _to_wss(alchemy) or solana_wss() \
        or "wss://api.mainnet-beta.solana.com"


def b58(b: bytes) -> str:
    n, s = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def amount(raw: int, quote_mint: str):
    """(value, unit): an all-zero or wrapped-SOL quote mint means lamports, USDC means 6 decimals."""
    return (raw / 1e6, "USDC") if quote_mint == USDC else (raw / 1e9, "SOL")


def parse_events(logs):
    """Claims, plus coin payouts (DistributeCreatorFeesEvent), which tell which coins pay into a GitHub account."""
    for line in logs:
        if not line.startswith("Program data: "):
            continue
        try:
            d = base64.b64decode(line[14:])
            if d[:8] == DISTRIBUTE:
                n, = struct.unpack_from("<I", d, 144)  # after ts, mint, bonding_curve, sharing_config, admin
                holders = [(b58(d[148 + 34 * i:180 + 34 * i]), struct.unpack_from("<H", d, 180 + 34 * i)[0])
                           for i in range(n)]
                raw, = struct.unpack_from("<Q", d, 148 + 34 * n)
                yield {"kind": "distribute", "mint": b58(d[16:48]), "holders": holders, "raw": raw}
            elif d[:8] == SOCIAL_CLAIM:
                ts, n = struct.unpack_from("<qI", d, 8)
                o = 20 + n + 1  # user_id string, platform u8
                raw, = struct.unpack_from("<Q", d, o + 96)  # after social_fee_pda, recipient, claim authority
                yield {"kind": "social", "ts": ts, "user_id": d[20:20 + n].decode(), "pda": b58(d[o:o + 32]),
                       "recipient": b58(d[o + 32:o + 64]),
                       "raw": raw, "lifetime": struct.unpack_from("<Q", d, o + 112)[0],
                       "lifetime_stable": struct.unpack_from("<Q", d, o + 168)[0] if len(d) >= o + 176 else 0,
                       "quote": b58(d[o + 136:o + 168])}
        except Exception as e:  # layout changed or truncated log: say so instead of dying silently
            logger.warning("fee_claims: could not parse event: %r", e)


async def get_json(http, url, body=None, headers=None):
    try:
        async with http.request("POST" if body else "GET", url, json=body, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=30)) as r:
            return await r.json(content_type=None) if r.status == 200 else None
    except Exception:
        return None


async def rpc(http, method, params):
    for url in solana_rpcs():
        r = await get_json(http, url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if r and "result" in r:
            return r["result"]
    return None


async def get_tx(http, sig):
    return await rpc(http, "getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0}])


async def social_coins(http, pda, limit=30):
    """(mint, name) of coins that recently paid fees into a GitHub holding account, biggest payer first. The claim
    event names no coin, so this reads the account's last `limit` transactions for payouts into it."""
    paid = {}
    for s in await rpc(http, "getSignaturesForAddress", [pda, {"limit": limit}]) or []:
        tx = None if s.get("err") else await get_tx(http, s["signature"])
        for ev in parse_events(tx["meta"]["logMessages"] or []) if tx else []:
            if ev["kind"] == "distribute":
                paid[ev["mint"]] = paid.get(ev["mint"], 0) + sum(ev["raw"] * bps for a, bps in ev["holders"] if a == pda)
    out = []
    for m in sorted((m for m in paid if paid[m]), key=lambda m: -paid[m])[:5]:
        c = await get_json(http, f"https://frontend-api-v3.pump.fun/coins-v2/{m}", headers=PUMP_HEADERS) or {}
        out.append((m, c.get("symbol") or c.get("name") or m[:6]))
    return out


PADRE_FEED_FILE = "data/padre_feed.jsonl"  # one event per line, next to the milestone pumps; memedash serves it at /api/padre-feed


def _padre_feed(ev, gh, val, unit, coins, sig) -> None:
    """Append the claim to the feed the Padre browser extension polls through the dashboard.
    Best effort: a failure here must never cost the Discord alert. ts is the write time, so the
    extension (which reads past its newest ts) never skips a claim whose block is older than a pump."""
    try:
        with open(PADRE_FEED_FILE, "a") as f:
            f.write(json.dumps({"ts": time.time(), "kind": "claim", "claimed_at": ev["ts"], "github": gh,
                                "user_id": ev["user_id"], "amount": val, "unit": unit,
                                "coins": [{"mint": m, "name": n} for m, n in coins],
                                "recipient": ev["recipient"], "sig": sig}) + "\n")
    except Exception as e:
        logger.warning("fee_claims: padre feed write failed: %s", e)


async def embed(http, ev, sig, val, unit, coins, gh):
    tx = f"https://solscan.io/tx/{sig}"
    base = {"url": tx, "color": 0x2ECC71, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ev["ts"]))}
    who = f"[{gh}](https://github.com/{gh})" if gh else f"GitHub id {ev['user_id']}"
    links = " · ".join(f"[{name}]({TRADE_URL.format(mint=m)})" for m, name in coins)
    if coins:  # title and picture: the coin that paid in the most recently
        base = {**base, "url": TRADE_URL.format(mint=coins[0][0]),
                "thumbnail": {"url": COIN_IMAGE.format(mint=coins[0][0])}}
    return {**base, "title": f"GitHub @{gh or ev['user_id']} claimed {val:,.2f} {unit} in creator fees",
            "description": f"{who} withdrew fees routed to their GitHub account at <t:{ev['ts']}:T>.\n"
                           f"Coins paying this account: {links or 'none found in its recent transactions'}\n"
                           f"To wallet [`{ev['recipient']}`](https://solscan.io/account/{ev['recipient']})\n"
                           f"Lifetime claimed (SOL): {amount(ev['lifetime'], '')[0]:,.2f}\n[Transaction]({tx})"}


async def handle(http, sig, logs, post=True):
    """Alert on each first-ever GitHub claim in a transaction; returns the embeds (post=False: dry run)."""
    out = []
    for ev in parse_events(logs):
        if ev["kind"] != "social":
            continue
        val, unit = amount(ev["raw"], ev["quote"])
        if val < (MIN_USDC if unit == "USDC" else MIN_SOL) or BLACKLIST & {ev["recipient"], ev["user_id"]}:
            continue
        # first claim ever by this GitHub account: its lifetime total is just this claim
        if ev["lifetime_stable" if unit == "USDC" else "lifetime"] > ev["raw"]:
            continue
        gh = (await get_json(http, f"https://api.github.com/user/{ev['user_id']}") or {}).get("login")
        coins = await social_coins(http, ev["pda"])
        e = await embed(http, ev, sig, val, unit, coins, gh)
        _padre_feed(ev, gh, val, unit, coins, sig)
        logger.info("fee_claims: %.2f %s by GitHub %s %s", val, unit, ev["user_id"], sig)
        if post:
            async with http.post(WEBHOOK, json={"embeds": [e]}) as r:
                if r.status >= 300:
                    logger.warning("fee_claims: Discord %s", r.status)
        out.append(e)
    return out


async def run_fee_claims():
    if not WEBHOOK:
        logger.info("fee_claims: FEE_CLAIM_DISCORD_WEBHOOK unset — not started")
        return
    async with aiohttp.ClientSession() as http:
        g = await rpc(http, "getAccountInfo", [FEE_GLOBAL, {"encoding": "base64"}])
        # disc 8, bump 1, authority 32, disable_flags 1, social_claim_authority 32
        authority = b58(base64.b64decode(g["value"]["data"][0])[42:74])
        url = ws_url()
        logger.info("fee_claims: watching GitHub claims >= %g SOL / %g USDC on %s",
                    MIN_SOL, MIN_USDC, url.split("/v2/")[0].split("?")[0])
        sub = {"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
               "params": [{"mentions": [authority]}, {"commitment": "confirmed"}]}
        while True:
            # ponytail: claims landing during a reconnect are missed; backfill with getSignaturesForAddress if needed.
            try:
                async with http.ws_connect(url, heartbeat=30) as ws:
                    await ws.send_json(sub)
                    async for m in ws:
                        if m.type != aiohttp.WSMsgType.TEXT or "ClaimSocialFeePda" not in m.data:
                            continue
                        v = json.loads(m.data)["params"]["result"]["value"]
                        if v["err"] is None:
                            await handle(http, v["signature"], v["logs"])
            except Exception as e:
                logger.warning("fee_claims: stream dropped (%r); reconnecting", e)
            await asyncio.sleep(2)
