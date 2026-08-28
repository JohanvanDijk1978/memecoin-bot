# memedash — handoff (2026-08-28)

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
4. **No alerting off the page.** v1.37 added a sound in the browser, but the
   page still does not ping Telegram or Discord
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
WG_BASIS_PER_ROUND     8           chain cost-basis lookups per round
WG_EVM_BASIS           unset       1 = reconstruct EVM entry from Etherscan
SOLSCAN_PREFIXES       playground,v2.0
```

New tables live in the same `dash.db`: `wgroups`, `wgroup_wallets`,
`wallet_holdings`, `wallet_lots`, `wgroup_tokens`, `wgroup_seen`, and — from
v1.37 — `wgroup_hidden`. They are
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
