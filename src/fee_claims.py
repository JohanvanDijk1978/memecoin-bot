"""
fee_claims.py
─────────────
Discord alert when a GitHub account claims pump.fun creator fees for the first time.

pump.fun coins can route creator fees to a GitHub account ("fee sharing"). When the account withdraws them the
Pump Fees program emits SocialFeePdaClaimed (all its coins at once). Every such claim is signed by one
social_claim_authority, so polling that address's history gives only claims. Layouts are from
https://github.com/pump-fun/pump-public-docs (idl/pump.json, idl/pump_fees.json).
Ported from SolanaVolume/fee_claims.py.

Env: FEE_CLAIM_DISCORD_WEBHOOK (required), FEE_CLAIM_MIN_SOL (1), FEE_CLAIM_MIN_USDC (200),
FEE_CLAIM_BLACKLIST (recipient wallets / numeric GitHub ids, comma-separated), FEE_CLAIM_POLL_S (3).
"""

import asyncio
import base64
import json
import logging
import os
import struct
import time

import aiohttp

from src.multiwallet_sources import solana_rpcs, solana_wss  # also loads fomo/.env for the RPC keys

logger = logging.getLogger(__name__)

FEE_GLOBAL = "CHqnuTkj6sXDFknM652aEFPECZh9qVsBXWkhPohmV9dA"  # Pump Fees FeeProgramGlobal: holds social_claim_authority
DISTRIBUTE = bytes.fromhex("a537817004b3ca28")
SOCIAL_CLAIM = bytes.fromhex("3212c141edd2eaec")
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TRADE_URL = "https://trade.padre.gg/trade/solana/{mint}"
COIN_IMAGE = "https://images.pump.fun/coin-image/{mint}?variant=256x256"
PUMP_HEADERS = {"Origin": "https://pump.fun", "Referer": "https://pump.fun/", "User-Agent": "Mozilla/5.0"}
FEES_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"  # owns the GitHub holding accounts (SocialFeePda)
SOCIAL_PDA = bytes([139, 96, 53, 17, 42, 169, 206, 150])  # SocialFeePda account discriminator

WEBHOOK = os.getenv("FEE_CLAIM_DISCORD_WEBHOOK", "").strip()
MIN_SOL = float(os.getenv("FEE_CLAIM_MIN_SOL") or 1)
MIN_USDC = float(os.getenv("FEE_CLAIM_MIN_USDC") or 200)
POLL_S = float(os.getenv("FEE_CLAIM_POLL_S") or 3)
BLACKLIST = {w.strip() for w in (os.getenv("FEE_CLAIM_BLACKLIST") or "").split(",") if w.strip()}

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


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
                yield {"kind": "distribute", "mint": b58(d[16:48]), "holders": holders, "raw": raw,
                       "quote": b58(d[156 + 34 * n:188 + 34 * n])}
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


def parse_social_pda(data: bytes):
    """A GitHub holding account as the fee program stores it: discriminator, bump, platform, user_id string, one
    byte, lifetime claimed (u64 lamports), last claim time; read off a live account on 2026-10-15 (the 140 zero
    bytes after that are reserved). None for any other account of the program."""
    if data[:8] != SOCIAL_PDA:
        return None
    n, = struct.unpack_from("<I", data, 10)
    return {"user_id": data[14:14 + n].decode(), "lifetime": struct.unpack_from("<Q", data, 15 + n)[0]}


SEEN: dict[str, float] = {}   # signature -> when handled, shared by the socket and the poll so neither repeats the other
EARLY: dict[str, float] = {}  # holding account -> when its sweep was alerted; once per account, and the claim within 5 min is old news
SWEPT: set = set()            # sweep signatures already read


def pre_balance(tx, account: str) -> int:
    """Lamports `account` held before `tx` (static keys first, then the looked-up ones, as preBalances lists them)."""
    loaded = tx["meta"].get("loadedAddresses") or {}
    keys = tx["transaction"]["message"]["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])
    return tx["meta"]["preBalances"][keys.index(account)] if account in keys else 0


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
    return await rpc(http, "getTransaction", [sig, {"encoding": "json", "commitment": "confirmed",
                                                "maxSupportedTransactionVersion": 0}])


async def social_coins(http, pda, sig="", logs=(), limit=30):
    """(mint, name) of coins that recently paid fees into a GitHub holding account, biggest payer first. The claim
    event names no coin, so this reads the claim's own logs (claims often bundle the payout that funds them) and the
    account's last `limit` transactions for payouts into it. "confirmed": the claim is handled seconds after it
    lands, when the payouts just before it are not finalized yet (2026-10-15: 36.17 SOL claim showed no coin)."""
    paid = {}

    def add(lines):
        for ev in parse_events(lines):
            if ev["kind"] == "distribute":
                paid[ev["mint"]] = paid.get(ev["mint"], 0) + sum(ev["raw"] * bps for a, bps in ev["holders"] if a == pda)

    add(logs)
    for s in await rpc(http, "getSignaturesForAddress", [pda, {"limit": limit, "commitment": "confirmed"}]) or []:
        tx = None if s.get("err") or s["signature"] == sig else await get_tx(http, s["signature"])
        add(tx["meta"]["logMessages"] or [] if tx else [])
    return [(m, await coin_name(http, m)) for m in sorted((m for m in paid if paid[m]), key=lambda m: -paid[m])[:5]]


async def coin_name(http, mint):
    c = await get_json(http, f"https://frontend-api-v3.pump.fun/coins-v2/{mint}", headers=PUMP_HEADERS) or {}
    return c.get("symbol") or c.get("name") or mint[:6]


async def github_login(http, user_id):
    return (await get_json(http, f"https://api.github.com/user/{user_id}") or {}).get("login")


PADRE_FEED_FILE = "data/padre_feed.jsonl"  # one event per line, next to the milestone pumps; memedash serves it at /api/padre-feed


def _padre_feed(ev, gh, val, unit, coins, sig, early=False) -> None:
    """Append the claim to the feed the Padre browser extension gets through the dashboard. early: the sweep that
    precedes the claim, not the claim itself. Best effort: a failure here must never cost the Discord alert. ts is
    the write time, so the extension (which reads past its newest ts) never skips a claim whose block is older
    than a pump."""
    try:
        with open(PADRE_FEED_FILE, "a") as f:
            f.write(json.dumps({"ts": time.time(), "kind": "claim", "early": early, "claimed_at": ev["ts"],
                                "github": gh, "user_id": ev["user_id"], "amount": val, "unit": unit,
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
        if time.time() - EARLY.get(ev["pda"], 0) < 300:
            logger.info("fee_claims: claim by GitHub %s landed; its sweep was alerted already", ev["user_id"])
            continue
        gh = await github_login(http, ev["user_id"])
        coins = await social_coins(http, ev["pda"], sig, logs)
        e = await embed(http, ev, sig, val, unit, coins, gh)
        _padre_feed(ev, gh, val, unit, coins, sig)
        logger.info("fee_claims: %.2f %s by GitHub %s %s", val, unit, ev["user_id"], sig)
        if post:
            async with http.post(WEBHOOK, json={"embeds": [e]}) as r:
                if r.status >= 300:
                    logger.warning("fee_claims: Discord %s", r.status)
        out.append(e)
    return out


def embed_early(gh, user_id, val, unit, mint, name, sig):
    who = f"[{gh}](https://github.com/{gh})" if gh else f"GitHub id {user_id}"
    return {"title": f"GitHub @{gh or user_id}: {val:,.2f} {unit} of {name} fees swept into their account, unclaimed",
            "url": TRADE_URL.format(mint=mint), "color": 0xF1C40F, "thumbnail": {"url": COIN_IMAGE.format(mint=mint)},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "description": f"[{name}]({TRADE_URL.format(mint=mint)})'s creator fees were swept into {who}'s GitHub "
                           f"account, which has never claimed. The claim can follow in seconds (DCITY: 4 s) or "
                           f"hours.\n[Sweep](https://solscan.io/tx/{sig})"}


async def early(http, pda, user_id, post=True):
    """A GitHub holding account that never claimed just gained MIN_SOL or more. The claimant's wallet sweeps a
    coin's fees into the account 2-4 s before the claim (DCITY, 2026-10-15: sweep 19:10:28, claim 19:10:32), so the
    sweep names the coin and the amount before any claim exists. Its transaction is readable once confirmed, about
    a second after the socket saw the balance move. Alerts, and returns the embeds (post=False: dry run)."""
    for _ in range(16):
        sigs = await rpc(http, "getSignaturesForAddress", [pda, {"limit": 3, "commitment": "confirmed"}]) or []
        fresh = [s for s in sigs if s["signature"] not in SWEPT and not s.get("err")
                 and time.time() - (s.get("blockTime") or 0) < 30]
        if fresh:
            break
        await asyncio.sleep(0.25)
    else:
        return []
    if len(SWEPT) > 1000:
        SWEPT.clear()
    for s in fresh:  # newest first
        SWEPT.add(s["signature"])
        tx = await get_tx(http, s["signature"])
        paid, quote = {}, ""
        for ev in parse_events(tx["meta"]["logMessages"] or []) if tx else []:
            if ev["kind"] == "distribute":
                share = sum(ev["raw"] * bps for a, bps in ev["holders"] if a == pda) // 10000
                if share:
                    paid[ev["mint"]] = paid.get(ev["mint"], 0) + share
                    quote = ev["quote"]
        val, unit = amount(sum(paid.values()), quote)
        if not paid or val < (MIN_USDC if unit == "USDC" else MIN_SOL):
            continue
        # a stocked account getting its routine payout is not news: a hot coin pays its GitHub account every few
        # minutes (2026-10-16: 20 alerts in an hour). Only the payout that first brings the account past the
        # threshold counts, and the claim alert covers the rest.
        if pre_balance(tx, pda) >= MIN_SOL * 1e9:
            continue
        mint = max(paid, key=paid.get)
        EARLY[pda] = time.time()
        gh, name = await asyncio.gather(github_login(http, user_id), coin_name(http, mint))
        e = embed_early(gh, user_id, val, unit, mint, name, s["signature"])
        _padre_feed({"ts": s.get("blockTime") or int(time.time()), "user_id": user_id, "recipient": ""},
                    gh, val, unit, [(mint, name)], s["signature"], early=True)
        logger.info("fee_claims: %.2f %s swept for GitHub %s from %s, claim incoming", val, unit, user_id, mint)
        if post:
            async with http.post(WEBHOOK, json={"embeds": [e]}) as r:
                if r.status >= 300:
                    logger.warning("fee_claims: Discord %s", r.status)
        return [e]
    return []


async def _guarded(coro):
    try:
        await coro
    except Exception as e:
        logger.warning("fee_claims: %r", e)


async def watch(http, authority):
    """One websocket at processed commitment, 1-2 s ahead of confirmed: every change of an account the fee program
    owns (a sweep into a GitHub holding account, a claim, a creation: a few hundred a day, not the pump program's
    firehose), plus the claim authority's logs, which arrive in the notification itself with nothing to fetch.
    SOL only: a USDC sweep lands in the account's token account, which the fee program does not own; the claim
    path still catches those. The poll stays as the backstop, since sockets have silently skipped claims before."""
    url = solana_wss()
    if not url:
        logger.info("fee_claims: no SOLANA_WSS/SOLANA_RPC socket, polling only")
        return
    backoff = 2.0
    while True:
        try:
            bal: dict[str, int] = {}
            async with http.ws_connect(url, heartbeat=30, max_msg_size=0) as ws:
                await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "programSubscribe", "params": [
                    FEES_PROGRAM, {"encoding": "base64", "commitment": "processed",
                                   "filters": [{"memcmp": {"offset": 0, "bytes": b58(SOCIAL_PDA)}}]}]})
                await ws.send_json({"jsonrpc": "2.0", "id": 2, "method": "logsSubscribe",
                                    "params": [{"mentions": [authority]}, {"commitment": "processed"}]})
                logger.info("fee_claims: socket up, GitHub holding accounts and claims at processed")
                backoff = 2.0
                async for m in ws:
                    if m.type is not aiohttp.WSMsgType.TEXT:
                        if m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        continue
                    d = json.loads(m.data)
                    v = ((d.get("params") or {}).get("result") or {}).get("value") or {}
                    if d.get("method") == "programNotification":
                        acct = v.get("account") or {}
                        info = parse_social_pda(base64.b64decode(acct["data"][0]))
                        prev, bal[v["pubkey"]] = bal.get(v["pubkey"]), acct["lamports"]
                        # never claimed, and gained MIN_SOL in one go, or first seen holding that much (an account
                        # quiet since the socket came up changes first on its sweep): the transaction decides
                        if (info and not info["lifetime"] and v["pubkey"] not in EARLY
                                and acct["lamports"] - (prev or 0) >= MIN_SOL * 1e9):
                            asyncio.create_task(_guarded(early(http, v["pubkey"], info["user_id"])))
                    elif d.get("method") == "logsNotification" and not v.get("err") and v.get("signature") not in SEEN:
                        SEEN[v["signature"]] = time.time()
                        asyncio.create_task(_guarded(handle(http, v["signature"], v.get("logs") or [])))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("fee_claims: socket failed (%r), retry in %.0fs", e, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


async def run_fee_claims():
    if not WEBHOOK:
        logger.info("fee_claims: FEE_CLAIM_DISCORD_WEBHOOK unset — not started")
        return
    async with aiohttp.ClientSession() as http:
        g = await rpc(http, "getAccountInfo", [FEE_GLOBAL, {"encoding": "base64"}])
        # disc 8, bump 1, authority 32, disable_flags 1, social_claim_authority 32
        authority = b58(base64.b64decode(g["value"]["data"][0])[42:74])
        # Polls the authority's history as well as watching the socket: Alchemy's socket silently skipped claims
        # (2026-10-14: 4.05 SOL at 19:42 and 70.56 SOL at 23:51, connection up, no error). ~40 claims/hour, so this
        # is one cheap call per poll plus one getTransaction per new claim.
        hist = [authority, {"limit": 100, "commitment": "confirmed"}]
        SEEN.update({s["signature"]: time.time() for s in await rpc(http, "getSignaturesForAddress", hist) or []})
        logger.info("fee_claims: polling GitHub claims >= %g SOL / %g USDC every %gs", MIN_SOL, MIN_USDC, POLL_S)
        asyncio.create_task(watch(http, authority))
        while True:
            await asyncio.sleep(POLL_S)
            try:
                sigs = await rpc(http, "getSignaturesForAddress", hist)
                if not sigs:
                    continue
                for s in reversed(sigs):  # oldest first
                    if s["signature"] in SEEN:
                        continue
                    tx = None if s.get("err") else await get_tx(http, s["signature"])
                    if s.get("err") or tx:  # a failed fetch stays unseen and is retried next poll
                        SEEN[s["signature"]] = time.time()
                    if tx:
                        await handle(http, s["signature"], tx["meta"]["logMessages"] or [])
                # ponytail: more than 100 claims between two polls would skip the oldest; page with "before" if so
                window, cutoff = {s["signature"] for s in sigs}, time.time() - 600
                for k in [k for k, t in SEEN.items() if k not in window and t < cutoff]:  # the socket's recent ones stay
                    del SEEN[k]
            except Exception as e:
                logger.warning("fee_claims: poll failed (%r)", e)


if __name__ == "__main__":  # python -m src.fee_claims: the holding-account layout against a live account's bytes
    acct = bytes.fromhex("8b6035112aa9ce96fe0108000000313133373532323102bfb5dd2a0b0000003e26d16a00000000") + bytes(140)
    assert parse_social_pda(acct) == {"user_id": "11375221", "lifetime": 47963813311}
    assert parse_social_pda(bytes(179)) is None
    assert amount(47963813311, "") == (47.963813311, "SOL")
    tx = {"transaction": {"message": {"accountKeys": ["A", "B"]}},
          "meta": {"preBalances": [5, 1559560, 7], "loadedAddresses": {"writable": ["C"], "readonly": []}}}
    assert pre_balance(tx, "B") == 1559560 and pre_balance(tx, "C") == 7 and pre_balance(tx, "Z") == 0
    print("ok")
