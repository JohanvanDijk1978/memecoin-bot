# Handoff: Multi-wallet buy alerts

**Date:** 2026-08-29
**Commit:** `195dd86` (committed locally from the Cowork session — **not pushed**, no GitHub credentials there)
**Status:** Complete and offline-tested. Nothing has touched a live RPC or Telegram yet — see *What was verified, and what was not*.

When several monitored wallets buy the same token inside a window, the bot posts to its own
Telegram channel:

```
🚨 3 wallets bought feesh
📋 List: ALL · Rule: ≥3 wallets in 120 min

🪙 feesh (feesh) · Solana
💰 Market cap: $129.46k · Price: $0.0001294
📄 CA: B7q2X2uMrft6VaVJMcRy7Zoia9tpxHgWC3qgiak8pump

🛍 Buys:
• rowdy · 12.74M feesh · $88.20k MC · TX (20:39 UTC)
• ProfitPUMP · 15.69M feesh · $102.00k MC · TX (21:03 UTC)
• RowdyFOMO · 15.12M feesh · $110.40k MC · TX (21:12 UTC)

🔗 DexScreener | GMGN | Birdeye | Explorer | Website | Twitter
```

The market cap on each buy line is the market cap **at the moment of that buy**, computed
from the transaction itself (USD spent ÷ tokens received × supply) — not a quote taken when
the alert fired, which can be minutes later.

---

## Deploy — three steps, in this order

**1. Push.** From VS Code, `git push origin main`. The webhook pulls and `/root/deploy.sh`
restarts `memebot`. Nothing below works until the code is on the box.

**2. The channel.** Create the Telegram channel, add the bot as an **admin with permission
to post**, then forward any message from it to
[@username_to_id_bot](https://t.me/username_to_id_bot) to read its `-100…` id, and put it in
`/root/memecoin-bot-new/.env`:

```
MULTIWALLET_CHANNEL_ID=-1001234567890
```

Unset, alerts go to `YOUR_TELEGRAM_USER_ID` (your DM), so the feature is testable before the
channel exists. Restart after editing: `systemctl restart memebot`.

**3. The chain keys.** The watcher reads `SOLANA_RPC`, `ETH_RPC`, `BASE_RPC`, `BSC_RPC`,
`ROBINHOOD_RPC` and the matching `*_WSS` from `fomo/.env`, without overriding anything
already set in the bot's own `.env` — the same precedence `dashboard/wallets.py` uses, so
one deployed file serves both services. **`fomo/.env` is not in git and was absent from the
VPS as of 2026-08-28.** Copy it once:

```
scp C:\Users\mzshu\Downloads\memebot\fomo\.env root@209.250.245.16:/root/memecoin-bot-new/fomo/.env
```

Without it: Solana degrades to the public RPC with **no websocket** (sweep only, ~30s late),
and the four EVM chains have **no endpoint at all** and are skipped entirely.

---

## Architecture

| File | Responsibility |
|---|---|
| `src/multiwallet_store.py` | SQLite at `data/multiwallet.db` (WAL, gitignored). `mw_wallets`, `mw_buys`, `mw_alerts`, `mw_cursors`, `mw_tokens`, `mw_config`. |
| `src/multiwallet_sources.py` | Chain detection. Pure parsers `parse_solana_buys` / `parse_evm_buys`, plus `SolanaWatcher` and `EvmWatcher` (websocket + reconcile sweep). |
| `src/multiwallet.py` | The rule, the message, the Telegram send, `run_multiwallet_watcher()`. |
| `src/bot.py` | `/add /remove /list /buys /multirule`, behind the existing `is_allowed` gate and registered in `BOT_COMMANDS`. |
| `main.py` | `run_multiwallet()` in the existing `asyncio.gather`. |
| `tools/diag_multiwallet.py` | Live probe for the VPS: endpoints, websocket handshakes, channel reachability, wallet replay. |
| `tools/test_multiwallet.py` | Offline test. No keys, no network, no bot token. |

This is memebot's first database. JSON was considered and rejected: every read here is a
windowed range query ("which distinct wallets bought this CA in the last 120 minutes") and
every write is one row on a hot path. `sqlite3` is stdlib and `data/` is already gitignored,
so this added no dependency and nothing to deploy.

### Why not memedash

`dashboard/wgroups.py` already tracks wallet groups, but it is a **holdings poller** — it
asks what a wallet owns every 45s and diffs. That answers "who holds this now", not "who
just bought", and it cannot give you a transaction hash, a buy time, or a price paid. The
two features are complementary and share nothing but the idea of a wallet list.

---

## The detection contract

**A buy is:** the wallet's token balance went **up**, and in the **same transaction** value
went **out** of that same wallet — SOL/ETH/BNB, wrapped native, or a stablecoin.

Everything else is rejected by construction: airdrops, transfers in from another wallet,
sells, failed/reverted transactions, and another wallet's swap that merely mentions yours.
This is read from **balance deltas, not instructions**, so Jupiter, Pump, PumpSwap, Raydium,
Meteora and whatever ships next month all work without a per-DEX parser.

**Transport**

- **Solana** — one websocket, one `logsSubscribe {mentions:[wallet]}` per monitored wallet.
  Sub-second. Adding a wallet is one more subscribe on the same connection; `/add` is picked
  up within `MULTIWALLET_SYNC_SEC` (20s) with no reconnect.
- **EVM** — one `eth_subscribe("logs")` per chain, filtered server-side on the ERC-20
  Transfer topic with every monitored wallet in the `to` slot. One subscription covers the
  whole list, so cost does not grow with the number of wallets. A changed wallet list drops
  and re-subscribes (the filter is fixed at subscribe time).
- **Both** are backed by a reconcile sweep — `getSignaturesForAddress` per wallet with an
  `until` cursor, `eth_getLogs` from the last handled block — every `MULTIWALLET_RECONCILE_SEC`
  (300s) while the socket is up, every `MULTIWALLET_POLL_SEC` (30s) when it is not.
  **This sweep is the only thing covering a reconnect gap. Do not remove it when tidying.**
- A chain with no websocket URL runs sweep-only rather than going dark. `/list` and the diag
  both say which chains are live and which are sweeping.

Reconnects use exponential backoff to 120s. Every failure path logs; none of them can raise
into the bot's other loops.

---

## The alert lifecycle

1. A buy is parsed, priced (`quote × cached native price`) and written to `mw_buys`.
   `PRIMARY KEY (chain, tx, wallet, token)` means replaying a transaction is a silent no-op.
2. Distinct wallets for that `chain + CA` inside the window are counted. **A wallet counts
   once** however many times it bought; its line shows the summed amount, its first buy's
   time and link, and `×N`.
3. At `min_wallets` the channel gets a post. At each further wallet it gets another one —
   `4 wallets bought …` — never an edit. An edit produces no notification, and the fourth
   wallet is the most actionable moment the feature has.
4. `mw_alerts.max_count` holds the highest count already announced, so each milestone fires
   exactly once. **This is also what makes a restart and every sweep silent.**
5. Above `max_wallets` (6) the token is muted for `cooldown_h` (24h), then it may start over
   from the first milestone.
6. Dexscreener is called **only** when a token crosses the threshold, and only for that
   token. Nothing slow sits in the detection path.

A failed Telegram send records nothing, so the next buy retries the alert rather than
skipping the milestone.

---

## Commands

| Command | Notes |
|---|---|
| `/add <wallet> <name>` | Solana or EVM. An EVM address is watched on every configured EVM chain at once. Re-adding with a different name renames. |
| `/remove <wallet-or-name>` | Matches on address or display name. |
| `/list` | Wallets by chain, the active rule, and which chains are live vs sweeping. |
| `/buys` | Last 15 detected buys. **This is how you tell "nothing is happening" from "detection is broken".** |
| `/multirule 3 120` | min wallets, window minutes. Optional 3rd and 4th arguments set the milestone ceiling and the cooldown in hours. No arguments prints the rule. Persists to the DB — no redeploy, no restart. |

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MULTIWALLET_CHANNEL_ID` | your DM | Where alerts go. |
| `MULTIWALLET_EVM_CHAINS` | `ethereum,base,bsc,robinhood` | Chains to watch; a chain with no RPC is skipped. |
| `MULTIWALLET_RECONCILE_SEC` | 300 | Sweep interval while the socket is up. |
| `MULTIWALLET_POLL_SEC` | 30 | Sweep interval for a chain with no websocket. |
| `MULTIWALLET_SYNC_SEC` | 20 | How soon `/add` reaches the subscriptions. |
| `MULTIWALLET_EVM_LOG_SPAN` | 1800 | Max blocks one `eth_getLogs` sweep will ask for. |
| `MULTIWALLET_DB` | `data/multiwallet.db` | State file. Deleting it loses the wallet list. |

The rule itself lives in the DB (`/multirule`), not in the environment.

---

## What was verified, and what was not

**Verified, offline, in the cloud container** (`tools/test_multiwallet.py`, 22 checks, all
passing):

- Solana parser: a swap is a buy; an airdrop, a sell, a failed tx and another wallet's swap
  are not.
- EVM parser: WETH-out and native-ETH-out swaps are buys; an airdrop and a reverted tx are
  not; a token with unknown decimals is still detected and its amount filled in afterwards.
- Rule engine: two wallets are not enough; one wallet buying twice is still one wallet; the
  third wallet posts once; a further buy from a counted wallet is silent; the fourth wallet
  posts its own alert; **replaying every transaction after a restart posts nothing**; a buy
  older than the window does not count; the ceiling silences the token and the cooldown
  releases it.
- The five command handlers were driven with fake `Update` objects and render correctly.
- The watcher was run for real against a deliberately bogus RPC: backoff, degradation to
  sweep-only, and clean shutdown all behave.

**Not verified — this is the honest half:**

- No RPC, websocket or Telegram call has been made. Cowork has no egress to Helius, Alchemy,
  Dexscreener or api.telegram.org.
- Real transaction shapes. The parsers were fed transactions built by hand from the
  documented `getTransaction` / receipt schemas. A versioned-transaction quirk or an
  unusual `loadedAddresses` layout would only show up on the box.
- Whether Helius accepts ~40 concurrent `logsSubscribe` subscriptions on one connection, and
  whether Alchemy's free tier accepts a topic-only `logs` subscription per chain. The diag
  answers both in one run.
- Robinhood's swaps. Its wrapped-native address is unknown, so only native-value buys are
  detected there (see *Loose ends*).

---

## Verify on the VPS

```bash
cd /root/memecoin-bot-new
python3 tools/test_multiwallet.py            # should print "every check passed"
python3 tools/diag_multiwallet.py            # endpoints, websockets, channel
```

The diag is read-only and safe to run while the bot is live. What good output looks like:

- **Endpoints** — `solana https://mainnet.helius-rpc.com [SOLANA_RPC] · ws wss://…`, and each
  EVM chain resolving to `…g.alchemy.com [ETH_RPC]` etc. Anything reading `[public default]`
  or `— none` means step 3 of the deploy did not happen.
- **Websockets** — `OK — subscription <n>` on Solana and on every EVM chain. A refusal here
  is the one result that changes the design: it would mean falling back to sweep-only, which
  the code already supports through `MULTIWALLET_POLL_SEC`.
- **Telegram** — `OK — channel "…"`. `chat not found` means the bot is not an admin of the
  channel, or the id is wrong; `-100…` ids are easy to mistype.

Then a live smoke test, which takes about ten minutes:

```
/add <a wallet you know trades constantly> tester1
/add <a second one> tester2
/multirule 2 120        ← temporarily, so two wallets are enough
/buys                   ← after a few minutes: buys should be appearing
```

`/buys` populating is proof the whole detection path works end to end. When an alert lands,
put the rule back with `/multirule 3 120`, and `/remove tester1` / `tester2` if they were
only for the test. Then add the real list.

Log lines to watch:

```bash
grep -i multiwallet /root/memecoin-bot-new/data/bot.log | tail -40
```

`multiwallet solana: websocket up, N wallets` and `multiwallet <chain>: websocket up, N
wallets in one subscription` on start; `multiwallet: buy …` per detection; `multiwallet: 🪙
alerted N wallets on …` per post.

---

## Troubleshooting

| Symptom | First thing to check |
|---|---|
| Channel is silent, `/buys` is empty | Endpoints. `/list` footer says `(sweep)` or omits chains → `fomo/.env` is not on the box. |
| `/buys` fills but the channel stays quiet | The rule (`/multirule`), then the channel: diag's Telegram line. A missing admin right looks exactly like a broken watcher. |
| Buys appear only for Solana | EVM websocket refused or no `*_RPC` — diag's Websockets section names the reason. |
| One wallet never produces a buy | `python3 tools/diag_multiwallet.py <wallet> 50` — it prints a verdict per transaction, including *why* a transaction was not a buy. |
| An alert repeated after a restart | Should be impossible (`mw_alerts`). If it happens, check `data/multiwallet.db` was not deleted or replaced. |
| Alerts stop for one hot token | Expected: ceiling reached, 24h cooldown. `/multirule 3 120 8 6` would raise the ceiling to 8 and shorten the cooldown to 6h. |

---

## Loose ends, deliberately not done

- **Robinhood quote assets.** `_EVM_QUOTES["robinhood"]` in `multiwallet_sources.py` is
  empty — the chain's wrapped-native and stablecoin addresses are not known here. Native-value
  buys are detected; a swap routed through a wrapped token is not. One line fixes it once you
  have the addresses from a real Robinhood swap.
- **EVM wallet replay in the diag** is not implemented (Solana only). The `eth_getLogs`
  sweep covers the same ground on start, but it is less pleasant to read.
- **Named lists.** `mw_wallets.list` and the alert's `List: ALL` line exist, and the whole
  path is list-aware, but `/add` takes no list argument yet. Adding one is a command change,
  not a migration.
- **Per-list rules.** One rule for everything today.
- **Airdrop spam on EVM** costs one `eth_getTransactionReceipt` per spam transaction that
  targets a monitored wallet (deduplicated per tx). If a wallet is being blasted, this shows
  up as receipt calls, not as false alerts — the quote-out requirement rejects them.
- **`mw_buys` is pruned after 7 days.** `/buys` history does not go back further.
