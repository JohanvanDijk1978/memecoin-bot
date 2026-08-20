# fomo bot — handoff (2026-08-20)

## Where this stands

Sessions 26-32 were one continuous run on wallet identity: why it failed, why
it silently failed, and how to stop paying a chain scan for it. Session 33
carried the same discipline to Pump, where the mapping is published rather than
inferred and the only thing worth engineering is not asking twice.

The arc, shortest version:

0. Pump's mapping needed no discovery, only a cache: `pump_profiles.py` makes
   wallet -> profile one request ever, remembers the wallets that have no
   profile, and stops `/token` re-asking them on every render.
1. `/fomo` hid every resolution failure -- `fomo_resolve_diag.py` now names the
   stage that lost the wallet, for both chains, in one command.
2. The bot had never run the block route, and FOMO's growth had pushed the
   sponsor and mint routes past their 12000-signature reach. It runs now,
   bounded, and `@397397` resolves.
3. FOMO publishes exact positions (`/hodlers/top`, and `topHoldings` on the
   leaderboard). Matching those against on-chain owners is a wallet for the
   price of one holder query -- no sponsor index, no block scan. `/token` names
   holders from it live, and confirmed matches are adopted into the cache.

### Run these first on a fresh session

```powershell
python fomo_map_top.py --dry-run        # match rate before writing anything
python fomo_map_top.py --top 100        # bulk-label the leaderboard
python fomo_resolve_diag.py <handle>    # why one handle still has no wallet
python pump_map_top.py --dry-run        # Pump profiles for wallets we know
python pump_resolve_diag.py <wallet>    # why one wallet has no Pump profile
```

`fomo_map_top.py` has never been run live -- the sandbox has no RPC or
fomo.family egress (section 5). Its first real run is the open item.

### Open

1. **`FOMO_ENRICH_TIMEOUT` cancels, it does not just stop waiting.**
   `_enrich_fomo_message()` wraps enrichment in `asyncio.wait_for`, so a block
   scan running past 20s is discarded mid-flight and repeated on the next
   `/fomo` -- the handle never converges. Raising the timeout is the one-line
   mitigation; the structural fix is a detached task that outlives the card's
   edit budget, analysed in session 27 and not implemented.
2. **The duplicate 50-swap request** (session 20) is still unimplemented:
   `WalletResolver._resolve()` re-fetches swaps that `stats.raw_swaps` already
   holds.
3. **`fomo/` is still untracked in git.** First commit remains Johan's call.
4. **Adoption is unproven at scale.** Every gate is unit-tested and the
   matching logic was validated against a known pair (@Quanterty), but no
   adopted mapping has been checked against a hand-traced wallet yet. Spot-check
   a few from `fomo_map_top.py --csv` before trusting the cache wholesale.
5. **`/hodlers/top` pagination is unknown.** It returned 48 rows for a token
   with 1006 holders; no cursor parameter has been looked for.
6. **Pump has no known batch profile route.** `pump_profiles` batches by
   bounded concurrency over `/users/{term}`, which is what the site itself
   does. If a multi-user endpoint exists, `lookup_many()` is the one place to
   swap it in.
7. **`pump_map_top.py` and `pump_resolve_diag.py` have never run live** — same
   sandbox egress limit as `fomo_map_top.py` (section 5).

## Session 33 — `/pump` stops re-asking Pump the same question

`/token` on a Solana mint called `PumpClient.resolve()` once per holder row,
live, on every invocation — including the rows Pump has no profile for, whose
`404` was re-asked forever. Nothing was cached, nothing was deduplicated, and
`/pump`, `/wallet`, `/pumpwallet`, `/pumptrack` and `/pumpuntrack` each paid the
same request independently. Sessions 26-32 had already solved this shape for
FOMO; this session applies it to Pump without pretending the two sources are
the same thing.

They are not. FOMO does not publish its traders' wallets, so `fomo_wallet.py`
has to *find* one — sponsor index, mint scan, block route — and the result is
cached **forever** because it was proved on chain. Pump publishes the mapping
outright: **a Pump profile IS a Solana wallet**, `GET /users/{wallet}` and
`GET /users/{username}` return the same record, and `address` on it is the
canonical identifier (session 23 already made every profile URL use it). There
is no discovery stage, no corroboration gate and no ambiguity to reject. What
remained worth copying from `/fomo` was only the expensive lesson: ask once.

`wallet_profile_cache.py` is the part both flows genuinely share, extracted
rather than duplicated: tolerant JSON reads, an **atomic** write (temp file +
`os.replace`, which `fomo_wallet._save_cache` still does not do), `KeyedLocks`
— `WalletResolver._locks` verbatim in behaviour — and a `ProfileCache` keyed
store with aliases. Nothing in it knows what a profile is.

`pump_profiles.PumpProfileResolver` is the Pump-specific half. `lookup()`
returns a `PumpLookup` carrying the *reason*, not a bare `None`, because
`/pumpwallet` has to tell "Pump has no profile for this wallet" apart from
"Pump did not answer". Statuses: `cached`, `cached-missing`, `resolved`,
`missing`, `unavailable`, `unsupported`.

Three deliberate differences from the FOMO cache:

1. **Positive entries expire.** A wallet proved by an on-chain signature is
   permanent; a Pump username, avatar and follower count are not.
   `PUMP_PROFILE_TTL` is 7 days, and `/pump`'s card passes the much shorter
   `PUMP_PROFILE_CARD_TTL` (300s) as `max_age` against the same store — one
   cache, two freshness bars, so holder labelling stays free while the card
   stays current.
2. **Negative caching, which `/fomo` deliberately does not do.** A FOMO miss
   means a scan did not reach far enough and a later run may fix it, so writing
   it off would be wrong. A Pump miss is an authoritative 404 from the only
   source of truth. It is cached for `PUMP_PROFILE_NEGATIVE_TTL` (6h) — this is
   the whole `/token` saving. A **transient** failure (timeout, 5xx, transport)
   is never written as an absence, so a Pump outage cannot poison the cache for
   six hours; that distinction is unit-tested from both sides.
3. **The alias direction is reversed.** FOMO caches handle -> wallet. Pump's
   canonical key is the wallet and the username is a mutable alias pointing at
   it, so `/pump zinc` warms the wallet entry and vice versa — one request
   answers every spelling.

Session 20's `-32602` lesson carries over unchanged in spirit: an `0x…` term is
never sent to Pump's Solana profile route. When `pump_evm.py` has already
discovered which profile owns that EVM wallet the query is rewritten to it;
otherwise the caller is told `unsupported` rather than being charged a request
that must 404.

`/token` now prefetches its entire Solana holder list through one bounded,
deduplicated batch before any row renders, so `_holder_label()` is a pure cache
read. Holders the Pump EVM cache already names are excluded from that batch.
A prefetch failure is swallowed — the card renders without identities, exactly
as before.

`pump_map_top.py` is `fomo_map_top.py`'s counterpart, and the difference is
instructive: `fomo_map_top.py` *infers* wallets from published positions,
`pump_map_top.py` only pays requests in advance. It seeds from
`wallet_cache.json`, `pump_evm_cache.json` and `pump_tracks.json` by default —
so every wallet `/fomo` proved on chain becomes a candidate Pump profile and
the two caches compound — plus `--token`, `--wallets` and `--file`. `--dry-run`
learns in memory and writes nothing. For `--token` it prefers Helius DAS
`getTokenAccounts` over `getTokenLargestAccounts` for the same reason session 32
did: the latter stops at 20.

`pump_resolve_diag.py` is `fomo_resolve_diag.py`'s counterpart. Same
construction — it drives the resolver the bot drives and installs a temporary
handler on the `pump.*` loggers, so it cannot diverge — and its stages
(`input`, `cache`, `evm-map`, `profile`, `panels`) exist mainly to make the
three kinds of "no" distinguishable at a glance. Full addresses in the summary,
`--csv`, `--json`, exit 0/1/2, `--no-write` for a read-only run.

`_reply_pump_error()` is gone, replaced by `_resolve_pump_user()`, which every
Pump command now shares — so the resolver is the only path to a Pump profile in
the bot and no command can accidentally bypass the cache.

Regression coverage (48 new tests in `test_pump_profiles.py`): a wallet is
asked about once across calls, across six concurrent callers, across a batch
containing repeats, and across a process restart; a 404 is remembered and a
timeout/5xx/unexpected exception is not; an `0x…` address never reaches the
Solana route and a discovered one resolves through its Solana profile; a
username and its wallet share one entry; a corrupt cached row is refetched
rather than believed; `--dry-run` writes no file; rendering the same `/token`
card three times makes exactly two requests; a Pump outage leaves the holder
row intact; and every Pump command path returns the right one of the three
refusals. Verification: **230** conventional unit tests (48 pump-profile, 182
pre-existing — unchanged), the standalone offline Solana suite, `pyflakes`
clean against the pre-existing baseline, and `py_compile` on every module.

An end-to-end trace over the whole stack (fake transport under `PumpClient`,
so `_get` is exercised too) confirms the shape: three holder wallets, one with
a profile, cost **three** HTTP GETs — total, across two `/token` renders, a
`/pump` card, a `/pump <username>` alias hit, a `/wallet` on a profile-less
wallet, and a third `/token` render after a simulated process restart. Before
this change the first two renders alone cost six, and every render after that
cost three more.

Not verified live: the sandbox has no Pump egress (section 5), so
`pump_map_top.py` and `pump_resolve_diag.py` have never made a real request.
Their first live run is on borz, same as `fomo_map_top.py`.

## Session 32 — bulk wallet labelling from published positions

`fomo_map_top.py` labels the top N leaderboard traders without a single sponsor
index, mint scan or block scan. The insight is that `/v2/leaderboard?limit=100`
already returns `topHoldings` per trader — `tokenAddress`, `networkId` and the
exact `humanAmount` — which is the same fingerprint `/hodlers/top` supplies.
One leaderboard call seeds ~300 (handle, token, amount) fingerprints across the
top 100.

Per distinct token it then fetches the on-chain owner set once and matches
amounts. Solana uses Helius DAS `getTokenAccounts`, which pages every holder;
`getTokenLargestAccounts` stops at 20 and would silently miss anyone below that
line. EVM reuses `TokenIntelligenceClient._holders`. Each token is also queried
through `/hodlers/top`, so traders far outside the top 100 get labelled as a
side effect of sharing a token — the yield compounds well beyond the seed list.

Everything is written through `adopt_holder_matches()`, so the corroboration is
unchanged: Solana needs a FOMO-sponsored transaction on the wallet, EVM needs
contract code on the chain whose token is held, existing mappings are never
overwritten. `--dry-run` reports the same numbers without writing. The run ends
by listing the handles no token could explain; those are the only ones that
still need `fomo_resolve_diag.py <handle> --fresh`.

Regression coverage: holdings group by (token, chain), the amounts survive as
match fingerprints end to end, malformed holding rows are skipped rather than
fatal, and every FOMO network id has a display name. Verification: 164
conventional unit tests, the standalone offline Solana suite and `py_compile`
pass. The live run has to happen on borz.

## Session 31 — EVM holders reach the cache too

`/token 0xb0c2…7777` (CETS, BSC) named @Drillpig_, @admiralfinest and @vydamo_
from the holder list, but `/wallet 0x11631d…202a` immediately afterwards
reported no profile. Adoption was written for Solana only and dispatched every
match to `WalletResolver` regardless of chain, so an EVM holder hit two walls:
its `0x…` address would have gone into the Solana `wallet` field, and its
corroboration was `getSignaturesForAddress` against a `0x` address, which is a
JSON-RPC `-32602` rather than a check. The exception was caught and the pair
skipped, so nothing was corrupted — but nothing was ever cached either, and
`/wallet`'s reverse lookup is cache-only.

`_adopt_holder_wallets()` now routes by the token's chain.
`EvmWalletResolver.adopt_holder_matches()` is the EVM counterpart, writing
`evmWallet` with `evmSource: hodlers+amount+rpc` and `evmStatus: verified` so
`find_cached_wallets()` reports it. Corroboration is the bar this module
already trusts: the address must have **deployed contract code on the chain
whose token the trader holds**. FOMO wallets are ERC-4337 contracts; an EOA
whale holding the matching amount has none. Code on some other chain does not
count.

Conflict handling matches the Solana path — an existing mapping wins, and a
wallet already claimed by another handle is refused, both before any RPC. One
extra check was needed here: `find_cached_wallets()` only reports EVM records
marked `verified`, so adoption also scans the raw cache; an unverified record
still means another handle owns that address.

Regression coverage: an EVM holder reaches the EVM resolver and never the
Solana one, the chain slug is passed through, code-less and wrong-chain
addresses are refused, existing Solana records survive adoption, and a Solana
address is never adopted as an EVM wallet. Verification: 160 conventional unit
tests, the standalone offline Solana suite and `py_compile` pass.

## Session 30 — `/token` names FOMO holders, and the cache learns from them

`/token E3i7…pump` showed three of the token's FOMO holders as bare wallets.
Nothing was missing: they were rows 4, 5 and 6, and their balances matched the
FOMO UI exactly. Identity was the gap. `_holder_label()` resolved handles only
through `find_cached_wallets()` over `wallet_cache.json`, so a trader who had
never been through `/fomo` had no name — @rowdy and @quanterty were labelled
because they happened to be cached.

The Holders tab has a source, and route-guessing never found it because **it is
spelled `hodlers`**:

    GET /hodlers/top?tokens=[{"address":"<mint>","networkId":1399811149}]

Found by `token_page_sniff.py`, which drives the persistent Chrome profile to
`https://fomo.family/tokens/solana/<mint>`, clicks Holders and records every
`prod-api.fomo.family` call. Two earlier dead ends are documented in
FOMO_API.md so they are not re-walked: every `/holders` spelling 404s, and the
`/v2/userTokens/aggregatedSnapshot*` family is keyed by `query.userId` — one
trader's portfolio, not a token's holders.

`user.address` on those rows is FOMO's synthetic address and still is not a
wallet. `humanAmount` is the join: FOMO reports the exact position, `/token`
already computes on-chain owners, and one unambiguous amount match names a
wallet. `fomo_hodlers.match_holders_to_wallets()` accepts a pairing only when
the amount identifies exactly one wallet AND that wallet matches exactly one
trader, so two holders of near-identical size leave both unnamed rather than
guessing. Rounding is tolerated because FOMO truncates `humanAmount` for
display.

The rule was validated against a known-good pair before shipping: `/token` had
already named `8f39Xh…tsEr` as @Quanterty from the cache, and `/hodlers/top`
independently reports Quanterty holding 16,682,532.40 of the same mint. Against
the live capture, 48 published holders named every FOMO wallet in the top ten.

A failed or slow holder lookup returns `{}` and the card renders exactly as
before. Dev holders get a 🛠️ marker.

### Adoption — the holder list as a wallet resolver

Every confident match is also a wallet→handle mapping, so `/token` now feeds
the wallet cache. This is the cheapest identity source in the project: no
sponsor index, no mint scan, no block route — FOMO states the position and the
chain says who holds it.

A cached wallet is permanent and is trusted by `/fomo` and `/wallet`, so
adoption is gated harder than display:

1. **Separation.** `confident_matches()` additionally requires the runner-up
   balance to be `CACHE_SEPARATION` (50) tolerances away. A neighbour close
   enough to raise doubt is still labelled on the card — that is reversible —
   but never written.
2. **Corroboration.** `WalletResolver.adopt_holder_matches()` requires the
   wallet to have co-signed a FOMO-sponsored transaction, the same bar
   `_resolve_from_balances` applies to a single balance fingerprint. A whale
   who merely happens to hold the matching amount fails it.
3. **No overwrites.** An existing mapping wins; a disagreement is logged and
   skipped, and a wallet already claimed by another handle is refused. Both
   checks run before the RPC call, so a re-run of the same token costs nothing.

Records land as `walletSource: hodlers+amount+fomo-sponsor` with the token that
produced them in `hodlerToken`. Adoption runs as a background enrichment task,
so the token card never waits for it, and `FOMO_ADOPT_HOLDER_WALLETS=0` keeps
the naming while turning off persistence.

### Not yet used

The holder rows also carry `value`, `pnl`, `unrealizedPnl`, `realizedPnl`,
`costBasis`, `averageEntryPrice` and `averageHoldTimeSeconds` — everything
FOMO's own Holders table shows. `/token` uses only the handle and the dev flag.

Worth considering: `/hodlers/top` accepts an array of tokens, so a deliberate
cache-warming pass over trending mints would resolve many handles per call.

Verification: 133 conventional unit tests (19 hodlers, 25 response, 27
features, 32 diagnostic, 22 wallet, 3 API + others), the standalone offline
Solana suite and `py_compile` pass.

## Session 29 — Wide buys match sells, and an open position book

Wide's Latest buys were the only activity list still numbered `1. 2. 3.` with a
plain bold ticker, while sells and theses used a colour marker and a Padre link.
Buys now render `🟢 [$TICKER](padre) · USD · MC · chain · relative`, identical in
shape to the sell rows. `_token_link()` is the single constructor for a linked
ticker and is shared by buys, sells and the new position rows; a chain Padre
cannot route (Robinhood) still falls back to a bold ticker rather than a dead
link.

`Open positions` is a new Wide field listing the trader's active book, largest
position first: linked ticker, average entry, size in tokens and USD, and PnL
with ROI. `fomo_features.open_positions()` derives it from the `activeTrades`
rows already fetched for the profile, so the field costs no extra request.

Two deliberate choices in that derivation. PnL is per unit --
`amount x (current - entry)` -- not `value - totalCostBasis`, because that basis
covers units already sold and would report a half-closed winner as a loser. And
prices use a new `fmt_price()` rather than `fmt_usd()`, which rounds to cents and
renders every memecoin entry as `$0.00`; `fmt_price` keeps four significant
digits below a cent. A position FOMO has not priced lists with `⚪` and `PnL —`
rather than an invented number.

Rows are packed through `_fit_field()` against Discord's 1,024-character field
limit -- the same failure that truncated holder rows in session 14, except an
over-long field rejects the entire message.

Verification: 102 conventional unit tests (27 features, 20 response, 32
diagnostic, 15 wallet, 3 API + others), the standalone offline Solana suite and
`py_compile` pass.

## Session 28 — exportable diagnostic summary

Running `fomo_resolve_diag.py` over a batch of handles produced a summary whose
wallets were truncated to ten characters, which is useless for the thing a batch
run is for: collecting the addresses. The summary now prints full addresses in
aligned columns, shows `[stage]` in place of a missing wallet, and reports how
many handles missed. `--csv PATH` exports the same rows as
`handle, solana, solana_status, evm, evm_status, error` for a spreadsheet or a
tracker; `--json` still carries the whole per-stage report. Single-chain runs
emit only that chain's columns.

Verification: 55 conventional unit tests (37 diagnostic, 15 wallet, 3 API), the
standalone offline Solana suite, and `py_compile` pass.

## Session 27 — the block route is no longer opt-in

`/fomo 397397` returned an EVM wallet and no Solana one, while
`fomo_resolve_diag.py 397397 --deep` resolved
`5dB6rj9CoXMLQCAymoC5UXCb1LtFjbM5rbut3MNuj9Q` in 35 RPC calls. Nothing had
regressed: `fomo_bot.py` constructs `WalletResolver(self._http, SOLANA_RPCS)`,
whose `deep` defaulted to `False`, so the embed path has never run the block
route. What changed is FOMO, not the code.

Both cheap routes are capped at `MAX_SIG_PAGES * 1000` = 12000 signatures. That
cap is a moving target: it buys less and less wall-clock time as FOMO's
throughput grows. For this handle the sponsor index stopped 12000 signatures
back, short of a swap barely a day old, and the mint route hit the same wall on
all four picked mints. The balance fallback then found 6 exact fingerprints but
no unambiguous owner. The block route reads the chain at `createdAt` and depends
on no account's signature history, so it is the only route FOMO's growth cannot
outrun — and it matched in slot 440184488 on the first swap it was given.

`WalletResolver` now tries all three routes per swap, cheapest first, before
moving to the next swap -- and allows the block route on the newest
`FOMO_WALLET_DEEP_ATTEMPTS` (default 2) picks only. A two-pass shape was tried
first and rejected: running every cheap route across every swap before any
block scan pays four full mint scans (12 pages of 1000 signatures each) before
reaching the one route that can still answer, which is precisely the wrong
order for a handle already known to be behind the cap. Swaps past the bound
still get the cheap routes, since a quiet mint is cheap and might still hit.

`deep` now defaults to `FOMO_WALLET_DEEP` (default 1) instead of `False`;
passing `deep=False` still forces the old behaviour, and that case now says so
in its log line. The cache records the winning route as
`walletSource: fomo-sponsor | fomo-mint | fomo-blocks`.

Cost is bounded and one-time: a resolution that would previously have failed
now spends a slot search plus a `getBlock` per slot on at most two swaps, and a
resolved handle is cached forever. A handle that resolves via sponsor or mint
pays nothing extra — pass 2 never runs.

`fomo_resolve_diag.py` follows the same default: `--deep/--no-deep` now defaults
to `FOMO_WALLET_DEEP`, so the diagnostic reproduces the bot rather than
diverging from it, and it prints a `routes` line naming which routes will run.

### Known operational limit

`_enrich_fomo_message()` wraps enrichment in `asyncio.wait_for(...,
FOMO_ENRICH_TIMEOUT)`, which **cancels** the coroutine on timeout and then reads
whatever reached the cache. A block scan that runs past the 20s default is
therefore discarded rather than finished, and the next `/fomo` starts it over --
the handle never converges. Raising `FOMO_ENRICH_TIMEOUT` is the one-line
mitigation. The structural fix is to let resolution run as a detached task that
survives the card's edit budget, so the cache is written even when the edit
deadline passes; not implemented.

Regression coverage: the block route reaches the newest swap without waiting on
the others, is bounded to the newest N picks while older ones keep the cheap
routes, is skipped entirely on a cheap-route hit, and the disabled case names
`FOMO_WALLET_DEEP` in its log. Verification: 50 conventional unit tests (15
wallet, 32 diagnostic, 3 API), the standalone offline Solana suite, and
`py_compile` all pass.

## Session 26 — one-command wallet-resolution triage

`/fomo` hides every resolution failure: an unresolved handle shows no wallet
line, and the reason is only ever written to the bot's own log at INFO/DEBUG.
There was no way to ask "why did this handle not get a Solana or EVM wallet"
without reading the running bot's log or reaching for `evm_diag.py`, which
answers only the EVM half and needs a known-correct wallet to trace.

`fomo_resolve_diag.py` takes one or more handles and prints a per-chain report:
cache state, RPC/provider configuration, how much usable evidence FOMO actually
returned, which swaps discovery picked, and a verdict naming the stage that lost
the wallet plus the fix. It drives the same calls the bot's enrichment path
makes — `WalletResolver.resolve()`, `WalletResolver.resolve_from_balances()`,
`EvmWalletResolver.resolve()` — and installs a temporary handler on the `fomo`
logger, so the resolvers' own explanations are captured rather than
reimplemented and the tool cannot drift from the bot.

Stages: `config`, `rpc`, `panels`, `evidence`, `discovery`, `transfers`,
`ranking`, `deployment`, `balances`. Two facts the logs never carried are
computed locally: the swap window broken down by why each row was rejected
(`non-Solana mint (EVM contract)`, `networkId 56 (bsc)`, `no usable token leg`),
which separates an EVM-only trader from a broken panel; and which chains have
the Alchemy endpoint `alchemy_getAssetTransfers` requires, since Blockscout does
not cover BSC.

    python fomo_resolve_diag.py Rowdy
    python fomo_resolve_diag.py Rowdy frankdegods --fresh
    python fomo_resolve_diag.py Rowdy --chain solana --deep
    python fomo_resolve_diag.py Rowdy --details -v
    python fomo_resolve_diag.py Rowdy --json hunt_out/diag_rowdy.json

Exit code 0 when every requested chain resolved, 1 when any did not, 2 on a
setup error. `evm_diag.py` remains the microscope for the EVM half once triage
points at `discovery`.

Regression coverage is 30 offline tests over the pure classification: swap
rejection reasons, exact vs aggregate EVM evidence counting, Alchemy provider
detection, and every verdict rule. Verification: `test_fomo_resolve_diag` (30
tests) and `py_compile` pass in a sandbox with no network egress; the live path
still has to run on borz per §5.

## Session 25 — global-only Discord commands

The bot no longer copies or syncs its command tree as guild-specific commands.
`setup_hook()` always performs one global sync, and `DISCORD_GUILD_ID` was
removed from runtime configuration and `.env.example`.

Older versions already registered duplicate guild commands in Discord, so
simply stopping future copies was insufficient. On the first `on_ready()` of a
process, the bot now clears the local guild command tree and syncs that empty
tree to every connected guild. This deletes the legacy server-specific entries
while leaving the global command tree untouched. Successful cleanup is retained
for the process; an HTTP failure is logged and retried on a later ready event.

Regression coverage verifies the cleanup performs only `clear_commands(guild)`
and `sync(guild)` and never copies globals into a guild. Verification: 78
conventional unit tests and the standalone offline Solana suite pass.

## Session 24 — Compact wallet querying state

The initial Compact `/fomo` card now renders `Linked wallets: Querying ⏳` when
either enabled wallet resolver still has work to do. The pending state is
explicitly passed through the initial renderer and enrichment task. Completion
forces one final Compact edit even when neither resolver found a wallet, so the
card cannot remain stuck on Querying; it changes to the verified wallet lines
or the final no-wallet result. Wide rendering is unchanged.

Regression coverage verifies both the pending placeholder and its empty-result
completion. Verification: 77 conventional unit tests and the standalone
offline Solana suite pass.

## Session 23 — wallet-address Pump profile links

Every Pump profile URL is now constructed from the profile's full Solana wallet
rather than its username. `pump_api.pump_profile_url()` is the single URL
constructor, and `PumpUser.profile_url` delegates to it. When Discord displays
a Pump username with a wallet, both the `@username` and readable shortened
wallet are clickable and target the profile URL containing the complete wallet
address.

The rule is applied to the main `/pump` card, `/wallet`, `/pumpwallet`, token
holder identity rows, `/pumptracked`, and Pump tracking alerts. Holder rows do
not repeat the explorer wallet when the linked Pump wallet is the same address.
No active Python path constructs `pump.fun/profile` from a handle or username.

Regression coverage checks the model URL, main profile card, holder-style
identity row and tracking alert. Verification: 75 conventional unit tests, the
standalone offline Solana suite and syntax compilation all pass.

## Session 22 — interactive Compact and Wide `/fomo` layouts

`/fomo <handle>` now responds with a requester-only Discord selector before
performing the lookup. Its two options are visually labeled and described:
Compact contains essential profile information only, while Wide contains the
full profile with all available information. Other users cannot operate the
requester's selector, and the selection message reports generation progress.

Wide delegates to the original `build_embed()` function unchanged. Compact has
a separate strict renderer containing only the profile picture, display name
and handle, Social, Strategy, Portfolio, linked X/Twitter account, and one
combined Linked wallets field. It omits the bio, clan, best trade, recent
buys/sells/theses, PnL ranks, FOMO link field, privacy/join metadata and footer.

Both choices call the same `_generate_fomo_profile()` fetch and enrichment
pipeline. The chosen layout is passed only to the initial render and subsequent
wallet-enrichment edit, so Compact cannot turn into Wide when a background
wallet arrives. Verification: 72 conventional unit tests, the standalone
offline Solana suite and syntax compilation all pass.

## Session 21 — FomoScan dependency removed

The unofficial Railway-hosted FomoScan identity service has been removed from
the runtime. The bot no longer configures or calls its `/get-user/{handle}`
endpoint and no longer contains its retry, fallback-URL or circuit-breaker
logic. `FOMOSCAN_PUBLIC_URL`, `FOMOSCAN_FALLBACK_URLS` and
`FOMOSCAN_COOLDOWN_SECONDS` were removed from `.env.example`.

FOMO EVM support remains enabled. Resolution now uses, in order: the permanent
local cache, corroborated transaction-backed evidence, and exact current-token
balance matching. Operators can still deployment-check and cache an
independently verified mapping with `evm_resolve.py --handle HANDLE --wallet
0x...`. Running that helper without `--wallet` only displays an existing cached
mapping. Existing cached wallets are retained and require no external identity
service.

Active documentation and regression coverage now describe the on-chain-only
resolver. A no-evidence regression proves the resolver returns without making
an identity HTTP request, while cached mappings continue to resolve with zero
network calls. Verification: 68 conventional unit tests, the standalone
offline Solana suite and syntax compilation all pass.

## Session 20 — mixed-chain Solana safety and deterministic EVM matching

After the supported-chain header exposed the complete FOMO feed, the Solana
resolver began receiving EVM contracts. It passed those `0x...` addresses to
`getSignaturesForAddress`, where Solana providers correctly returned JSON-RPC
`-32602 Invalid param`. The RPC wrapper treated that caller error as a provider
outage, tried every endpoint and then unnecessarily paused discovery for 15
seconds. A separate EVM failure occurred when equally close transfer matches
caused Python to compare two non-orderable `EvmTransfer` objects while sorting.

`fomo_wallet.py` now admits only valid base58 Solana mints and, when chain
metadata is present, requires Solana network ID `1399811149`. The filter is
applied when choosing evidence, resolving a wallet and verifying cached
wallets. Older fixtures and payloads without network metadata remain supported
when their mint is a valid Solana address. JSON-RPC `-32602` now raises the
dedicated `RpcInvalidParams` error immediately: it does not fail over to healthy
providers and does not activate the all-provider circuit breaker.

`fomo_evm.py` now sorts nearby transfer candidates using scalar fields only:
time delta, creation time, transaction hash and wallet. Equal timestamps are
therefore deterministic and can no longer compare `EvmTransfer` instances.

Regression coverage verifies mixed-chain filtering, invalid-parameter handling
without failover/cooldown, and tied EVM candidates. Verification after both
recent sessions: 65 conventional unit tests and every standalone
`test_offline.py` Solana regression pass. `git diff --check` reports only the
repository's existing CRLF warnings.

### Next recommended optimization — remove the duplicate 50-swap request

The core `/fomo` path already fetches `/swaps?limit=50` in
`FomoClient.profile_panels()` and retains it as `TraderStats.raw_swaps`.
Background `WalletResolver._resolve()` currently requests that same uncached
endpoint again. This does not affect time to first result, but the duplicate,
now larger multi-chain response delays enrichment behind the shared background
browser lock and adds avoidable FOMO API pressure. The low-risk next change is
to pass `stats.raw_swaps` into `WalletResolver.resolve()` and fetch only when no
raw swaps were supplied. This optimization has been recommended but not yet
implemented.

## Session 19 — complete FOMO chain coverage and live Ouroboros proof

The live `0xOuroboros` diagnostic found that the FOMO frontend always sends
`x-supported-chains: 1,56,143,4663,8453,1399811149`. The bot omitted this
header, so FOMO defaulted its API responses to Solana and no amount of Helius,
Alchemy or explorer fallback could discover EVM evidence that never entered the
application. Without the header the profile returned 41 Solana swaps and zero
EVM evidence. With it, the same request returned a 50-item mixed-chain window,
23 open/closed trades across Ethereum, BSC, Robinhood, Base and Solana, and 70
EVM evidence items.

The three supplied contracts were confirmed in 49 evidence items:

- Ethereum: `0xe172e9b6cfbeeb5593bdce3f077356fdb33af904`
- BSC: `0x4e8fc9e5a6d2b9c6e7ca8b923661ca4e78087777`
- Robinhood: `0xb9972ca7188e511174947e3936a5315ac7073277`

FOMO's spotlight, trade lists and trade detail endpoint expose the same wallet,
`0xb089d6ac26e0fe26e1a3a5076e4feaaf4d797180`, for profile user ID
`d5b00d6a-3881-5ba0-805b-25bfa0371932`.

A single source of truth now lives in `fomo_chains.py`. Both the direct HTTP
headers in `fomo_api.py` and browser-page requests in `fomo_browser.py` send the
complete supported-chain list plus JSON content negotiation; browser requests
continue to attach authorization when available. Regression tests cover both
transports and prove mixed-chain Ouroboros evidence survives a Solana-heavy
50-swap window. A live smoke test through the modified client returned chains
1, 56, 4663, 8453 and 1399811149 and all three target contracts.

## Session 18 — transaction-backed EVM identity discovery

Profiles missing from FomoScan could only discover an EVM wallet from a current
token balance. That failed after positions were sold and whenever the true
wallet was outside the bounded public holder pages. The supplied `0xOuroboros`
contracts also proved the trades span Ethereum, BSC and Robinhood while the
identity index returns 404.

`TraderStats` now retains the already-fetched raw trades and swaps. For an
uncached identity, the bot selects up to six low-liquidity/older EVM trades and
batches `/trades/{id}` through the background browser page. `fomo_evm.py`
extracts exact buy/sell fingerprints, reads token transfer history through the
configured Alchemy endpoints with Blockscout fallback, and matches chain,
direction, timestamp and token amount. Stablecoin value is checked when the
transaction exposes it. A wallet is accepted only when one unambiguous address
explains at least two independent transactions and has deployed code on an
evidence chain. The cache records `evmSource: transactions+rpc`, confirmation
count and evidence tokens. Balance matching and FomoScan remain fallbacks.

Solana discovery now runs concurrently with the complete `EVM wallet -> EVM
activity` branch after the core profile card is sent. EVM buys and sells start
as soon as the EVM wallet is available and no longer wait for Solana. The cache
update sections are synchronous and merge the current entry, so the two wallet
results preserve one another.

Live provider validation confirmed `alchemy_getAssetTransfers` works without a
known wallet for the supplied Ethereum, BSC and Robinhood token contracts.
Regression coverage accepts two corroborating transactions, rejects one-off and
split-wallet evidence, verifies RPC failover, detail batching/background
routing, proves both wallet branches start concurrently, and proves EVM
activity does not wait for Solana. Verification: 59 unit tests plus the
standalone offline Solana suite pass.

## Session 17 — batched foreground FOMO panels

After the two-stage response removed wallet and explorer work from time to first
result, the remaining core profile path still launched balances, spotlight,
trades and swaps with `asyncio.gather` but serialized all four calls behind
`BrowserTransport`'s single page lock. Four tracked profiles also generated
eight uncached browser calls every five seconds through that same lock.

`FomoClient.profile_panels()` now applies the existing per-path cache first and
sends all remaining core panel URLs through one `BrowserTransport.get_many()`
operation. The page executes them concurrently with `Promise.all`, while every
response still passes through the existing FOMO auth, envelope, error and cache
logic. Non-browser transport retains an `asyncio.gather` fallback.

The browser transport now has independent foreground and background pages and
locks inside the same authenticated persistent context. Tracker polls and the
post-response Solana wallet lookup use the background lane; `/fomo` profile
panels use the foreground lane. Background traffic therefore cannot queue ahead
of a user lookup, while each lane remains internally serialized to avoid tab
churn and overlapping tracker polls.

Regression coverage verifies one four-URL foreground batch, cache reuse,
background routing and page separation.

## Session 16 — two-stage `/fomo` response latency

`/fomo` previously waited for optional Solana wallet discovery, its Helius
balance fallback, EVM identity discovery and multi-chain EVM activity before
sending the Discord embed. A cold or unresolved identity could therefore hold
the entire profile behind many RPC/explorer calls even though the core FOMO
panels were already available.

The command now fetches and sends the core FOMO profile first, including any
permanently cached Solana/EVM wallets. It retains the returned Discord webhook
message and completes missing wallet discovery plus EVM activity in a bounded
background task, editing the same card only when enrichment adds data. The
default background deadline is 20 seconds and is configurable with
`FOMO_ENRICH_TIMEOUT`. Timed-out tasks preserve any identity already written to
the cache, never delay the visible profile, and are canceled cleanly during bot
shutdown. `FomoBot` keeps strong references to active enrichment tasks so they
cannot be garbage-collected before completion.

Regression coverage verifies that successful enrichment edits the existing
card with both wallets and that a deadline returns cleanly without an edit when
no partial result exists.

## Session 15 — sell-aware FOMO Solana wallet discovery

`/fomo frankdegods` returned the verified EVM wallet but no Solana wallet. The
production timestamps explain the immediate miss: every configured Solana RPC
failed during the Solana discovery phase, then EVM resolution completed. There
was no cached Solana result to display. A second general gap made recovery less
reliable for profiles whose recent FOMO window is sell-heavy: the fallback
always indexed `outTokenAddress`/`outHumanAmount`, which is the distinctive
token leg for buys but usually SOL/USDC for sells.

The user-supplied wallet
`498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ` was independently verified on
chain. Transaction
`2pGKeXzMddZcGZxcbUoLStKhgAyWHS7Mm9Qqh1p1iLrVVWZ5AYm7tXyGsBzpQffm5bUGTkXa8ubHxvnJUaUoajLj`
occurred at `2026-08-19T01:47:44Z` (03:47 Amsterdam), reduced that wallet's
`zj1jpp...BF8ry2k` balance by `7,451,794.455427`, and has exactly FOMO sponsor
`AgmL...N51` plus the supplied wallet as signers. Two earlier buys of the same
mint at 00:40/00:42 UTC have the same signer pair. The mapping is cached with
three confirmations while preserving Frank's existing EVM record.

Wallet discovery is now direction-aware: buys search the non-quote output token
and positive balance delta; sells search the non-quote input token and negative
balance delta. Strict swap matching and loose amount fallback both honor this
direction. `pick_swaps` still prioritizes buys but no longer discards sells when
any buy exists, and the default FOMO swap window increased from 25 to 50. A new
Solana mapping merges into an existing cache record rather than overwriting its
EVM wallet. These are general changes for every FOMO handle, not a Frank-only
rule.

Verification: 43 unit tests, the standalone offline wallet suite,
`py_compile`, and `git diff --check` pass. `/wallet
498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ` now reverse-resolves to
`@frankdegods` with three on-chain confirmations.

## Session 14 — Windows save, Discord field and RPC-pressure fixes

A production log exposed three independent failure modes. Tracking writes used
the fixed name `fomo_tracks.json.tmp`, which can collide with another save or a
short Windows file lock and caused `[WinError 5] Access is denied`. The store
now creates a unique same-directory temporary file, flushes it, retries a
transient `PermissionError` with short exponential delays, and atomically
replaces the destination. It also compares the serialized state with the last
successful write, so unchanged 1-second Pump polls no longer rewrite and fsync
the JSON file.

Ten identity-rich `/token` holder rows could exceed Discord's 1,024-character
field limit. Complete rows are now packed into as many holder fields as needed,
each guaranteed to fit the Discord limit. This retains all requested top-ten
holders instead of truncating the card.

Solana provider exhaustion was being amplified by duplicate subscriptions and
immediate retries. `PumpChainClient` now shares signature responses for the
same wallet for 0.9 seconds and opens a 2–30 second exponential circuit after
all configured RPCs fail. FOMO wallet discovery now serializes provider probes,
checks every configured endpoint once, then pauses new discovery for 15 seconds
instead of running eight long retry rounds per concurrent lookup. Cached
wallets and normal FOMO/Pump profile data remain available during the pause.
The configured 5-second FOMO and 1-second Pump intervals are unchanged.

Verification: 42 unit tests, the standalone offline wallet suite, `py_compile`,
and `git diff --check` pass. Regression tests cover the Windows replace retry,
no-op tracking saves, Discord holder field splitting, and duplicate Pump wallet
RPC coalescing.

## Session 13 — unified management and token intelligence

Three public command capabilities were added. `/wallet <address>` now searches
both services: verified FOMO Solana/EVM cache mappings, Pump Solana profiles
live, and discovered Pump EVM mappings through `pump_evm_cache.json`. A miss is
explicit about the identity-index limitation; it never guesses from matching
usernames.

`/untrack` is a private multi-select containing both FOMO and Pump subscriptions
for the current channel. It can remove one or several profiles without an exact
handle. `/tracksettings` first selects an existing FOMO/Pump subscription, then
opens the appropriate three-choice activity menu. `TrackingStore` now updates
`activityFilters` in place, preserving all signature/trade/callout baselines so
changing a filter cannot replay older activity. The old service-specific
untrack commands remain compatible.

`/token <address> [holders]` accepts Solana or EVM token addresses, with a
Discord Top 5/Top 10 choice. `token_intelligence.py` selects the most liquid
DexScreener pair for metadata, image and market cap. Pump coin metadata is the
fallback for fresh Pump mints. Solana holders use `getTokenSupply`,
`getTokenLargestAccounts`, and parsed `getMultipleAccounts` to turn token
accounts into owner wallets and coalesce duplicate owners. Ethereum/BSC/Base
use CoinMarketCap's public holder index; Robinhood uses Blockscout. Holder rows
show balance and ownership percentage where available, link to the correct
explorer, and annotate verified cached FOMO/Pump identities. Pump Solana
profiles are additionally resolved live, so they do not have to be tracked.
FOMO identities not yet present in the verified wallet cache cannot safely be
reverse-discovered and remain plain wallets.

Verification: 38 unit tests pass, `py_compile` passes, and importing the live
Discord command tree registers all 15 commands including `token`, `untrack`,
`tracksettings`, and the expanded `wallet`. A live DexScreener lookup returned
Solana USDC metadata, and a live current Pump mint returned 15 largest token
accounts whose parsed owners and percentages were successfully decoded. Very
large tokens such as Solana USDC can exceed a provider's
`getTokenLargestAccounts` account-scan ceiling; the command degrades to an
unavailable holder panel while retaining token metadata.

## Session 12 — per-subscription activity filters

Tracking scheduling is split into two independent, non-overlapping tasks.
`FOMO_TRACK_INTERVAL` now has a 1-second code minimum and is configured to 5
seconds. New `PUMP_TRACK_INTERVAL` has a 0.25-second code minimum and is
configured to 1 second. If the Pump variable is absent, it falls back to the
old FOMO interval for backward compatibility. Each task sleeps after its prior
poll completes, so a slow provider increases that service's effective interval
without spawning concurrent duplicate polls. Startup logs both values.

The FOMO handle `change` is activity-verified to Robinhood Chain smart account
`0x92bff72e6c943a1348aedf15d68e758e811dec64` and cached with source
`activity+rpc`. The account is a deployed `Simple7702Account`. Six supplied
STONKBROKER/SMOOVIE trades were matched by timestamp and direction against its
public transfer history; decoding the matched ERC-4337 `handleOps` batch was
required to separate the smart-account sender from the bundler and routers.
The normal EVM activity reader reproduces the supplied buy/sell values within
live-price and fee differences.

`change` is also mapped to Solana wallet
`J9WiAZKf8JnCkHFL8fLCCXdEgdoLjLRqU2EGsDjdqYga`. The user-supplied candidate
was independently checked through Solana RPC: the account exists, has at least
100 recent signatures, and four sampled transactions are signed by that wallet
with FOMO's known gas sponsor as fee payer. The cache records four confirmations
with source `manual+fomo-sponsor` while preserving the Robinhood mapping.

Tracking card values now use the chain's native currency instead of dollars:
SOL for Solana, ETH for Ethereum/Base and BNB for BSC. FOMO swap events retain
the exact `inHumanAmount`/`outHumanAmount` when the native asset was actually
swapped; stablecoin-denominated events and new-position cost bases use live
wrapped-native USD prices. Pump/PumpSwap values use exact SOL quote amounts or
convert supported stablecoin quotes with Pump's SOL price. Market cap remains
in USD. A missing price renders `— SOL/ETH/BNB` rather than silently reverting
to a dollar amount.

Activity selection belongs to tracking, not profile/wallet lookup. After
`/fomotrack <handle>`, an interactive multi-select offers Buys, Sells and
Theses. After `/pumptrack <handle|wallet>`, it offers Buys, Sells and Callouts.
Users may select any one, two or all three. The selected list is persisted per
channel subscription and displayed by the public `/fomotracked` and
`/pumptracked` lists. Legacy single-choice and no-filter records still load;
no-filter records safely default to all activity.

Polling continues to observe and baseline every event before applying the
delivery filter. This means switching a subscription's filter cannot replay
older hidden activity. Re-running either tracking command refreshes its
baseline and replaces its filter. `/fomo` and `/pump` are simple lookup commands
again and have no activity parameter; `/pump` no longer performs unnecessary
recent-transaction decoding during a profile lookup.

## Session 11 — EVM outage recovery and profile data

FOMO profile stats expose the three newest sell and thesis events alongside the
existing market-cap-enriched buys. The profile lookup renders all available
recent information without requiring a filter choice.

FomoScan's prolonged Railway 503 was still producing three `httpx` request
lines and a warning on the first cache miss. The default is now one probe per
outage window, followed by 15 minutes of cache-only operation. The single
status is informational rather than a warning. `httpx`/`httpcore` INFO request
logging is suppressed because query-string RPC credentials were being printed
verbatim; the bot's own endpoint diagnostics remain sanitized.

The outage exposed a functional gap: uncached FOMO users lost EVM enrichment
and on-chain EVM activity. `fomo_evm.py` now discovers the wallet directly from
FOMO's open-position balance data using the same exact-holder technique proven
for Pump: holder-index uniqueness, live ERC-20 `balanceOf`, and deployed smart
wallet code are all required before caching. FomoScan is only the fallback.

`fomo_evm_activity.py` now merges buys and sells across Ethereum, BSC, Base and
Robinhood. Ethereum/BSC use the configured Alchemy transfer history plus live
transaction receipts; Base/Robinhood use Blockscout transaction transfer
details. Stablecoin pairing excludes airdrops. A live smoke test on Collectible
returned real Ethereum, BSC and Robinhood buys plus Robinhood sells, including
the two previously verified WALL3 buys. The full unit count is now 32.

## Session 10 — FomoScan outage isolation

The Railway-hosted FomoScan index returned live HTTP 503 while the corresponding
fomo.family profile remained HTTP 200. RPC fallback cannot solve this stage:
the index maps a human handle to an EVM address, while RPCs can only verify an
address after it is known. The public FomoScan Chrome extension was inspected;
it uses the same Railway `/get-user` and `/verify-user` backend and exposes no
second public host or local arbitrary-profile EVM derivation.

`EvmWalletResolver` now accepts an ordered primary plus
`FOMOSCAN_FALLBACK_URLS`, retries temporary 429/500/502/503/504 responses, and
opens a 60-second circuit breaker when every configured index is unavailable.
During that interval it operates cache-only and emits one outage warning rather
than delaying and logging a 503 for every `/fomo` request. Successful mappings
remain permanently cached. Tests cover primary-503/backup-success and repeated
503 circuit suppression. This makes `/fomo` independent of a brief outage, but
true zero-dependency availability still requires operating a self-hosted mirror
of the identity index.

## Session 9 — private RPC failover and Pump balance confirmation

Private Alchemy HTTP fallbacks are stored only in ignored `.env` for Solana,
Ethereum, BSC and Robinhood Chain. `rpc_config.py` parses ordered primary plus
comma-separated fallback lists and redacts key-bearing URL paths from logs.
Both Pump Solana tracking and FOMO Solana wallet derivation now try the next RPC
automatically after an HTTP/RPC failure or rate limit. FOMO EVM deployment
checks likewise fail over per chain.

Pump EVM discovery no longer trusts the holder index alone. After the unique
indexed balance match, `pump_evm.py` reads ERC-20 `decimals` and `balanceOf`
from the candidate's chain RPC and caches the wallet only when the on-chain
balance independently matches Pump's public `amountHeld` fingerprint. Existing
cache files remain compatible and are upgraded on the next `/pump` lookup when
their optional `verified_onchain` field is absent.

The four supplied HTTP endpoints returned the expected live networks (Ethereum
1, BSC 56, Robinhood 4663 and Solana core 4.2.1). All four supplied WSS URLs
completed secure handshakes. WSS values are configured for a future realtime
listener, but current tracking deliberately remains polling-based. Verification:
26 unit tests, the standalone offline suite, `py_compile`, and a live
`1000XCryptoD` discovery with `verified_onchain=True` all pass.

## Session 8 — Pump.fun profiles and tracking

The same Discord workflow now supports Pump.fun without changing the existing
FOMO commands. New public commands are `/pump`, `/pumpwallet`, `/pumptrack`,
`/pumptracked` and `/pumpuntrack`.

`pump_api.py` isolates Pump's public website APIs. It resolves either a handle
or wallet to the canonical profile, and loads portfolio totals, holdings/PnL,
created coins, callouts and USD coin market caps. Market-cap cards deliberately
use `usd_market_cap`/`market_cap_usd`; Pump's plain `market_cap` may be expressed
in the quote asset. Empty holdings, callouts and created-coin lists degrade to
empty panels rather than failing the profile command.

Callout payloads contain only `coinMint`, not a symbol. `/pump` now batch-loads
coin metadata for every displayed callout and keys it by mint. This fixes the
old `$TOKEN` fallback for callouts that were no longer among the user's current
holdings or created coins.

`pump_evm.py` now discovers the separate Pump EVM wallet without using a
username/X guess or a private Pump route. It reads the profile's public EVM
positions, takes an exact `(chain, token, amountHeld)` balance fingerprint and
matches it against a public current-holder index. JSON-number rounding is the
only tolerated difference and ambiguous matches are rejected. Ethereum, BSC
and Base use CoinMarketCap's documented keyless holder endpoint; Robinhood and
other supported chains fall back to paginated Blockscout holders. Successful
matches are persisted in `pump_evm_cache.json`, displayed by `/pump`, and can
be reverse-resolved by `/pumpwallet` after discovery.

The method was blind-validated on `1000XCryptoD`: Pump reported
`12,686,197.783044254` units of BSC token
`0x311cdbc8fbe3e5e04602aa688316efca5d327777`; the 100-address indexed holder
set produced exactly one match,
`0x1160079f1463dc5f9f20b1f1b9cf628718649c18`, whose live on-chain balance was
`12,686,197.783044255065638262`. The supplied address was not used as an input
to discovery.

Buy/sell alerts do not depend on Pump's private activity tab. `pump_chain.py`
polls the configured `SOLANA_RPC`, baselines wallet signatures, decodes the
official bonding-curve `TradeEvent` and PumpSwap `BuyEvent`/`SellEvent`, filters
events to the tracked wallet and derives the PumpSwap base mint from that
wallet's exact token balance delta. Current V2 quote-mint fields, USDC quotes
and legacy native-SOL events are supported. Event IDs are transaction signature
plus log index; callouts use Pump's `calloutId`.

`pump_tracks.json` is separate from `fomo_tracks.json`. `/pumptrack` baselines
both signatures and callouts, including a recovery flag when either source is
temporarily unavailable, so it never replays old history after recovery.
Alerts retain the compact `$value · MC $cap` design and Padre Solana links.
`PUMP_MIN_TRADE_USD=0` alerts every decoded trade; a positive value filters
known USD amounts.

Verification on 2026-08-18:

- 24 FOMO/Pump unit tests and the standalone offline transaction suite pass;
- `py_compile` passes for the new resolver and bot integration;
- live handle-only resolution reproduced all six supplied EVM wallets exactly:
  `sapphy`, `zinc`, `flatstarfish086`, `FlippingProfits`, `1000XCryptoD` and
  `Uniswapvillain` (Base, BSC and Robinhood Chain);
- a production smoke test resolved `thedetective`, loaded its portfolio,
  holdings and callouts, then decoded 8 real PumpSwap trades from its latest
  20 Solana transactions.

The working tree is not committed or deployed. Restart the standalone bot to
sync the five new commands.

Session 2 solved the Solana wallet problem. Session 3 fixed hot-mint discovery.
Session 4 added verified EVM wallets. Sessions 5-6 expanded the profile,
tracking, entry market caps and Robinhood Chain activity. Session 7 redesigned
tracking alerts as rich, chain-aware activity cards.

**Current status:** `/fomo` shows an expanded profile with verified Solana/EVM
wallets, portfolio value, best trade, three cross-chain latest buys and ranked
PnL. `/fomotrack` sends separate green buy, red sell and purple thesis cards
with linked Padre tickers. The working tree changes are not committed or
deployed yet.

## Session 7 — tracking alert cards

The old `@handle activity` wall of one-line events was replaced with one focused
Discord embed per event:

- `🟢 @handle bought`, `🔴 @handle sold`, or `📝 @handle wrote a thesis`;
- a `$TICKER` link to `https://trade.padre.gg/trade/<chain>/<address>` for
  Solana, Base, BSC and Ethereum;
- exact swap value, network, provider, contract address, token thumbnail and
  the event timestamp;
- thesis text in its own field, with a purple card;
- aggregate cost basis is explicitly labelled as such for a new position and
  is never presented as the exact swap value.

Direction comes from FOMO's relationship fields: `outTradeId` is a buy and
`inTradeId` is a sell. A qualifying initial swap and its new trade row are
coalesced so Discord does not receive duplicate cards. The tracking baseline
now retains a bounded token metadata index, allowing later sells to keep their
ticker, chain and artwork after the original trade leaves the current API page.
Old `fomo_tracks.json` files migrate naturally on the next successful poll.

`/fomotrack` success confirmations are public. `/fomotracked` publicly lists
all subscriptions for the current channel, sorted by handle and linked to each
FOMO profile. Empty lists are public as well. Errors remain private, and
`/fomountrack` confirmations remain private.

`/wallet <address>` reverse-searches `wallet_cache.json` and returns the linked
FOMO handle publicly. It supports exact case-sensitive Solana addresses and
case-insensitive verified EVM addresses. The result includes verification
context and refreshes canonical handle/display/avatar data from FOMO when the
API is available. It deliberately returns no guess for wallets that have not
yet been verified and indexed through the existing resolvers.

Tracking embed timestamps normalize FOMO's trailing `Z` before creating the
Discord embed. Do not switch this back to `discord.utils.parse_time`: the
discord.py 2.4 implementation fails on these timestamps under Python 3.10.

Trade tracking cards use the compact layout: `@handle bought/sold $TICKER` in
the linked title, then `amount · MC market-cap`, the copyable contract, token
thumbnail and footer. Network, Route and Signal rows are gone. Market caps are
batched through the existing DEX Screener helper; failure degrades to `MC —`
without suppressing the alert. Thesis cards retain their separate text field.

Tests cover buy/sell/thesis classification, duplicate suppression, Padre URLs,
metadata retention and persistence. The full feature/EVM/Solana/offline suite
and `py_compile` pass. The local standalone `.venv` remains broken, so the
actual Discord render still needs a bot restart in the repaired environment.

## Session 6 — Robinhood Chain and current operational handoff

### Why Collectible only showed Solana

FOMO's `/v2/users/{id}/swaps` response is not a complete cross-chain history.
Collectible bought WALL3 (`0xc31d45...d243`) twice on Robinhood Chain, but
neither transaction appeared in that feed. FomoScan independently verifies
Collectible's EVM wallet as `0xfa2b3e...3111`; it is deployed on Base, BSC and
Robinhood Chain as the same EIP-7702 account.

`fomo_evm_activity.py` now reads Robinhood Chain Blockscout separately. It:

- reads incoming ERC-20 transfers to the verified wallet;
- loads all token transfers from the same transaction;
- requires a stablecoin or otherwise USD-priced input, excluding airdrops;
- uses the largest repeated router input instead of summing internal route legs;
- derives entry market cap as paid USD / tokens received × total supply;
- caches each wallet's result for 60 seconds.

Live verified Collectible output on 2026-08-18:

- WALL3 $5,688.55 at $3.83M, tx `0x1447f9...a3195`
- WALL3 $2,370.34 at $3.65M, tx `0x98a928...b2de`
- next valid Robinhood buy: PACK $99.51

The Robinhood and FOMO feeds are merged by ISO timestamp, de-duplicated by
chain/activity id and truncated to the newest three. Collectible's EVM wallet
is now cached in `wallet_cache.json`.

### Current `/fomo` layout

1. Social / Strategy / Portfolio
2. Best trade
3. Latest buys (three rows: amount, entry MC, chain, relative time)
4. Solana wallet
5. EVM wallet
6. PnL · leaderboard rank
7. Links (FOMO and X only)

Removed by request: trade/swap counts, win rate, generated PnL image card, and
Solscan/BaseScan/BscScan links.

### `/fomotrack` registration

The command is registered in `fomo_bot.py`. `setup_hook()` syncs global commands;
`on_ready()` also copies and syncs all commands into every connected guild once
per process, even when `DISCORD_GUILD_ID` is configured. Startup must log a line
like:

```text
synced 5 command(s) instantly to guild ...: fomo, fomotrack, fomountrack, ...
```

If it does not appear after restarting and refreshing Discord:

- confirm the running process uses this working copy;
- check Server Settings → Integrations → bot → Commands;
- the installing app needs `bot` / `applications.commands` scope;
- the member needs Use Application Commands in that channel;
- the bot needs View Channel, Send Messages and Embed Links for alerts.

Tracking persists to ignored `fomo_tracks.json`, polls every 60 seconds by
default, and alerts for FOMO trade IDs, swaps ≥ `FOMO_LARGE_SWAP_USD` (default
$1,000), and thesis comment IDs. **Open limitation:** the tracker still polls
FOMO trades/swaps only; Robinhood on-chain buys are merged into `/fomo` but are
not yet tracking alerts.

### Files changed in sessions 5-6

- `fomo_bot.py`: expanded layout, guild command sync, tracking commands,
  Robinhood merge.
- `fomo_features.py`: portfolio/best trade/three latest buys, entry MC, feed merge.
- `fomo_evm_activity.py`: Robinhood Blockscout parser.
- `fomo_tracking.py`: JSON subscriptions and change detection.
- `fomo_api.py`: uncached polling, trades endpoint and DEX Screener market data.
- `test_fomo_features.py`: Solana/Base/BSC and live-shaped Robinhood fixtures.
- `.env.example`, `.gitignore`, `README.md`, `FOMO_API.md`: configuration/docs.

### Verification and environment

- 13 unit tests pass across feature, EVM-wallet and Solana-wallet suites.
- `test_offline.py` passes all sponsor/mint wallet-resolution regressions.
- `py_compile` passes for all active bot modules.
- Live Robinhood parsing reproduced both Collectible WALL3 buys exactly.
- The workspace `.venv` is broken: it points at a removed Python 3.10 install.
  The shell Python used for diagnostics has httpx but no `discord.py`. Recreate
  the standalone venv and run `pip install -r requirements.txt` before launch.
- This bot must run on borz/residential internet because FOMO Cloudflare blocks
  the Vultr VPS. It is not the main `memebot.service`.
- The Helius key was pasted into chat during debugging; rotate it.

Run after recreating the environment:

```powershell
python fomo_browser.py --login
python evm_resolve.py --handle Collectible --fresh
python fomo_bot.py
```

## Session 5 — expanded cards and tracking

`/fomo` now fetches balances, spotlight, unsorted recent trades and up to 50
swaps. The single result adds portfolio value, best trade and the three latest
deduplicated buys across Solana, Base, BSC and other EVM networks. The generated
PnL image card, win rate, trade/swap counts and all explorer links were removed;
the verified wallet values remain visible. The leaderboard PnL block is last,
between the wallet fields and Links.

Each latest buy shows its full trade cost basis and market cap at entry. Direct
historical fields win when present; otherwise the resolver uses the trade's
weighted `avgEntryPrice` and DEX Screener's current price/market cap to infer
circulating supply and reconstruct entry market cap. Inferred values carry `~`.

FOMO's `/v2/users/{id}/swaps` feed did not include Collectible's Robinhood Chain
activity. `fomo_evm_activity.py` now reads the verified EVM wallet's ERC-20
transfers from Robinhood Chain Blockscout, requires a priced input in the same
transaction (so airdrops are excluded), and derives buy size plus entry market
cap from the token received. The feeds are merged chronologically before taking
three rows. Live proof for `Collectible` on 2026-08-18:

- WALL3 $5,688.55 at $3.83M, tx `0x1447f9...a3195`
- WALL3 $2,370.34 at $3.65M, tx `0x98a928...b2de`

`/fomotrack handle` persists a channel subscription in `fomo_tracks.json` and
baselines current IDs so old activity is not replayed. The background poller
alerts for newly observed trades, swaps at least `FOMO_LARGE_SWAP_USD`, and new
thesis comment IDs. `/fomountrack handle` removes the channel subscription.
Polling defaults to 60 seconds and never uses explorer links. Global commands
are also copied into every connected guild once on startup—even when
`DISCORD_GUILD_ID` is set—avoiding Discord's global propagation delay for
`/fomotrack`.

## Session 4 — verified EVM wallets

FOMO's own `user.evmAddress` is synthetic just like its Solana `address`. For
Konito the API field `0x00f38d...5c00` has no code and nonce zero on Base and
BNB Chain. Do not display it.

The public FomoScan identity index returns Konito's independently verified
Solana wallet from session 2 and EVM wallet `0x273941...f3627`. The EVM address
is a deployed smart contract on both Base and BNB Chain, consistent with FOMO's
documented ERC-4337 architecture. Rowdy and frankdegods produced the same
pattern: one verified address deployed at the same address across both chains.

What shipped:

- `fomo_evm.py`: automatic verified-only resolver using FomoScan's public
  profile lookup, then `eth_getCode` against Base/BSC; stores `evmWallet` beside
  the Solana cache.
- `evm_resolve.py`: standalone `--handle` / `--fresh` diagnostic.
- `fomo_bot.py`: resolves both wallets. First-time cache writes are sequential
  because both resolvers preserve fields in one file. Explorer links were later
  removed from the Discord result by request.
- `test_fomo_evm.py`: verified/deployed, unverified, dead-address and cache tests.
- `.env.example`: browser, Solana and EVM settings are now documented.

FomoScan is an independent, unofficial service. Failures degrade cleanly to no
EVM field and are never fatal to `/fomo`; automatic results must be explicitly
marked `verified`. Empty/unverified results are not cached.

FomoScan is currently stale for `onmycheck` (both wallet fields are null). For
known mappings that are missing from the index, `evm_resolve.py --wallet 0x...`
requires live contract code on Base or BSC and stores the mapping with
`evmSource: manual+rpc`. Ownership is asserted by the operator; deployment
alone cannot prove that a wallet belongs to a handle.

Run:

```powershell
python evm_resolve.py --handle Rowdy --fresh
python evm_resolve.py --handle onmycheck --wallet 0xb6e00eaea52e9a91e0a9af67301fa1bc6d02e7ac
python fomo_bot.py
```

---

## 0. Session 3 — resolution no longer depends on the mint

### What failed

`@Rowdy` came back UNRESOLVED. All four candidate swaps were buys of the same
token, and that token had **>12000 signatures newer than a two-hour-old swap**.
Paging a mint backwards does not terminate against that, so every attempt died
the same way:

```
mint too busy: 12000 signature(s) scanned and still newer than this swap
```

Two separate faults, both now fixed:

1. **The only route to a transaction was the mint.** Session 2 already knew a
   hot mint is unpageable — that is why *verification* runs against the wallet
   instead. But *discovery* still had to start at a mint, so a trader with no
   quiet token was unreachable.
2. **`pick_swaps` had no mint diversity.** It took the four most recent buys,
   which for a trader having a big day on one token are four swaps of the same
   token. Four attempts, one mint, one failure mode. They now spread over
   distinct mints, so the attempts fail independently.

### The fix — index the sponsor, not the token

FOMO pays the fee on every sponsored trade, so
`AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51` carries a complete,
chronologically dense index of the platform's trading — and **its length is set
by FOMO's throughput, not by how viral a token is.** That is the whole point: a
memecoin's signature rate is unbounded from FOMO's side, the sponsor's is not.

`SponsorIndex` pages that history once per resolution and every swap of the
same trader is looked up in it, so four attempts cost one scan. Session 2
already wrote down "fallback: scan the sponsor" as a suggestion in the failure
message — it was never implemented. Now it is, and it is the **first** route,
not the fallback.

There are three routes now, tried cheapest first:

| route | looks at | good for | fails when |
|---|---|---|---|
| **sponsor** | the gas sponsor's signatures | everything, by default | the swap predates the index reach, or FOMO used another payer |
| **mint** | the traded token's signatures | quiet tokens | the mint is hot (this is the @Rowdy case) |
| **blocks** | `getBlock` at the timestamp | last resort, `--deep` | the RPC pruned the block |

The block route depends on no account's history at all — `createdAt` pins the
slot, the slot's block contains the transaction — so nothing on chain can
outrun it. It uses `transactionDetails: "accounts"`, which returns account keys
plus token balances and drops the instruction data, i.e. exactly what the
amount match and the signer rule need and nothing more. It costs a slot search
plus a `getBlock` per slot, so it is opt-in rather than automatic.

### Try it

```powershell
python wallet_resolve.py --anchor                 # still passes, unchanged answer
python wallet_resolve.py --handle Rowdy --fresh   # sponsor route
python wallet_resolve.py --handle Rowdy --fresh --deep   # if that still misses
```

The output now prints which route found the transaction, and flags a fee payer
that is not a known sponsor. If an unknown payer recurs, add it:
`FOMO_SPONSORS=Agm...,<other>` in `.env` — comma-separated, all get indexed.

### Merged, not overwritten

A second session reached the same diagnosis independently and had already
written a sponsor route into these files. Two of its ideas were better and were
kept:

- **`rpc_display_name`** — the CLI printed the full `SOLANA_RPC`, Helius API key
  and all. It now prints scheme and host only.
- **the strict match.** Candidate acceptance used to be "somebody in this tx
  received the out-amount". It now first asks that the tx's *derived trader*
  account for **both legs** of the exact swap, and only falls back to the loose
  out-amount test if nothing strict matches in that chunk. This matters
  precisely because of the sponsor route: the sponsor's two-minute window holds
  every FOMO trade rather than one trader's, so it is the one place two users
  could collide on the same mint and amount. `test_offline.py` covers it — a
  decoy tx crediting the right amount to a pool one second nearer does not win.

Where the two differed structurally, this version won: the sponsor history is
paged **once per resolution** and shared by all four attempts rather than
re-paged per swap; the sponsor is the **first** route rather than a fallback
after four mint scans; `FOMO_SPONSORS` allows more than one payer; and
`pick_swaps` tops up with repeats instead of returning fewer than asked (a
repeat is worthless to the mint route, but a different timestamp is a genuinely
different lookup for the sponsor route).

### Confidence

The sponsor rule, the signer rule and the amount match are all session-2
findings that held on two traders. What is **new and unverified on chain** is
the paging and the block reshaping, because neither sandbox has RPC egress
(see §5). It is covered by `test_offline.py`, which replays canned RPC pages —
paging, window filtering, index reuse, nearest-first ordering, the block-shape
normaliser and mint diversity all pass there. First real run is `--anchor`,
whose answer is known; if that still passes, the routes are sound.

Also fixed while in there: `mint_delta` now prefers `uiAmountString` over the
RPC's rounded `uiAmount`, which matters on nine-figure supplies like Rowdy's
5,732,942.956183; and the mint scan's "too busy" message no longer claims
busyness when the mint's history simply ended.

---

## 1. The wallet problem — SOLVED

### What was wrong

`FomoUser.sol_address` read `raw["address"]`, and that address has never received
a lamport. The previous session proved the address was wrong but concluded the
real wallet was **unreachable**. That conclusion was incorrect.

fomo.family publishes **four** addresses per trader and not one is the trading
wallet:

| field | Konito's value | on chain? |
|---|---|---|
| `user.address` | `DGzQ31Tsg5a4Kgqi5AWTCpTfFxsQjBdevmSPNbGzsXc5` | no |
| `swap.address` | `2XyT8odcS3iCVca48QeQKMkBuR4UcqpQzRrcHNmzuEpj` | no |
| `trade.userAddress` | `2XyT8odcS3iCVca48QeQKMkBuR4UcqpQzRrcHNmzuEpj` | no |
| `evmAddress` | — | n/a |
| **real wallet** | **`93fjdwW7S3Aw4TkrnMzy51sZ5pP4ArpvtmFYujNyDVgH`** | **yes** |

Checked across `/v2/users/{id}`, `/swaps`, `/trades`, `/spotlight`, `/balances`,
`/v2/transfers/with/{id}` and `/trades/{tradeId}`. There is also **no transaction
signature anywhere in the API** — verified, and note the old `{80,90}` base58
regex *would* have matched an 88-char signature, so that was a real absence and
not a regex bug. **Stop looking for a published wallet. There isn't one.**

### The rule

> **The trader is the signer that is NOT the fee payer.**

FOMO sponsors gas. On Konito's tx `63o3ZL1hpSCtf3ww...`:

```
fee payer  AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51   FOMO's gas sponsor
co-signer  93fjdwW7S3Aw4TkrnMzy51sZ5pP4ArpvtmFYujNyDVgH   the trader
```

The sponsor was the only account with a SOL delta, exactly the 410000-lamport
fee. The trader still signs, because moving their own tokens needs their
signature.

**This single mistake is why every earlier probe failed.**
`verify_wallet_onchain.py` read only `signers[0]`, which is *always* the sponsor,
so it reported a stranger every time and fell through to a "count recent txs"
branch — which is where the bogus "zero transactions, wallet doesn't exist"
conclusion came from.

Do **not** use token ownership as the primary rule. The sample tx had 5 distinct
token owners; only the signer test is exact.

### Finding the transaction without a signature

FOMO gives three fields that identify it uniquely:

| field | value |
|---|---|
| `swap.outTokenAddress` | the mint |
| `swap.outHumanAmount` | the exact amount received |
| `swap.createdAt` | the time — tracks `blockTime` to ~1s |

Konito's swap at `13:52:03.014Z` for `25540.610209543` of `5P3DUdtj...` is the tx
at `blockTime 1787061122` crediting `25540.610210` to `93fjdwW7...`.

Two phases, both load-bearing:

1. **One mint scan** for a candidate. `getSignaturesForAddress(mint)` → filter to
   ±120s → fetch **nearest-first**, stop at the first amount match. Nearest-first
   is what makes it viable: a hot mint had 255 candidates in the window and the
   match was 6th. `getTransaction` goes out 10 per HTTP request, because rate
   limits count *requests*. Stop at the first hit — older swaps only sit behind
   more signatures, so extra mint scans buy nothing.
2. **Verify against the candidate wallet's own history**, never against more
   mints. A hot memecoin mint can have >12000 signatures newer than a two-day-old
   swap, so paging a mint backwards is hopeless — that is what
   `no candidates after 12000 signatures` meant. Nothing was missing from the
   chain. A trader's own account runs to 500–800 signatures over the same period
   and must contain every swap FOMO reports. Matches buys and sells.

### Results

| handle | wallet | confidence | cost |
|---|---|---|---|
| Konito | `93fjdwW7S3Aw4TkrnMzy51sZ5pP4ArpvtmFYujNyDVgH` | CONFIRMED 5/5 | 11 RPC calls |
| onmycheck | `Ay77dkJkbjPCLbhHmwNg5z4WVtP2bMUpjKNnWFo1CuD2` | CONFIRMED 5/5, matches hand-traced | 19 RPC calls |

`Ay77dkJk...` was briefly suspected of being a misattribution because it owns
token accounts in *Konito's* transaction. It is correct — two FOMO traders simply
touched the same pools. Suspicion withdrawn.

---

## 2. What shipped

| file | status | purpose |
|---|---|---|
| `fomo_wallet.py` | **new** | the library: rule, tx search, verification, cache, `WalletResolver` |
| `wallet_resolve.py` | **new** | CLI over it — `--anchor`, `--handle`, `--expect`, `--fresh` |
| `wallet_hunt.py` | **new** | the diagnostic that found all this — `--tx` dissects a tx by role, `--handle` dumps every route to `hunt_out/` and deep-searches it |
| `fomo_bot.py` | modified | embed shows the derived wallet; both synthetic addresses dropped |
| `test_offline.py` | **new (s3)** | replays canned RPC pages — the only way to check this logic without egress |

`fomo_bot.py`'s embed degrades cleanly: no RPC, no `httpx`, or an unresolvable
handle just omits the **Wallet** field rather than printing a dead address.
Ranks, PnL, volume, hold time and followers render regardless. Resolution is
cached to disk and locked per handle, so two simultaneous `/fomo` calls don't
both pay for the scan.

### .env additions

```ini
SOLANA_RPC=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
FOMO_RESOLVE_WALLETS=1          # 0 turns the Wallet field off entirely
FOMO_WALLET_CACHE=wallet_cache.json
FOMO_SPONSORS=Agm...            # optional, comma-separated; defaults to the known sponsor
```

A trader's wallet never changes, so a handle is resolved **once, ever**. The
public RPC throttles hard and prunes history — Helius or QuickNode is not
optional for real use.

### Superseded

`wallet_check.py`, `find_wallet_source.py`, `verify_wallet_onchain.py`,
`swap_fields.py`, `wallet_chain_probe.py` are all answered now and can be
deleted. `wallet_hunt.py` is worth keeping — dissecting a tx by role and dumping
every route for grepping are both reusable.

---

## 3. Still true from last session

- **Cloudflare.** `fomo_browser.py`'s Playwright transport is what beats the WAF:
  a real Chrome, persistent profile, API calls run *inside* the page. Standing
  decision: no TLS-fingerprint evasion. The bot runs on borz, never the VPS.
- **Auth.** In browser mode the page's own live Privy token is read from
  localStorage per call — no refresh, no rotation race.
- **`discord.py` ≠ `discord.py-self`.** This folder needs its own venv.
- On borz `channel="chrome"` doesn't resolve and it falls back to bundled
  Chromium. Harmless; `playwright install chrome` silences the warning.

---

## 4. Open

1. **`fomo/` is untracked in git.** Nothing in it has ever been committed
   (`git status` shows `?? fomo/`). Left that way deliberately — the first commit
   is Johan's call.
2. **`/trades/{tradeId}` is unmined.** Returns `{trade, swaps, transfers, comment,
   userHandle, isDev, numReplies}` with `realizedPnlUsd`, `avgEntryPrice` /
   `avgExitPrice`, cost basis and the thesis comment. Much richer than anything
   currently in the embed — the obvious source if `/fomo` should show best trades
   rather than just aggregate stats.
3. **Swaps pagination** is still unsolved: `limit` works, every cursor param is
   silently ignored while `hasNextPage` stays true.
4. **Stale docs.** `README.md` still describes the Privy refresh token as the auth
   model and omits `fomo_browser.py`; `.env.example` never got `FOMO_TRANSPORT`,
   `FOMO_CHROME_PROFILE` or `FOMO_CHROME_HEADLESS`.

## 5. Environment constraint (for whoever picks this up)

Neither the Cowork sandbox nor the desktop VM has network egress to a Solana RPC
— both 403 at the proxy — and Cloudflare blocks fomo.family from both. **Every
on-chain or FOMO call has to run in Johan's own terminal** in `fomo/.venv`. The
working pattern this session: write the script, have Johan run it, verify logic
offline against the saved JSON in `hunt_out/`.

## Running it

```powershell
cd C:\Users\mzshu\Downloads\memebot\fomo
.venv\Scripts\activate

python wallet_resolve.py --anchor              # self-test, known answer
python wallet_resolve.py --handle Konito       # resolve (cached after first run)
python wallet_resolve.py --handle X --fresh    # ignore the cache

python fomo_bot.py
```
