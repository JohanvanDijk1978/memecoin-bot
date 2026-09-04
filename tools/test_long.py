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
import json
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


# ── Pons and o1 fixtures, shaped like their real minified bundles ────────────
PONS_STOCKS = [
    ("NVDA", "NVIDIA", "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC"),
    ("SPCX", "SpaceX Class A", "0x4a0E65A3EcceC6dBe60AE065F2e7bb85Fae35eEa"),
    ("GOOGL", "Alphabet Class A", "0x2e0847E8910a9732eB3fb1bb4b70a580ADAD4FE3"),
    ("TSLA", "Tesla", "0x322F0929c4625eD5bAd873c95208D54E1c003b2d"),
    ("GME", "GameStop", "0x1b0E319c6A659F002271B69dB8A7df2F911c153E"),
    ("AAPL", "Apple", "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9"),
    ("SPY", "SPDR S&P 500", "0x117cc2133c37B721F49dE2A7a74833232B3B4C0C"),
    ("MSFT", "Microsoft", "0xe93237C50D904957Cf27E7B1133b510C669c2e74"),
    ("META", "Meta", "0xc0D6457C16Cc70d6790Dd43521C899C87ce02f35"),
    ("COIN", "Coinbase", "0x6330D8C3178a418788dF01a47479c0ce7CCF450b"),
    ("MU", "Micron", "0xfF080c8ce2E5feadaCa0Da81314Ae59D232d4afD"),
    ("PLTR", "Palantir", "0x894E1EC2D74FFE5AEF8Dc8A9e84686acCB964F2A"),
    ("RIVN", "Rivian", "0x1111111111111111111111111111111111111111"),
    ("PFE", "Pfizer", "0x2222222222222222222222222222222222222222"),
    ("JNJ", "Johnson & Johnson", "0x3333333333333333333333333333333333333333"),
    ("MRVL", "Marvell", "0x4444444444444444444444444444444444444444"),
]


def make_pons_chunk(extra: str = "") -> str:
    entries = ",".join(
        f'{{address:"{a}",symbol:"{s}",name:"{n}",decimals:18,isNative:!1,assetClass:"equity"}}'
        for s, n, a in PONS_STOCKS)
    return (
        'let r={address:i.zeroAddress,symbol:"ETH",name:"Ether",decimals:18,'
        'isNative:!0,assetClass:"native"},o=new Map([r,'
        '{address:s.ROBINHOOD_WETH_ADDRESS,symbol:"WETH",name:"Wrapped Ether",'
        'decimals:18,isNative:!1,assetClass:"native"},'
        '{address:"0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",symbol:"USDG",'
        'name:"Global Dollar",decimals:6,isNative:!1,assetClass:"stablecoin"},'
        + entries + extra + ']);'
    )


def make_o1_chunk(extra: str = "") -> str:
    # 48 distinct entries so the count clears o1's min_assets=40 guard; the
    # addresses must be unique or they collapse into one another.
    rows = [(f"{s}{i}" if i else s, n, a if not i else f"0x{i:02x}" + a[4:])
            for i in range(3) for s, n, a in PONS_STOCKS]
    stocks = ",".join(
        f'E({{symbol:`{s}`,name:`{n}`,address:`{a.lower()}`,decimals:18}})'
        for s, n, a in rows)
    return (
        '{symbol:`ETH`,label:`ETH`,name:`Ether`,address:d,decimals:18,'
        'icon:`/icons/crypto/eth.png`,priceAddress:a,priceNetworkId:t,'
        'creationRoute:c.STANDARD,category:T.CRYPTO_NATIVE,'
        'suiteId:`robinhood-mainnet-launchpad-v4-minimal`},'
        '{symbol:`cbBTC`,label:`cbBTC`,name:`Coinbase Wrapped BTC`,'
        'address:`0x5555555555555555555555555555555555555555`,decimals:8,'
        'priceNetworkId:8453,category:T.CRYPTO_MAJOR},'
        'k=[' + stocks + extra + ']'
    )


def test_venue_parsers() -> None:
    print("\n▶ Pons and o1 parsers")
    pons = S.parse_assets_pons(make_pons_chunk())
    by = {r["symbol"]: r for r in pons}
    check("pons parses every entry", len(pons) == len(PONS_STOCKS) + 2,
          f"got {len(pons)}")
    check("pons drops the identifier-address WETH rather than colliding on ETH",
          "WETH" not in {r["symbol"] for r in pons})
    check("pons maps assetClass=equity onto kind=stock", by["NVDA"]["kind"] == "stock")
    check("pons maps stablecoin", by["USDG"]["kind"] == "stable" and by["USDG"]["decimals"] == 6)
    check("pons identifier address falls back to zero", by["ETH"]["address"] == S.ZERO_ADDRESS)
    check("pons lowercases addresses",
          by["NVDA"]["address"] == "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec")
    check("pons chunk passes the config-chunk guard",
          S.looks_like_config_chunk(pons, S.VENUES["pons"].min_assets))

    o1 = S.parse_assets_o1(make_o1_chunk())
    o1by = {r["symbol"]: r for r in o1}
    check("o1 parses the stock entries", len(o1) >= 40, f"got {len(o1)}")
    check("o1 treats a category-less entry as a stock", o1by["NVDA"]["kind"] == "stock")
    check("o1 reads an explicit crypto category", o1by["cbBTC"]["kind"] == "crypto")
    check("o1 keeps the chain id when present",
          o1by["cbBTC"]["extra"]["chain_id"] == 8453)
    check("o1 skips entries whose address is an identifier (ETH/USDG)",
          "ETH" not in o1by)
    check("o1 chunk passes its own guard",
          S.looks_like_config_chunk(o1, S.VENUES["o1"].min_assets))

    check("a Long chunk is not mistaken for a Pons one",
          S.parse_assets_pons(make_chunk()) == [])
    check("a Pons chunk is not mistaken for a Long one",
          not S.looks_like_config_chunk(S.parse_numeraires(make_pons_chunk()),
                                        S.VENUES["long"].min_assets))


def test_venue_registry() -> None:
    print("\n▶ venue registry")
    check("three venues registered", set(S.VENUES) == {"long", "pons", "o1"})
    check("each venue has a parser and a chunk pattern",
          all(v.parser and v.chunk_re for v in S.VENUES.values()))
    check("all three settle on Robinhood Chain",
          all(v.chain_id == S.ROBINHOOD_CHAIN_ID for v in S.VENUES.values()))

    nx = '<script src="/_next/static/chunks/abc123.js"></script>'
    pons_html = '<script src="/_next/static/immutable/chunks/1inl5h6g6dsdt.js"></script>'
    vite = ('<link rel="modulepreload" href="/assets/pairedAsset-DftLG667.js">'
            '<script src="/assets/index-BOhafeTy.js"></script>')
    check("long pattern matches Next.js chunks",
          len(S.chunk_urls_from_html(nx, "https://x", S.VENUES["long"].chunk_re)) == 1)
    check("pons pattern matches the immutable path",
          len(S.chunk_urls_from_html(pons_html, "https://x", S.VENUES["pons"].chunk_re)) == 1)
    check("long pattern does NOT match the immutable path",
          len(S.chunk_urls_from_html(pons_html, "https://x", S.VENUES["long"].chunk_re)) == 0)
    check("o1 pattern matches Vite assets incl. modulepreload",
          len(S.chunk_urls_from_html(vite, "https://x", S.VENUES["o1"].chunk_re)) == 2)

    import os as _os
    _os.environ["LONG_VENUES"] = "long,o1"
    check("LONG_VENUES selects a subset",
          [v.id for v in S.enabled_venues()] == ["long", "o1"])
    _os.environ.pop("LONG_VENUES")


def test_pons_feed_normalise() -> None:
    print("\n▶ pons launch feed")
    item = {
        "version": "v2", "token": "0xAADB7a2dB2A3f59113188eF26Fd7B245964aaFA2",
        "symbol": "WICK", "name": "LmfaoWick", "logo": "",
        "transactionHash": "0xc762", "blockNumber": 54054117,
        "launchedAt": "2026-09-04T06:32:26.000Z",
        "quoteAsset": {"address": "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC",
                       "symbol": "NVDA", "name": "NVIDIA", "decimals": 18,
                       "isNative": False, "assetClass": "equity"},
    }
    n = S.PonsLaunchWatcher.normalise(item)
    check("venue tagged", n["venue"] == "pons")
    check("token lowercased", n["token_address"].startswith("0xaadb"))
    check("numeraire pulled from quoteAsset",
          n["token_numeraire_address"] == "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec")
    check("numeraire symbol carried through", n["numeraire_symbol"] == "NVDA")
    check("assetClass mapped to our kind", n["numeraire_kind"] == "stock")
    check("timestamp preserved", n["token_creation_timestamp"].startswith("2026-09-04"))
    check("empty logo becomes None", n["token_image_public_url"] is None)


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

    watcher.frontends = {"long": FakeFrontend()}
    watcher.frontend = watcher.frontends["long"]
    store.mark_seeded("factory")
    store.mark_seeded("indexer")
    store.mark_seeded("feeds")
    store.mark_seeded("pons_feed")

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

    watcher.frontends["long"] = BrokenFrontend()
    watcher.frontend = watcher.frontends["long"]
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
    check("alert says no venue offers it yet",
          "Not yet offered by any venue we watch" in notifier.sent[0]["description"])
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


def test_block_classifier() -> None:
    print("\n▶ 403 classifier")
    cf = S.describe_block(
        "https://app.long.xyz/", 403,
        "<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title>",
        {"cf-ray": "a35a…-AMS", "server": "cloudflare", "content-type": "text/html"})
    check("names Cloudflare's WAF", "Cloudflare's WAF" in cf)
    check("prescribes curl_cffi", "curl_cffi" in cf)
    check("keeps the cf-ray for the record", "cf-ray=" in cf)

    origin = S.describe_block(
        "https://api.long.xyz/v1/assets", 403,
        '{"message":"Forbidden resource","statusCode":403}',
        {"content-type": "application/json", "server": "cloudflare"})
    check("an app-level refusal is NOT called a WAF block",
          "Cloudflare's WAF" not in origin and "origin refused" in origin)


async def test_degraded_start() -> None:
    """The failure that actually happened on the VPS: app.long.xyz 403s. The
    watcher must keep the other three detectors alive rather than going dark."""
    print("\n▶ degraded start (frontend blocked)")
    from src import long_watcher as W

    # A fresh database: the earlier tests already seeded a frontend, and this
    # scenario is specifically "first ever start, and the source is blocked".
    store.set_db_path(os.path.join(tempfile.mkdtemp(prefix="longblocked_"), "long.db"))

    notifier = W.CollectingNotifier()
    watcher = W.LongWatcher(notifier=notifier)

    class BlockedFrontend(S.LongFrontendWatcher):
        def __init__(self):
            super().__init__(http=None)

        async def _page_html(self, page):
            raise RuntimeError(S.describe_block(
                "https://app.long.xyz/create", 403, "Just a moment...",
                {"cf-ray": "x", "content-type": "text/html"}))

    watcher.frontends = {"long": BlockedFrontend()}
    watcher.frontend = watcher.frontends["long"]
    store.mark_seeded("factory")
    store.mark_seeded("indexer")
    store.mark_seeded("feeds")
    store.mark_seeded("pons_feed")

    res = await watcher.seed()
    check("seed reports the frontend as degraded", res.get("degraded") == ["long"],
          f"got {res.get('degraded')}")
    check("seeding still sent nothing", len(notifier.sent) == 0)
    known = store.known_numeraires(4663)
    check("baseline filled the numeraire set", len(known) >= 55, f"got {len(known)}")
    check("baseline carries real tickers",
          any(r["symbol"] == "NVDA" for r in known.values()))
    check("frontend is NOT marked seeded, so the loop will retry",
          not store.is_seeded("frontend"))

    # The on-chain detector must still work, and must know the stock is on Long.
    log = {
        "topics": [S.TOPIC_DEPLOYED, "0x" + "22" * 32],
        "data": encode_deployed("0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC",
                                "NVIDIA • Robinhood Token", "NVDA"),
        "blockNumber": "0x3000", "transactionHash": "0x" + "cc" * 32,
    }
    row = S.decode_deployed_log(log)
    row["source"] = "robinhood_factory:ws"
    row["chain_ts"] = None
    await watcher.on_stock_deployed(row)
    check("the factory detector still alerts while the frontend is blocked",
          len(notifier.sent) == 1)
    check("and it names the venues that already offer it, thanks to the baseline",
          "Already offered by: Long.xyz" in notifier.sent[0]["description"],
          notifier.sent[0]["description"][:160])

    # Recovery against a stale baseline: everything the baseline had, plus nine
    # assets listed while we were blind. Nine is more than the cap, so it must be
    # absorbed with a log line rather than fired as nine "Long now supports X".
    with open(os.path.join("tools", "long_baseline.json"), encoding="utf-8") as fh:
        base = json.load(fh)
    live = [{"symbol": r[0], "name": r[0], "kind": r[1], "address": r[2],
             "decimals": r[3], "feed": (r[4] or None)} for r in base["assets"]]
    live += [{"symbol": f"NEW{i}", "name": f"New {i}", "kind": "stock",
              "address": "0x" + f"{i:040x}", "decimals": 18, "feed": None}
             for i in range(1, 10)]      # from 1: 0x000…0 is ETH's own address
    before = len(notifier.sent)
    absorbed = await watcher.on_numeraires(
        {"fingerprint": "recovered", "chunk_url": "x", "numeraires": live},
        max_new_alerts=5)
    check("a 9-asset gap is absorbed, not fired",
          len(absorbed) == 9 and len(notifier.sent) == before,
          f"absorbed={len(absorbed)} sent={len(notifier.sent) - before}")

    # ...but a realistic recovery — one asset listed while we were blind — IS
    # the alert we actually want.
    live2 = live[:-9] + [{"symbol": "PYPL", "name": "PayPal", "kind": "stock",
                          "address": "0x" + "7a" * 20, "decimals": 18, "feed": None}]
    added2 = await watcher.on_numeraires(
        {"fingerprint": "recovered2", "chunk_url": "x", "numeraires": live2},
        max_new_alerts=5)
    check("one asset missed while blind still alerts on recovery",
          len(added2) == 1 and len(notifier.sent) == before + 1,
          f"added={len(added2)} sent={len(notifier.sent) - before}")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="longtest_")
    store.set_db_path(os.path.join(tmp, "long.db"))
    print(f"Long watcher — offline checks (db: {tmp})")
    test_parsers()
    test_fingerprint()
    test_log_decoder()
    test_store()
    test_block_classifier()
    test_venue_parsers()
    test_venue_registry()
    test_pons_feed_normalise()
    asyncio.run(test_end_to_end())
    asyncio.run(test_factory_path())
    asyncio.run(test_degraded_start())
    print(f"\n{'='*54}\n  {PASS} passed, {FAIL} failed\n{'='*54}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
