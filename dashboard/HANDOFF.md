# memedash — handoff (2026-08-28)

## v1.38 — alerts, exits, and wallet discovery

Three features, all interviewed rather than assumed. The decisions and their
reasons are in project memory (`project_wg_v138.md`); this is what shipped.

### 1. Telegram DM on a new convergence

`dashboard/alerts.py` is `src/send_ping.py` rewritten on httpx — the dashboard
sends its own DMs rather than routing through the bot, because both processes
already read the same `../.env` and inventing IPC between them would buy
nothing. Alerts go to `YOUR_TELEGRAM_USER_ID` only, never to
`TELEGRAM_ALERT_GROUP`: a convergence should not compete with the CA firehose.
Override the destination with `WG_ALERT_CHAT`, or turn the whole thing off with
`WG_ALERTS=0`.

**Dedupe is by milestone, not by clock.** `wgroup_alerts` stores the highest
wallet count already announced per token, so 2 wallets alerts once, 3 alerts
once, and a wallet that sells and rebuys back to the same count is silent
forever — while a genuine 2→3→4 climb in ten minutes still sends all three. A
dismissed token never alerts. The message is the bot's Markdown ping shape plus
a line per wallet, sent as a photo using the v1.37 banner with a text fallback,
because a dead banner URL must not cost the alert.

If the DMs are silent the page says why, in its notes line — that check is
`A.status()`, and it beats reading the journal.

### 2. Cooling cards

A card no longer vanishes when the count drops below two. `wgroup_seen` gained
`ended_at`, and the token lingers for `WG_COOL_SECONDS` (900) as a dimmed card
that names who sold, when, and what they were holding. **Exits are page-only —
no DM.** Arrivals ping; departures do not.

Membership is tracked in `wgroup_members` and diffed every round; a wallet that
leaves is written to `wgroup_exits` and appears on the card. Live cards carry
their recent exits too, so a token at 3 wallets that just lost its 4th says so
without waiting to break.

**Full exits only, by choice**: a wallet is "out" when it falls below the $50
floor. No partial-sell flagging.

**The bug this feature exposed, and the rule it leaves behind:** the membership
diff originally read `_convergences`, which applies the 2-wallet threshold. When
a token fell to one holder it left that list entirely, and the diff concluded
the *remaining* holder had sold — a card would have announced "Whale A sold
out" while Whale A was still in it. `_holdings_by_token()` is now the
threshold-free view, and `_convergences()` is the thin filter on top.
**Anything that diffs membership must use `_holdings_by_token`, never
`_convergences`.**

### 3. Wallet discovery

`dashboard/discover.py` suggests untracked wallets that recur across the tokens
a group converges on. It imports `fomo/token_traders.py` — Johan's call — which
is cheaper than it sounds: `fomo/` is tracked in git so it is already on the
VPS, and `token_traders` is pure stdlib, so there is nothing new to install.
The import is still guarded; if it ever fails, discovery falls back to holder
lists and the rest of the page is untouched.

A wallet needs **2+ different convergences** to be suggested. Ranking is
recurrence first, then PnL and early-buyer count as tie-breakers within a tier —
a wallet in three of your finds should not be outranked by one that got lucky
once. Each row names the tokens, marks the ones it was early in, and has an
"Add to group" button.

**Infrastructure is detected across the whole scan, not per token.** This is the
subtle part and the same trap `/connected` hit in the FOMO work: a pool sits on
one side of one token's swaps, but a router like Jupiter sits on one side of
*everything* — and recurrence across tokens is exactly what this feature ranks
by, so an unfiltered router would top every list forever. The per-token check
cannot see that, and it skips small samples entirely. So pass 1 reads every
token's history, `infrastructure_addresses()` runs once over the union, and
pass 2 ranks against what is left.

Discovery runs on its own slow loop (`WG_DISCOVER_INTERVAL`, 6h) and on the
"Find wallets" button. It never runs inside a holdings round — one history
request per convergence is the expensive call pattern on this page.

### Verified, and not

Offline as always (no crypto-API egress): **77 assertions green** — 37 on the
alert milestones and the exit/cooling lifecycle, 19 on discovery against the
real `token_traders` parser and aggregator with seeded Helius payloads, and 21
driving the real UI in Playwright with zero JS errors. That covers the photo
fallback, the cooling purge, scan-wide infrastructure filtering, and one-click
add end to end.

**Unverified, and the first things to watch on the VPS:** whether
`api.helius.xyz/v0/addresses/{mint}/transactions` answers for your key and
returns the shape the parser expects (the seeded payloads are my construction,
not a capture), and whether Telegram accepts the Markdown for a token whose
name has punctuation — `_escape()` strips the characters that open a style run,
but real memecoin names are inventive. Run one convergence through and look at
the DM before trusting it.


## v1.37 — four changes to Wallet Groups

1. **Dismiss a card.** An ✕ in the card's top-right corner hides that token from
   the group permanently — it is written to a new `wgroup_hidden` table, so it
   survives a reload, a restart and a rescan. The token keeps being scanned and
   keeps counting toward the group's holdings; it just never gets a card again.
   An "N hidden" chip in the bar lists what you dismissed and brings any of it
   back. Dismissed tokens are also skipped by the cost-basis resolver, so a
   token you do not want to look at stops costing requests.
2. **A ping on a new convergence**, reusing `/static/ping.mp3`. It has its own
   🔔 in the bar and its own `wg_mute` key, deliberately *not* shared with the
   live CA feed's mute — those are different signals and you will want them
   muted independently. It fires only on a token that was not in the previous
   payload; changing the "held by at least" pill is silent, and so is bringing a
   dismissed token back. Browsers reject audio until the page has been clicked
   once, same caveat as the CA feed.
3. **The card wears the token's banner.** Dexscreener's `info.header`, with
   `info.openGraph` as the fallback, is stored in a new `banner` column on
   `wgroup_tokens` (added by an `ALTER TABLE` migration — existing databases
   upgrade in place) and painted behind the card under a gradient scrim. A token
   with no banner falls back to its profile picture, blurred into a colour wash
   rather than stretched, because a square logo scaled to a wide card looks
   broken.
4. **`WG_MIN_POSITION_USD` now defaults to 50, not 1.** A wallet under $50 in a
   token does not count as holding it, so a card needs two wallets each with
   real size. This is the single most effective noise filter on the page — at $1
   any airdrop that hit two tracked wallets manufactured a card. Set the env var
   back to `1` to restore the old behaviour.

Verified offline with the usual pattern (section 5): the real app under uvicorn
with seeded providers, driven by Playwright — 21 assertions covering the dust
floor, both art paths, the ping's silence rules, and the dismiss/reload/restore
round trip. The live providers are still unverified, and `info.header` being
present on Dexscreener's real payload is the one new assumption worth watching:
if it is missing the card simply falls back to the logo.

## Where this stands

v1.36 added **Wallet Groups** (`#/wallets`), the first page in memedash that
reads on-chain wallet state instead of the bot's call history. You give it a
set of wallets with your own labels; it shows only the memecoins **two or more
of them hold right now** — who is in, how much supply each controls, and what
each one is up or down. A card appears the moment a second tracked wallet is in
a token and is removed the moment the count drops below two, so it reads as a
live signal feed rather than a table.

Nothing about the existing pages changed except one function signature
(`_notify_subscribers` now takes a message) and the version.

The whole feature is **unverified against live APIs**. It was built and tested
in a session with no network egress to Solana RPC, Solscan, Etherscan or
Dexscreener (section 5). Every provider call is written defensively and every
number on a card says where it came from, but the first real run is yours.

### Run these first on the VPS

```bash
cd /root/memecoin-bot-new
python3 tools/diag_wallet_groups.py <a solana wallet you track>
python3 tools/diag_wallet_groups.py <a solana wallet> <a mint it holds>   # + cost basis
python3 tools/diag_wallet_groups.py 0x<an evm wallet you track>
journalctl -u memedash -f | grep -i "wallet-group\|etherscan\|solscan"
```

The diag prints which keys were found, which provider answered per chain, what
Dexscreener priced, and whether Solscan could reconstruct an average entry. It
writes nothing. If a column on the page looks wrong, this is the first thing to
run — not the logs.

### Open

1. **Every live provider is unverified.** Ranked by how likely they are to
   surprise you: Solscan's `account/defi/activities` response shape (section 3),
   Etherscan V2 `addresstokenbalance` entitlement (section 4), then Solana RPC
   and Dexscreener, which are the same calls the bot already makes.
2. **Robinhood Chain is configured out, not coded out.** Set
   `WG_CHAIN_ID_ROBINHOOD` and `EVM_RPC_ROBINHOOD` and it joins the scan with
   no code change. Its numeric chain id was not hard-coded because it was not
   confirmed.
3. **EVM cost basis is off** (`WG_EVM_BASIS=1` to enable). It reconstructs the
   USD side from native value in the same transaction at *today's* price, so it
   is always flagged `partial`. Solana is the accurate path.
4. **Discord still gets nothing.** v1.38 sends Telegram DMs, but the page does
   not ping Discord
   when a convergence appears. The SSE event (`"wg"`) is the hook if you want
   that — `_notify_subscribers("wg")` already fires on every appear/disappear.
5. **Groups are global, not per-user.** There is one Basic-auth password, so
   anyone with it sees and can edit every group. Fine for a single-operator
   dashboard, worth knowing before sharing the URL.

---

## 1. What was built

| File | Role |
|---|---|
| `dashboard/wallets.py` | Providers only. No knowledge of the dashboard — takes an httpx client and addresses, returns dicts. |
| `dashboard/wgroups.py` | Tables, the two loops, and `/api/wgroups/*`. Idle and free until a group exists. |
| `dashboard/static/wgroups.js` | The page. Lazily imported by app.js and handed a helper bundle, so the imports stay one-way. |
| `tools/diag_wallet_groups.py` | Provider verification from whichever machine runs it. |

Modified: `main.py` (7 lines of wiring plus the version), `static/app.js`,
`static/index.html` (nav entry + the page's CSS), `README.md`.

### The version convention now has FOUR spots

`VERSION` in `main.py`, `VERSION` in `app.js`, `?v=` on app.js in
`index.html`, and `?v=` on the `import("./wgroups.js")` inside `app.js`. Miss
the fourth and the browser serves a stale page while the sidebar reports
matching versions — the worst of both.

## 2. Why a round is ordered the way it is

The holdings loop (45 s, `WG_HOLDINGS_INTERVAL`) does this, in this order:

1. scan every tracked wallet — one request per Solana wallet, one per EVM
   wallet per chain. A wallet in two groups is scanned once.
2. work out which tokens **two or more wallets of the same group** hold. This
   is pure set arithmetic on what step 1 returned, before anything is priced.
3. price only those, plus tokens already on a card, plus positions that changed
   since last round.
4. fold the balance changes into the observed cost basis — now that there is a
   price to value them at.
5. rebuild each group's convergence set; anything that appeared or disappeared
   pushes the SSE event.
6. resolve at most `WG_BASIS_PER_ROUND` (8) chain cost bases, only for
   positions that are on a card right now.

**Steps 2 and 3 are in that order deliberately.** The expensive side is
pricing; the cheap side is intersection. Pricing everything six wallets hold
would be hundreds of Dexscreener calls a round to find the five tokens that
matter. This is the same "scan the low-cardinality side" rule the FOMO wallet
work landed on.

Prices refresh on their own loop (15 s, `WG_PRICE_INTERVAL`) over the carded
set only, so PnL moves without re-scanning a single wallet.

## 3. Cost basis — the layers, and why they exist

Holdings are easy; cost basis is the hard half. Five sources, best first, and
the page always says which one it used (hover the average-entry cell):

| `basis` | Means | Shown as |
|---|---|---|
| `chain` | Full swap history read from Solscan | exact entry |
| `partial` | History truncated, or a SOL leg priced at today's SOL price | entry + `≈` |
| `observed` | Buys this dashboard watched happen — exact for anything opened while it was running | entry + `◷` |
| `pre-existing` | Position predates tracking and no history was readable | `—` |
| `unknown` | A balance moved while the token had no price | `—` |

Three rules keep this honest, and all three matter:

- **`fold_trades()` returns None rather than fold an unpriced buy in at $0.**
  A $0 cost basis renders as infinite profit — a confidently wrong number is
  worse than a dash.
- **A wallet's first scan records everything as `pre-existing`, not
  `observed`.** We did not watch those buys; inventing a basis for them would
  be a guess presented as a fact. They get a real entry from swap history when
  they qualify for a card.
- **Combined PnL only sums wallets with a known basis**, and the footer says
  `2/3 wallets priced` when it had to.

If the reconstructed size disagrees with the on-chain balance by more than 2%
(transfers in, an airdrop, a truncated page), the average entry is kept and the
basis is rescaled onto the amount actually held — and the row drops to
`partial`. A failed lookup backs off 6 h, or 15 min if the *provider* was the
thing that was down (`_basis_provider_ok`).

### Solscan, and the free key

`solscan_get()` negotiates prefix and header style once and remembers, the same
way `fomo/solscan_api.py` does. **It tries `/playground` before `/v2.0`**,
because the key in `fomo/.env` is a free one and every `/v2.0` path answers 401
for it. Override with `SOLSCAN_PREFIXES="v2.0"` if the plan is ever upgraded.

The response parser is the least verified code in the feature. It reads
`routers.token1/amount1/token1_decimals` and the `child_routers` under it,
prefers Solscan's own `value` field when it is a positive number, and otherwise
prices the counter-leg: a stablecoin leg is exact, a WSOL leg uses today's SOL
price and is marked `partial` if the trade is more than seven days old. If
`/playground` does not serve `account/defi/activities` at all on a free key,
every Solana position falls back to `observed` and the page says so in the
notes line — it does not fail, it just gets less accurate.

## 4. EVM holdings — two providers, one of them free

1. **Etherscan V2 `addresstokenbalance`** — one request per wallet per chain,
   `chainid` selects the chain. It is a **Pro-plan action**. On the first
   refusal the provider retires itself for the life of the process rather than
   spending a request per wallet per round to re-learn the same thing.
2. **Watchlist scan** — batched `eth_call balanceOf` (40 per HTTP request) over
   the EVM tokens memedash already knows on that chain, against a public RPC.
   Free, needs no key, and **only discovers tokens the bot has seen**. The page
   states this in its notes line when it is the provider in use, because "we
   scanned everything" and "we scanned what we knew" look identical otherwise.

Which one is running is in the diag output and in `PROVIDER_STATUS`.

## 5. What was and was not tested

Verified offline, with the real code:

- seeded-provider round trips proving a card appears at exactly two wallets and
  is removed when the count drops below two, plus the SSE push on both
- the cost-basis ladder: `pre-existing` → `chain` when history arrives,
  `observed` for a position opened while watching, average entry surviving a
  60% partial sell, `fold_trades` refusing an unpriced buy, and the >2% rescale
- the real FastAPI app under uvicorn with fake providers, driven by Playwright:
  min-wallet filter (4/2/1 cards at 2/3/4), both sort directions, the editor
  modal, a card vanishing when three of four holders sell, zero JS errors
- HTTP Basic auth returning 401 on every new endpoint, GET and POST

Not tested, and not testable from here: **every live API**. Neither the Cowork
cloud container nor the desktop VM has egress to Dexscreener, Solscan, Helius
or Etherscan — all four 403 at the proxy. This is the same constraint the FOMO
work hit (`fomo/HANDOFF.md` §5), and the same answer: the logic is verified
offline against seeded data, and the network half runs in your terminal.

## 6. Environment and env vars

Keys are found automatically — real environment variables win, then
`dashboard/.env`, then `../fomo/.env`, then `../.env`. That is where
`SOLANA_RPC`, `SOLSCAN_API_KEY` and `ETHERSCAN_API_KEY` already live on the
VPS, so there is **nothing new to deploy**. Nothing is required either: with no
keys at all the page runs on public RPCs with observed cost basis only.

```
SOLANA_RPC, SOLANA_RPC_FALLBACKS   wallet positions (Helius)
SOLSCAN_API_KEY                    real average entry from swap history
ETHERSCAN_API_KEY                  EVM balances (Pro) and EVM history
EVM_RPC_ETHEREUM|BASE|BSC|…        override the public balance RPCs
WG_CHAIN_ID_<CHAIN>                add a chain that is not built in
WG_HOLDINGS_INTERVAL   45          seconds between wallet scans
WG_PRICE_INTERVAL      15          seconds between price refreshes
WG_MIN_POSITION_USD    50          below this, a wallet does not count as holding
WG_ALERTS              1           0 disables Telegram alerts entirely
WG_ALERT_CHAT          unset       override the DM destination
WG_COOL_SECONDS        900         how long a broken card lingers as "cooling"
WG_DISCOVER_INTERVAL   21600       background wallet-discovery refresh
WG_DISCOVER_TOKENS     12          convergences read per discovery scan
WG_DISCOVER_MIN        2           convergences a wallet needs to be suggested
WG_DISCOVER_EARLY_N    10          how many first buyers count as "early"
WG_BASIS_PER_ROUND     8           chain cost-basis lookups per round
WG_EVM_BASIS           unset       1 = reconstruct EVM entry from Etherscan
SOLSCAN_PREFIXES       playground,v2.0
```

New tables live in the same `dash.db`: `wgroups`, `wgroup_wallets`,
`wallet_holdings`, `wallet_lots`, `wgroup_tokens`, `wgroup_seen`, `wgroup_hidden`
(v1.37) and, from v1.38, `wgroup_members`, `wgroup_exits`, `wgroup_alerts` and
`wgroup_candidates`. They are
created on startup and are **not** rebuildable from `ca_history.json` — the
group definitions are the only thing in that database that is real state rather
than a read model. Deleting `dash.db` now loses your groups. Everything else in
those tables re-derives on the next scan.

## 7. Noise control

Two filters keep the page from filling with things that are not signals, and
both are worth knowing before you conclude a token is missing:

- **Dust does not count.** A wallet holds a token, for the purpose of the
  2-wallet threshold, only above `WG_MIN_POSITION_USD` — **$50 as of v1.37**,
  and the summary line says so, because "why is that token not here" is
  otherwise unanswerable from the page. Airdrops of a hot memecoin to every
  wallet in a group would otherwise manufacture a card.
- **What you dismissed stays dismissed.** `wgroup_hidden` is checked on the way
  out of `/live`, so a token you ✕'d is absent from the feed and from the group
  picker's count while still being scanned. If a token you expect is missing,
  check the "N hidden" chip before you check the providers.
- **Majors, stables, wrapped natives and LSTs are skipped**, by mint and by
  symbol. And a token with no Dexscreener pair never qualifies at all, which
  quietly excludes LP receipts and staking positions.

## Running it

```powershell
# local, no keys needed — the page works, cost basis is observed-only
cd C:\Users\mzshu\Downloads\memebot\dashboard
python -m uvicorn main:app --port 8080
# http://localhost:8080/#/wallets
```

```bash
# VPS
systemctl restart memedash
journalctl -u memedash -n 50
```
