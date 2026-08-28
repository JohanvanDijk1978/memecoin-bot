# memedash 🪙

Analytics dashboard for memecoin-bot. Reads `../data/ca_history.json` (bot untouched),
keeps its own SQLite DB, polls Dexscreener for live peaks, serves a dark web UI.

Full design: see `../DASHBOARD_DESIGN.md`.

## Wallet Groups

`#/wallets` tracks a set of wallets together and shows only the memecoins that
**two or more of them hold right now** — who is in, how much supply each
controls, and what each one is up or down. A card appears the moment a second
tracked wallet is in a token and is removed the moment the count drops below
two, so the page reads as a live signal feed rather than a table. Each card is
backed by the token's own banner art, a new convergence rings a bell you can
mute, and the ✕ in a card's corner dismisses that token from the group for good
(an "N hidden" chip in the bar brings it back).

- `wallets.py` — providers only: Solana positions over `getTokenAccountsByOwner`,
  EVM positions over Etherscan's Pro balance endpoint or a free `balanceOf`
  scan of tokens the dashboard already knows, prices from Dexscreener, and
  average entry from Solscan swap history.
- `wgroups.py` — the tables, the two loops and `/api/wgroups/*`. Idle and free
  until a group exists.

Cost basis is hybrid on purpose. A position opened while the dashboard was
watching is exact. A position that predates tracking is reconstructed from the
wallet's swap history when Solscan can answer, and otherwise shows `—` rather
than a made-up entry — every number on a card says where it came from (hover
the average-entry cell).

Check the providers on the machine that runs it:

```bash
python3 tools/diag_wallet_groups.py <wallet> [<token>]
```

## One-time VPS setup

```bash
# after git push has synced this folder to the VPS:
pip3 install fastapi uvicorn httpx
cp /root/memecoin-bot-new/dashboard/memedash.service /etc/systemd/system/
nano /etc/systemd/system/memedash.service   # set DASH_PASSWORD
systemctl daemon-reload
systemctl enable --now memedash
```

Then open `http://209.250.245.16:8080` — username anything, password = DASH_PASSWORD.

Add to `/root/deploy.sh` so redeploys pick up changes:

```bash
systemctl restart memedash
```

## Local dev (Windows)

```powershell
cd dashboard
pip install fastapi uvicorn httpx
python -m uvicorn main:app --port 8080
# open http://localhost:8080  (no DASH_PASSWORD set = no login)
```

## Env vars

- `DASH_PASSWORD` — enables HTTP Basic auth (any username). Unset = open.
- `HISTORY_FILE` — path to ca_history.json (default `../data/ca_history.json`)
- `DASH_DB` — path to SQLite DB (default `dashboard/data/dash.db`)

Wallet Groups reads a few more, and finds them on its own: real environment
variables win, then `dashboard/.env`, then `../fomo/.env`, then `../.env` —
which is where `SOLANA_RPC`, `SOLSCAN_API_KEY` and `ETHERSCAN_API_KEY` already
live on the VPS. Nothing is required; without keys the page falls back to
public RPCs and observed cost basis.

- `SOLANA_RPC`, `SOLANA_RPC_FALLBACKS` — Helius etc. for wallet positions
- `SOLSCAN_API_KEY` — real average entry from a wallet's swap history
- `ETHERSCAN_API_KEY` — EVM token balances (Pro plans only) and EVM history
- `EVM_RPC_ETHEREUM|BASE|BSC|…` — override the public RPCs used for balances
- `WG_CHAIN_ID_<CHAIN>` — numeric chain id for a chain not built in, e.g.
  `WG_CHAIN_ID_ROBINHOOD=…` together with `EVM_RPC_ROBINHOOD` adds Robinhood
  Chain to the scan with no code change
- `WG_HOLDINGS_INTERVAL` (45), `WG_PRICE_INTERVAL` (15) — seconds
- `WG_MIN_POSITION_USD` (50) — below this a wallet does not count as holding
- `WG_EVM_BASIS=1` — reconstruct EVM average entry from Etherscan (approximate)

## Notes

- `dashboard/data/` is gitignored (DB lives there). Deleting `dash.db` rebuilds
  from the JSON on next start — only dashboard-observed peaks are lost.
- Peak poller: tokens called in the last 48h every ~5 min (batches of 30,
  1 request per 2 s), older tokens once a day, dead tokens skipped.
- Multiplier = MAX(first_mc, bot peak, dashboard-observed peak) / first_mc.
  Historical calls made before the dashboard was deployed only have bot peaks,
  which understate performance (see design doc §2 for the GT backfill plan).
