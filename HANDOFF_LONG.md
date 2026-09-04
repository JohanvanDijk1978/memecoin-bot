# Launchpad new-stock watcher — handoff

*(Long.xyz, Pons and o1. Named `long_*` because Long came first.)*

Built 2026-09-04. Everything below was verified against the live apps that day
unless it says otherwise. Read this before re-deriving anything.

**Read order if you are picking this up cold:** §0 (where it stands) → §1–2 (how
these launchpads work and what the earliest signal is) → §11 (what the first live
run taught, which is the most transferable part). §3–8 are reference. §9 is the
Cloudflare story, §10 is Pons and o1.

**Commits, all local until Johan pushes:**

| commit | what |
|---|---|
| `5b52579` | the watcher — four detectors, dedup, latency instrumentation |
| `3f71b95` | handoff §0 |
| `26dc209` | Cloudflare transport, degraded mode, `probe_long_403.py` |
| `c1598e5` | Pons and o1 — venue registry, venue-scoped store |
| `a554cf0` | interstitial retry, required primary page, strict 403/429 classification |

`tools/test_long.py` — **123 checks, all passing offline.**

---

## 0. State at handoff — read this first

**2026-09-04, run on the VPS. Verdict, per venue:**

| venue | reachable from the box | detector |
|---|---|---|
| **Long** | ✅ **but only through curl_cffi** — aiohttp gets a Cloudflare challenge on every path, including static JS | working after §9 |
| **Pons** | ✅ plain aiohttp, no tricks (Vercel) — 42 assets read live | working |
| **o1** | ❌ **HTTP 429** to a datacentre IP, on every client tried | slow-polled; see below |
| factory / feeds | ✅ RPC + WSS resolve from `fomo/.env` | working |

`tools/probe_long_403.py` settled the Long question outright: `aiohttp bare` and
`aiohttp browserish` both 403 with a challenge page, `curl binary` 403, and
**`curl_cffi chrome` 200 on the page, on `/create`, and on the config chunk**.
It is a TLS/JA3 bot-score block exactly as with fomo, and `curl_cffi` is now in
`requirements.txt`.

**Two bugs that first run exposed, both fixed (§9):**

1. **The first impersonated request gets the interstitial, the second passes.**
   `/create` was read as dead while `/` succeeded — and `/create` is the ONLY
   page referencing the config chunk (46 chunks vs 42), so the venue looked
   broken with the message "array not found in any of 42 chunks". The transport
   now retries through the challenge, warms up before seeding, and treats the
   first page as **required** rather than parsing a plausible partial set.
2. **A Vercel 429 was being reported as a Cloudflare WAF block** because its body
   was HTML. `is_cf_challenge()` is now strict (`cf-mitigated: challenge`, or a
   CF-fronted 403/503 whose body carries the interstitial text) and a 429 is
   named as rate limiting with its `Retry-After`.

**o1 and the 429.** Vercel rate-limits the VPS's address; no transport changes
it, and it is not a bot-score block. o1 therefore polls at **120 s**
(`O1_POLL_SECONDS`) with a 30-minute backoff ceiling on repeated 429s. This
costs little: o1 lists ~194 of the ~206 on-chain stocks, so the factory event
already covers nearly everything its array diff would say. Its Convex backend
answers from the box regardless.

**Next steps:**

1. `cd /root/memecoin-bot-new && git pull && systemctl restart memebot`
2. `python3 tools/diag_long.py venues` — should now print all three lists and
   the on-chain stocks nobody lists.
3. `python3 tools/diag_long.py simulate PYPL --send` to see a real alert land.
4. Watch for `long: cleared the Cloudflare interstitial on attempt 2` in the log
   — that line is the fix working.

**If the channel stays silent:** near-silence is correct. Long lists a stock
rarely, Robinhood's last stock-token batch was 2026-07-28, and coin alerts fire
only on the first-ever coin against a pairing asset. A noisy channel means
something is broken, not a working one.

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
| `tools/probe_long_403.py` | four HTTP clients × every host, to name the Cloudflare fix from evidence (§9) |
| `tools/long_baseline.json` | the 57-asset Long snapshot as of 2026-09-04 (Long's degraded-mode seed) |

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
python3 tools/probe_long_403.py       # WHY app.long.xyz 403s, and which client gets through
python3 tools/test_long.py            # 113 offline checks, no network at all
python3 tools/diag_long.py            # everything live, read-only
python3 tools/diag_long.py frontend   # every venue's asset table + poll timing
python3 tools/diag_long.py venues     # the three lists side by side + the untouched pool
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

**Verified offline:** all 113 checks in `tools/test_long.py`, including the
end-to-end simulation (seed silent → one new ticker → exactly one alert → silent
on re-read and on restart), and the blocked-frontend start from §9.

**NOT verified, and Johan must run `tools/diag_long.py` on the box to close these:**
1. ~~That Cloudflare serves a non-browser HTTP client at all.~~ **ANSWERED, and
   the answer was no** — the VPS gets 403 on every `app.long.xyz` page. Full
   Chrome headers, a curl_cffi escalation path and a degraded-mode fallback were
   added in response; run `tools/probe_long_403.py` to find out which client gets
   through. See **§9**, which supersedes this item.
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

---

## 9. The Cloudflare block on `app.long.xyz` (2026-09-04)

### What happened
`tools/diag_long.py` on the VPS:

```
long: page /create failed: https://app.long.xyz/create -> HTTP 403
long: page /       failed: https://app.long.xyz/       -> HTTP 403
RuntimeError: no chunk URLs found on any Long page
```

The same fetch succeeds from a browser on borz, and succeeded through the
browser pane during the research. So it is the **client** being judged, not the
address — the identical conclusion the FOMO work reached on 2026-08-18
(`fomo/FOMO_API.md` §1): Cloudflare scores the **TLS/JA3 fingerprint** and the
`sec-ch-ua` / `sec-fetch-*` header set, and a bare `aiohttp` client has neither.
A residential IP does not fix it and a datacentre IP is not the cause.

### What was changed in response

**1. Browser-shaped headers, and a transport that can escalate.**
`Http.get_text(url, kind="doc"|"script")` now sends the full Chrome document or
script header set. `LONG_TRANSPORT` (default `auto`) controls what happens on a
403/503: `auto` logs the classified reason once, retries through **curl_cffi**
(which reproduces Chrome's TLS fingerprint), and if that works, sticks with it
for the rest of the process rather than paying a blocked round-trip per poll.
`LONG_TRANSPORT=aiohttp` or `curl_cffi` force one. `LONG_IMPERSONATE` (default
`chrome`) picks the profile.

curl_cffi is an **optional** dependency, deliberately not in `requirements.txt`
until the probe proves it is the fix — a failed wheel build there would break the
whole install:

```bash
pip install curl_cffi --break-system-packages
```

**2. `describe_block()`** — ported from `fomo_api.describe_403()`, and for the
same reason: a WAF block and an app-level refusal need opposite fixes. It reads
`cf-mitigated`, the content type and the body markers, and says which one you
have plus the `cf-ray` for the record. Every non-200 on a Long page or chunk now
raises that sentence instead of `HTTP 403`.

**3. The watcher degrades instead of dying.** Seeding failure on the frontend is
no longer fatal:

* `seed_from_baseline()` loads `tools/long_baseline.json` (57 assets + 2 leverage
  tokens, captured 2026-09-04) so the other detectors can still answer "is this
  already on Long?"
* `store` is *not* marked seeded for the frontend, so `_frontend_loop` retries
  forever with its existing backoff, and performs the deferred seeding the moment
  it gets through — logging `frontend detector RECOVERED`.
* Recovery is **capped at 5 new assets** (`max_new_alerts`). One or two assets
  listed while we were blind are exactly the alerts we want; nine of them mean
  the baseline has gone stale, and a burst of "Long now supports X" for things it
  has supported all along is worse than a log line.

`tools/test_long.py` covers all of this — 83 checks now, including a simulated
blocked start that asserts the factory detector still alerts *and* still knows
the stock is on Long from the baseline.

### If the probe says every client is blocked
Options, cheapest first:

1. **Live with the degraded mode.** The first-coin detector catches a listing
   within minutes of the first launch against it, which in practice is minutes
   after the listing. This is genuinely close to good enough.
2. **Fetch the page from borz** (residential IP, and Chrome is already there for
   fomo) and have it POST the parsed asset list to the VPS, or write it to a file
   the VPS pulls. `fomo/fomo_browser.py` is the working pattern — a persistent
   Chrome profile driven by Playwright, whose page context supplies the Origin,
   the cf_clearance cookie and a real TLS fingerprint at once.
3. **A residential proxy for that one request.** Least appealing: a recurring
   cost and another dependency, for one small GET every few seconds.

Do not add a headless-Chrome dependency to the VPS before trying 1 and 2 —
memebot has no browser stack today and that is worth keeping.

---

## 10. Pons and o1 (added 2026-09-04)

Same question asked of two more Robinhood-Chain launchpads, and the answer has
the same shape at all three: **every one of them ships its supported-asset list
as a hardcoded array inside a content-hashed frontend bundle.** None serves it
from an API. So one detector, parameterised per venue, covers all three.

### What each one is

| | **Long** | **Pons** | **o1** |
|---|---|---|---|
| host | app.long.xyz | www.ponsfamily.com | launch.o1.exchange |
| stack | Next.js · Railway · **Cloudflare** | Next.js · **Vercel** | Vite SPA · **Vercel** + **Convex** |
| asset URLs | `/_next/static/chunks/*.js` | `/_next/static/immutable/chunks/*.js` | `/assets/*.js` (incl. `modulepreload`) |
| array shape | `s("NVDA","NVIDIA","stock","0x…",18,feed)` | `{address:"0x…",symbol:"NVDA",name:"NVIDIA",decimals:18,isNative:!1,assetClass:"equity"}` | `E({symbol:\`NVDA\`,name:\`NVIDIA\`,address:\`0x…\`,decimals:18})` |
| assets listed | **57** (1 native, 1 stable, 48 stocks, 7 ETFs) | **43** (2 native, 1 stable, 40 equity) | **~194 Robinhood + 13 Base** |
| launch feed | GraphQL `Token` | `GET /api/pons-launches` | Convex `POST /api/query` |
| reachable from the VPS | ❌ 403 (§9) | expected ✅ | expected ✅ |

### The finding that matters most
**o1 lists nearly the entire Robinhood stock universe** — ~194 of the ~206
tokens the factory has deployed, including BND, FICO, INFQ and PANW that Long
does not carry. Long (57) and Pons (43) are curated subsets. So the signal that
matters is different per venue:

* **Long, Pons** — the array diff is the news. A stock crossing from "exists on
  chain" to "listed here" is a real event.
* **o1** — the factory `Deployed` event effectively *is* the listing, because o1
  lists almost everything. Its array diff mostly confirms.

`python3 tools/diag_long.py venues` prints the three lists side by side, what is
unique to each, and what exists on chain that **nobody** lists — which is the
pool any of them will pick from next.

### o1 also pairs against Base
o1 is the only one of the three offering stock tokens on **Base (8453)** — 13 of
them (AAPL, AMZN, COIN, CRCL, GOOGL, INTC, META, MSFT, MSTR, NVDA, SNDK, SPCX,
TSLA), a different issuer on a different chain from the Robinhood ones. Those are
parsed and stored (the `extra.chain_id` field), but there is **no factory
detector for Base** — nobody has identified the issuer or its event yet. That is
the single biggest open item here; `ETHERSCAN_API_KEY` on the box covers Base
through Etherscan v2 (`chainid=8453`) and would answer it in a few calls.

### o1's Convex backend — the one real push channel
o1 runs on Convex (`https://exciting-fox-990.convex.cloud`). Two ways in, both
confirmed working **unauthenticated**:

* `POST /api/query` with `{path, args, format:"json"}` — plain HTTP.
* `wss://…/api/{version}/sync`, the reactive protocol the app itself uses. Frames
  look like `{"type":"ModifyQuerySet","modifications":[{"type":"Add","queryId":1,
  "udfPath":"dashboard:recentLaunchSnapshots","args":[{...}]}]}`.

That websocket is the only genuine subscription available across all three
venues (Long's GraphQL WS closes 1006, Pons has none). It is **not** wired into
alerting because the one udfPath we captured, `dashboard:recentLaunchSnapshots`,
returns `{chainId, imageUrl, symbol, tokenAddress}` and **no numeraire** — so it
cannot answer "first coin against asset X". Finding a udf that does carry the
paired asset is the cheapest remaining upgrade; capture it by hooking
`WebSocket.prototype.send` in the browser and opening o1's create page.

### What was built for them
* `Venue` registry in `long_sources.py` (`VENUES`, `enabled_venues()`), selected
  with `LONG_VENUES=long,pons,o1`.
* `parse_assets_pons()` and `parse_assets_o1()`, plus `VenueFrontendWatcher`,
  which is the old `LongFrontendWatcher` with the parser and asset-URL pattern
  lifted out. `LongFrontendWatcher` remains as an alias.
* `PonsLaunchWatcher` — polls `/api/pons-launches`, whose items carry a nested
  `quoteAsset`, so the first-coin-per-asset detector works there with no extra
  lookup. Pons has 4.2M launches lifetime, which is exactly why only the FIRST
  use of an asset ever alerts.
* `O1ConvexClient` — HTTP query client, used by the diag today.
* The store is venue-scoped: `venue_assets` and `venue_first_use` replace
  `long_numeraires` / `long_numeraire_use`, and rows from the single-venue schema
  are migrated in on first open (copied, not dropped, so a rollback still finds
  its data).
* A stock-deploy alert now says **which venues already offer it** rather than
  just "on Long / not on Long".

### Traps specific to these two
* **Pons's ETH and WETH both use identifier addresses** (`i.zeroAddress`,
  `s.ROBINHOOD_WETH_ADDRESS`) which would collapse onto the same key and silently
  overwrite each other. Only ETH is kept; WETH is dropped. No stock is ever
  affected — the equities are always emitted as literals.
* **o1's crypto entries use identifier addresses too** and are skipped for the
  same reason. o1's parsed list is therefore stocks-only, which is what we want.
* **o1 classifies by an explicit `category:` field on crypto entries and nothing
  on equities**, so "no category" means stock. If o1 ever starts tagging
  equities explicitly, `_O1_KIND` needs the new name.
* **Per-venue min-asset guards** (`Venue.min_assets`: long 8, pons 15, o1 40)
  stop a stray array of tickers being mistaken for the config array. Raise them
  if a venue grows; never lower one below about a quarter of its real size.
* **Only Long has a committed baseline file.** If Pons or o1 cannot be fetched at
  startup, that venue's listing detector stays dark until the fetch works — it
  does not fall back, and it does not stop anything else. The log line is
  `long[<venue>]: DEGRADED`.

---

## 11. What the first live run on the VPS taught (2026-09-04)

Both of these were invisible in every offline test and in the browser, and both
produced a *plausible* wrong answer rather than an error — which is what made
them expensive.

### The first impersonated request is the one that gets challenged
curl_cffi's first request to `app.long.xyz` came back `cf-mitigated: challenge`
("Just a moment…"); the next one succeeded, because the session had picked up
the clearance cookie. The code treated that first answer as a hard failure, so:

* `/create` → failed
* `/` → succeeded (session now warm)
* union of chunks → **42, from `/` alone**
* `/`'s HTML does **not** reference the config chunk; `/create`'s does

…and the watcher reported "pairable-asset array not found in any of 42 chunks",
which reads exactly like a broken parser. Nothing was wrong with the parser.

**Fixes:** `_curl_through_challenge()` retries up to 3 times with growing delays
while the response is still an interstitial; `Http.warmup()` takes the challenge
once at startup before any real read; and `snapshot()` now treats the FIRST page
in `Venue.pages` as required, refusing to parse a partial chunk set rather than
drawing conclusions from it.

**The general lesson, worth carrying to any future scraper:** when a source is
assembled from several requests, a partial success is more dangerous than a
total failure. Name the request that is load-bearing and fail loudly without it.

### "The body is HTML" does not mean Cloudflare
The 403 classifier called any HTML error body a WAF block. o1's Vercel 429 is an
HTML page, so the diagnosis said "blocked by Cloudflare's WAF" about a host that
is not behind Cloudflare at all, and prescribed curl_cffi — which cannot help
with a rate limit. `is_cf_challenge()` is now strict, and 429 has its own branch
that says to poll less often.

### Where each venue actually stands from a datacentre IP
* **Long** — CF bot-score block; only curl_cffi passes. Static assets are
  blocked too, so there is no "just fetch the chunk" shortcut.
* **Pons** — completely open to plain aiohttp. Nothing needed.
* **o1** — 429 on every client. Not a bot score, an origin rate limit, and the
  probe hitting it four times in a row plus the diag twice more will have made
  it worse. If it stays 429 at a 120 s poll, the fallbacks in order are: accept
  it (the factory event covers ~194 of its ~206 assets anyway), fetch it from
  borz instead, or drop o1 from `LONG_VENUES` entirely.
* **api.long.xyz `/v1/config` and `/v1/assets`** return 403 even through
  curl_cffi, with `content-type: application/json` — that is the app's own API
  key gate, not the WAF. Correct behaviour, no action.
