"""
tools/test_long.py
──────────────────
Offline proof for the Long.xyz watcher. No network, no Discord, no RPC.

What this can prove
  * the pairable-asset array is parsed correctly out of a minified chunk, and
    a chunk that merely mentions stocks is NOT mistaken for the config chunk
  * a build fingerprint changes when and only when a chunk hash changes
  * the `Deployed` event decoder produces the right ticker/name/address from
    real ABI bytes
  * the store's dedup key fires an alert exactly once, across a restart
  * SEEDING IS SILENT, and the very next poll with one extra ticker produces
    exactly one alert — the end-to-end simulation Johan asked for

What it CANNOT prove, and the handoff says so plainly: the live response
shapes, Cloudflare's behaviour under a real deploy, the websocket handshake,
and whether Blockscout answers `description()` for a fresh feed. Those are
`tools/diag_long.py`, run from the box.

Run:  python3 tools/test_long.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import long_store as store            # noqa: E402
from src import long_sources as S              # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


# ── fixtures: shaped exactly like the live minified bundle ───────────────────
def make_chunk(extra_entries: str = "") -> str:
    """A miniature of the real chunk. The helper is called `s` here because
    that is what the minifier happened to pick on 2026-09-04 — the parser must
    not depend on that, which the `q(` variant below checks."""
    return (
        'let o="1"===t.default.env.NEXT_PUBLIC_ROBINHOOD_NVDA3X_LAUNCH,'
        's=(e,t,o,s,i,n,c)=>({symbol:e,name:t,kind:o,address:s,decimals:i}),i=['
        's("ETH","Ether","native",r.zeroAddress,18,"0x78F3556b67E17Df817D51Ef5a990cDaF09E8d3A9"),'
        's("USDG","Global Dollar","stable","0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",6,"0x61B7e5650328764B076A108EFF5fa7282a1B9aD2"),'
        's("AAPL","Apple","stock","0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9",18,"0x6B22A786bAa607d76728168703a39Ea9C99f2cD0"),'
        's("AMC","AMC Entertainment","stock","0x05a3d1Cd21d0C88145E82600E62e7E496e0F222B",18,void 0),'
        's("NVDA","NVIDIA","stock","0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC",18,"0x379EC4f7C378F34a1B47E4F3cbeBCbAC3E8E9F15"),'
        's("TSLA","Tesla","stock","0x322F0929c4625eD5bAd873c95208D54E1c003b2d",18,"0x4A1166a659A55625345e9515b32adECea5547C38"),'
        's("MSFT","Microsoft","stock","0xe93237C50D904957Cf27E7B1133b510C669c2e74",18,"0x45C3C877C15E6BA2EBB19eA114Ea508d14C1Af2E"),'
        's("META","Meta","stock","0xc0D6457C16Cc70d6790Dd43521C899C87ce02f35",18,"0x7C38C00C30BEe9378381E7B6135d7283356D71b1"),'
        's("COIN","Coinbase","stock","0x6330D8C3178a418788dF01a47479c0ce7CCF450b",18,"0xA3a468A452940B7D6b69991207B508c609a98Ef2"),'
        's("SPY","SPDR S&P 500 ETF","etf","0x117cc2133c37B721F49dE2A7a74833232B3B4C0C",18,"0x319724394D3A0e3669269846abE664Cd621f9f6A"),'
        's("GLD","SPDR Gold Shares","etf","0xC9a981FEE1F9DEc688bb123ccDeCc63D0deBFC4e",18,void 0)'
        + extra_entries +
        '];let a=[{address:(0,t.getAddress)("0xF51fb54DE60f6e16252E852A5Ed0E60B8307606A"),'
        'name:"NVDA 3x Long",ticker:"NVDAx3L",accountIndex:14991,targetLeverage:3,'
        'underlyingSymbol:"NVDA",poolFee:3e3,poolTickSpacing:30}];'
    )


NEW_TICKER_ENTRY = (
    ',s("PYPL","PayPal","stock","0x1234567890AbCdEf1234567890aBcDeF12345678",18,void 0)'
)


def make_html(chunks: list[str]) -> str:
    tags = "".join(f'<script src="/_next/static/chunks/{c}.js" async></script>' for c in chunks)
    return f"<!doctype html><html><head>{tags}</head><body>Long</body></html>"


# ── ABI encoding helper, so the log fixture is real bytes not a guess ─────────
def encode_deployed(stock: str, name: str, symbol: str) -> str:
    def word(h: str) -> str:
        return h.rjust(64, "0")

    def dyn(text: str) -> str:
        raw = text.encode().hex()
        padded = raw.ljust(((len(raw) + 63) // 64) * 64, "0")
        return word(format(len(text.encode()), "x")) + padded

    head_words = 3
    name_off = head_words * 32
    sym_off = name_off + 32 + ((len(name.encode()) + 31) // 32) * 32
    return ("0x"
            + word(stock[2:].lower())
            + word(format(name_off, "x"))
            + word(format(sym_off, "x"))
            + dyn(name) + dyn(symbol))


# ── 1. parsers ────────────────────────────────────────────────────────────────
def test_parsers() -> None:
    print("\n▶ parsers")
    rows = S.parse_numeraires(make_chunk())
    by = {r["symbol"]: r for r in rows}
    check("parses every entry", len(rows) == 11, f"got {len(rows)}")
    check("stock fields correct",
          by.get("NVDA", {}).get("address") == "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec"
          and by["NVDA"]["name"] == "NVIDIA" and by["NVDA"]["kind"] == "stock"
          and by["NVDA"]["decimals"] == 18)
    check("feed captured when present",
          by["AAPL"]["feed"] == "0x6b22a786baa607d76728168703a39ea9c99f2cd0")
    check("`void 0` feed becomes None", by["AMC"]["feed"] is None)
    check("non-quoted address (r.zeroAddress) falls back to zero",
          by["ETH"]["address"] == S.ZERO_ADDRESS)
    check("ETF kind preserved", by["SPY"]["kind"] == "etf")
    check("stable kind preserved and decimals honoured",
          by["USDG"]["kind"] == "stable" and by["USDG"]["decimals"] == 6)

    lev = S.parse_leverage_tokens(make_chunk())
    check("leverage token parsed",
          len(lev) == 1 and lev[0]["symbol"] == "NVDAx3L"
          and lev[0]["extra"]["underlying"] == "NVDA")

    # The minifier renames the helper on every build; the parser must not care.
    check("minifier-renamed helper still parses",
          len(S.parse_numeraires(make_chunk().replace("s(", "q7$("))) == 11)

    check("chunk with a stray ticker string is rejected",
          not S.looks_like_config_chunk(
              S.parse_numeraires('let o=["AAPL","NVDA","TSLA","MSFT"];')))
    check("real chunk is accepted", S.looks_like_config_chunk(rows))
    check("empty chunk yields nothing", S.parse_numeraires("") == [])


def test_fingerprint() -> None:
    print("\n▶ build fingerprint")
    a = S.chunk_urls_from_html(make_html(["aaa111", "bbb222"]))
    b = S.chunk_urls_from_html(make_html(["bbb222", "aaa111"]))
    c = S.chunk_urls_from_html(make_html(["aaa111", "ccc333"]))
    check("urls absolutised", a[0].startswith("https://app.long.xyz/_next/static/chunks/"))
    check("order does not matter", S.chunk_fingerprint(a) == S.chunk_fingerprint(b))
    check("a changed chunk hash changes the fingerprint",
          S.chunk_fingerprint(a) != S.chunk_fingerprint(c))
    check("duplicates collapse", len(S.chunk_urls_from_html(make_html(["x1", "x1"]))) == 1)


def test_log_decoder() -> None:
    print("\n▶ Deployed event decoder")
    log = {
        "address": S.RH_STOCK_FACTORY,
        "topics": [S.TOPIC_DEPLOYED,
                   "0x00000000000000000000000000000000c2425be3658540dd8e2424cbf3c5c649"],
        "data": encode_deployed("0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9",
                                "Apple • Robinhood Token", "AAPL"),
        "blockNumber": "0x14b3f9",
        "transactionHash": "0x5ddd494c3217a5e3ef5b8f32471669c4d09d544354d790e744cd9c39ded5901b",
    }
    row = S.decode_deployed_log(log)
    check("decodes", row is not None)
    check("address", row["address"] == "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9")
    check("symbol", row["symbol"] == "AAPL")
    check("name stripped of the Robinhood suffix", row["name"] == "Apple")
    check("raw name kept", row["raw_name"] == "Apple • Robinhood Token")
    check("block number decoded from hex", row["block_number"] == 0x14b3f9)
    check("wrong topic is ignored",
          S.decode_deployed_log({"topics": ["0xdeadbeef"], "data": "0x"}) is None)
    check("truncated data does not raise",
          S.decode_deployed_log({"topics": [S.TOPIC_DEPLOYED], "data": "0x1234"}) is None)

    def encode_string(text: str) -> str:
        raw = text.encode().hex()
        return ("0x"
                + format(32, "x").rjust(64, "0")
                + format(len(text.encode()), "x").rjust(64, "0")
                + raw.ljust(((len(raw) + 63) // 64) * 64, "0"))

    desc = S.decode_description(encode_string("AAPL / USD"))
    check("description decoded", desc == "AAPL / USD", f"got {desc!r}")
    check("empty description is None", S.decode_description("0x") is None)
    check("symbol from description", S.symbol_from_description("AAPL / USD") == "AAPL")
    check("garbage description rejected", S.symbol_from_description("not a ticker!!") is None)


# ── 2. store ──────────────────────────────────────────────────────────────────
def test_store() -> None:
    print("\n▶ store")
    check("alert claimed once", store.claim_alert("k1", "src", "s"))
    check("same key refused", not store.claim_alert("k1", "src", "s"))
    check("different key allowed", store.claim_alert("k2", "src", "s"))

    check("new stock inserted", store.add_rh_stock({"address": "0xAbC", "symbol": "ZZZ"}))
    check("duplicate stock refused", not store.add_rh_stock({"address": "0xabc", "symbol": "ZZZ"}))
    check("lookup is case-insensitive", store.has_rh_stock("0xABC"))

    check("numeraire insert reports new",
          store.upsert_numeraire(4663, {"address": "0xDeF", "symbol": "QQQ", "kind": "etf",
                                        "name": "Invesco", "decimals": 18}))
    check("numeraire re-upsert is not new",
          not store.upsert_numeraire(4663, {"address": "0xdef", "symbol": "QQQ", "kind": "etf",
                                            "name": "Invesco", "decimals": 18}))
    check("known set reflects it", "0xdef" in store.known_numeraires(4663))
    store.mark_numeraire_removed(4663, "0xdef")
    check("removal hides it", "0xdef" not in store.known_numeraires(4663))

    check("unlisted stocks join correctly",
          any(r["address"] == "0xabc" for r in store.unlisted_rh_stocks(4663)))

    check("sighting recorded once", store.record_sighting("stock:ZZZ", "robinhood_factory"))
    check("earliest sighting wins", not store.record_sighting("stock:ZZZ", "robinhood_factory"))
    store.record_sighting("stock:ZZZ", "long_frontend")
    rep = store.latency_report()
    entry = next((r for r in rep if r["subject"] == "stock:ZZZ"), None)
    check("latency report pairs the sources",
          entry is not None and len(entry["sources"]) == 2
          and entry["first_source"] == "robinhood_factory")


# ── 3. end-to-end: seed silently, then detect exactly one new listing ─────────
async def test_end_to_end() -> None:
    print("\n▶ end-to-end simulation (seed silent → one new ticker → one alert)")
    from src import long_watcher as W

    notifier = W.CollectingNotifier()
    watcher = W.LongWatcher(notifier=notifier)

    # A fake frontend: build 1 has 11 assets, build 2 adds PYPL.
    class FakeFrontend(S.LongFrontendWatcher):
        def __init__(self):
            super().__init__(http=None)
            self.state = 1

        async def _page_html(self, page):
            chunks = ["cfg0001", "vendor01"] if self.state == 1 else ["cfg0002", "vendor01"]
            return make_html(chunks)

        async def _chunk_text(self, url):
            if "cfg0001" in url:
                return make_chunk()
            if "cfg0002" in url:
                return make_chunk(NEW_TICKER_ENTRY)
            return "console.log('vendor');"

    watcher.frontend = FakeFrontend()
    store.mark_seeded("factory")
    store.mark_seeded("indexer")
    store.mark_seeded("feeds")

    await watcher.seed()
    check("seeding sends nothing", len(notifier.sent) == 0, f"sent {len(notifier.sent)}")
    check("seeding recorded the baseline", len(store.known_numeraires(4663)) >= 11)

    changed = await watcher.frontend.build_changed()
    check("unchanged build reports no change", changed is None, f"got {changed}")

    # Long ships a build that adds PYPL.
    watcher.frontend.state = 2
    fp = await watcher.frontend.build_changed()
    check("new build detected by fingerprint", fp is not None)

    snap = await watcher.frontend.snapshot()
    added = await watcher.on_numeraires(snap)
    check("exactly one asset detected as new", len(added) == 1, f"got {[a['symbol'] for a in added]}")
    check("it is PYPL", added and added[0]["symbol"] == "PYPL")
    check("exactly one alert fired", len(notifier.sent) == 1, f"sent {len(notifier.sent)}")

    a = notifier.sent[0]
    check("alert names the ticker", a["ticker"] == "PYPL")
    check("alert names the company", a["company"] == "PayPal")
    check("alert carries the token address",
          a["address"] == "0x1234567890abcdef1234567890abcdef12345678")
    check("alert names its source", a["source"] == "long_frontend")
    check("alert carries a CEST timestamp with milliseconds",
          "CEST" in a["detected_at_cest"] or "+02" in a["detected_at_cest"])
    check("alert states confidence", "confidence" in a and a["confidence"])
    check("alert cites the build as evidence", "build" in a["evidence"])
    embed = W.build_embed(a)
    check("embed renders", embed["title"].startswith("🚀") and len(embed["fields"]) > 3)
    check("embed links to the explorer",
          any("Explorer" in (f["value"] or "") for f in embed["fields"]))

    # The same build again, and a restart, must both stay silent.
    snap2 = await watcher.frontend.snapshot()
    await watcher.on_numeraires(snap2)
    check("re-reading the same build is silent", len(notifier.sent) == 1)

    watcher2 = W.LongWatcher(notifier=notifier)
    watcher2.frontend = watcher.frontend
    await watcher2.on_numeraires(await watcher2.frontend.snapshot())
    check("a restart does not re-alert", len(notifier.sent) == 1)

    # A mass disappearance is refused rather than believed.
    class BrokenFrontend(FakeFrontend):
        async def _chunk_text(self, url):
            if "cfg" in url:
                return make_chunk()[:400]      # truncated build → most assets gone
            return "console.log('vendor');"

    watcher.frontend = BrokenFrontend()
    try:
        broken = await watcher.frontend.snapshot()
        res = await watcher.on_numeraires(broken)
        check("a truncated build does not mass-delist", res == [])
    except RuntimeError:
        check("a truncated build is rejected outright", True)
    check("still exactly one alert after the broken build", len(notifier.sent) == 1)


async def test_factory_path() -> None:
    print("\n▶ factory event → alert")
    from src import long_watcher as W
    notifier = W.CollectingNotifier()
    watcher = W.LongWatcher(notifier=notifier)

    log = {
        "topics": [S.TOPIC_DEPLOYED, "0x" + "11" * 32],
        "data": encode_deployed("0x9999999999999999999999999999999999999999",
                                "Palo Alto Networks • Robinhood Token", "PANW"),
        "blockNumber": "0x2000",
        "transactionHash": "0x" + "ab" * 32,
    }
    row = S.decode_deployed_log(log)
    row["source"] = "robinhood_factory:ws"
    row["chain_ts"] = None

    await watcher.on_stock_deployed(row)
    check("new stock token alerts once", len(notifier.sent) == 1)
    await watcher.on_stock_deployed(row)
    check("the same deploy never alerts twice", len(notifier.sent) == 1)
    check("alert says it is not on Long yet",
          "Not yet offered by Long" in notifier.sent[0]["description"])
    check("high confidence for a decoded chain event",
          notifier.sent[0]["confidence"].startswith("high"))

    # The indexer backstop: first coin ever against an unseen numeraire.
    await watcher.on_token({
        "token_numeraire_address": "0x9999999999999999999999999999999999999999",
        "token_address": "0x" + "cd" * 20, "token_symbol": "MOON",
        "token_name": "Moon Coin",
        "token_creation_timestamp": "2026-09-04T04:59:33+00:00",
    })
    check("first coin against a numeraire alerts", len(notifier.sent) == 2)
    await watcher.on_token({
        "token_numeraire_address": "0x9999999999999999999999999999999999999999",
        "token_address": "0x" + "ef" * 20, "token_symbol": "MOON2",
        "token_name": "Moon Coin 2",
        "token_creation_timestamp": "2026-09-04T05:00:00+00:00",
    })
    check("the second coin against it is silent", len(notifier.sent) == 2)
    check("the first-coin alert resolved the ticker from the chain registry",
          notifier.sent[1]["ticker"] == "PANW")

    check("both sources are recorded against one subject",
          len(store.sightings("stock:PANW")) >= 2)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="longtest_")
    store.set_db_path(os.path.join(tmp, "long.db"))
    print(f"Long watcher — offline checks (db: {tmp})")
    test_parsers()
    test_fingerprint()
    test_log_decoder()
    test_store()
    asyncio.run(test_end_to_end())
    asyncio.run(test_factory_path())
    print(f"\n{'='*54}\n  {PASS} passed, {FAIL} failed\n{'='*54}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
