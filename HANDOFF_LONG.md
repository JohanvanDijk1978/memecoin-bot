# Long.xyz new-stock watcher — handoff

Built 2026-09-04. Everything below was verified against the live app that day
unless it says otherwise. Read this before re-deriving anything about Long.

---

## 1. How Long actually decides which stocks it supports

Long (`https://app.long.xyz`) is a Next.js app on Railway behind Cloudflare. It
lets you launch a coin paired with a tokenised real-world stock on **Robinhood
Chain (chain id 4663, ~101 ms blocks)**.

Three data sources sit behind it, and **only one of them holds the supported-stock
list**:

| Source | What it is | Holds the stock list? |
|---|---|---|
| `https://api.long.xyz/v1/graphql` | Hasura over an **Envio HyperIndex** indexer. Introspection **wide open, no auth**. Types: `Asset`, `Token`, `AuctionPool`, `GraduationPool`, `NumerairePrice`, `raw_events`, `chain_metadata`. Indexes chains 4663 and 8453 (Base). | ❌ only what has already been launched |
| `https://api.long.xyz/v1/…` REST | NestJS. Live routes found: `/assets`, `/config`, `/health`, `/market/tokens/{addr}/stats`. Query params are gated by an API key that ships in the bundle: `lxyz_49534dc2febae30294149790a8152f44bf915ebbe0332213` (public, not a secret). | ❌ |
| the frontend JS bundle | a **hardcoded array** compiled into a chunk | ✅ **this is the list** |

The array entry shape, as emitted by the minifier:

```js
s("NVDA","NVIDIA","stock","0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC",18,"0x379EC4f7…9F15")
//  symbol  name    kind    numeraire token on chain 4663           dec   chainlink feed | void 0
```

`kind` ∈ `native | stable | stock | etf` (and `leverage` for a second, differently
shaped array holding `NVDA 3x Long` / `NVDA 5x Long`).

**So "Long added a stock" literally means "Long shipped a frontend build with a
new entry in that array."** There is no API call, no on-chain registry and no
config endpoint that says it first. This was checked: `/v1/assets`, `/v1/config`,
`/numeraires`, `/stocks`, `/market/*` and GraphQL introspection all fail to
contain it.

### Baseline captured at build time
57 pairable assets — 1 native (ETH), 1 stable (USDG), **48 stocks**, 7 ETFs;
30 of them carry a Chainlink price feed. Full snapshot in
`tools/long_baseline.json`. That file is a reference, **not** the seed — the
watcher seeds itself from the live source so it can never start from a stale set.

---

## 2. The earliest signal — and why it is not Long at all

Every Robinhood tokenised stock is a **BeaconProxy** (implementation contract
named `Stock`) deployed by one factory on Robinhood Chain:

```
factory  0x4783C67b63dE2B358Ac5951a7D41F47A38F3C046      (itself an ERC1967Proxy)
event    Deployed(bytes32 indexed uid, address stock, string name, string symbol)
topic0   0xd9b0c6a1c0de228715ad0fa09f3259686ee84f8cc675e03ef7e47a9cdafa76d6
```

One `eth_subscribe("logs", {address: factory, topics: [topic0]})` on the Alchemy
Robinhood websocket **already configured in `fomo/.env`** gives push delivery
with the ticker, company name and token address decoded straight out of the log
data. There is nothing upstream of this short of the mempool, and these are
Robinhood-operated transactions that do not linger there.

**But it answers a different question.** At build time the factory had emitted
**206** `Deployed` events while Long offered **57** assets. The most recent
deploys — `BND` (Vanguard Total Bond Market ETF, 2026-07-28), `FICO` (Fair
Isaac), `INFQ` (Infleqtion) — are **not** on Long. So:

* `Deployed` → *"a tokenised stock now exists"* — earliest, push, but Long may
  never list it, and the last batch was five weeks before this was built.
* frontend array diff → *"you can launch against it now"* — the tradeable moment.

Both are watched. Every alert names its source.

---

## 3. All the signals, ranked

| # | Signal | Mechanism | Latency | Auth | Push? | Confidence | Can precede the UI? |
|---|---|---|---|---|---|---|---|
| 1 | Robinhood factory `Deployed` | `eth_subscribe` logs on `ROBINHOOD_WSS` | **sub-second** after block inclusion | Alchemy key (have it) | ✅ | high — decoded from the chain | yes, by weeks |
| 2 | Chainlink `EACAggregatorProxy` deployed | Blockscout tx list of EOA `0xfE3c266C0F994f9552b70D9107214Fe0ED0d74d8` + `description()` eth_call | minutes (poll 300 s) | none / Alchemy | ❌ | **low, unproven** | plausibly hours–days |
| 3 | Long frontend array diff | 1 cache-busted GET → chunk-hash fingerprint; chunks re-read only on a deploy | **≈ poll interval, 5 s default** | none | ❌ | high — Long's own array | it *is* the UI |
| 4 | First coin ever against a numeraire | GraphQL `Token` poll, 2 s | seconds after the first launch | none | ❌ | high — you cannot launch against an asset Long rejects | no, but it is a backstop if #3's parser breaks |
| 5 | Announcement (X/Discord) | — | minutes–hours late | — | — | — | no |

**Why #3 is the fastest *Long* signal available:** Long ships the list inside its
own bundle, so the earliest machine-observable moment is the deploy that carries
it. There is no server-side config to watch that changes sooner — that was the
main thing the research ruled out.

### Latency floor, and the Cloudflare trap
`app.long.xyz` returns `cache-control: s-maxage=31536000` and
`x-nextjs-stale-time: 300`, so a plain GET can be served up to **five minutes
stale** from Cloudflare's edge — which would eat the whole budget. Every poll
therefore carries a unique `?_lw=<ms>` so the edge cache key misses and the
request reaches the origin. That is one small origin request every 5 s: polite,
and it is the difference between 5 s and 5 min.

---

## 4. What was built

| File | Role |
|---|---|
| `src/long_sources.py` | pure parsers + the four detectors (`LongFrontendWatcher`, `RobinhoodFactoryWatcher`, `LongIndexerWatcher`, `FeedWatcher`) and the shared `Http`/`JsonRpc` transports |
| `src/long_store.py` | SQLite at `data/long.db` (WAL, gitignored): known numeraires, on-chain stock registry, feeds, first-use per numeraire, the dedup ledger and the latency table |
| `src/long_watcher.py` | orchestration, alert formatting, Discord webhook notifier, seeding, supervision |
| `main.py` | `run_long()` added to the `asyncio.gather` |
| `tools/test_long.py` | 70 offline checks, no network |
| `tools/diag_long.py` | live diagnostics, run on the box |
| `tools/long_baseline.json` | the 57-asset snapshot as of 2026-09-04 |

### Design decisions worth not relitigating

* **The hot loop fetches one HTML page, not JS.** Chunk filenames are
  content-hashed, so the sorted chunk list is a fingerprint of the build. Only a
  changed fingerprint triggers chunk downloads, and then only the chunks that are
  *new* in that build — an unchanged chunk keeps its hash, so its bytes cannot
  have changed.
* **The array is matched by shape, never by identifier.** The helper function is
  called `s` today; the minifier renames it every build. `tools/test_long.py`
  asserts the parser still works when it is renamed to `q7$`.
* **A mass disappearance is refused, not believed.** If more than a quarter of
  the known numeraires vanish from a parse, the watcher logs an error and
  changes nothing. A truncated download must never look like a delisting.
* **Seeding is silent and one-shot,** with the flag in the DB. Without it the
  first start would announce 206 stock tokens and 57 numeraires.
* **One dedup key per subject, in SQLite** (`long_store.claim_alert`). A
  reconnect, a reconcile sweep, a restart and two detectors finding the same
  stock all collapse to exactly one message.
* **The websocket has a reconcile sweep** (`eth_getLogs` from the last processed
  block) and it is the ONLY thing covering a reconnect gap. Do not remove it when
  tidying — same lesson as the multi-wallet watcher.
* **Removals are logged, never paged.** A delisting is interesting; it is not
  what makes money, and a parser hiccup would otherwise wake you at 3am.
* **Feed detection is deliberately slow (300 s)** and marked low confidence.
  Blockscout's public tier returned 429 during research.

### Alert contents
ticker · company · kind · token address · paired stock · source · confidence ·
detection time in **CEST with milliseconds** · on-chain time and the detection
lag in ms when known · the exact evidence (build fingerprint + chunk name, or
block + tx) · links to Blockscout and Long. Colour-coded per source.

### Latency instrumentation
`long_latency` stores the FIRST time each source saw each subject (subject =
`stock:<TICKER>`). `python3 tools/diag_long.py latency` prints, per stock, which
source was first and how many ms behind each other source was. After a couple of
real listings this answers the question the whole build was for.

---

## 5. How to run it

1. **Create the Discord channel + webhook**, paste it into `.env`:
   `LONG_DISCORD_WEBHOOK=https://discord.com/api/webhooks/…`
2. **scp the env to the box** (`.env` is not in git):
   `scp C:\Users\mzshu\Downloads\memebot\.env root@209.250.245.16:/root/memecoin-bot-new/.env`
   `fomo/.env` is already there and supplies `ROBINHOOD_RPC` / `ROBINHOOD_WSS`.
3. **Push** — the webhook auto-deploys and restarts memebot. The watcher is a
   task inside `main.py`, so `systemctl restart memebot` is all it needs.
4. **Confirm on the box** it actually deployed — a clean `deploy.log` proves
   nothing (see `reference_vps_setup`):
   `cd /root/memecoin-bot-new && git log --oneline -1 && grep -c run_long main.py`

Expected first-run log lines:

```
long: seeded 57 pairable assets from build <fingerprint>
long: seeded 206 Robinhood stock tokens from the factory
long: subscribed to Robinhood stock factory 0x4783C6…
✅ long watcher up — frontend 5s, indexer 2s, factory ws, feeds on
```

## 6. How to test it

```bash
python3 tools/test_long.py            # 70 offline checks, no network at all
python3 tools/diag_long.py            # everything live, read-only
python3 tools/diag_long.py frontend   # the pairable-asset table + poll timing
python3 tools/diag_long.py gap        # on-chain stocks Long does NOT list yet
python3 tools/diag_long.py chain      # factory events + websocket probe
python3 tools/diag_long.py graphql    # indexer lag, newest coins, ws probe
python3 tools/diag_long.py ping       # post a test embed to the webhook
python3 tools/diag_long.py simulate PYPL          # fake listing, prints the embed
python3 tools/diag_long.py simulate PYPL --send   # ...and posts it for real
```

`simulate` writes to a throwaway database, so it can never make the live watcher
miss a real listing.

---

## 7. Verification status — read this before trusting anything

**Verified (live, through the browser on borz, 2026-09-04):**
* the GraphQL endpoint, its schema, `chain_metadata` (chain 4663 at block
  53,999,248, fully indexed), and real `Token`/`Asset` rows
* the factory address, the `Deployed` signature and topic0, 206 historical
  events, and that AAPL/SPCX/GLD/USAR/SKHY all share that one creator
* the numeraire array, and that **the exact production regex in
  `long_sources.py` extracts 57 unique assets from the real chunk with no false
  positives** — this was run against the live bundle, not a fixture
* the Chainlink feed deployer, and that feed proxies are `EACAggregatorProxy`
* Cloudflare's cache headers on `app.long.xyz`

**Verified offline:** all 70 checks in `tools/test_long.py`, including the
end-to-end simulation (seed silent → one new ticker → exactly one alert → silent
on re-read and on restart).

**NOT verified, and Johan must run `tools/diag_long.py` on the box to close these:**
1. **That Cloudflare serves a non-browser HTTP client at all.** The cloud
   container is proxy-blocked (403) so this could not be tested from here. If the
   VPS gets a 403 or a challenge page, `snapshot()` will raise and the watcher
   will refuse to start — which is the correct failure, and the fix is a browser
   `User-Agent` (already sent) plus, if needed, `Accept`/`Accept-Language`
   headers. **This is the single most likely thing to be wrong.**
2. That Alchemy's Robinhood websocket accepts a `logs` subscription filtered on
   the factory address (the multi-wallet watcher's EVM subscriptions work on the
   same endpoint, so this is likely, not certain).
3. That `eth_getLogs` over a 5M-block window is accepted — the sweep already
   walks in bounded windows and backs off, but the exact ceiling is unknown.
4. That `description()` on a fresh feed returns `TICKER / USD`. Blockscout
   rate-limited the attempt (429); the decoder is unit-tested against real ABI
   bytes, so this is about the call, not the parse.
5. Whether a feed deployment actually precedes a Long listing. Unproven — the
   latency table is what will answer it.

---

## 8. Open opportunities to make this faster

* **GraphQL subscriptions.** The schema exposes `Asset_stream` / `Token_stream`,
  but `wss://api.long.xyz/v1/graphql` closed with **1006** on every subprotocol
  (`graphql-transport-ws`, `graphql-ws`, none) from a browser on the allowed
  origin. `diag_long.py graphql` re-probes it every run; the day it answers,
  switch `LongIndexerWatcher` from polling to push and drop that detector's
  latency to ~0.
* **Alchemy `alchemy_minedTransactions`.** Would turn the feed watcher from a
  300 s Blockscout poll into a push subscription filtered on the deployer EOA —
  worth trying if feed deployment ever proves to be a real leading indicator.
* **`api.mainnet.base.long.xyz`** exists (`/v1/market/tokens/{addr}/stats`,
  plus `notifications.mainnet.base.long.xyz`). The host pattern suggests a
  per-chain naming scheme; a `…robinhood.long.xyz` sibling was not found, but a
  future session should re-probe — a server-side config route would beat the
  bundle diff.
* **A Robinhood-side signal upstream of the factory.** The factory's `deploy()`
  is called by `0x5516B3451d4d6C9f63353Fe7Bc9537477ECCE000`; watching that EOA's
  *pending* transactions would beat the mined event by a block or two. Marginal
  at 101 ms blocks, but it is the only remaining headroom.
* **Long's own `/v1/config`** returns `featured_tokens` / `hidden_tokens` /
  `prediction_tokens` / `residency_tokens` — all empty at build time. If Long
  ever starts populating those server-side, that is a config channel that could
  move before a frontend deploy. Worth a cheap periodic check.
