"""
long_watcher.py
───────────────
The Long.xyz watcher: four independent detectors, one deduplicated alert
stream, one Discord webhook.

What it answers
───────────────
"Long added support for a stock that was not available before" — as early as
the information exists anywhere machine-readable. See `long_sources.py` for how
each source was found and why it is where it is; the short version:

  1. `robinhood_factory`  a new tokenised stock is DEPLOYED on Robinhood Chain.
                          Websocket push, sub-second. Upstream of Long itself.
  2. `long_frontend`      Long ships a build whose pairable-asset array gained
                          an entry. This is the tradeable moment.
  3. `long_indexer`       the first coin ever launched against a numeraire —
                          proves Long enabled it even if our parser broke.
  4. `chainlink_feed`     a price feed appears for a ticker Long does not list.
                          Speculative; low confidence by construction.

Every alert carries its source, a confidence, and CEST timestamps to the
millisecond. Every subject is also written to `long_latency`, so after a few
real listings `/longlatency` answers the question this was really built for:
which source is consistently first.

Deduplication is a single SQLite primary key (`long_store.claim_alert`). A
reconnect, a reconcile sweep, a restart and two detectors finding the same
stock all collapse to exactly one message.

Seeding: the first run records the current world silently. Without that, the
first start would announce 206 Robinhood stock tokens and 56 Long numeraires.
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

from src import long_store as store
from src.long_sources import (
    Http,
    LongFrontendWatcher,
    LongIndexerWatcher,
    RobinhoodFactoryWatcher,
    FeedWatcher,
    ROBINHOOD_CHAIN_ID,
    ROBINHOOD_EXPLORER_API,
    ROBINHOOD_RPC,
    ROBINHOOD_WSS,
    LONG_APP_BASE,
    LONG_GRAPHQL_URL,
    clean_stock_name,
)

logger = logging.getLogger(__name__)

load_dotenv()

LONG_DISCORD_WEBHOOK = os.getenv("LONG_DISCORD_WEBHOOK", "")
LONG_ENABLED = os.getenv("LONG_WATCHER_ENABLED", "1") not in ("0", "false", "False")
FRONTEND_POLL_SECONDS = float(os.getenv("LONG_FRONTEND_POLL_SECONDS", "5"))
INDEXER_POLL_SECONDS = float(os.getenv("LONG_INDEXER_POLL_SECONDS", "2"))
FEED_POLL_SECONDS = float(os.getenv("LONG_FEED_POLL_SECONDS", "300"))
FEED_WATCHER_ENABLED = os.getenv("LONG_FEED_WATCHER", "1") not in ("0", "false", "False")

EXPLORER_WEB = ROBINHOOD_EXPLORER_API.replace("/api/v2", "")

# Discord embed colours, by how much the alert should make you move.
COLOR_LISTED = 0x2ECC71      # Long now supports it — act
COLOR_DEPLOYED = 0x3498DB    # exists on-chain, not listed yet
COLOR_FIRST_COIN = 0x9B59B6  # first launch against a numeraire
COLOR_FEED = 0x95A5A6        # speculative


# ── time formatting ───────────────────────────────────────────────────────────
def _cest_tz():
    """Europe/Amsterdam, which is CEST for most of the year. Falls back to a
    fixed +02:00 if tzdata is missing (bare containers often lack it)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Europe/Amsterdam")
    except Exception:
        from datetime import timedelta
        return timezone(timedelta(hours=2), "CEST")


_TZ = _cest_tz()


def cest(ts: Optional[float] = None) -> str:
    """`2026-09-04 06:31:22.418 CEST` — millisecond precision, as asked."""
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), _TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d} " + dt.strftime("%Z")


# ── notifier ──────────────────────────────────────────────────────────────────
class Notifier:
    """Alert sink. Deliberately one small interface so the provider can be
    swapped (Telegram, Slack, a second webhook) without touching detection."""

    async def send(self, alert: dict) -> bool:
        raise NotImplementedError


class DiscordWebhookNotifier(Notifier):
    def __init__(self, url: str, http: Optional[Http] = None, username: str = "Long Watch"):
        self.url = url
        self.http = http
        self.username = username

    async def send(self, alert: dict) -> bool:
        if not self.url:
            logger.warning("long: LONG_DISCORD_WEBHOOK unset — alert not delivered: %s",
                           alert.get("title"))
            return False
        payload = {"username": self.username, "embeds": [build_embed(alert)]}
        content = alert.get("content")
        if content:
            payload["content"] = content
        try:
            if self.http is not None:
                async with self.http.session.post(self.url, json=payload) as r:
                    ok = r.status in (200, 204)
                    if not ok:
                        logger.warning("long: webhook %s: %s", r.status, (await r.text())[:200])
                    return ok
            async with aiohttp.ClientSession() as s:
                async with s.post(self.url, json=payload,
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                    return r.status in (200, 204)
        except Exception as e:
            logger.warning("long: webhook failed: %s", e)
            return False


class CollectingNotifier(Notifier):
    """Used by tools/test_long.py — the offline proof that an alert fires
    exactly once."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, alert: dict) -> bool:
        self.sent.append(alert)
        return True


# ── alert rendering ───────────────────────────────────────────────────────────
def build_embed(a: dict) -> dict:
    fields = []

    def add(name, value, inline=True):
        if value:
            fields.append({"name": name, "value": str(value)[:1024], "inline": inline})

    add("Ticker", f"`{a['ticker']}`" if a.get("ticker") else None)
    add("Company", a.get("company"))
    add("Kind", a.get("kind"))
    add("Token address", f"`{a['address']}`" if a.get("address") else None, inline=False)
    add("Paired stock", a.get("paired_stock"))
    add("Confidence", a.get("confidence"))
    add("Source", f"`{a['source']}`")
    add("Detected", a.get("detected_at_cest"), inline=False)
    if a.get("chain_time_cest"):
        add("On-chain time", a["chain_time_cest"], inline=False)
    if a.get("lag_ms") is not None:
        add("Detection lag", f"{a['lag_ms']} ms after the on-chain event")
    add("Evidence", a.get("evidence"), inline=False)

    links = []
    if a.get("address"):
        links.append(f"[Explorer]({EXPLORER_WEB}/address/{a['address']})")
    if a.get("tx_hash"):
        links.append(f"[Tx]({EXPLORER_WEB}/tx/{a['tx_hash']})")
    links.append(f"[Long]({LONG_APP_BASE})")
    if a.get("long_url"):
        links.append(f"[Coin]({a['long_url']})")
    add("Links", " · ".join(links), inline=False)

    return {
        "title": a.get("title", "Long update")[:256],
        "description": (a.get("description") or "")[:2048],
        "color": a.get("color", COLOR_LISTED),
        "fields": fields[:25],
        "footer": {"text": f"long_watcher · {a.get('source', '?')}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **({"thumbnail": {"url": a["image"]}} if a.get("image") else {}),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Orchestration
# ═══════════════════════════════════════════════════════════════════════════

class LongWatcher:
    def __init__(self, notifier: Optional[Notifier] = None, http: Optional[Http] = None):
        self.http = http
        self.notifier = notifier
        self.frontend: Optional[LongFrontendWatcher] = None
        self.indexer: Optional[LongIndexerWatcher] = None
        self.factory: Optional[RobinhoodFactoryWatcher] = None
        self.feeds: Optional[FeedWatcher] = None
        self.started_at = time.time()
        self.stats = {"frontend_polls": 0, "builds_seen": 0, "alerts": 0, "errors": 0}

    # ── alert plumbing ────────────────────────────────────────────────────
    async def _fire(self, key: str, alert: dict) -> bool:
        """One alert, at most once, ever. Every path goes through here."""
        alert.setdefault("detected_at", time.time())
        alert["detected_at_cest"] = cest(alert["detected_at"])
        if not store.claim_alert(key, alert.get("source", "?"), alert.get("subject", key),
                                 {k: v for k, v in alert.items() if isinstance(v, (str, int, float))}):
            logger.debug("long: alert %s already sent, skipping", key)
            return False
        self.stats["alerts"] += 1
        logger.info("🔔 long: %s (%s)", alert.get("title"), alert.get("source"))
        if self.notifier:
            await self.notifier.send(alert)
        return True

    @staticmethod
    def _subject(ticker: Optional[str], address: Optional[str]) -> str:
        """The thing an alert is ABOUT, stable across sources. Ticker when we
        have one — the factory event and the frontend array agree on ticker but
        not always on casing of the address."""
        if ticker:
            return f"stock:{ticker.upper()}"
        return f"addr:{(address or '').lower()}"

    # ── source 2: Robinhood factory ───────────────────────────────────────
    async def on_stock_deployed(self, row: dict, *, seeding: bool = False) -> None:
        addr = row["address"]
        subject = self._subject(row.get("symbol"), addr)
        is_new = store.add_rh_stock({
            "address": addr, "symbol": row.get("symbol"), "name": row.get("name"),
            "uid": row.get("uid"), "block_number": row.get("block_number"),
            "tx_hash": row.get("tx_hash"), "chain_ts": row.get("chain_ts"),
        })
        store.record_sighting(subject, "robinhood_factory",
                              f"block {row.get('block_number')} {row.get('tx_hash')}")
        if seeding or not is_new:
            return

        listed = store.known_numeraires(ROBINHOOD_CHAIN_ID)
        already_on_long = addr in listed
        await self._fire(f"stock_deploy:{addr}", {
            "title": f"🆕 New Robinhood stock token: {row.get('symbol')}",
            "description": (
                f"**{row.get('name') or row.get('symbol')}** was just deployed on Robinhood "
                f"Chain. This is upstream of Long — the token now exists, and Long can list "
                f"it as a pairing asset at any time."
                + ("\n\nIt is **already** in Long's pairable set." if already_on_long else
                   "\n\nNot yet offered by Long.")
            ),
            "ticker": row.get("symbol"),
            "company": row.get("name"),
            "kind": "stock (on-chain)",
            "address": addr,
            "tx_hash": row.get("tx_hash"),
            "source": row.get("source", "robinhood_factory"),
            "subject": subject,
            "confidence": "high — decoded straight from the factory's Deployed event",
            "evidence": f"`Deployed` @ block {row.get('block_number')} on chain {ROBINHOOD_CHAIN_ID}",
            "chain_time_cest": cest(row["chain_ts"]) if row.get("chain_ts") else None,
            "lag_ms": (int((time.time() - row["chain_ts"]) * 1000)
                       if row.get("chain_ts") else None),
            "color": COLOR_DEPLOYED,
        })

    # ── source 1: Long's frontend ─────────────────────────────────────────
    async def on_numeraires(self, snapshot: dict, *, seeding: bool = False,
                            max_new_alerts: Optional[int] = None) -> list[dict]:
        rows = snapshot["numeraires"]
        seen_now = {r["address"] for r in rows}
        before = store.known_numeraires(ROBINHOOD_CHAIN_ID)
        added: list[dict] = []

        for r in rows:
            subject = self._subject(r.get("symbol"), r["address"])
            is_new = store.upsert_numeraire(ROBINHOOD_CHAIN_ID, r)
            store.record_sighting(subject, "long_frontend",
                                  f"build {snapshot.get('fingerprint')}")
            if is_new and not seeding:
                added.append(r)

        # Removals are logged, never alerted loudly — a delisting is interesting
        # but it is not the thing that makes money, and a parser hiccup would
        # otherwise page you at 3am claiming Long dropped 40 stocks.
        removed = [a for a in before if a not in seen_now]
        if removed and not seeding:
            if len(removed) > max(3, len(before) // 4):
                logger.error("long: %d numeraires vanished from build %s — refusing to "
                             "trust this parse", len(removed), snapshot.get("fingerprint"))
                return []
            for addr in removed:
                store.mark_numeraire_removed(ROBINHOOD_CHAIN_ID, addr)
                logger.warning("long: numeraire removed from Long: %s (%s)",
                               before[addr]["symbol"], addr)

        if max_new_alerts is not None and len(added) > max_new_alerts:
            # Recovering from a blind spell against a baseline that may be months
            # stale. A burst of "Long now supports X" for assets it has supported
            # all along is worse than silence — absorb and say so.
            logger.warning("long: %d assets are new relative to the recorded set "
                           "(cap %d) — absorbing silently rather than bursting: %s",
                           len(added), max_new_alerts,
                           ", ".join(a["symbol"] for a in added[:20]))
            return added

        for r in added:
            onchain = store.has_rh_stock(r["address"])
            await self._fire(f"long_listing:{r['address']}", {
                "title": f"🚀 Long now supports {r['symbol']}",
                "description": (
                    f"**{r['name']}** ({r['symbol']}) is now offered as a pairing asset on "
                    f"Long. You can launch a coin against it."
                    + ("" if onchain else
                       "\n\n⚠️ The token was not in our on-chain registry — either it "
                       "predates seeding or the factory watcher missed it.")
                ),
                "ticker": r["symbol"],
                "company": r["name"],
                "kind": r["kind"],
                "address": r["address"],
                "source": "long_frontend",
                "subject": self._subject(r["symbol"], r["address"]),
                "confidence": ("high — appeared in Long's own pairable-asset array"
                               if onchain else
                               "medium — in Long's array but unknown to the on-chain registry"),
                "evidence": f"build `{snapshot.get('fingerprint')}` · chunk `"
                            f"{(snapshot.get('chunk_url') or '').rsplit('/', 1)[-1]}`",
                "color": COLOR_LISTED,
                "content": "@here" if os.getenv("LONG_PING_HERE") == "1" else None,
            })
        return added

    # ── source 3: Long's indexer ──────────────────────────────────────────
    async def on_token(self, t: dict, *, seeding: bool = False) -> None:
        num = (t.get("token_numeraire_address") or "").lower()
        if not num:
            return
        created = _parse_iso(t.get("token_creation_timestamp"))
        first = store.record_numeraire_use({
            "numeraire": num, "token_address": t.get("token_address"),
            "token_symbol": t.get("token_symbol"), "token_name": t.get("token_name"),
            "created_ts": created,
        })
        if not first or seeding:
            return

        listed = store.known_numeraires(ROBINHOOD_CHAIN_ID)
        meta = listed.get(num)
        onchain = [s for s in store.all_rh_stocks() if s["address"] == num]
        ticker = (meta["symbol"] if meta else None) or (onchain[0]["symbol"] if onchain else None)
        company = (meta["name"] if meta else None) or (onchain[0]["name"] if onchain else None)
        subject = self._subject(ticker, num)
        store.record_sighting(subject, "long_indexer",
                              f"first coin {t.get('token_symbol')} {t.get('token_address')}")

        await self._fire(f"first_coin:{num}", {
            "title": f"🥇 First coin ever launched against {ticker or num[:10]}",
            "description": (
                f"**{t.get('token_name')}** (`{t.get('token_symbol')}`) is the first coin "
                f"paired with {ticker or num}. Nobody had used this pairing asset before, "
                f"which means Long enabled it — independently of whether we caught the "
                f"frontend change."
            ),
            "ticker": ticker,
            "company": company,
            "kind": (meta["kind"] if meta else "unknown"),
            "address": num,
            "paired_stock": f"{t.get('token_symbol')} → {ticker or num}",
            "source": t.get("source", "long_indexer"),
            "subject": subject,
            "confidence": ("high — a coin cannot be launched against an asset Long does "
                           "not accept"),
            "evidence": f"Token `{t.get('token_address')}` created "
                        f"{t.get('token_creation_timestamp')}",
            "chain_time_cest": cest(created) if created else None,
            "lag_ms": int((time.time() - created) * 1000) if created else None,
            "long_url": f"{LONG_APP_BASE}/token/{t.get('token_address')}",
            "image": t.get("token_image_public_url"),
            "color": COLOR_FIRST_COIN,
        })

    # ── source 4: Chainlink feeds ─────────────────────────────────────────
    async def on_feed(self, f: dict, *, seeding: bool = False) -> None:
        addr = f["address"]
        is_new = store.add_feed(f)
        if not is_new or seeding:
            return
        sym = f.get("symbol")
        listed_syms = {r["symbol"] for r in store.known_numeraires(ROBINHOOD_CHAIN_ID).values()}
        if sym and sym in listed_syms:
            logger.info("long: feed for already-listed %s, no alert", sym)
            return
        subject = self._subject(sym, None) if sym else f"feed:{addr}"
        store.record_sighting(subject, "chainlink_feed", addr)
        await self._fire(f"feed:{addr}", {
            "title": f"📡 Price feed deployed for {sym or 'an unknown ticker'}",
            "description": (
                f"A Chainlink aggregator (`{f.get('description') or '?'}`) was deployed on "
                f"Robinhood Chain for a ticker Long does not currently list. About half of "
                f"Long's listed stocks carry a feed, so this *may* precede a listing — "
                f"treat as a heads-up, not a confirmation."
            ),
            "ticker": sym,
            "address": addr,
            "tx_hash": f.get("tx_hash"),
            "source": "chainlink_feed",
            "subject": subject,
            "confidence": "low — unproven leading indicator, see HANDOFF_LONG.md",
            "evidence": f"EACAggregatorProxy created at block {f.get('block_number')}",
            "color": COLOR_FEED,
        })

    # ── seeding ───────────────────────────────────────────────────────────
    def _baseline_path(self) -> str:
        return os.getenv("LONG_BASELINE_PATH") or os.path.join("tools", "long_baseline.json")

    def seed_from_baseline(self) -> int:
        """Fallback when Long's frontend is unreachable (Cloudflare, an outage).

        `tools/long_baseline.json` is the asset set captured when this was built.
        Seeding from it is strictly worse than reading the live array — it can be
        months stale — but it lets the three detectors that do NOT depend on
        app.long.xyz keep working, and it is what makes a stock-deploy alert able
        to say "already on Long" or "not yet offered". The frontend loop keeps
        retrying and re-seeds from the live source the moment it gets through.
        """
        path = self._baseline_path()
        if not os.path.exists(path):
            logger.error("long: no baseline at %s — the on-chain detectors will run "
                         "but cannot say whether a stock is already on Long", path)
            return 0
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        n = 0
        for row in data.get("assets", []):
            symbol, kind, address, decimals, feed = (row + [None] * 5)[:5]
            store.upsert_numeraire(ROBINHOOD_CHAIN_ID, {
                "symbol": symbol, "name": symbol, "kind": kind, "address": address,
                "decimals": decimals, "feed": feed or None,
            })
            n += 1
        for lev in data.get("leverage_tokens", []):
            ticker, name, address, underlying = (list(lev) + [None] * 4)[:4]
            store.upsert_numeraire(ROBINHOOD_CHAIN_ID, {
                "symbol": ticker, "name": name, "kind": "leverage",
                "address": (address or "").lower(), "decimals": 18, "feed": None,
                "extra": {"underlying": underlying},
            })
            n += 1
        store.set_cursor("frontend:seeded_from", f"baseline {data.get('captured_at_cest', '?')}")
        return n

    async def seed(self) -> dict:
        """Record the world as it is, silently. Runs once; the flag lives in the
        DB so a restart never re-seeds and never re-announces."""
        result = {"numeraires": 0, "stocks": 0, "numeraire_uses": 0}

        if not store.is_seeded("frontend"):
            try:
                snap = await self.frontend.snapshot()
                await self.on_numeraires(snap, seeding=True)
                result["numeraires"] = len(snap["numeraires"])
                store.set_cursor("frontend:fingerprint", snap["fingerprint"])
                store.set_cursor("frontend:seeded_from", f"live {snap['fingerprint']}")
                store.mark_seeded("frontend")
                logger.info("long: seeded %d pairable assets from build %s",
                            result["numeraires"], snap["fingerprint"])
            except Exception as e:
                # NOT fatal. Three of the four detectors never touch app.long.xyz,
                # and the first-coin detector alone still catches a listing within
                # minutes. Going dark entirely because one source is blocked would
                # be the worse failure.
                result["frontend_degraded"] = str(e)
                n = self.seed_from_baseline()
                logger.error("long: could not read Long's pairable-asset array: %s", e)
                logger.error("long: FRONTEND DETECTOR DEGRADED — seeded %d assets from "
                             "the baseline file instead. The factory, indexer and feed "
                             "detectors are unaffected and the frontend loop keeps "
                             "retrying. See HANDOFF_LONG.md §9.", n)

        if not store.is_seeded("factory") and self.factory and ROBINHOOD_RPC:
            n = await self.factory.sweep(span=60_000_000)
            result["stocks"] = n
            store.mark_seeded("factory")
            logger.info("long: seeded %d Robinhood stock tokens from the factory", n)

        if not store.is_seeded("indexer") and self.indexer:
            for t in await self.indexer.used_numeraires():
                await self.on_token({
                    "token_numeraire_address": t.get("token_numeraire_address"),
                    "token_address": t.get("token_address"),
                    "token_symbol": t.get("token_symbol"),
                    "token_creation_timestamp": t.get("token_creation_timestamp"),
                }, seeding=True)
                result["numeraire_uses"] += 1
            store.mark_seeded("indexer")
            logger.info("long: seeded %d numeraires already in use", result["numeraire_uses"])

        if not store.is_seeded("feeds") and self.feeds and ROBINHOOD_RPC:
            try:
                for c in await self.feeds._created_contracts(pages=3):
                    store.add_feed({"address": c["address"], "description": None,
                                    "symbol": None, "block_number": c.get("block_number"),
                                    "tx_hash": c.get("tx_hash")})
                store.mark_seeded("feeds")
            except Exception as e:
                logger.warning("long: feed seeding failed (will retry next start): %s", e)

        return result

    # ── loops ─────────────────────────────────────────────────────────────
    async def _frontend_loop(self) -> None:
        consecutive_errors = 0
        while True:
            try:
                self.stats["frontend_polls"] += 1
                if not store.is_seeded("frontend"):
                    # Deferred seeding: the source was blocked at startup and has
                    # just answered. Reconcile against whatever is recorded, but
                    # capped, so a stale baseline cannot produce a burst.
                    snap = await self.frontend.snapshot()
                    added = await self.on_numeraires(snap, max_new_alerts=5)
                    store.set_cursor("frontend:fingerprint", snap["fingerprint"])
                    store.set_cursor("frontend:seeded_from", f"live {snap['fingerprint']}")
                    store.mark_seeded("frontend")
                    logger.warning("long: frontend detector RECOVERED — read %d assets "
                                   "from build %s, %d new vs the recorded set",
                                   len(snap["numeraires"]), snap["fingerprint"], len(added))
                    await asyncio.sleep(FRONTEND_POLL_SECONDS)
                    continue
                fp = await self.frontend.build_changed()
                if fp:
                    self.stats["builds_seen"] += 1
                    logger.info("long: new frontend build %s — re-reading pairable assets", fp)
                    snap = await self.frontend.snapshot()
                    store.set_cursor("frontend:fingerprint", snap["fingerprint"])
                    added = await self.on_numeraires(snap)
                    if not added:
                        logger.info("long: build %s shipped, pairable set unchanged (%d assets)",
                                    fp, len(snap["numeraires"]))
                consecutive_errors = 0
                delay = FRONTEND_POLL_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                self.stats["errors"] += 1
                delay = min(FRONTEND_POLL_SECONDS * (2 ** min(consecutive_errors, 6)), 300)
                logger.warning("long: frontend poll failed (%d in a row): %s — next in %.0fs",
                               consecutive_errors, e, delay)
            await asyncio.sleep(delay)

    async def run(self) -> None:
        if not LONG_ENABLED:
            logger.info("long: watcher disabled (LONG_WATCHER_ENABLED=0)")
            return

        async with Http() as http:
            self.http = http
            if self.notifier is None:
                self.notifier = DiscordWebhookNotifier(LONG_DISCORD_WEBHOOK, http)

            self.frontend = LongFrontendWatcher(http)
            self.indexer = LongIndexerWatcher(
                http, self.on_token,
                get_cursor=lambda: store.get_cursor("indexer:after"),
                set_cursor=lambda v: store.set_cursor("indexer:after", v),
            )
            self.factory = RobinhoodFactoryWatcher(
                http, self.on_stock_deployed,
                get_cursor=lambda: (int(store.get_cursor("factory:block") or 0) or None),
                set_cursor=lambda b: store.set_cursor("factory:block", b),
            )
            self.feeds = FeedWatcher(http, self.on_feed)

            if not ROBINHOOD_RPC:
                logger.warning("long: ROBINHOOD_RPC unset — on-chain detectors are off. "
                               "Deploy fomo/.env to the box (see reference_vps_setup).")

            try:
                seeded = await self.seed()
            except Exception as e:
                logger.error("long: seeding failed outright — refusing to start rather "
                             "than alerting on a world we never recorded: %s", e)
                return
            if seeded.get("frontend_degraded"):
                logger.warning("long: starting DEGRADED — 3 of 4 detectors live")

            logger.info("✅ long watcher up — frontend %.0fs, indexer %.0fs, factory %s, "
                        "feeds %s", FRONTEND_POLL_SECONDS, INDEXER_POLL_SECONDS,
                        "ws" if ROBINHOOD_WSS else "sweep-only",
                        "on" if (FEED_WATCHER_ENABLED and ROBINHOOD_RPC) else "off")

            tasks = [
                asyncio.create_task(self._frontend_loop(), name="long_frontend"),
                asyncio.create_task(self.indexer.run(INDEXER_POLL_SECONDS), name="long_indexer"),
            ]
            if ROBINHOOD_RPC or ROBINHOOD_WSS:
                tasks.append(asyncio.create_task(self.factory.run(), name="long_factory"))
            if FEED_WATCHER_ENABLED and ROBINHOOD_RPC:
                tasks.append(asyncio.create_task(
                    self.feeds.run(store.known_feed_addresses, FEED_POLL_SECONDS),
                    name="long_feeds"))

            # Supervise: a task that dies takes its reason to the log and is
            # restarted, rather than silently leaving a detector dark.
            while True:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    name = t.get_name()
                    exc = t.exception() if not t.cancelled() else None
                    logger.error("long: task %s exited (%s) — restarting in 10s", name, exc)
                    tasks.remove(t)
                    await asyncio.sleep(10)
                    factory_map = {
                        "long_frontend": lambda: self._frontend_loop(),
                        "long_indexer": lambda: self.indexer.run(INDEXER_POLL_SECONDS),
                        "long_factory": lambda: self.factory.run(),
                        "long_feeds": lambda: self.feeds.run(
                            store.known_feed_addresses, FEED_POLL_SECONDS),
                    }
                    if name in factory_map:
                        tasks.append(asyncio.create_task(factory_map[name](), name=name))

    # ── health ────────────────────────────────────────────────────────────
    def health(self) -> dict:
        return {
            "uptime_s": int(time.time() - self.started_at),
            "stats": dict(self.stats),
            "factory_ws_connected": bool(self.factory and self.factory.connected),
            "numeraires_known": len(store.known_numeraires(ROBINHOOD_CHAIN_ID)),
            "rh_stocks_known": len(store.all_rh_stocks()),
            "alerts_sent": store.alert_count(),
            "now_cest": cest(),
        }


def _parse_iso(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        txt = s.replace("Z", "+00:00")
        return datetime.fromisoformat(txt).timestamp()
    except Exception:
        return None


async def run_long_watcher() -> None:
    """Entrypoint used by main.py."""
    await LongWatcher().run()
