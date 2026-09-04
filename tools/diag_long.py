"""
tools/diag_long.py
──────────────────
Live diagnostics for the Long.xyz watcher. This is the half `tools/test_long.py`
cannot do: it talks to the real sources. Run it on the VPS (or any machine with
crypto-API egress) — a Cowork session cannot reach any of these hosts.

    python3 tools/diag_long.py                # everything, read-only
    python3 tools/diag_long.py frontend       # just Long's pairable-asset array
    python3 tools/diag_long.py chain          # just Robinhood Chain / the factory
    python3 tools/diag_long.py graphql        # just Long's indexer (+ websocket probe)
    python3 tools/diag_long.py gap            # on-chain stocks Long does NOT list
    python3 tools/diag_long.py latency        # what our own detections have shown
    python3 tools/diag_long.py ping           # post a test alert to the webhook
    python3 tools/diag_long.py simulate PYPL  # fire one fake listing alert end-to-end

Nothing here writes to the watcher's state except `simulate`, which writes into
a throwaway database so it can never make the real watcher miss a real listing.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import long_sources as S      # noqa: E402
from src import long_store as store    # noqa: E402
from src import long_watcher as W      # noqa: E402


def hr(title: str) -> None:
    print(f"\n{'─' * 72}\n{title}\n{'─' * 72}")


def env_report() -> None:
    hr("configuration")
    rows = [
        ("LONG_APP_BASE", S.LONG_APP_BASE),
        ("LONG_GRAPHQL_URL", S.LONG_GRAPHQL_URL),
        ("stock factory", S.RH_STOCK_FACTORY),
        ("Deployed topic0", S.TOPIC_DEPLOYED),
        ("feed deployer", S.RH_FEED_DEPLOYER),
        ("ROBINHOOD_RPC", _mask(S.ROBINHOOD_RPC)),
        ("ROBINHOOD_WSS", _mask(S.ROBINHOOD_WSS)),
        ("explorer", S.ROBINHOOD_EXPLORER_API),
        ("LONG_DISCORD_WEBHOOK", _mask(W.LONG_DISCORD_WEBHOOK)),
        ("db", os.getenv("LONG_DB_PATH") or "data/long.db"),
        ("now", W.cest()),
    ]
    for k, v in rows:
        print(f"  {k:<22} {v or '⟨unset⟩'}")


def _mask(url: str) -> str:
    if not url:
        return ""
    if len(url) < 28:
        return url
    return url[:24] + "…" + url[-6:]


async def diag_frontend(http: S.Http) -> None:
    hr("Long frontend — the authoritative pairable-asset array")
    fw = S.LongFrontendWatcher(http)
    t0 = time.time()
    snap = await fw.snapshot()
    ms = int((time.time() - t0) * 1000)
    rows = snap["numeraires"]
    print(f"  build fingerprint : {snap['fingerprint']}")
    print(f"  config chunk      : {snap['chunk_url'].rsplit('/', 1)[-1]}")
    print(f"  chunks on page    : {snap['chunk_count']}")
    print(f"  resolved in       : {ms} ms")
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print(f"  assets            : {len(rows)}  ({', '.join(f'{k}={v}' for k, v in sorted(kinds.items()))})")
    print(f"  with a price feed : {sum(1 for r in rows if r['feed'])}")
    print("\n  symbol   kind     decimals  address                                     feed")
    for r in sorted(rows, key=lambda x: (x["kind"], x["symbol"])):
        print(f"  {r['symbol']:<8} {r['kind']:<8} {r['decimals']:<9} {r['address']}  "
              f"{'yes' if r['feed'] else '—'}   {r['name'][:28]}")

    t0 = time.time()
    changed = await fw.build_changed()
    print(f"\n  hot-path poll (1 request): {int((time.time() - t0) * 1000)} ms, "
          f"changed={changed}")
    print("  ↑ this is what runs every "
          f"{W.FRONTEND_POLL_SECONDS:.0f}s; the chunk fetch above only happens on a deploy")


async def diag_graphql(http: S.Http) -> None:
    hr("Long indexer — https://api.long.xyz/v1/graphql")
    iw = S.LongIndexerWatcher(http, _noop)
    meta = await iw.gql("{ chain_metadata{chain_id block_height latest_processed_block "
                        "is_hyper_sync} }")
    for c in meta.get("chain_metadata", []):
        lag = c["block_height"] - c["latest_processed_block"]
        print(f"  chain {c['chain_id']:<6} head {c['block_height']:<12} "
              f"indexed {c['latest_processed_block']:<12} lag {lag} blocks")

    data = await iw.gql(
        "{ Token(where:{chain_id:{_eq:%d}}, order_by:{token_creation_timestamp:desc}, limit:5)"
        "{ token_symbol token_name token_address token_numeraire_address "
        "token_creation_timestamp } }" % S.ROBINHOOD_CHAIN_ID)
    print("\n  newest coins launched on Long:")
    for t in data.get("Token", []):
        print(f"    {t['token_creation_timestamp']}  {t['token_symbol']:<12} "
              f"→ numeraire {t['token_numeraire_address']}")

    used = await iw.used_numeraires()
    print(f"\n  distinct numeraires already used by a coin: {len(used)}")

    print("\n  websocket subscription probe:")
    res = await iw.try_websocket()
    if res.get("ok"):
        print(f"    ✅ answers on `{res['protocol']}` — a future session can switch "
              f"Token_stream to push")
        print(f"       first frame: {res['first'][:120]}")
    else:
        print(f"    ❌ {res.get('error')}")
        print("       (expected: it closed 1006 on every subprotocol on 2026-09-04. "
              "Polling is the supported path.)")


async def diag_chain(http: S.Http) -> None:
    hr("Robinhood Chain — the stock-token factory")
    if not S.ROBINHOOD_RPC:
        print("  ⟨ROBINHOOD_RPC unset — deploy fomo/.env to this box, see "
              "reference_vps_setup⟩")
        return
    rpc = S.JsonRpc(http, S.ROBINHOOD_RPC)
    head = await rpc.block_number()
    print(f"  head block: {head}")

    found: list[dict] = []
    window = 5_000_000
    cur = 0
    while cur <= head and len(found) < 400:
        end = min(cur + window, head)
        try:
            logs = await rpc.get_logs(S.RH_STOCK_FACTORY, [S.TOPIC_DEPLOYED], cur, end)
        except Exception as e:
            print(f"  eth_getLogs {cur}-{end} failed: {e}")
            break
        for lg in logs:
            row = S.decode_deployed_log(lg)
            if row:
                found.append(row)
        cur = end + 1
    print(f"  Deployed events found: {len(found)}")
    print("\n  newest 12 tokenised stocks:")
    for r in sorted(found, key=lambda x: x["block_number"] or 0, reverse=True)[:12]:
        print(f"    block {r['block_number']:<12} {r['symbol']:<10} {r['address']}  {r['name']}")

    if S.ROBINHOOD_WSS:
        print("\n  websocket subscription probe:")
        try:
            async with http.session.ws_connect(S.ROBINHOOD_WSS, heartbeat=30) as ws:
                await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                                    "params": ["logs", {"address": S.RH_STOCK_FACTORY,
                                                        "topics": [S.TOPIC_DEPLOYED]}]})
                msg = await asyncio.wait_for(ws.receive(), timeout=10)
                print(f"    ✅ subscribed: {str(msg.data)[:160]}")
                print("    (a Deployed event is rare — no message here is normal)")
        except Exception as e:
            print(f"    ❌ {type(e).__name__}: {e}")
    else:
        print("\n  ⟨ROBINHOOD_WSS unset — the factory watcher would run sweep-only⟩")


async def diag_gap(http: S.Http) -> None:
    hr("the gap — tokenised stocks that exist on-chain but Long does not offer")
    fw = S.LongFrontendWatcher(http)
    listed = {r["address"]: r for r in (await fw.snapshot())["numeraires"]}

    rpc = S.JsonRpc(http, S.ROBINHOOD_RPC)
    if not S.ROBINHOOD_RPC:
        print("  ⟨needs ROBINHOOD_RPC⟩")
        return
    head = await rpc.block_number()
    onchain: dict[str, dict] = {}
    cur, window = 0, 5_000_000
    while cur <= head:
        end = min(cur + window, head)
        try:
            for lg in await rpc.get_logs(S.RH_STOCK_FACTORY, [S.TOPIC_DEPLOYED], cur, end):
                row = S.decode_deployed_log(lg)
                if row:
                    onchain[row["address"]] = row
        except Exception as e:
            print(f"  window {cur}-{end}: {e}")
            break
        cur = end + 1

    missing = [r for a, r in onchain.items() if a not in listed]
    extra = [r for a, r in listed.items() if a not in onchain]
    print(f"  on-chain stock tokens : {len(onchain)}")
    print(f"  offered by Long       : {len(listed)}")
    print(f"  eligible but unlisted : {len(missing)}   ← the pool Long picks its next listing from")
    print(f"  on Long, not from the factory: {len(extra)} "
          f"({', '.join(r['symbol'] for r in extra[:10])})")
    print("\n  newest 25 unlisted, most recently deployed first:")
    for r in sorted(missing, key=lambda x: x["block_number"] or 0, reverse=True)[:25]:
        print(f"    {r['symbol']:<10} {r['address']}  {r['name']}")


def diag_latency() -> None:
    hr("latency — which source saw each stock first")
    rep = store.latency_report()
    if not rep:
        print("  no sightings recorded yet. This table fills as the watcher runs;")
        print("  after a couple of real listings it answers the question the whole")
        print("  build was for: is the frontend or the chain consistently first?")
        return
    for r in rep:
        print(f"\n  {r['subject']}   first via {r['first_source']} at {W.cest(r['first_seen'])}")
        for s in r["sources"]:
            print(f"    {s['source']:<22} +{s['delta_ms']:>10} ms   {s['detail'][:60]}")


async def diag_ping() -> None:
    hr("webhook test")
    if not W.LONG_DISCORD_WEBHOOK:
        print("  ❌ LONG_DISCORD_WEBHOOK is not set in .env")
        return
    n = W.DiscordWebhookNotifier(W.LONG_DISCORD_WEBHOOK)
    ok = await n.send({
        "title": "✅ Long watcher webhook test",
        "description": "If you can read this, alerts will reach this channel.",
        "ticker": "TEST", "company": "diag_long.py", "kind": "diagnostic",
        "source": "diag", "confidence": "n/a — this is a test",
        "detected_at_cest": W.cest(), "color": 0x7289DA,
    })
    print("  ✅ delivered" if ok else "  ❌ failed — see the log line above")


async def diag_simulate(ticker: str) -> None:
    hr(f"simulation — pretend Long just listed {ticker}")
    tmp = tempfile.mkdtemp(prefix="longsim_")
    store.set_db_path(os.path.join(tmp, "long.db"))
    print(f"  throwaway db: {tmp}  (the real watcher's state is untouched)")

    notifier = W.CollectingNotifier()
    live = W.LONG_DISCORD_WEBHOOK and "--send" in sys.argv
    watcher = W.LongWatcher(notifier=notifier)

    async with S.Http() as http:
        fw = S.LongFrontendWatcher(http)
        watcher.frontend = fw
        store.mark_seeded("factory")
        store.mark_seeded("indexer")
        store.mark_seeded("feeds")

        snap = await fw.snapshot()
        await watcher.on_numeraires(snap, seeding=True)
        print(f"  seeded {len(snap['numeraires'])} real assets from build "
              f"{snap['fingerprint']} — {len(notifier.sent)} alerts (must be 0)")

        fake = dict(snap)
        fake["numeraires"] = snap["numeraires"] + [{
            "symbol": ticker.upper(), "name": f"{ticker.upper()} Inc (simulated)",
            "kind": "stock", "address": "0x" + "5i".replace("i", "1") * 20,
            "decimals": 18, "feed": None,
        }]
        fake["fingerprint"] = "simulated-build"
        added = await watcher.on_numeraires(fake)
        print(f"  after the simulated build: {len(added)} new asset(s), "
              f"{len(notifier.sent)} alert(s)")

        again = await watcher.on_numeraires(fake)
        print(f"  same build re-read      : {len(again)} new asset(s), "
              f"{len(notifier.sent)} alert(s) total   ← must still be 1")

        if notifier.sent:
            a = notifier.sent[0]
            print("\n  the alert that would be posted:")
            print(json.dumps(W.build_embed(a), indent=2)[:2200])
            if live:
                ok = await W.DiscordWebhookNotifier(W.LONG_DISCORD_WEBHOOK, http).send(a)
                print(f"\n  posted to Discord: {'✅' if ok else '❌'}")
            elif W.LONG_DISCORD_WEBHOOK:
                print("\n  (add --send to actually post this to the channel)")


async def _noop(_):
    return None


async def main() -> None:
    what = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    env_report()

    if what == "latency":
        diag_latency()
        return
    if what == "ping":
        await diag_ping()
        return
    if what == "simulate":
        await diag_simulate(sys.argv[2] if len(sys.argv) > 2 else "PYPL")
        return

    async with S.Http() as http:
        try:
            if what in ("all", "frontend"):
                await diag_frontend(http)
        except Exception as e:
            print(f"\n  ❌ frontend: {type(e).__name__}: {e}")
        try:
            if what in ("all", "graphql"):
                await diag_graphql(http)
        except Exception as e:
            print(f"\n  ❌ graphql: {type(e).__name__}: {e}")
        try:
            if what in ("all", "chain"):
                await diag_chain(http)
        except Exception as e:
            print(f"\n  ❌ chain: {type(e).__name__}: {e}")
        try:
            if what in ("all", "gap"):
                await diag_gap(http)
        except Exception as e:
            print(f"\n  ❌ gap: {type(e).__name__}: {e}")

    if what == "all":
        diag_latency()


if __name__ == "__main__":
    asyncio.run(main())
