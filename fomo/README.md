# FOMO + Pump.fun research bot

A standalone Discord bot that turns FOMO and Pump.fun usernames into expanded
trader profiles, with optional activity alerts.

```
username → /v2/users/userHandle/{handle} → user object (26 fields)
                    ↓ id
           /v2/users/{id}/leaderboard → rank + PnL for 24h / 7d / 30d / all-time
                    ↓
              Discord embed
```

`FOMO_API.md` is the full API reference — read that first, it documents every
verified route, the auth model, and the constraints.

## Two things that will bite you

1. **Cloudflare blocks datacenter IPs.** The Vultr VPS gets a 403 on every path.
   This bot has to run on borz (or anything with a residential IP). It is *not*
   a `systemctl restart memebot` service.
2. **`discord.py` ≠ `discord.py-self`.** The main memebot uses the self-bot fork;
   this needs the real library. They install the same `discord` package name, so
   keep this folder on its own virtualenv.

## Setup (on borz)

```powershell
cd C:\Users\mzshu\Downloads\memebot\fomo
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env`:

- `DISCORD_BOT_TOKEN` — from the Discord developer portal.
- `SOLANA_RPC` — paid/archive RPC used to derive real Solana wallets.
- `*_RPC_FALLBACKS` — comma-separated backup endpoints used after a primary
  HTTP/RPC failure or rate limit. Key-bearing URLs belong only in `.env`.
- `*_WSS` — reserved realtime endpoints. The current alert loops still poll;
  configuring WSS does not silently change their delivery semantics.
- `FOMO_TRACK_INTERVAL` and `PUMP_TRACK_INTERVAL` — independent polling delays.
  This workspace uses 5 seconds for FOMO and 1 second for Pump. A poll never
  overlaps a still-running poll of the same service.
- Log into the persistent browser once with `python fomo_browser.py --login`.

Install the Discord app to the server with the `bot` and
`applications.commands` scopes. For profile replies and tracking alerts the bot
role needs View Channel, Send Messages and Embed Links. Members must be allowed
to Use Application Commands in the target channel.

Slash commands are registered globally only. On startup the bot also syncs an
empty command set to each connected guild once, removing server-specific
commands left by older releases so Discord does not show duplicate entries.
`DISCORD_GUILD_ID` is no longer used.

Wallet enrichment is enabled by default. `FOMO_RESOLVE_WALLETS=0` disables
Solana resolution and `FOMO_RESOLVE_EVM=0` disables verified EVM resolution.
After `/fomo <handle>`, Discord asks the requester to choose **Compact** or
**Wide**. Compact renders only the profile image, identity, Social, Strategy,
Portfolio, linked X account and verified wallets. Wide uses the complete
existing profile renderer, including trades, theses, ranks and metadata. Both
layouts use the same profile fetch and background wallet-enrichment path; the
choice changes rendering only.
While Compact wallet enrichment is still running, its Linked wallets field
shows `Querying ⏳`. The completed edit replaces that state with verified
wallets or the final no-wallet result.
`/fomo` no longer waits for this optional on-chain work: it sends the core
profile first, includes any cached wallets immediately, and edits that same
card when background enrichment completes. `FOMO_ENRICH_TIMEOUT` bounds the
background work (20 seconds by default), so a slow RPC cannot hold the command
open indefinitely. Its balances, spotlight, trades and swaps panels are fetched
in one parallel in-browser batch. Tracking and post-response wallet discovery
use a separate background browser page, so polling cannot queue ahead of an
interactive profile lookup.
Successful identity mappings are kept locally. Solana discovery now runs in
parallel with the complete `EVM wallet → EVM activity` branch after the initial
card is visible. EVM buys and sells therefore start as soon as the EVM wallet is
known instead of waiting for Solana. HTTP request-line logging is disabled
because RPC URLs can contain private API keys.

For an uncached profile, the bot batches several low-liquidity/older EVM
trade-detail requests, searches each token's chain history near FOMO's
timestamp, matches direction and exact token amount, validates the stablecoin
value when it is available, and requires the same address across at least two
independent transactions. It then requires deployed smart-wallet code on an
evidence chain before caching the result. Current-balance matching is the
second discovery path. `FOMO_EVM_DISCOVERY_TOKENS` and
`FOMO_EVM_DISCOVERY_PAGES` bound this work.

An independently verified EVM wallet can be deployment-checked and cached with
`python evm_resolve.py --handle HANDLE --wallet 0x...`.

Then check the API works before wiring up Discord:

```powershell
python probe.py Binkieee
python probe.py Binkieee --json
python probe.py --top 24h
python probe.py --search bink
```

Expected profile data for `Binkieee`: 135K followers, roughly $9.4M volume,
plus ranked PnL and the expanded portfolio/trading panels when available.

Finally:

```powershell
python fomo_bot.py
```

## Commands

| Command | What it does |
|---|---|
| `/fomo <handle>` | Choose a Compact identity card or the complete Wide profile. |
| `/pump <handle\|wallet>` | Look up a Pump profile, portfolio and latest callouts. |
| `/wallet <address>` | Reverse-search a Solana or EVM wallet across FOMO and Pump identities. |
| `/token <address>` | Market cap, image, the top 50 holders **and** the 50 best-performing traders by PnL/ROI with FOMO/Pump identities, ten a page. |
| `/thesis <address>` | What this token's biggest FOMO holders wrote about it, five a page. |
| `/connected <target> [strict]` | Wallets with strong on-chain evidence of belonging to the same cluster as a FOMO trader's. |
| `/track <platform> <target>` | Track a FOMO trader or a Pump profile; pick the alert types from a menu. |
| `/tracked` | List everything tracked in this channel, with **Edit** and **Remove** buttons. |
| `/fomotop [24h\|all-time] [n]` | Leaderboard. |

`/track` replaced `/fomotrack` and `/pumptrack`: the platform is a choice on the
one command rather than a second command name. `/tracked` replaced four —
`/fomotracked`, `/pumptracked`, `/tracksettings` and `/untrack` — because all of
them opened the same list of subscriptions and differed only in the verb.
Select one or more rows, then **Edit** to change one subscription's alert types
or **Remove** to drop every selected one. `/pumpwallet` is gone; `/wallet`
already answers for both platforms, and `/fomosearch` is gone with it.

Tracking alerts are sent as individual activity cards: green for buys, red for
sells and purple for theses. Trade values are shown in the chain's native
currency: SOL on Solana, ETH on Ethereum/Base and BNB on BSC. Exact native swap
amounts are preferred; stablecoin trades are converted using a live native-coin
price. Each card includes the token, chain, contract and timestamp; supported
tokens link directly to their Solana, Base, BSC or Ethereum page on Padre. The
configured large-swap threshold still controls
follow-up swap alerts, while a new position and a qualifying opening swap are
coalesced into one card. Successful `/track` confirmations and `/tracked` lists
are public so everyone in the channel can see what is being monitored.

Pump tracking uses the public Pump profile APIs for identities, portfolio data,
coin metadata and callouts. Exact trade direction and amounts come from the
official Pump bonding-curve `TradeEvent` and PumpSwap `BuyEvent`/`SellEvent`
records in Solana transactions. Registration baselines both recent transaction
signatures and callout IDs, so existing history is never replayed. Pump cards
use the same compact amount · market-cap layout and Padre token links as FOMO;
trade amounts are displayed in SOL.

FOMO and Pump tracking run in separate background loops. The current `.env`
uses `FOMO_TRACK_INTERVAL=5` and `PUMP_TRACK_INTERVAL=1`; actual checks occur
after the configured delay plus however long the previous poll takes.
Unchanged polls do not rewrite the tracking JSON. Duplicate subscriptions for
the same Pump wallet share their near-simultaneous signature response, and a
temporary all-provider RPC outage opens a short circuit breaker rather than
hammering every fallback once per second.
Set `PUMP_MIN_TRADE_USD=0` to alert on every detected trade or raise it to apply
a minimum value. `/pump` resolves callout mints through Pump's coin endpoint, so
old or sold callouts still show their real ticker instead of `$TOKEN`.

After `/track` resolves the profile, Discord shows a multi-select menu. Choose
any one, two or all three activity types — buys, sells and theses on FOMO; buys,
sells and callouts on Pump. Each subscription stores its own combination, and
`/tracked` shows that choice. The **Edit** button changes only this stored
filter and keeps the existing baseline, so activity hidden by an old filter is
never replayed later. **Remove** can drop several subscriptions at once.

`/token` auto-detects Solana, Ethereum, BSC, Base or Robinhood Chain from the
token's most liquid DexScreener pair. It shows market cap, image, contract and
the top 50 holder owners across five pages of ten, turned with Previous/Next.
Every page is rendered before the first is sent, so paging costs a message edit
rather than another round of identity lookups. Reaching past the twentieth
holder needs a Helius endpoint in `SOLANA_RPC`: `getTokenLargestAccounts` stops
at 20 token accounts, so Solana holders come from Helius DAS `getTokenAccounts`
and fall back to the shorter list when no Helius endpoint answers.
SPL token accounts are converted to their actual
Solana owner wallets. Ethereum/BSC/Base use the public holder index and
Robinhood uses Blockscout. Every holder is checked against verified FOMO
wallets and Pump's independent identity cache; Solana Pump profiles are also
resolved live, so the profile does not need to be tracked. Unknown holders are
shown as linked wallet addresses rather than guessed identities.

### Top Traders — best performers, not busiest wallets

The same card carries a **Top Traders** button next to Previous/Next. It is the
same shape as the holders — fifty rows, ten a page, the same FOMO and Pump
identity labelling — and it answers the other question worth asking about a
token: **who is actually winning on it.**

Holders are a single ranked query; performance has to be reconstructed out of
transfer history, so the list is rendered only when the button is pressed, and
then kept on the card. Toggling back and forth afterwards costs nothing.

| chain | source |
|---|---|
| Solana | Helius parsed transaction history for the mint (owner accounts, no token-account resolution). Without a Helius `api-key` in `SOLANA_RPC` it falls back to a much smaller batched `getTransaction` sample and logs that it did. |
| Ethereum / BSC / Base / Robinhood | `alchemy_getAssetTransfers` for the token, newest first; Robinhood also has Blockscout as a fallback. |

Each row is one wallet's ledger for this token:

```text
`1.` 🔵 @rowdy · 4bC1…9xQz · 12 tx
🟢 ` $130K  +$12,450  +382.5%`
        entry     PnL      ROI
```

- **Entry** — the *weighted average* acquisition price (total spent / total
  bought), shown as the market cap it implies when the token's supply is known,
  and as a price when it is not.
- **PnL** — realised PnL on everything sold, against that weighted-average cost
  basis, plus unrealised PnL on what is still held at the current price. `◐`
  marks a position that is still open, so part of the figure moves with price.
- **ROI** — PnL over the capital actually invested (`PnL / cost basis × 100`).

**Ranked by PnL by default.** The `Sort:` button cycles PnL → ROI → Volume
without another provider request — the client returns the rows any of the three
rankings needs. Volume (bought + sold) is still there, but only when it is
asked for: it measures activity, and a whale who makes one enormous buy and
never sells is not a top trader.

**Where the dollars come from.** A swap's counter-leg is in the same
transaction as its token leg. On Solana both routes already return the whole
transaction, so USDC/USDT and SOL legs price the trade for free (native
transfers below 0.005 SOL are rent and fees, not a price). On EVM the token
page carries only the token, so the venue's own WETH/USDC movement is read back
in one bounded extra query per pool and joined by transaction hash.

**How much history the sample reaches is the whole ballgame.** A token's best
traders bought at its beginning — they entered at a $23K market cap and sold at
$133K — so a sample that only reaches the last few hundred transactions does
not merely see less, it systematically excludes the winners and ranks the tail:
recent buyers, all at nearly the same entry, all up the same few percent.
Paging therefore continues until the token's *first* transaction, bounded by
`TOKEN_TRADER_SOLANA_PAGES` (30 × 100 parsed transactions),
`TOKEN_TRADER_EVM_PAGES` (5 × 1000 transfers) and `TOKEN_TRADER_BUDGET_SECONDS`
(60s of wall clock, since paging is sequential). It stops early the moment
history runs out, so a quiet token costs a fraction of the ceiling.

The card reports which of the two it got: **`full history`** when paging reached
the token's first transaction, or a transaction count with a **`+`** when a
budget cut it short — in which case the board is a window rather than a verdict.
Results are cached for five minutes per token.

**Three kinds of cost basis, kept apart.** Inventory is tracked in three
buckets, because "this cost nothing" and "we cannot read what this cost" are
different facts:

| bucket | what it is | on sale |
|---|---|---|
| paid | acquired in a transaction with a readable money leg | realises proceeds − weighted-average cost |
| free | acquired in a transaction where nothing of value moved at all — an airdrop, a dev allocation, a transfer in | realises the full proceeds; cost basis is genuinely zero |
| unknown | acquired in a transaction that *did* move value this bot could not read | realises nothing — crediting the whole sale would invent a profit |

A sale consumes all three in proportion. Unrealised PnL counts only the paid
bucket, so a wallet sitting on a free allocation is not credited with a profit
it never traded for. A wallet that sells more than the sample saw it buy is
selling inventory from before the window, and that excess is excluded too. Any
of these marks the row `~`, and a free-only wallet shows a real PnL with no ROI
— there was no capital at risk to divide by.

Liquidity pools, routers and programs are excluded — an address appearing in
more than 20% of the sampled transactions is the venue, not a participant — as
are burn and null addresses.

### Checking a token's ranking

`token_traders_diag.py` drives the same client `/token` does and answers the
three questions in the order they go wrong — coverage, pricing, then one
wallet's ledger:

```powershell
python token_traders_diag.py 7RY9w8brhM4DgQwiwn4D9cVnk4L7RJuZESS3mEKmpump
python token_traders_diag.py <mint> --wallet <address>      # trade by trade
python token_traders_diag.py <mint> --pages 60 --budget 180 # go deeper
python token_traders_diag.py <mint> --rank roi --csv hunt_out/traders.csv
```

It exits 0 when the sample reached the start of the token's history and 1 when
it was cut short, so a disagreement with a full-history tool can be diagnosed
as *coverage* or *accounting* rather than guessed at.

`/thesis` answers the other half of that page: what the biggest FOMO holders
actually wrote about the token. Entries are ranked by position value and carry
the holder's handle, their X account, position, PnL and hold time above the
thesis itself, five to a page. It prefers `/feed/token/sortedThesis`, which
answers in one request; when that route is unavailable or returns a shape this
build does not recognise, it falls back to `/hodlers/top` plus one
`/trades/{tradeId}` per holder, which is verified but pays a request each.

When a Pump username is paired with a wallet, both the `@username` and the
shortened wallet are clickable. Both links target
`https://pump.fun/profile/{FULL_SOLANA_WALLET}`; Pump profile URLs are never
constructed from usernames.

For a verified FOMO EVM wallet, `/fomo` also reads on-chain activity from
Ethereum and BSC through the configured Alchemy endpoints and from Base and
Robinhood through Blockscout. A non-stable transfer is classified as a swap
only when the same transaction contains a stablecoin leg, which filters out
airdrop spam. These results are merged with FOMO's own feed for both buys and
sells.

An uncached EVM wallet can be discovered even after its positions were sold.
The resolver uses FOMO's per-trade history because the general swap feed is not
complete for EVM networks. A single matching transfer is never cached, and
different candidate wallets across the evidence are rejected as ambiguous.

FOMO Solana wallet discovery uses the platform sponsor's transaction history
and matches the non-quote token balance leg. It handles both buys (positive
token delta) and sells (negative token delta), searches up to 50 recent swaps,
and keeps existing EVM cache data when adding the Solana wallet. If every
configured Solana RPC is temporarily unavailable, the profile still renders
without enrichment and a later lookup retries after the short circuit breaker.

Pump does not return its linked EVM address directly. The bot discovers it from
a public, ownership-specific fingerprint instead: Pump's exact token balance is
matched against the current holder index for that same chain and token. A
unique index match is then independently confirmed with ERC-20 `balanceOf`
through the configured chain RPCs before it is cached in `pump_evm_cache.json`.
This avoids unsafe username/X-handle guessing and does not require Pump login
credentials.

## `/connected` — wallets in the same cluster

`/connected <handle|address>` looks for other wallets with unusually strong
on-chain evidence of belonging to the same cluster as a trader's known one. It
takes a FOMO username or a raw Solana/EVM address; a username is resolved
through exactly the path `/fomo` uses, so the two can never disagree about
which wallet a handle owns.

**It never claims shared ownership.** Nothing observable on a chain says that.
It reports how strong the *evidence* is, as a score out of 100 in three bands —
**Very High**, **High**, **Possible** — and every page repeats that the number
measures evidence, not ownership. `strict:true` surfaces Very High only; the
default is High and above, with the weaker band behind a button.

### The signals

| signal | what earns it |
|---|---|
| repetition | 3, 5, 10 or 20+ direct transfers between the two wallets |
| reciprocity | value moved both ways, at least twice each way |
| longevity | the relationship spans 7, 30 or 90+ days |
| spread | transfers on 3, 8 or 20+ separate dates |
| value | $1k, $10k or $100k+ moved in total |
| funding | the known wallet funded it first, and especially if funds came back |
| identity | the wallet cache already knows the candidate as a FOMO or Pump handle |
| cross-chain | the *same* verified identity turns up as a candidate on two chains |

A band is additionally capped by how many **independent** signals fired — four
for Very High, three for High, two for Possible — so one very loud signal (a
hundred transfers in a single day) never reads as strongly as three quiet ones
agreeing. A single transfer is never scored at all.

### What is excluded

Precision over recall: it is better to return nothing than to return a router.
Three defences, cheapest first.

1. **Known addresses** — exchanges, bridges, routers, programs, burn addresses
   and FOMO's own gas sponsor are dropped before they can occupy a slot.
   `CONNECTED_LABELS_FILE` points at a JSON `{"address": "label"}` map that is
   merged over the built-in list, so an operator can add one without a release.
2. **Account type** — on Solana a real wallet is owned by the system program and
   is not executable, which rules out pools, token accounts, vaults and PDAs
   outright. EVM cannot use that test, because FOMO's own wallets are ERC-4337
   contracts: there, contract code without a known identity is a scoring
   penalty and a printed caution instead.
3. **Degree** — one bounded page of the candidate's own history. An address
   dealing with `CONNECTED_HIGH_DEGREE` (40) or more distinct counterparties is
   a service, whatever it is called. This costs a request per candidate, so it
   runs last and only on the few that survived everything else.

An empty answer is the expected one for most traders, and the card says so
rather than reporting something weak.

### Cost and caching

Solana history comes from Helius parsed transactions (`CONNECTED_SOLANA_PAGES`,
5 pages of 100 by default); EVM from `alchemy_getAssetTransfers` in both
directions per chain (`CONNECTED_EVM_PAGES`). An EVM wallet is checked on the
chains the wallet cache has already seen it deployed on, falling back to
`CONNECTED_EVM_CHAINS` (base, bsc). Only SOL, native EVM coins and stablecoins
carry a USD figure — everything else is counted as an unpriced transfer rather
than given an invented value. A whole run is cached in `connected_cache.json`
for `CONNECTED_CACHE_TTL` (6 hours), keyed by the wallet set and the bar it ran
at.

Each result carries the transactions behind it: the select menu under the card
opens an ephemeral evidence panel with explorer links for the sampled
transactions and the reasons the score was awarded.

## Where wallet identities come from

Four sources, cheapest first — and `/fomo` now tries them in that order rather
than reaching for the scan first:

1. `wallet_cache.json` — a handle is resolved once, ever.
2. **FOMO's own holder list, asked about this trader's own positions.**
   `resolve_from_holders()` sends one `/hodlers/top` request (spelled
   *hodlers*) covering every Solana token the trader holds. The reply says
   which of those tokens publish a row naming them — and only those cost an
   on-chain owner query, because a token that does not name the trader can
   never name their wallet. Two tokens agreeing on one wallet is the evidence;
   a single token is corroborated before it is written.
3. On-chain discovery — the sponsor index, the mint scan, then the block route.
   The strongest evidence there is, and by far the most expensive: a 12-page
   sponsor index and up to four mint scans before its first answer.
4. **Exact balance fingerprints.** FOMO's balances panel reports raw integer
   amounts; one that identifies exactly one on-chain owner is a candidate.

The same holder match also runs from the other direction: `/token` matches
every published position on a token against its on-chain owners and adopts what
it can, so `/fomo` and `/wallet` know those traders afterwards without any
scan. `FOMO_ADOPT_HOLDER_WALLETS=0` keeps the naming but stops the writing.

### The corroboration gate

Sources 2 and 4 derive a wallet from an amount rather than from a transaction,
and a cached wallet is permanent, so neither writes one without corroboration.
`_corroborate()` is the single gate, and it has two rungs:

- **`verify_wallet`** scans the *candidate's own* signature history for the
  swaps FOMO reports for this trader. That ties the wallet to those trades, not
  merely to the platform, and it is the direction that scales — a trader's
  account runs to hundreds of signatures where a viral mint runs to tens of
  thousands. It needs the trader's swap rows, which `/fomo` and the diagnostic
  both have.
- **The sponsor check** only asks whether the wallet ever co-signed a
  FOMO-sponsored transaction, and it looks at the newest 40 alone. It is the
  fallback for callers with no swaps — `/token`'s adoption path renders a card
  for a token, not for a trader, and would otherwise pay a FOMO request per
  holder.

What separates them is refuted from inconclusive. If `verify_wallet` finds no
signatures near any swap time it never got to look, so the weaker check still
gets its turn; if it looked at the wallet's history and this trader's swaps are
not in it, that is a refusal and the weaker check does not get to overturn it.
The cache records which rung passed, in `walletSource` — `verify2`, `2tokens`,
`fomo-sponsor`.

Because the holder route's own gate is `verify_wallet`, a hit there is
transaction-backed too. That is what makes running it ahead of the scan cost no
evidence, only fewer requests — and it matters because the enrichment budget
(`FOMO_ENRICH_TIMEOUT`) is a wall clock that *cancels* whatever is still
running, so a handle the scan cannot reach used to spend the whole budget
proving it and never reach the cheap route at all.

Pump.fun is the opposite shape and needs none of that. **A Pump profile IS a
Solana wallet**: `GET /users/{wallet}` and `GET /users/{username}` return the
same record, and its `address` is the canonical identifier. So there is nothing
to infer and nothing to corroborate — the only cost is one HTTP request, and
the only thing worth engineering is not paying it twice.

`pump_profiles.PumpProfileResolver` is that. It keeps the `/fomo` properties
that still apply — an on-disk map, a per-wallet `asyncio.Lock` with a second
cache read inside it so concurrent callers make one request, and a failure that
can never raise into a Discord command — and adds the two Pump needs that FOMO
does not:

- **Expiry.** A FOMO wallet proved by an on-chain signature is permanent. A
  Pump profile is Pump's claim about a mutable username, avatar and follower
  count, so positive entries expire (`PUMP_PROFILE_TTL`, 7 days) and the
  `/pump` card asks for a shorter `PUMP_PROFILE_CARD_TTL` than holder
  labelling does.
- **Negative caching.** `/fomo` deliberately does not negative-cache: a miss
  there means a scan did not reach far enough, which a later run may fix. A
  Pump miss is an authoritative 404 from the only source of truth, so it is
  remembered for `PUMP_PROFILE_NEGATIVE_TTL` (6 hours) — which is what stops
  `/token` re-asking Pump about the same profile-less holders on every render.
  A *transient* failure is never written as an absence, so a Pump outage
  cannot poison the cache.

`/token` prefetches its whole Solana holder list through one bounded,
deduplicated batch before any row renders, so labelling costs no requests at
all. An `0x…` address is never sent to Pump's Solana profile route; it resolves
only once `pump_evm.py` has discovered which Pump profile owns it.

## Bulk wallet labelling

Resolving a handle by scan is expensive. Resolving a hundred is not worth it —
and mostly unnecessary, because FOMO publishes exact positions:
`/v2/leaderboard?limit=100` carries each trader's `topHoldings` (token, chain,
exact amount), and `/hodlers/top` carries the same for ~48 holders of any
token. The chain publishes who owns every balance. One unambiguous amount match
is a wallet.

```powershell
python fomo_map_top.py --dry-run            # what it would learn, no writes
python fomo_map_top.py --top 100
python fomo_map_top.py --period 24h --csv hunt_out/top100.csv
```

Cost is one holder query per distinct token, not one scan per trader, and every
token queried also labels traders outside the top 100 who happen to hold it.
Matches pass the same corroboration `/token` uses before anything is cached, so
the scan path (`fomo_resolve_diag.py <handle> --fresh`) is only needed for the
handles it lists as still unknown at the end.

`pump_map_top.py` is the Pump counterpart, and it is a cache warmer rather than
a resolver — there is nothing to match, only requests to pay in advance. It
gathers candidate wallets from sources the project already has, asks Pump about
each exactly once, and stores both the profiles and the definitive absences.

```powershell
python pump_map_top.py --dry-run                 # what it would learn, no writes
python pump_map_top.py                           # every wallet we already know
python pump_map_top.py --token E3i7...pump       # that token's holders
python pump_map_top.py --csv hunt_out/pump.csv
```

With no flags it seeds from `wallet_cache.json`, `pump_evm_cache.json` and
`pump_tracks.json` — so every wallet `/fomo` has proved on chain becomes a
candidate Pump profile, and the two caches compound.

## Diagnosing a missing wallet

`/fomo` degrades quietly: a handle whose wallet cannot be resolved simply shows
no wallet line, and the reason only reaches the bot's own log. To see the reason
for one or more handles:

```powershell
python fomo_resolve_diag.py Rowdy                  # both chains, cache allowed
python fomo_resolve_diag.py Rowdy frankdegods --fresh
python fomo_resolve_diag.py Rowdy --chain solana --no-deep
python fomo_resolve_diag.py Rowdy --details -v     # mine /trades/{id}, show logs
python fomo_resolve_diag.py Rowdy --json hunt_out/diag_rowdy.json
python fomo_resolve_diag.py Rowdy unipcs asta --csv hunt_out/wallets.csv
```

The summary table prints **full** addresses, never abbreviated, so it can be
pasted straight into an explorer or a tracker; a handle that missed shows
`[stage]` in that column instead. `--csv` writes the same rows as
`handle, solana, solana_status, evm, evm_status, error`, and `--json` keeps the
whole per-stage report.

It runs the same resolver calls the bot makes — including the same route set,
so `--no-deep` reproduces the pre-`FOMO_WALLET_DEEP` behaviour — and prints, per
chain, the cache and RPC configuration, how much usable evidence FOMO returned, which swaps were
picked, and a verdict naming the stage that lost the wallet plus what to do
about it. Exit code is 0 when every requested chain resolved, 1 otherwise, so it
can gate a check.

Common verdicts:

| stage | means |
|---|---|
| `config` | no Solana RPC, `FOMO_WALLET_DEEP=0`, no Helius for the holder or balance routes, or no Alchemy endpoint for an evidence chain |
| `rpc` | every configured Solana RPC failed; discovery is paused for 15s |
| `panels` | FOMO returned no swaps at all — transport or Cloudflare, not the wallet logic |
| `evidence` | the profile window holds no usable rows for that chain (an EVM-only or Solana-only trader) |
| `hodlers` | FOMO published no holder row naming this trader, or the published position did not identify exactly one wallet |
| `discovery` | evidence existed but no transaction matched — check `FOMO_SPONSORS` (Solana) or `evm_diag.py --expect` (EVM) |
| `balances` | exact balance fingerprints matched zero or more than one on-chain owner |
| `verification` | a candidate was found and then refused: this trader's own swaps are not in that wallet's history |
| `ranking` | two EVM addresses scored identically; resolve by hand with `evm_resolve.py --wallet` |
| `deployment` | the EVM candidate has no contract code on a chain it traded on |

`python -m unittest test_fomo_resolve_diag` covers the classification offline.

## Diagnosing a missing Pump profile

`pump_resolve_diag.py` is the same tool for the Pump side. It drives the same
`PumpProfileResolver.lookup()` the bot drives and captures the `pump.*` log
records, so it cannot drift from `/pump`.

```powershell
python pump_resolve_diag.py 4y2T1ghy...dvE1
python pump_resolve_diag.py hdegroot 1000XCryptoD --details
python pump_resolve_diag.py <wallet> --fresh -v
python pump_resolve_diag.py w1 w2 w3 --csv hunt_out/pump_wallets.csv
```

Its stages are shorter than FOMO's because the mapping is published rather than
inferred. What it exists to make visible is that there are three different
kinds of "no":

| stage | means |
|---|---|
| `input` | the term cannot address a Pump profile — usually an `0x…` wallet whose Pump profile has not been discovered by `pump_evm.py` yet |
| `cache` | answered without a request: either a known profile, or a known *absence* recorded from a real 404 |
| `profile` | Pump was asked: it returned the profile, or it returned 404 and the absence is now cached |
| `transport` | Pump did not answer. Deliberately **not** cached, so this clears by itself |

`--no-write` runs it without persisting anything; `--card` applies the shorter
freshness bar the `/pump` card uses instead of the full TTL. Exit code is 0 when
every term resolved, 1 otherwise.

`python -m unittest test_pump_profiles` covers the cache and resolver offline.

## Token rotation

Privy access tokens last 60 minutes. The client refreshes automatically on
expiry and on any 401. Privy **rotates the refresh token on every use**, and the
new one is written to `.fomo_session.json` — so:

- Don't delete `.fomo_session.json`; it holds the current refresh token.
- If you log in on fomo.family in a browser, the bot's token can get rotated out
  from under it. It'll fail with a clear "re-copy it" message. If that becomes
  annoying, register a second FOMO account just for the bot.

## Files

| File | |
|---|---|
| `FOMO_API.md` | API reference — routes, schema, auth, gotchas |
| `fomo_api.py` | async client: Privy refresh, caching, typed `FomoUser` |
| `fomo_bot.py` | Discord bot + embed builder |
| `fomo_features.py` | Best trade, portfolio and cross-chain latest-buy derivation |
| `fomo_evm_activity.py` | Robinhood Chain wallet buys omitted by FOMO's swap feed |
| `fomo_tracking.py` | Persistent subscriptions and alert change detection |
| `pump_api.py` | Public Pump profile, holdings, callout and coin adapter |
| `pump_chain.py` | Official Pump/PumpSwap event decoder and Solana polling |
| `pump_tracking.py` | Pump subscription snapshots and normalized alerts |
| `pump_evm.py` | Exact-balance Pump EVM discovery and reverse cache |
| `pump_profiles.py` | Wallet ↔ Pump profile resolution, cached and deduplicated |
| `wallet_profile_cache.py` | The keyed, expiring, negative-caching JSON store both flows share |
| `token_intelligence.py` | Cross-chain metadata, top-holder owners, percentages and the trader-history routes |
| `token_traders.py` | Provider-shape parsing, the cost-basis ledger and the PnL/ROI ranking behind `/token`'s Top Traders |
| `token_traders_diag.py` | Why a token's trader ranking looks wrong: coverage, pricing and one wallet's ledger |
| `connected_wallets.py` | `/connected`: counterparty history, infrastructure filtering and the confidence model |
| `fomo_wallet.py` / `wallet_resolve.py` | Real Solana wallet resolver + CLI |
| `fomo_evm.py` / `evm_resolve.py` | Verified EVM smart-wallet resolver + CLI |
| `fomo_hodlers.py` | FOMO's `/hodlers/top` holder list, matched to on-chain wallets |
| `fomo_resolve_diag.py` | Why a handle resolved no Solana/EVM wallet — stage-by-stage |
| `fomo_map_top.py` | Bulk-label the top leaderboard traders' wallets from published positions |
| `pump_resolve_diag.py` | Why a wallet resolved no Pump profile — stage-by-stage |
| `pump_map_top.py` | Bulk-warm the Pump profile cache from wallets the project already knows |
| `token_page_sniff.py` | Records the API calls fomo.family's token page makes |
| `token_holders_probe.py` | Probes candidate routes and reads their validation errors |
| `evm_diag.py` | EVM microscope: traces a known wallet through every match gate |
| `probe.py` | CLI for testing lookups without Discord |

Wide's `Latest buys`, `Latest sells` and `Open positions` all render a colour
marker plus a `$TICKER` linked to Padre. `Open positions` lists the active book
largest first with average entry, size and unrealised PnL, derived from the
trade rows already fetched for the profile — no extra request. Its PnL is per
unit, so a partially sold position is not reported as a loss.

Latest buys include the reconstructed market cap at entry. FOMO does not expose
historical market cap directly, so the bot uses the full trade entry price plus
current DEX Screener price/market cap and labels the result with `~`.
Robinhood Chain buys are read from the verified EVM wallet through its public
Blockscout API and merged with FOMO's Solana swaps before the newest three are
selected.
