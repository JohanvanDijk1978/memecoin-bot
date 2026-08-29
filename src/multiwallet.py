"""
multiwallet.py
──────────────
The multi-wallet buy watcher: several wallets you track buying the same coin
inside a window is the signal, and it goes to its own Telegram channel.

  🚨 3 wallets bought feesh
  📋 List: ALL · Rule: ≥3 wallets in 120 min

Where the pieces live
  multiwallet_store.py    SQLite state (wallets, buys, alerts, cursors)
  multiwallet_sources.py  chain detection (Solana + EVM websockets and sweeps)
  this file               the rule, the message, and the loop that ties them

Decisions worth knowing before editing
──────────────────────────────────────
**One post per milestone, never an edit.** When a fourth wallet joins inside
the window the channel gets a new "4 wallets bought" post rather than an edited
third one: an edit produces no notification, and a fourth wallet buying is the
most actionable moment the feature has. `mw_alerts.max_count` is the highest
count already announced, so a milestone can only fire once however many times
the same token is re-evaluated — this is the same guard memedash uses for
convergence DMs, and it is what makes a restart or a reconcile sweep silent.
Above `max_wallets` the token is muted for `cooldown_h`, so one hot coin cannot
own the channel; after that it may start again from the first milestone.

**Nothing slow sits in the detection path.** A buy is parsed from the
transaction and written to SQLite immediately; Dexscreener is called only when
a token has actually reached the threshold, and only for the token that did.
Price and market cap *at the moment of the buy* come from the transaction
itself (USD spent ÷ tokens received, × supply), not from a later quote — so the
"marketcap at time of buy" column stays honest even when the alert fires
minutes after the buy that triggered it.

**A wallet counts once.** The threshold counts DISTINCT wallets; a wallet that
buys five times is one line in the list and one wallet toward the rule.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp
from dotenv import load_dotenv

from src import multiwallet_sources as sources
from src import multiwallet_store as store
from src.utils import chain_display_name, dex_wait, gmgn_url

load_dotenv()
logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"

TOKEN_TTL = float(os.getenv("MULTIWALLET_TOKEN_TTL", "120"))     # seconds of metadata reuse
NATIVE_TTL = 300.0
PRUNE_HOURS = 6.0

# Native-asset price references on Dexscreener (any chain's ETH is one price).
_NATIVE_REF = {
    "SOL": "So11111111111111111111111111111111111111112",
    "ETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "BNB": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
}
_native_cache: dict[str, tuple[float, float]] = {}     # symbol -> (price, fetched_at)

_EXPLORER_TX = {
    "solana":    "https://solscan.io/tx/{tx}",
    "ethereum":  "https://etherscan.io/tx/{tx}",
    "base":      "https://basescan.org/tx/{tx}",
    "bsc":       "https://bscscan.com/tx/{tx}",
    "robinhood": "https://robinhoodchain.blockscout.com/tx/{tx}",
}
_EXPLORER_TOKEN = {
    "solana":    "https://solscan.io/token/{address}",
    "ethereum":  "https://etherscan.io/token/{address}",
    "base":      "https://basescan.org/token/{address}",
    "bsc":       "https://bscscan.com/token/{address}",
    "robinhood": "https://robinhoodchain.blockscout.com/token/{address}",
}


def channel_id() -> str:
    """The dedicated channel, falling back to the owner's DM so the feature is
    testable the moment it deploys rather than after a channel exists."""
    return (os.getenv("MULTIWALLET_CHANNEL_ID", "").strip()
            or os.getenv("YOUR_TELEGRAM_USER_ID", "").strip())


def enabled() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and channel_id())


# ── formatting helpers ────────────────────────────────────────────────────
def strip_md(text: Any) -> str:
    """Remove the characters that open a Markdown entity, rather than escaping.

    utils.escape\_md is right for text that sits OUTSIDE an entity, but a token
    called FREE_MONEY goes inside `*bold*` here, and Telegram's legacy Markdown
    does not honour a backslash escape inside an entity — the message is
    rejected with "can't find end of the entity" and the alert is lost. The
    dashboard's alerts.py learned the same thing. Dropping the character costs
    a cosmetic underscore; keeping it costs the whole post.
    """
    return (str(text or "")
            .replace("\\", "").replace("*", "").replace("_", "")
            .replace("`", "").replace("[", "(").replace("]", ")"))


def fmt_mcap(value: float) -> str:
    """$129.46k / $1.23M — the shape the reference alert uses. Deliberately
    finer-grained than utils.fmt_usd, which rounds $129.46k to $129K."""
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.2f}k"
    return f"${value:,.2f}"


def fmt_amount(value: float) -> str:
    """12.74M, 831.16k, 4.2 — token quantities as the reference alert shows."""
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "?"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.2f}k"
    if value >= 1:
        return f"{value:.2f}"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def fmt_price(value: float) -> str:
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    if value >= 1:
        return f"${value:,.4f}"
    return f"${value:.10f}".rstrip("0")


def utc_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%H:%M UTC")
    except (TypeError, ValueError, OSError):
        return "?"


def explorer_tx(chain: str, tx: str) -> str:
    template = _EXPLORER_TX.get(chain, "")
    return template.format(tx=tx) if template else ""


def explorer_token(chain: str, address: str) -> str:
    template = _EXPLORER_TOKEN.get(chain, "")
    return template.format(address=address) if template else ""


def birdeye_url(chain: str, address: str) -> str:
    slug = {"solana": "solana", "ethereum": "ethereum", "base": "base",
            "bsc": "bsc"}.get(chain, "")
    return f"https://birdeye.so/token/{address}?chain={slug}" if slug else ""


def link_row(chain: str, address: str, links: dict) -> str:
    """DexScreener | GMGN | Birdeye | Explorer | Website | Twitter | Telegram —
    whatever exists for this chain and this token, in that order. Socials come
    from Dexscreener's token profile and are simply absent when it has none."""
    parts = [f"[DexScreener](https://dexscreener.com/{chain}/{address})"]
    if url := gmgn_url(chain, address):
        parts.append(f"[GMGN]({url})")
    if url := birdeye_url(chain, address):
        parts.append(f"[Birdeye]({url})")
    if url := explorer_token(chain, address):
        parts.append(f"[Explorer]({url})")
    for label in ("website", "twitter", "telegram"):
        if url := (links or {}).get(label):
            parts.append(f"[{label.title()}]({url})")
    return " | ".join(parts)


# ── Dexscreener enrichment ────────────────────────────────────────────────
def _pick_pair(pairs: list[dict], address: str) -> Optional[dict]:
    wanted = store.normalize(address)
    scoped = [p for p in pairs
              if store.normalize((p.get("baseToken") or {}).get("address") or "") == wanted]
    pool = scoped or pairs
    if not pool:
        return None
    return max(pool, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))


def _links_from_pair(pair: dict) -> dict[str, str]:
    info = pair.get("info") or {}
    links: dict[str, str] = {}
    for site in (info.get("websites") or []):
        if site.get("url"):
            links.setdefault("website", site["url"])
    for social in (info.get("socials") or []):
        kind = (social.get("type") or social.get("platform") or "").lower()
        if social.get("url") and kind in ("twitter", "telegram", "discord"):
            links.setdefault(kind, social["url"])
    return links


async def fetch_token(session: aiohttp.ClientSession, chain: str, address: str,
                      max_age: float = TOKEN_TTL) -> dict:
    """Name, symbol, current mcap, supply, banner and socials for one token.

    Single-address calls only: Dexscreener silently drops EVM addresses from a
    mixed batch, and the bot has been bitten by that before.
    """
    cached = store.get_token(chain, address, max_age=max_age)
    if cached:
        return cached
    data: dict[str, Any] = {}
    try:
        await dex_wait()
        async with session.get(TOKENS_URL.format(address=address),
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
    except Exception as e:
        logger.info("multiwallet: dexscreener failed for %s (%r)", address[:8], e)
    pair = _pick_pair(data.get("pairs") or [], address)
    if not pair:
        stale = store.get_token(chain, address)     # better an old name than none
        return stale or {"symbol": "", "name": "", "image": "", "price": 0,
                         "mcap": 0, "supply": 0, "liq": 0, "links": {}}
    base = pair.get("baseToken") or {}
    price = float(pair.get("priceUsd") or 0)
    mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
    info = pair.get("info") or {}
    token = {
        "symbol": base.get("symbol") or "",
        "name": base.get("name") or "",
        "image": info.get("header") or info.get("imageUrl") or "",
        "price": price,
        "mcap": mcap,
        # supply is what turns a per-transaction price into a market cap. It is
        # not published directly, but mcap/price is exactly it and both come
        # from the same quote, so the ratio is self-consistent.
        "supply": (mcap / price) if price > 0 and mcap > 0 else 0,
        "liq": float((pair.get("liquidity") or {}).get("usd") or 0),
        "links": _links_from_pair(pair),
    }
    store.put_token(chain, address, token)
    result = store.get_token(chain, address) or token
    result["links"] = token["links"]
    return result


async def native_price(session: aiohttp.ClientSession, symbol: str) -> float:
    """USD price of SOL / ETH / BNB, cached — one quote serves every buy in the
    next few minutes, so the detection path never waits on a fresh one."""
    symbol = (symbol or "").upper()
    if symbol in ("USDC", "USDT", "DAI", "USDBC"):
        return 1.0
    reference = _NATIVE_REF.get(symbol)
    if not reference:
        return 0.0
    price, fetched = _native_cache.get(symbol, (0.0, 0.0))
    if price and time.time() - fetched < NATIVE_TTL:
        return price
    try:
        await dex_wait()
        async with session.get(TOKENS_URL.format(address=reference),
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json() if resp.status == 200 else {}
        pair = _pick_pair(data.get("pairs") or [], reference)
        price = float((pair or {}).get("priceUsd") or 0)
    except Exception as e:
        logger.info("multiwallet: native price for %s failed (%r)", symbol, e)
        price = 0.0
    if price:
        _native_cache[symbol] = (price, time.time())
        return price
    return _native_cache.get(symbol, (0.0, 0.0))[0]


# ── the rule ──────────────────────────────────────────────────────────────
def rule_line(rule: Optional[dict] = None, list_name: str = store.DEFAULT_LIST) -> str:
    rule = rule or store.get_rule()
    return (f"📋 List: {strip_md(list_name)} · Rule: ≥{rule['min_wallets']} wallets "
            f"in {rule['window_min']} min")


def group_buys(buys: list[dict]) -> list[dict]:
    """Collapse a token's buys to one row per wallet, oldest wallet first.

    A wallet that bought three times is one wallet for the threshold and one
    line in the message — its amounts summed, its FIRST buy's time, link and
    market cap kept, because the first entry is the one worth knowing.
    """
    names = store.wallet_names()
    rows: dict[str, dict] = {}
    for buy in sorted(buys, key=lambda b: b["ts"]):
        wallet = buy["wallet"]
        row = rows.get(wallet)
        if row is None:
            rows[wallet] = {
                "wallet": wallet,
                "name": names.get(wallet) or (wallet[:4] + "…" + wallet[-4:]),
                "amount": float(buy["amount"] or 0),
                "spent_usd": float(buy["spent_usd"] or 0),
                "price": float(buy["price"] or 0),
                "ts": float(buy["ts"]),
                "tx": buy["tx"],
                "buys": 1,
            }
        else:
            row["amount"] += float(buy["amount"] or 0)
            row["spent_usd"] += float(buy["spent_usd"] or 0)
            row["buys"] += 1
    return sorted(rows.values(), key=lambda r: r["ts"])


def format_alert(chain: str, address: str, token: dict, rows: list[dict],
                 rule: dict, list_name: str = store.DEFAULT_LIST) -> str:
    """The channel message. Pure function of already-gathered data, so the
    exact text can be checked without a network or a Telegram token."""
    count = len(rows)
    symbol = token.get("symbol") or "?"
    name = token.get("name") or symbol
    supply = float(token.get("supply") or 0)

    head = f"🚨 *{count} wallets bought {strip_md(symbol)}*"
    meta = f"🪙 *{strip_md(name)}* ({strip_md(symbol)}) · {chain_display_name(chain)}"
    market = (f"💰 Market cap: *{fmt_mcap(token.get('mcap'))}* · "
              f"Price: {fmt_price(token.get('price'))}")

    lines = [head, rule_line(rule, list_name), "", meta, market,
             f"📄 CA: `{address}`", "", "🛍 *Buys:*"]
    for row in rows:
        entry_mc = row["price"] * supply if row["price"] > 0 and supply > 0 else 0
        piece = [f"*{strip_md(row['name'])}*",
                 f"{fmt_amount(row['amount'])} {strip_md(symbol)}"]
        if entry_mc:
            piece.append(f"{fmt_mcap(entry_mc)} MC")
        elif row["spent_usd"]:
            piece.append(f"{fmt_mcap(row['spent_usd'])} in")
        link = explorer_tx(chain, row["tx"])
        stamp = utc_time(row["ts"])
        piece.append(f"[TX]({link}) ({stamp})" if link else f"({stamp})")
        suffix = f" ×{row['buys']}" if row["buys"] > 1 else ""
        lines.append("• " + " · ".join(piece) + suffix)

    lines += ["", "🔗 " + link_row(chain, address, token.get("links") or {})]
    return "\n".join(lines)


# ── Telegram ──────────────────────────────────────────────────────────────
async def send_alert(session: aiohttp.ClientSession, text: str,
                     image_url: str = "") -> Optional[int]:
    """Post to the multi-wallet channel. Returns the message_id, or None.

    Shaped like dex_watcher._send_telegram_alert on purpose, including the rule
    learned the hard way: sendPhoto gets its OWN try, so a Telegram timeout
    fetching a banner can never swallow the text fallback, and a Markdown parse
    error falls through to plain text rather than dropping the alert.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = channel_id()
    if not (token and chat):
        logger.warning("multiwallet: no TELEGRAM_BOT_TOKEN or MULTIWALLET_CHANNEL_ID")
        return None

    # A caption is capped at 1024 characters; a six-wallet buy list can exceed
    # that, and a truncated buy list is worse than no picture.
    if image_url and len(text) <= 1024:
        try:
            async with session.post(
                    TELEGRAM_API.format(token=token, method="sendPhoto"),
                    json={"chat_id": chat, "photo": image_url, "caption": text,
                          "parse_mode": "Markdown"},
                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
            if data.get("ok"):
                return int(data["result"]["message_id"])
            logger.info("multiwallet: sendPhoto refused (%s), falling back to text",
                        data.get("description"))
        except Exception as e:
            logger.info("multiwallet: sendPhoto errored (%r), falling back to text", e)

    for extra in ({"parse_mode": "Markdown"}, {}):
        payload = {"chat_id": chat, "text": text[:4096],
                   "disable_web_page_preview": True, **extra}
        try:
            for attempt in (1, 2):
                async with session.post(
                        TELEGRAM_API.format(token=token, method="sendMessage"),
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                if data.get("ok"):
                    return int(data["result"]["message_id"])
                if data.get("error_code") == 429 and attempt == 1:
                    wait = int((data.get("parameters") or {}).get("retry_after", 3))
                    logger.info("multiwallet: 429, retrying in %ss", wait)
                    await asyncio.sleep(min(wait, 30) + 1)
                    continue
                break
            logger.warning("multiwallet: sendMessage not ok (%s): %s",
                           "md" if extra else "plain", data)
        except Exception as e:
            logger.warning("multiwallet: sendMessage failed (%s): %r",
                           "md" if extra else "plain", e)
    return None


# ── the engine ────────────────────────────────────────────────────────────
class Watcher:
    """Owns the aiohttp session, the chain watchers, and the alert decision."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.started_at = time.time()
        self.buys_seen = 0
        self.alerts_sent = 0
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, chain: str, token: str) -> asyncio.Lock:
        """Two wallets can buy the same coin in the same second on two chains'
        callbacks at once; the count-and-post step has to be serial per token
        or both would announce the same milestone."""
        key = f"{chain}:{token}"
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def on_buy(self, buy: dict) -> None:
        """Called by a chain watcher the moment a buy is confirmed."""
        quote_symbol = buy.get("quote_sym") or ""
        unit_usd = (1.0 if buy.get("quote_is_stable")
                    else await native_price(self.session, quote_symbol))
        spent_usd = float(buy.get("quote_amt") or 0) * unit_usd
        amount = float(buy.get("amount") or 0)
        buy["spent_usd"] = spent_usd
        buy["price"] = (spent_usd / amount) if amount > 0 and spent_usd > 0 else 0.0

        if not store.record_buy(buy):
            return                       # already handled: sweep overlap or restart
        self.buys_seen += 1
        logger.info("multiwallet: buy %s %s %s for %.4f %s (%s)",
                    buy["chain"], store.wallet_names().get(store.normalize(buy["wallet"]), "?"),
                    buy["token"][:8], float(buy.get("quote_amt") or 0), quote_symbol,
                    buy["tx"][:12])
        try:
            await self.evaluate(buy["chain"], buy["token"])
        except Exception as e:
            logger.warning("multiwallet: evaluate failed for %s (%r)", buy["token"][:8], e)

    async def evaluate(self, chain: str, token_address: str,
                       list_name: str = store.DEFAULT_LIST) -> bool:
        """Decide whether this token has earned a post, and send it if so."""
        token_address = store.normalize(token_address)
        async with self._lock(chain, token_address):
            rule = store.get_rule()
            now = time.time()
            since = now - rule["window_min"] * 60
            buys = store.buys_in_window(chain, token_address, since)
            rows = group_buys(buys)
            count = len(rows)
            if count < rule["min_wallets"]:
                return False

            state = store.alert_state(list_name, chain, token_address)
            posted = int((state or {}).get("max_count") or 0)
            last_at = float((state or {}).get("last_at") or 0)

            if posted >= rule["max_wallets"]:
                # Ceiling reached: silent until the cooldown expires, then this
                # token may start over from the first milestone.
                if now - last_at < rule["cooldown_h"] * 3600:
                    return False
                posted = 0
            if count <= posted:
                return False             # this milestone was already announced

            token = await fetch_token(self.session, chain, token_address)
            if not token.get("symbol"):
                token = dict(token)
                token["symbol"] = next((b["symbol"] for b in buys if b.get("symbol")), "?")
            text = format_alert(chain, token_address, token, rows, rule, list_name)
            message_id = await send_alert(self.session, text, token.get("image") or "")
            if message_id is None:
                # A send that failed for a transient reason should be retried by
                # the next buy, so nothing is recorded here.
                logger.warning("multiwallet: alert for %s not delivered", token_address[:8])
                return False
            store.record_alert(list_name, chain, token_address, count, message_id)
            self.alerts_sent += 1
            logger.info("multiwallet: 🪙 alerted %d wallets on %s (%s)",
                        count, token.get("symbol") or token_address[:8], chain)
            return True


async def _sweep_loop(watcher, engine: Watcher) -> None:
    """Gap filler. Runs slowly while the socket is up and takes over the job
    entirely on a chain that has no websocket configured."""
    while True:
        live = getattr(watcher, "connected", False)
        await asyncio.sleep(sources.RECONCILE_SEC if live else sources.POLL_SEC)
        try:
            found = await watcher.sweep()
            if found:
                logger.info("multiwallet %s: sweep picked up %d buy(s) the socket missed",
                            watcher.name, found)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("multiwallet %s: sweep failed (%r)", watcher.name, e)


async def _prune_loop() -> None:
    while True:
        await asyncio.sleep(PRUNE_HOURS * 3600)
        try:
            removed = store.prune()
            if removed:
                logger.info("multiwallet: pruned %d old buys", removed)
        except Exception as e:
            logger.warning("multiwallet: prune failed (%r)", e)


async def run_multiwallet_watcher() -> None:
    """Entrypoint for main.py's gather.

    Costs nothing until it is used: with no wallets added, the Solana socket
    holds zero subscriptions, every EVM chain idles without subscribing, and
    the sweeps return immediately.
    """
    store.init_schema()
    if not enabled():
        logger.info("multiwallet: no MULTIWALLET_CHANNEL_ID and no owner DM — disabled")
        return

    async with aiohttp.ClientSession() as session:
        engine = Watcher(session)
        watchers: list[Any] = [sources.SolanaWatcher(session, engine.on_buy)]
        for chain in sources.evm_chains():
            watchers.append(sources.EvmWatcher(session, chain, engine.on_buy))

        report = ", ".join(f"{row['chain']}{'' if row['live'] else ' (sweep only)'}"
                           for row in sources.endpoint_report() if row["rpc"])
        logger.info("multiwallet: watching %d wallets on %s → chat %s",
                    len(store.list_wallets()), report or "no chains", channel_id())

        tasks = [asyncio.create_task(w.run_live()) for w in watchers]
        tasks += [asyncio.create_task(_sweep_loop(w, engine)) for w in watchers]
        tasks.append(asyncio.create_task(_prune_loop()))
        await asyncio.gather(*tasks, return_exceptions=True)


# ── read models for the bot commands ──────────────────────────────────────
def status_lines() -> list[str]:
    """What /list and /buys print under their own content."""
    rule = store.get_rule()
    lines = [f"⚙️ ≥{rule['min_wallets']} wallets in {rule['window_min']} min · "
             f"milestones to {rule['max_wallets']} · {rule['cooldown_h']}h cooldown"]
    chains = []
    for row in sources.endpoint_report():
        if not row["rpc"]:
            continue
        chains.append(f"{row['chain']}{'' if row['live'] else ' (sweep)'}")
    lines.append("📡 " + (", ".join(chains) if chains else "no chain endpoints configured"))
    return lines
