# FOMO (fomo.family) API — research notes

Researched 2026-08-18 against `https://prod-api.fomo.family` from a logged-in browser on
`borz`. Everything below was **verified live**, not inferred from docs (there are none).

---

## 0. TL;DR for the bot

| Question | Answer |
|---|---|
| Base URL | `https://prod-api.fomo.family` |
| `api.fomo.family` | Does **not** resolve to the API — use `prod-api.` |
| Auth | `Authorization: Bearer <Privy access JWT>` — required on every route |
| Token lifetime | **60 minutes** (ES256, `iss=privy.io`, `aud=cm6h485o300n3zj9yl6vpedq7`) |
| Refresh | `POST https://auth.privy.io/api/v1/sessions` |
| Envelope | `{success, message, responseObject, statusCode}` |
| Datacenter IPs | **Cloudflare-blocked.** Vultr VPS and Anthropic's cloud both get 403 on every path. |

---

## 1. The Cloudflare wall (the thing that shapes the whole design)

- The Vultr VPS gets an identical ~4.5 KB Cloudflare 403 on **every** path — this was already
  known, and it is still true. Route probing from there is useless.
- Anthropic's cloud container is blocked at the egress proxy too (403 on CONNECT).
- Even from a residential IP, a **top-level browser navigation** to
  `https://prod-api.fomo.family/v2/users/userHandle/Binkieee` gets the "Sorry, you have been
  blocked" interstitial. The WAF wants an XHR from the app, not a document request.
- The same URL fetched **as XHR from an open `https://fomo.family` page** works fine.

Practical consequence: the request must come from a residential IP with browser-ish headers
(`Origin: https://fomo.family`, `Referer: https://fomo.family/`, a real User-Agent).
**Do not build TLS-fingerprint / Cloudflare evasion** — the fix is where the request originates.

---

## 2. Auth

The web app authenticates with **Privy**. `localStorage` holds:

- `privy:token` — the access JWT sent as `Authorization: Bearer …`
- `privy:refresh_token` — used to mint a new access token

Access token claims: `sid, iss, iat, aud, sub, exp`. `exp - iat = 3600s`.

### Refresh (verified route, verified header contract)

```http
POST https://auth.privy.io/api/v1/sessions
Content-Type: application/json
privy-app-id: cm6h485o300n3zj9yl6vpedq7

{"refresh_token": "<refresh token>"}
```

A deliberately invalid token returns `401 {"error":"Invalid auth token","code":"missing_or_invalid_token"}`,
which confirms the route and that no extra client-id header is needed to reach the auth stage.

> **Privy rotates refresh tokens on use.** Whichever process refreshes last owns the session —
> so if the bot shares Johan's refresh token, logging in on the website will eventually
> invalidate the bot (and vice versa). Persist the rotated token to disk, and consider a
> dedicated FOMO account for the bot if that friction shows up.

### Auth failure modes

| Situation | Response |
|---|---|
| No `Authorization` header | `401 {"success":false,"message":"No authorization token provided",…}` |
| Garbage bearer | `401 {"success":false,"message":"Unexpected error in JWT authentication middleware",…}` |
| Unknown handle | `404 {"success":false,"message":"User not found","responseObject":null,"statusCode":404}` |
| Unknown route | `404 Not Found` (plain text, no envelope) |
| Bad/missing query param | `400 {…,"responseObject":{"errorCode":"ERR_VALIDATION_FAILED","validationErrors":[{field,message,code}]}}` |

The 400 validation errors name the missing field — that is how the param names below were found.

---

## 3. Routes that exist (and the ones that don't)

Enumerated by pulling all 239 JS chunks from the app's Remix manifest and grepping for
`/v2/` and `/v3/` path literals, then probing each.

### ❌ Does not exist

- `/v3/users/userHandle/{handle}` → `404 Not Found`. **There is no v3 at all** — zero `/v3/`
  literals in the entire bundle.
- `/v2/users/handle/{handle}` → `404 Not Found`.

Only `/v2/users/userHandle/{handle}` is real.

### ✅ Verified working

| Method | Path | Notes |
|---|---|---|
| GET | `/v2/users/userHandle/{handle}` | **case-insensitive** (`binkieee` → `Binkieee`) |
| GET | `/v2/users/{id}` | same object, keyed by UUID |
| GET | `/v2/users/{id}/leaderboard` | user object **+ rank/PnL blocks** |
| GET | `/v2/users/fuzzy-search?searchTerm=…&limit=…` | param is `searchTerm`, **not** `query` |
| GET | `/v2/users/{id}/swaps?limit=…` | `{swaps[], hasNextPage}` |
| GET | `/v2/users/{id}/followers` | `{users[…]}` — returns 200 regardless of `limit` |
| GET | `/v2/users/{id}/balances` | `{balances[], otherPnl, nativeEvmBalances[], livePerpPnl}` |
| GET | `/v2/users/{id}/spotlight` | `{bestTrades[], bestComments[]}` |
| GET | `/v2/leaderboard?limit=…` | `limit` is **required** (nan → 400) |
| GET | `/v2/leaderboard/{period}?limit=…` | `24h` confirmed → "24H Leaderboard found" |

### The Holders tab — `GET /hodlers/top` (found 2026-08-20)

**It is spelled `hodlers`.** Every `/holders` probe 404'd for that reason alone.
Recorded off the wire by `token_page_sniff.py` while loading
`https://fomo.family/tokens/solana/<address>` and clicking Holders:

```
GET /hodlers/top?tokens=[{"address":"<mint>","networkId":1399811149}]
    -> [{ tokenAddress, networkId, totalHolders, topHolders: [...] }]

GET /hodlers/devs?tokenAddress=<mint>&networkId=1399811149
    -> { tokenAddress, networkId, devHoldings }
```

`tokens` is a JSON **array** in the query string, so one call can cover several
tokens. `totalHolders` matched the UI's `Holders (1,005)`. The identity rows sit
nested under `topHolders`, which is why a top-level shape check reported this as
a miss on the first pass.

Other routes the same page used, none documented before:

| route | returns |
|---|---|
| `/feed/token?tokenAddress=&networkId=&excludeThesis=true&threshold=` | token activity feed with `displayName`, `marketCap`, `price` |
| `/feed/token/thesis` / `/feed/token/sortedThesis` | theses on the token, with `equity` and `authorTrade` — **`/thesis` calls `sortedThesis` first, but the shape is still a guess** (session 34); it falls back to `/hodlers/top` + `/trades/{tradeId}` |
| `/feed/tradingActivity?limit=&threshold=` | global recent trades |
| `/trades?userId=&orderBy=closedAt&tokenAddress=` | **`tokenAddress` filters the trades list** |
| `/v2/users?userIds=<id>&userIds=<id>` | batch user lookup — repeated `userIds` params |
| `/v2/users/{id}/swaps?tokenAddress=` | per-token swap history |
| `/proxy/tokenDetails`, `/proxy/tokenWarnings`, `/proxy/verifiedTokens` | market data and safety flags |
| `/tokenAllowList/detailed`, `/watchlist`, `/config` | app configuration |

`/v2/users?userIds=` is the natural partner to `/hodlers/top`: whatever user
identifiers the holder rows carry can be resolved to handles in one batch call.

### Probed 2026-08-20 — `userTokens` is per-user, not per-token

`/v2/userTokens/aggregatedSnapshot`, `/aggregatedSnapshotById` and
`/aggregatedSnapshot/interval` all exist (400 `ERR_VALIDATION_FAILED`, not 404)
and every one of them requires `query.userId`:

| route | required query |
|---|---|
| `/v2/userTokens/aggregatedSnapshot` | `userId`, `timestamp` |
| `/v2/userTokens/aggregatedSnapshotById` | `userId`, `snapshotId` (number) |
| `/v2/userTokens/aggregatedSnapshot/interval` | `userId` |

So this family is one trader's portfolio over time, **not** the token page's
Holders tab — confirmed live, the page calls it as
`aggregatedSnapshotById?userId=<id>&snapshotId=<unix>` and gets back
`{equity, pnl, snapshotId}`. No `/holders`-style route responded on any prefix
because the real one is spelled `/hodlers` (above). `/token`'s
FOMO identities therefore still come from the local `wallet_cache.json` reverse
lookup, which can only name handles `/fomo` has already resolved.
`token_page_sniff.py` records what the token page actually calls. That page is
chain-scoped: `https://fomo.family/tokens/{chain}/{address}`, e.g.
`https://fomo.family/tokens/solana/E3i7sTY5QYEBh3itepnomZQt7Eh5kzmHFk1vkm2pump`
— the unscoped `/tokens/{address}` and `/token/{address}` shapes do not
render.

### Seen in the bundle, not yet probed

`/v2/users/{id}/followingPaginate`, `/mutuals`, `/recommendedUsers`, `/referrals`,
`/referrerDetails`, `/transfers`, `/withdrawals`, `/v2/users/current`,
`/v2/users/current/followingIds`, `/v2/users/twitterCache`, `/v2/leaderboard/following`,
`/v2/clans/{id}` (+ `/feed`, `/holdings`, `/holdings/breakdown`, `/thesis`),
`/v2/clans/leaderboard`, `/v2/userTokens/aggregatedSnapshot(/interval|ById)`,
`/v2/supportedTokens`, `/v2/status`.

Anything under `/v2/send`, `/v2/fast-fill`, `/v2/refresh-tx`, `/v2/users/exportedKeys`,
`/v2/users/edit` is **write/trading/key material — leave it alone.**

---

## 4. The user object — full schema

`GET /v2/users/userHandle/{handle}` → `responseObject`, **26 keys**, stable across handles.

```jsonc
{
  "id":                     "bea10b9a-6cc5-51f2-9504-b7cb2d7e7a94",  // uuid
  "address":                "5PVeHkHCodbNyF4M194zoWtMWbkCLKyNKWvG19ouMt3H", // Solana
  "evmAddress":             "0x0137c178aa38535e8893a7583d8778f87bf0df29",
  "createdAt":              "2026-01-20T01:36:54.658Z",
  "displayName":            "Binkieee",
  "userHandle":             "Binkieee",   // <-- NOT "profileHandle"
  "profilePictureLink":     null,          // S3 URL or null
  "description":            "TROUPE KZ",
  "following":              51,
  "followers":              135507,
  "activated":              false,
  "isReferred":             true,
  "isRestricted":           false,
  "swapCount":              3735,
  "numTrades":              1257,
  "totalVolume":            9417335.999858,   // USD, lifetime
  "private":                false,
  "thumbhash":              null,
  "coverPhotoLink":         null,
  "coverPhotoThumbhash":    null,
  "clan":                   { "id": "...", "name": "Troupe KZ", "iconLink": "...", "iconThumbhash": "...", "role": "member" },
  "followsCurrentUser":     false,
  "numFriendsFollowing":    10,
  "friendsFollowing":       [ /* array of full user objects */ ],
  "averageHoldTimeSeconds": 325213,
  "twitter":                "https://x.com/Binkieeefomo"   // full URL or null
}
```

> ⚠️ **Correction to the original spec:** the field is `userHandle`, not `profileHandle`.
> `volume` is `totalVolume`. `averageHoldTimeSeconds` and `twitter` are the last two keys —
> they were being missed because responses get truncated before reaching them.

`followsCurrentUser` / `numFriendsFollowing` / `friendsFollowing` are relative to **whoever's
token is being used**, so they'll reflect the bot's account, not the asker's.

### Verified across three handles

| handle | followers | following | swapCount | numTrades | avgHold | totalVolume | clan |
|---|---|---|---|---|---|---|---|
| Binkieee | 135,507 | 51 | 3,735 | 1,257 | 325,213s (3d 18h) | $9.42M | Troupe KZ |
| change | 303,819 | 58 | 5,676 | 797 | 265,649s (3d 1h) | $20.97M | Ender |
| PoorGoat_ | 315,258 | 27 | 1,682 | 1,060 | 155,348s (1d 19h) | $1.36M | Conviction Capital |

---

## 5. PnL and ranks — `GET /v2/users/{id}/leaderboard`

Returns the full user object **plus**:

```json
{
  "rank":          { "rank": 445047, "pnl": -566292.62 },
  "rank24h":       { "rank": 282502, "pnl":  -50142.50 },
  "rank7d":        { "rank": 327626, "pnl": -323290.89 },
  "rank30d":       { "rank": 378760, "pnl": -315394.28 },
  "rankCampaigns": {}
}
```

This is the only place PnL-by-timeframe is exposed per user, so a full profile card is
**two calls**: `userHandle/{handle}` → take `id` → `users/{id}/leaderboard`.
(In practice `/leaderboard` alone carries both, so one call is enough if you already have the id.)

`GET /v2/leaderboard?limit=n` returns user objects with a flat `totalPnL` field instead.
`GET /v2/users/fuzzy-search?searchTerm=…` returns user objects with `pnl24h`.

---

## 6. Other response shapes

**`/v2/users/{id}/swaps?limit=n`** → `{swaps: [...], hasNextPage: bool}`; each swap:
`id, address, networkId, inTokenAddress, inAmount, inHumanAmount, outTokenAddress, outAmount,
outHumanAmount, humanUsdAmountIn, humanUsdAmountOut, createdAt, platformFeeAmount,
platformFeeHumanAmount, platformFeeToken, inTradeId, outTradeId, referralFeeTokenAmount,
referralFeeHumanAmount, referralFeeToken, referralFeeAddress, isOffPlatform, isCrossmint,
provider, inNetworkId, outNetworkId, recipient`.

**`/v2/users/{id}/balances`** → `{balances: [{balance, tokenFilterResult, userToken, activeTrade}],
otherPnl, nativeEvmBalances[], livePerpPnl}`.

**`/v2/users/{id}/spotlight`** → `{bestTrades[], bestComments[]}`, entries shaped
`{trade, swaps, transfers, displayName, userHandle, profilePictureLink, userId, comment}`.

### Open question: swaps pagination

`limit` works. `offset`, `page`, `before`, `cursor`, `createdBefore`, `lastCreatedAt` and
`startingAfter` are all **silently ignored** (same first row every time) while `hasNextPage`
stays `true`. The real cursor param is still unknown — worth one look at the app's own network
tab while scrolling a profile's swap list.

---

## 7. Rate limits

Unknown. The only CORS-exposed response headers are `cf-ray, content-length, content-type,
x-request-id` — no `x-ratelimit-*`. Treat it as an undocumented private API: cache aggressively,
keep it to a few requests per lookup, and don't hammer it.

---

## 8. Ethics / footing

This is FOMO's own private app API, reached with Johan's own logged-in session, reading data
that is already public on trader profile pages. It is not a scraper bypass and no evasion is
involved. If this grows beyond personal use, the right move is the one already noted for
Frontrun: ask FOMO directly for read access.

## Endpoints observed on a live profile page (2026-08-18)

Captured by `find_wallet_source.py` recording all 203 responses while loading
`/profile/{handle}` in the logged-in Playwright browser. These were NOT in the
route list derived from the JS bundle — worth mining before writing new features.

**User-scoped** (all take the uuid from `/v2/users/userHandle/{handle}`):

| Route | Notes |
|---|---|
| `/trades?userId={id}&orderBy=realizedPnlUsd` | per-token realised PnL — likely the best-trades panel |
| `/v2/userTokens/aggregatedSnapshot?userId={id}&timestamp={iso}[&interval=4]` | portfolio value at a point in time; drives the chart |
| `/v2/userTokens/aggregatedSnapshotById?userId={id}&snapshotId={epoch}` | same, keyed by a daily snapshot id (e.g. 1786968000) |
| `/v2/transfers/with/{id}` | transfers between the viewer and that user |
| `/v2/users/{id}/spotlight` | already known |
| `/v2/users/current/followingIds` | who the *token owner* follows — cheap follow-graph source |

**Global / viewer-scoped:**

`/config` · `/watchlist` · `/hodlers/friends` · `/tokenAllowList/detailed` ·
`/transfers/v2/supportedTokens` · `/v2/users` (bare) ·
`/proxy/mostHeld` · `/proxy/verifiedTokens` · `/proxy/cryptoTokens` · `/proxy/filterTokens`

Note `/trades`, `/watchlist`, `/config`, `/proxy/*`, `/hodlers/*` and
`/tokenAllowList/*` sit at the ROOT, not under `/v2`. Confirms again there is no `/v3`.

Third-party calls the app makes: `api.hyperliquid.xyz/info` (perp data),
`auth.privy.io`, PostHog + Datadog RUM via `app-actions*.fomo.family`,
`status.fomo.family/prod`, and a Facebook pixel.

### The wallet-mismatch finding

For `onmycheck` and `FIippingProfits`, the address we believed correct appears in
**none** of the 203 responses, is not in the page's visible text, and is not in any
link href. fomo.family does not publish that address anywhere on the profile.
So `raw["address"]` is not a wrong *field* — the two sources disagree about which
wallet is the trader's. `verify_wallet_onchain.py` settles it by reading the fee
payer off the trader's own swap signatures.

---

## 9. `/trades/{tradeId}` — the trade-detail route (verified 2026-08-18)

Not in the JS-bundle route list. Found by feeding a swap's `inTradeId` / `outTradeId`
to candidate paths. **Only `/trades/{id}` works** — `/v2/trades/{id}` is 404,
`/trades?tradeId=` and `/trades?id=` are 400. Not every trade id resolves; some return
`{"success":false,"message":"Trade with id ... not found"}`.

```jsonc
{
  "trade": {
    "id", "userAddress", "tokenAddress", "networkId",
    "avgEntryPrice", "avgExitPrice", "avgTransferInPrice", "avgTransferOutPrice",
    "humanTokenAmount", "totalCostBasis", "realizedPnlUsd",
    "sumSwapOpen", "sumSwapClosed", "sumTransferIn", "sumTransferOut",
    "createdAt", "closedAt", "updatedAt", "commentId",
    "tokenMetadata": { "symbol", "currentPrice", "liquidity", "networkId", "imageLargeUrl", "thumbhash" }
  },
  "swaps": [ /* the swaps composing this trade, same shape as /users/{id}/swaps */ ],
  "transfers": [],
  "comment": { "comment", "commentSegments", "reactions", "numLikes", "olderThesis", "newerThesis", ... },
  "userHandle", "userId", "displayName", "profilePictureLink", "isDev", "numReplies"
}
```

This is the richest per-trade view in the API — realised PnL, entry/exit averages, cost
basis and the trader's written thesis. The bot now uses trade-list and spotlight rows
for profile metrics and tracking; the detail route remains available for future drill-down.

The API does not expose historical market cap on saved examples. For Latest
buys, the bot reconstructs it as `current market cap × avgEntryPrice ÷ current
price`, using the most liquid DEX Screener pair for current market data. This is
an inferred figure and is displayed with `~`; a future direct historical field
would take precedence.

The swaps route is also not a complete cross-chain history: Collectible's two
WALL3 buys on Robinhood Chain (network 4663) were absent. The bot supplements it
with the public Robinhood Chain Blockscout token-transfer API for the verified
EVM wallet. An incoming token transfer counts as a buy only when the same
transaction contains a stablecoin or otherwise USD-priced input.

`GET /trades?userId={id}&orderBy=realizedPnlUsd` returns
`{activeTrades[], closedTrades[], hasNextPage, closedCount}`. Note `userAddress` is
**null** on those rows even though the detail route populates it.

## 10. None of the published addresses is the trading wallet

**Confirmed 2026-08-18.** fomo.family exposes four addresses per trader and all four are
synthetic, with zero on-chain history:

| field | where | on chain |
|---|---|---|
| `user.address` | user object | no |
| `swap.address` | every swap, constant per user | no |
| `trade.userAddress` | `/trades/{tradeId}` | no (same value as `swap.address`) |
| `evmAddress` | user object | — |

There is **no transaction signature anywhere in the API** either.

The real wallet is derived on chain instead — see `fomo_wallet.py`. Two facts make it
work:

1. **FOMO sponsors gas.** The fee payer is always the platform account
   `AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51`. **The trader is the signer that is
   NOT the fee payer.** Reading `signers[0]` gets the sponsor, every time.
2. **`swap.outTokenAddress` + `swap.outHumanAmount` + `swap.createdAt` identify the
   transaction.** `createdAt` tracks `blockTime` to about a second.

Verified on Konito (`93fjdwW7...`) and onmycheck (`Ay77dkJk...`), 5/5 corroborating
swaps each, ~11 RPC calls per handle, cached permanently.

> Never make a **mint** the index. A hot memecoin can carry >12000 signatures newer
> than a two-hour-old swap, so paging one backwards does not terminate — that is what
> made `@Rowdy` unresolvable until the sponsor route existed.
>
> Discover the transaction in the **gas sponsor's** history: it holds every FOMO trade
> and only FOMO trades, so its length is bounded by the platform's throughput rather
> than by a token's virality. Then verify against the **wallet's own history**, which
> runs to hundreds of signatures rather than thousands. `getBlock` at the swap's
> timestamp is the last resort and depends on no signature history at all.

## 11. EVM wallet resolution

`user.evmAddress` is not the EVM trading wallet. Konito's API value has no code
and nonce zero on Base and BNB Chain.

FOMO documents that its EVM accounts are ERC-4337 smart-contract wallets on
Base and BNB Chain:
https://fomo.family/blog/learn/fomo-security-wallet-architecture

`fomo_evm.py` first attempts transaction-backed discovery. It asks the
`/trades/{id}` detail route for several low-liquidity/older EVM positions, then
matches those historical swaps against token transfers on the corresponding
chains. Direction, timestamp and token amount must agree; stablecoin value is
also checked whenever the transaction exposes it. The same address must explain
at least two independent transactions, and it must have smart-wallet code on an
evidence chain before it is cached as `transactions+rpc`.

Current balance-fingerprint discovery is the second path for an open EVM
position. It matches FOMO's exact token balance against the public holder set,
confirms the candidate with live ERC-20 `balanceOf`, and requires deployed
smart-wallet code. If neither transaction evidence nor a unique current-balance
fingerprint is available, automatic discovery returns no wallet and tries again
on a later lookup. Empty results are not cached.

When the operator has independently verified the owner,
`evm_resolve.py --handle HANDLE --wallet 0x...` is the explicit fallback. It
requires contract code on at least one reachable configured chain and caches
the source as `manual+rpc`. The deployment check proves that it is a live smart
wallet; ownership of the handle/address mapping remains the operator's claim.
