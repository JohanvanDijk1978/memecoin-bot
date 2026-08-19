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

Wallet enrichment is enabled by default. `FOMO_RESOLVE_WALLETS=0` disables
Solana resolution and `FOMO_RESOLVE_EVM=0` disables verified EVM resolution.
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
| `/fomo <handle>` | Look up the complete FOMO trader profile and recent activity. |
| `/wallet <address>` | Reverse-search a Solana or EVM wallet across FOMO and Pump identities. |
| `/token <address> [top 5\|10]` | Show token market cap, image and top holders with FOMO/Pump identities. |
| `/untrack` | Select one or more current FOMO/Pump subscriptions to remove. |
| `/tracksettings` | Select a subscription and change its alert types without re-adding it. |
| `/fomotrack <handle>` | Select any combination of buys, sells and theses to track. |
| `/fomotracked` | Publicly list every tracked trader and their alert filter. |
| `/fomountrack <handle>` | Stop that trader's alerts in the current channel. |
| `/fomosearch <term>` | Top 8 fuzzy matches. |
| `/fomotop [24h\|all-time] [n]` | Leaderboard. |
| `/pump <handle\|wallet>` | Look up a Pump profile, portfolio and latest callouts. |
| `/pumpwallet <address>` | Return the Pump handle for a Solana wallet or a discovered EVM wallet. |
| `/pumptrack <handle\|wallet>` | Select any combination of buys, sells and callouts to track. |
| `/pumptracked` | Publicly list Pump profiles and their alert filters. |
| `/pumpuntrack <handle\|wallet>` | Stop that Pump profile's alerts in the current channel. |

Tracking alerts are sent as individual activity cards: green for buys, red for
sells and purple for theses. Trade values are shown in the chain's native
currency: SOL on Solana, ETH on Ethereum/Base and BNB on BSC. Exact native swap
amounts are preferred; stablecoin trades are converted using a live native-coin
price. Each card includes the token, chain, contract and timestamp; supported
tokens link directly to their Solana, Base, BSC or Ethereum page on Padre. The
configured large-swap threshold still controls
follow-up swap alerts, while a new position and a qualifying opening swap are
coalesced into one card. Successful `/fomotrack` confirmations and
`/fomotracked` lists are public so everyone in the channel can see what is being
monitored.

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

After `/fomotrack` or `/pumptrack` resolves the profile, Discord shows a
multi-select menu. Choose any one, two or all three activity types. Each
subscription stores its own combination, and `/fomotracked` or `/pumptracked`
shows that choice. `/tracksettings` changes only this stored filter and keeps
the existing baseline, so activity hidden by an old filter is never replayed
later. `/untrack` lists both services in one private selection menu and can
remove several subscriptions at once.

`/token` auto-detects Solana, Ethereum, BSC, Base or Robinhood Chain from the
token's most liquid DexScreener pair. It shows market cap, image, contract and
five or ten top holder owners. SPL token accounts are converted to their actual
Solana owner wallets. Ethereum/BSC/Base use the public holder index and
Robinhood uses Blockscout. Every holder is checked against verified FOMO
wallets and Pump's independent identity cache; Solana Pump profiles are also
resolved live, so the profile does not need to be tracked. Unknown holders are
shown as linked wallet addresses rather than guessed identities.

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
| `token_intelligence.py` | Cross-chain metadata, top-holder owners and percentages |
| `fomo_wallet.py` / `wallet_resolve.py` | Real Solana wallet resolver + CLI |
| `fomo_evm.py` / `evm_resolve.py` | Verified EVM smart-wallet resolver + CLI |
| `probe.py` | CLI for testing lookups without Discord |

Latest buys include the reconstructed market cap at entry. FOMO does not expose
historical market cap directly, so the bot uses the full trade entry price plus
current DEX Screener price/market cap and labels the result with `~`.
Robinhood Chain buys are read from the verified EVM wallet through its public
Blockscout API and merged with FOMO's Solana swaps before the newest three are
selected.
