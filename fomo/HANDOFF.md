# fomo bot — handoff (2026-08-18)

Session 2 solved the Solana wallet problem and wired it into the bot. Session 3
fixed hot-mint discovery. Session 4 added verified EVM smart wallets.

**Status: `/fomo` shows each trader's verified Solana and EVM wallets.**

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
- `fomo_bot.py`: resolves both chains and adds EVM + explorer links. First-time
  cache writes are sequential because both resolvers preserve fields in one file.
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
