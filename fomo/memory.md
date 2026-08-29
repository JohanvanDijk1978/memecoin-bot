# Full Memory — Zenza (Updated)

**Last updated:** 2026-08-29 (session 43)

---

## Profile

Entrepreneur and developer building automated crypto trading and monitoring tools.

- Based in the Netherlands (Nieuwegein area)
- Dutch speaker
- Comfortable with: Python, VPS deployment (Vultr), Telegram/Discord bot development, REST APIs, Anthropic API
- Works primarily in VS Code locally with deployment via GitHub
- Manages at least two real estate properties (Parkville and Ocean Pearl)
- Exploring an AI agents startup

---

## Primary Active Project: Memecoin Bot (VPS)

**Solana/ETH memecoin signal bot on VPS**

### Deployment
- Repo: `JohanvanDijk1978/memecoin-bot`
- VPS: `root@209.250.245.16`, project path `/root/memecoin-bot-new`
- Local path: `C:\Users\mzshu\Downloads\memebot`
- Deploy via git push only (webhook auto-pulls on port 9000)
- `.env` is never committed to git
- Bot stack: Python 3, python-telegram-bot, Telethon, Discord self-bot
- Operational preference: always use nohup, never pm2
- Restart: `pkill -f "python3 main.py"` then `nohup python3 main.py > data/bot.log 2>&1 &`
- `memory.md` in repo is kept updated after sessions

### Features
- `/pump` command with inline buttons (1h/6h/12h/24h) showing top pumping coins
- Peak MC tracking
- CA pings with Dexscreener data, Axiom/Padre/GMGN links, multipliers, scan counts
- Per-group cooldowns and multi-group alerts
- Discord scraper cleanup
- Peak MC tracking
- Reformatted wallet tracking into new JSON schema
- Cluster/bundle scanner module (early-buyer clustering + wallet hold-time profiling)

### Recent Work
- Fixed multiple bugs (undefined group_name, mirror.py syntax errors, wrong variable names)
- Added `/leaderboard` command ranking top groups and users by average multiplier
- Added `/status` command
- Added ticker symbol saving
- Wrote backfill script for existing CAs
- Added second Discord self-bot account and Discord mirror functionality

---

## Secondary Project: Fomo Bot (Discord, local on borz)

**Standalone Discord bot for FOMO (fomo.family) social crypto trading platform**

### Setup
- Lives in `fomo/` under memecoin-bot folder
- Runs locally on borz (not the VPS)
- Uses Helius as Solana RPC
- Deployment: local only

### Commands (Current Surface)
- `/fomo` — FOMO trader profile lookup
- `/pump` — Pump.fun data
- `/wallet` — wallet lookup (Solana/EVM)
- `/token` — token page with top 50 holders, refreshable in place
- `/thesis` — top holders' written theses
- `/track` — platform choice for tracking (FOMO/Pump)
- `/tracked` — edit/remove tracked items
- `/fomotop` — top traders

### `/fomo` Features
- Shows profile embed with display name, Twitter, SOL + EVM wallets, followers/following, trades/volume
- Wallet resolution via Helius
- Open positions field (token, avg entry, position, PnL)
- Wide buys use green marker
- Padre-linked tickers matching sell rows

### `/token` Implementation

- **Chains:** Solana, Ethereum, Base, BSC, Robinhood, Hyperliquid (HyperEVM)
- **Top 50 Holders:** 10 per page, paging through all 50
- **🔄 Refresh button (Session 40):** re-runs the whole card — market data,
  holder query, FOMO/Pump identity labelling — and replaces every page, keeping
  the reader on their page. 15s cooldown, lock, and a failed rebuild leaves the
  card standing rather than replacing it with an error
- Identity caches are NOT bypassed on refresh (a handle does not go stale like a
  holder list does)

### `/token` Top Traders — REMOVED (Session 40)

Taken off the card at the user's request. The ranking and P/L accounting were
correct; the sample depth never reliably reached a token's first transactions,
so it systematically ranked recent buyers. `token_traders.py`,
`token_intelligence.top_traders()` and `token_traders_diag.py` remain in the
repo and still work from the command line, driven by the same `TOKEN_TRADER_*`
env keys. The local-indexer plan below was the fix for the depth problem and is
now moot unless the board comes back.

### `/token` Hyperliquid Holders (Session 39)

**Problem:** `/token` returned 0 holders for every Hyperliquid token — CMC has
no `hyperevm` platform and no Blockscout instance serves chain 999.

**Pump.fun is NOT the source** (traced live in DevTools on EGG,
`0xb75d5ee14708e7efbea939311090061d72265608`):
- `/coins/top-holders/{mint}` → 400, Solana base58 only
- `/token-holders/{addr}/count` → 404 `Codex has no holder data for this token`
- `/pnl/coin/{addr}/holders` → POST, max 20 addresses, PnL enrichment only
- The `Pump.fun (n)` panel is positions of pump.fun **users**, streamed over
  `wss://multichain-prod.nats.realtime.pump.fun` and recomputed from trades —
  a cold page load makes no holders request at all

**Source used:** `scan-api.hl.eco/api/token/{addr}/holders?limit=50` (hl.eco /
hyperscan.com, the HyperEVM explorer). Indexes Transfer events, then reads the
top candidates' balances on-chain. Returns address, raw balance, `pct`,
`totalSupply`, `decimals`, `holderCount`, plus labels.
- Cross-check: its #1 row = 2.4568% of supply; pump.fun's panel shows Dior100x
  at 2.46% — same wallet, two independent paths
- Ranks within the top 500 transfer-delta candidates (`page.reachable`); exact
  for a top 50, not a full census
- Unknown token answers 200 with nulls → empty list, shorter card, no exception

**Shipped:** `_hyperevm_holders()` in `token_intelligence.py`, `hyperevm` /
`hyperliquid` → `Hyperliquid` in `CHAIN_NAMES`, `hyperscan.com/address/` in
`_holder_explorer`, 3 tests, new `hyperliquid_holders_probe.py`, three
`HYPEREVM_*` keys in `.env.example`.

**Still missing on this chain:** Top Traders (`unsupported` — no transfer
source for chain 999) and FOMO handle naming (`NETWORK_IDS` has no Hyperliquid
id; fomo.family does not carry the chain).

### `/connected` — rewritten (Session 40)

**The problem:** it reported Meteora, Jupiter and Raydium as connected wallets.
Not a filtering bug — an input bug. Helius parsed history was read whole, so
every swap contributed a "transfer" between the trader and a liquidity pool, and
a daily swapper produces exactly the pattern the scorer rewarded (many
transfers, both directions, months of span, high value). The pool outranked the
trader's real associates.

**What it does now:**
- Reads **only plain transfers** — Helius `type == "TRANSFER"`, no swap event,
  no DEX source, no failed transactions
- **Size bar:** 1+ SOL or 50+ USDC on Solana; $200+ native or 50+ stablecoin on
  EVM (`CONNECTED_MIN_SOL` / `CONNECTED_MIN_STABLE` / `CONNECTED_MIN_EVM_USD`)
- Non-native, non-stablecoin assets are **dropped**, not counted unpriced —
  they cannot be priced honestly so they cannot clear a value bar
- **Scores and bands deleted.** `Association` → `Connection`; ranked by value
  moved then transfer count. `strict` flag → `fresh` flag
- **Funding wallet**, never value-gated, its own field at the top of page one:
  Solscan Pro `sort_order=asc` (one request, needs `SOLSCAN_API_KEY`) →
  Helius paged backwards to exhaustion (`CONNECTED_FUNDING_PAGES` = 20 pages =
  2000 tx; hitting the ceiling reports **unknown**, never a guess) → EVM
  `alchemy_getAssetTransfers` with `order: "asc"`
- Still excludes structurally: known addresses, Solana account type, degree
  probe (run on **unfiltered** history — a service is a service because of the
  swaps it handles)
- Cache keys prefixed `CACHE_SCHEMA` = `v2`, so old scored reports are ignored

### `/fomo` unverified wallet fallback (Session 41)

`/fomo pudgypenguins` showed "No verified wallets found." while its own log
said both derived routes had found **1 unambiguous owner**. They had — and
threw it away, because neither could corroborate it (FOMO publishes no swap
for that handle, so `verify_wallet` had nothing to check).

There are now three states, not two: **verified**, **likely/unverified**, and
**none**. The corroboration gate and the wallet cache are untouched; a
candidate is never cached and never called linked. `WalletCandidate` +
an optional `candidates=` sink on `resolve_from_holders` /
`resolve_from_balances` carry the owner out to the card;
`choose_unverified_wallets()` merges by address (two routes agreeing beats
one) and refuses to break a tie — tied candidates are all shown. An owner
`verify_wallet` actively *refuted* is never offered. `/connected` drops the
fallback on purpose; `fomo_resolve_diag.py` prints it. Log line:
`using unverified fallback wallet for <handle>: <wallet>`.

### `/pump` EVM wallet diagnostic (Session 42)

Reported: `/pump eth` shows the Solana wallet but no EVM wallet. Wanted a Pump
counterpart to `python fomo_resolve_diag.py` that returns **both** wallets for
a Pump profile.

`pump_resolve_diag.py` already existed for the Solana/profile half; it now
covers EVM too, so one command prints both wallets and, when one is missing,
the gate that lost it:

- `evm-cache` — already discovered and confirmed, no requests
- `evm-portfolio` — no **open** position on Ethereum/BSC/Base/Robinhood.
  The usual answer and not a failure: Pump never publishes the EVM address, so
  discovery needs a live balance to fingerprint. A Solana-only trader has none.
- `evm-holders` — no holder index answered (CMC keyless route, or Blockscout)
- `evm-fingerprint` — zero holders at that exact balance, or more than one
  (ambiguity is refused, never guessed)
- `evm-verify` — a unique candidate the chain RPC would not confirm; a missing
  `ETH_RPC`/`BSC_RPC`/`BASE_RPC`/`ROBINHOOD_RPC` fails this the same way

Context for the numbers: `pump_evm_cache.json` holds 7 mappings against 4009
cached profiles. That is discovery being precise, not broken.

The tool walks `PumpEvmResolver`'s own ordering through its own helpers and
hands a surviving candidate back to `PumpEvmResolver.resolve()` for the
decision and the cache write — the walk explains, the resolver decides.

`pump_evm.py` gained `portfolio_rows()` (public), `order_positions()` and
`EXAMINED_POSITIONS`, all additive, so the diagnostic examines exactly the
slice `/pump` examines.

Flags: `--fresh`, `-v` (per-request HTTP log, API keys redacted to host),
`--no-evm`, `--evm-positions N`, `--require-evm`, `--no-write`, `--csv`,
`--json`. New offline suite: `python -m unittest test_pump_resolve_diag`.


**Session 42 outcome — `eth`'s EVM wallet found: `0x6a4aab5657f10d44d27e8ff06e3dfba7e1d3c7b3`**

- Confirmed by exact `balanceOf` against all three of Pump's published open
  Robinhood balances (372.5228803259225 / 50 / 25) and by holding 0 COPPERINU
  after selling 6.4M (the +$61.6k closed trade).
- Root cause of the miss: `_blockscout_holders` paged only 5 pages (250
  holders); this wallet is at holder rank ~1211. A throttled page also looked
  identical to an empty index.
- SECOND cause (the one that kept it broken after the depth fix): Blockscout
  is behind Cloudflare and refuses httpx's default User-Agent. Holder calls
  sent `Accept` alone and 403'd; `_positions` sent pump_api.HEADERS (with a UA)
  and worked — so positions were found but holders were always empty. Fixed
  with `EXPLORER_HEADERS` on every explorer request in `pump_evm.py`.
  FIXED in session 43 for `token_intelligence._blockscout_holders` and all
  three Blockscout calls in `fomo_evm.py` — it was indeed what broke `/token`
  Robinhood holders.
- THIRD cause: after the UA fix the search succeeded (found
  0x6a4aab…d3c7b3 in ~4 min) but the diagnostic then called resolve(), which
  re-paged the whole holder index — another 4 silent minutes, looked hung.
  Now hands the winner to adopt() instead: one search, ever.
- Blockscout holder pages take ~6.5s each. Added wall-clock budgets
  (PUMP_EVM_HOLDER_SECONDS 300 / PUMP_EVM_CARD_SECONDS 8), a rate-limit floor
  (Cloudflare: 180/window, ~40min lockout), HolderIndex.stopped, and live
  progress logging every 5 pages.
- Added `--adopt-evm 0x…`: proves a known address with balanceOf against
  Pump's published balances and caches only on agreement.
- Fixed: `PUMP_EVM_HOLDER_PAGES` (40) with backoff, `HolderIndex.complete`,
  a new `evm-truncated` gate, candidate corroboration across the profile's
  other balances, and a shallow `HOLDER_PAGES_CARD` (6) for `/pump` so the
  card reads what the deep tools cached.
- Found but not yet wired: `GET /user-portfolio/{sol}?filter=closed` and
  `GET /user-positions/{sol}?mints=…` publish `amountBought` for CLOSED
  positions (79 for `eth`, 25 on EVM chains) — a second fingerprint that
  works for positions the trader has exited. Needs the private ROBINHOOD_RPC;
  the public one times out on `eth_getLogs` over busy ranges.

### `/token` Robinhood holders — FIXED (Session 43)

**Symptom:** `/token address: 0xcacb0e9caccee63ec4d82952e561a291c68bcb68` ($GG)
and `0x5317c0d077d2eeb639448939b930d49c4984b63b` ($COPPERINU) both rendered
market cap and price fine and then `Top holders of 0 — Holder data is
currently unavailable.` Blockscout says those tokens have 3,129 and ~4k
holders.

**Cause 1 (the one that emptied the card):** the exact latent bug session 42
flagged and did not fix. `token_intelligence._blockscout_holders` sent
`headers={"Accept": "application/json"}` and nothing else. Blockscout is
behind Cloudflare, which refuses httpx's default User-Agent with a 403 — and
`_blockscout_holders` returns `[]` on any status >= 400, so a 403 and an
empty index are the same answer to `/token`. Verified live in Chrome: the
same URL with a browser UA answers 200 with 50 rows.

**Cause 2 (would have survived the UA fix, silently):** the parser read
decimals and total supply out of `raw["token"]`. This Blockscout version's
holders response has exactly two top-level keys, `items` and
`next_page_params` — no `token` object. So decimals fell back to 18 and
`supply` stayed `None`, which makes every `percentage` `None` and drops the
`%` off every holder row. Decimals and `total_supply` come from
`GET /api/v2/tokens/{address}` instead (`"18"` / `"1000000000000000000000000000"`
for GG). The `raw["token"]` read is kept as a fallback for Blockscout
versions that do inline it.

**Cause 3 (latent, not yet observed):** one page is 50 rows, which is exactly
`MAX_HOLDERS`, with no headroom — a short page would have truncated the card.
The reader now follows `next_page_params` until it has the limit, capped at
`BLOCKSCOUT_HOLDER_PAGES` (3).

**Shipped:**
- `EXPLORER_USER_AGENT` / `EXPLORER_HEADERS` in `token_intelligence.py`, on
  `_blockscout_holders` and `_blockscout_trader_flows`
- New `_blockscout_token_meta()`; `_blockscout_holders` rewritten to page
- The same `EXPLORER_HEADERS` in `fomo_evm.py`, on all three Blockscout calls
  (`_blockscout_token_transfers`, `_blockscout_quote_values`, the EVM holder
  index) — the same 403 was latent there
- `EXPLORER_USER_AGENT` in `.env.example`
- 4 new tests (`RobinhoodHolderTests` in `test_token_intelligence.py`): the UA
  is sent, percentages survive the missing `token` object, a short page pages
  on, and a genuine 403 still shortens the card rather than raising

**Verified:** live payloads for both CAs pulled through Chrome, then replayed
through the real parser — 5/5 rows, top holder 81,632,653.06 GG = 8.1633% of
the 1B supply, which matches the explorer. Not yet run through Discord.

### `/thesis` (New Command)
- Top holders' written theses ranked by position value
- Shows: handle, X account, position, PnL, hold time, thesis quote
- 5 entries per page

### Wallet Resolution
- Traced FOMO traders' real Solana wallets by hand on Solscan (ground truth for automation)
- Multiple resolution routes: holders → transactions → balances (cheapest first)
- User prefers targeted changes over refactors of unrelated code

### Technical Notes
- Routes, schemas, resolver design documented in repo: `fomo/HANDOFF.md`, `fomo/README.md`, `fomo/FOMO_API.md`
- User prefers being asked before permanent/irreversible data writes
- Approved bulk wallet-cache adoption explicitly

### Open Items from Handoff
1. `FOMO_ENRICH_TIMEOUT` cancels enrichment mid-flight (handles never converge on slow scans)
2. Duplicate 50-swap request in `WalletResolver._resolve()`
3. `fomo/` still untracked in git
4. Adoption unproven at scale (unit-tested but never spot-checked on hand-traced wallets)
5. `/hodlers/top` pagination unknown (returned 48 rows for 1006 holders)
6. Pump has no known batch profile route
7. `pump_map_top.py` and `pump_resolve_diag.py` never run live
8. `/feed/token/sortedThesis` never probed
9. `_das_holders` never made a real request
10. Batched `/hodlers/top` never sent with multiple tokens
11. Top Traders never made a real request (removed from `/token` in session 40;
    `token_traders_diag.py` still exercises it)
12. `/connected` never made a real request — and the session-40 rewrite adds
    two things a live run must settle: whether Helius types a gas-sponsored
    FOMO wallet's own sends as `TRANSFER` (if not, the filter is too strict and
    the card comes back empty), and whether 2000 pages of walk-back reaches a
    typical FOMO wallet's first transaction
13. Cross-chain evidence is identity-based only (bridge tracing not implemented)
14. `/connected` prices only SOL, native EVM coins and stablecoins; memecoin-only
    relationships are now invisible rather than unpriced
    15b. EVM has no swap filter — no transaction type exists there, so pools are
    held off by labels, contract code, degree and the $200 floor alone
15. Sponsor index reaches back under an hour (shrinking as FOMO grows)
16. Hyperliquid holders rank within hl.eco's top 500 candidates, not the full
    holder set
17. `hyperliquid_holders_probe.py` has never been run from the venv — every
    live read this session went through Chrome, since neither sandbox nor
    desktop VM has egress to the HyperEVM RPC or pump.fun

---

## Local Indexer Architecture (Planned for Future Build)

**Goal:** Eliminate Top Traders sample size limitation without API costs

**What it is:** One-day build (~8 hours), free forever, complete transaction history, fast queries

**Components:**
- Sync Worker: Scan all historical transactions (run once, 2-5 min per token)
- Real-time Listener: Poll Helius every 10s for new transactions
- Parser: Extract wallet, amount, price, timestamp
- P/L Calculator: avg_entry, avg_exit, profit/loss, ROI
- SQLite Database: Persistent storage
- Rank Engine: Sort by P/L, ROI, volume, etc
- FastAPI endpoint: `/traders/{mint}?sort=pnl&limit=50`

**Advantages:**
- Free (no HelloMoon subscription needed)
- Complete data (no sampling limits)
- Fast queries (cached locally)
- Persistent (survives restarts)
- Scalable (1000s of tokens)
- Can run on cheap VPS ($5-10/mo)

**Effort:** 4-6 hrs indexer + 1-2 hrs API + 2 hrs testing = ~1 workday

**Status:** Architecture documented, not built — and moot as of session 40,
since Top Traders came off `/token`. Revive only if the board comes back.

---

## FOMO Platform Research

- Researched programmatic/API access to Fomo (fomo.family)
- Focused on legitimate methods only — no auth bypass, no exploiting, no scraping private data
- Third-party bot (dlurfomobot) on Discord uses FOMO's routes to fetch trader profiles
- Goal: Know what can be legitimately automated vs what requires direct access

---

## Recent Work (Other Projects)

- Explored Compute Royale platform (Solana-based GPU compute competition)
- Built Pump.fun memecoin launcher bot: autonomous monitoring Twitter/X for viral trends, GPT-4o-mini for coin names/tickers, Replicate SDXL for artwork, deployed on Vultr VPS using screen sessions
- Built two crypto waitlist landing pages: "YieldX" (Solana green/purple dark theme) and "SOLDOWAY" (editorial cream/black style) for DeFi yield product with x402 gasless transactions and Solana wallet confirmation flow

---

## Technical Stack & Preferences

### Languages & Tools
- Python (primary)
- VS Code (local development)
- GitHub (version control & deployment)
- VPS: Vultr (209.250.245.16)
- nohup for process management (no pm2)
- Helius (Solana RPC)
- Telegram/Discord bot frameworks
- Anthropic API

### Crypto Tools & Platforms
- Solscan (manual wallet tracing; Solscan Pro `sort_order=asc` optionally used
  by `/connected` for the funding wallet — the only route that reads a wallet's
  oldest transaction in one request)
- Dexscreener (signal data)
- Axiom.trade (reference for top traders ranking)
- Padre.gg (GMGN links)
- Pump.fun
- Fomo.family
- Compute Royale

---

## Preferences & Notes

- Deploy via git push only (webhook auto-pulls)
- `.env` never committed to git; `.env.example` as template
- Always ask before permanent/irreversible data writes
- Prefers targeted changes over refactors
- High precision over recall (especially for `/connected` command)
- Compact, scannable table formats
- No invented data where actual data unavailable
- Wants to understand "how it works" before implementation (researches platforms/APIs)
- Pragmatic approach: use free APIs/infrastructure when possible, but understand limitations clearly

---

## Session: Solscan + the 10062 interaction stall (Aug 2026)

**Solscan.** The key is a *free* key. Free keys reach `/playground/...` only;
`/v2.0/...` is the paid Pro API and rejects them. The header is `token: <key>`,
not `Authorization: Bearer`. New `solscan_api.py` resolves prefix, header style
and parameter spelling at runtime, caches what worked, and logs every attempt
on failure. Solscan's gateway authenticates before it routes, so the body is
the only signal: `Token is missing` = no key arrived, `Token is invalid` = one
arrived and was rejected. **Answered:** `/playground/token/holders` is a 404 (so the key *is* accepted —
routing happened) and `/v2.0/token/holders` says "Please upgrade your api key
level". Solscan's holder list has no free route; only a Pro plan opens it.
Playground is account-scoped, so `/connected`'s funding lookup is fine.
`token:` is the only header Solscan reads.

**The holder that was actually missing was never Solscan's fault.**
`_query_helius_das` capped at 3 pages x 1,000 = 3,000 token accounts, and DAS
returns accounts in index order, not by balance — so the cap kept an arbitrary
slice, and any token over 3,000 accounts could drop a holder of any size.
`DAS_MAX_PAGES` now defaults to 40 and warns when the ceiling is hit.

**`404 Unknown interaction` (10062) on /token and /wallet.** Not a Discord or
Solscan problem: `ProfileCache.put()` saved the *entire* cache file on every
call. `wallet_cache.json` is ~770KB, so labelling 50 holders meant 50 full
serialisations on the event loop — ~18ms each measured on a Linux mount, more
on Windows with Defender inspecting each temporary file. Past three seconds of
that, Discord expires the *next* interaction's token and `defer()` raises.
Saves are now coalesced (`PROFILE_CACHE_SAVE_INTERVAL`, default 5s), with an
`atexit` flush and an explicit `flush()`. Measured: 50 puts, 1 write.
Coalescing is skipped when no event loop is running, so scripts and tests keep
write-through semantics.

Also: `_safe_defer()` turns a lost interaction into one warning instead of a
two-deep `CommandInvokeError`, and `bot.run(..., log_handler=None)` stops
discord.py adding a second root handler on top of `basicConfig` — which is why
every traceback was printed twice in two different formats.

**Follow-up: the 10062s got worse, not better.** Raising `DAS_MAX_PAGES` from
3 to 40 turned `/token` into up to forty *sequential* Helius round trips
(~10s+ at 250ms each) — it never blocked the loop, but it kept heavy work in
flight far longer and invited retries, each of which started another crawl.
DAS pages are independent, so they now go out concurrently
(`DAS_CONCURRENCY`, default 8): page one alone, so a small token still costs
exactly one request, then batches. Measured against a simulated 250ms round
trip: 37k accounts 10.0s -> 1.6s, 4.2k accounts 2.25s -> 0.51s, small token
unchanged at one request.

What actually blocks the loop is still unproven. `_loop_watchdog()` now runs
from `setup_hook`: it samples every 250ms and, on an overshoot past
`FOMO_LOOP_LAG_WARN` (default 1.0s), logs the lag and every live task with its
file and line. `FOMO_LOOP_DEBUG=1` additionally turns on asyncio's own
slow-callback warning, which names the exact coroutine (real overhead, so it
is opt-in). Ruled out by measurement: DAS page parsing (longest uninterrupted
block 3.7ms across 40 pages) and profile-cache writes (now coalesced).

**The watchdog stayed silent through five more 10062s — which settles it: the
event loop is not blocked.** `_safe_defer`'s old message ("the event loop was
busy") was an assumption, and a wrong one. Discord's three seconds run from
when *it* mints the interaction token, not from when the bot sees it, so a
10062 splits into two cases the traceback cannot tell apart: the event reached
us late (gateway/network/a second process holding the session), or our
acknowledgement was slow (REST). The snowflake carries the creation time, so
`_safe_defer` now measures the interaction's age on arrival and again at
refusal, logs both with `bot.latency`, and names which side is at fault.

Added `claim_single_instance()`: binds 127.0.0.1:47821 (`FOMO_LOCK_PORT`) at
startup and refuses to run beside another copy. Two processes on one bot token
both hold a gateway session; Discord routes an interaction to one and the
other fails to acknowledge with exactly this error — no stalled loop, no slow
network, just a stale process surviving a restart. A bound socket cannot go
stale the way a pid file can. `FOMO_SINGLE_INSTANCE=0` disables it.
