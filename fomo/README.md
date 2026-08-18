# fomo — FOMO (fomo.family) research bot

A standalone Discord bot that turns a FOMO username into a trader profile embed.

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
- Log into the persistent browser once with `python fomo_browser.py --login`.

Wallet enrichment is enabled by default. `FOMO_RESOLVE_WALLETS=0` disables
Solana resolution and `FOMO_RESOLVE_EVM=0` disables verified EVM resolution.

If FomoScan has not indexed a known verified EVM wallet yet, validate and cache
the explicit mapping with `python evm_resolve.py --handle HANDLE --wallet 0x...`.

Then check the API works before wiring up Discord:

```powershell
python probe.py Binkieee
python probe.py Binkieee --json
python probe.py --top 24h
python probe.py --search bink
```

Expected output for `Binkieee`: 135K followers, 1,257 trades, 3,735 swaps,
~$9.4M volume, ~3d 18h avg hold.

Finally:

```powershell
python fomo_bot.py
```

## Commands

| Command | What it does |
|---|---|
| `/fomo <handle>` | Full trader embed. Case-insensitive; falls back to fuzzy search. |
| `/fomosearch <term>` | Top 8 fuzzy matches. |
| `/fomotop [24h\|all-time] [n]` | Leaderboard. |

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
| `fomo_wallet.py` / `wallet_resolve.py` | Real Solana wallet resolver + CLI |
| `fomo_evm.py` / `evm_resolve.py` | Verified EVM smart-wallet resolver + CLI |
| `probe.py` | CLI for testing lookups without Discord |
