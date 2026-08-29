# Agent Memory

## Who I am working with

- Name: Johan
- Location: Amsterdam, NL
- VPS: Vultr, IP 209.250.245.16
- Bot folder: /root/memecoin-bot-new
- Agent folder: /root/coding-agent
- Editor: VS Code (local), deploys via git push to VPS
- GitHub: github.com/JohanvanDijk1978/memecoin-bot (public)
- Local path: C:\Users\mzshu\Downloads\memebot
- Telegram user ID: 1768528319

## Stack

- Python 3
- Telegram bot (python-telegram-bot + Telethon)
- Discord self-bot (two accounts)
- Solana/ETH memecoin signals bot
- Anthropic API (claude-sonnet-4-5) for coding agent

## Deploy process

- Edit in VS Code → git add/commit/push → webhook auto-pulls → bot + agent restart (NO manual pull needed)
- .env is NOT in git — deploy via: scp C:\Users\mzshu\Downloads\memebot\.env root@209.250.245.16:/root/memecoin-bot-new/.env
- Deploy script: /root/deploy.sh
- Deploy logs: /root/deploy.log
- Bot logs: tail -f /root/memecoin-bot-new/data/bot.log
- Agent logs: tail -f /root/coding-agent/agent.log
- Webhook config: /etc/webhook.conf (must point to /root/memecoin-bot-new)

## Running services

- Bot: /root/memecoin-bot-new/main.py
- Coding agent: /root/coding-agent/agent.py
- Webhook: systemctl status webhook (port 9000)
- Bot auto-starts on reboot via systemd; agent via /etc/rc.local

## Preferences

- Always VS Code solutions, never terminal-only edits
- Bot runs under systemd as memebot.service (/etc/systemd/system/memebot.service) — restart with `systemctl restart memebot`, status with `systemctl status memebot`
- NEVER pkill the bot — systemd re-spawns it and you'll fight the supervisor
- Coding agent (agent.py) still runs via nohup, not systemd. Never pm2.
- Keep code clean and simple
- Emoji: use 🪙 for coins, not 👤

## Bot commands

- /status — uptime and scraper status (checks log for telegram/discord/mirror activity)
- /leaderboard — group and user leaderboard by avg multiplier (top 7 groups, top 10 users)
- /pump — top 10 pumping coins with 4 timeframe buttons (1h/6h/12h/24h)

## Allowed users for bot commands

- ALLOWED_USERS = {1768528319} in bot.py (can add more IDs with comma)

## File structure

- src/bot.py — Telegram bot commands
- src/telegram_scraper.py — monitors Telegram groups, sends CA pings
- src/discord_scraper.py — monitors Discord channels (two accounts)
- src/mention_store.py — stores CA history in data/ca_history.json
- src/send_ping.py — sends alerts to Telegram (uses load_dotenv(), NOT hardcoded path)
- src/mirror.py — mirrors messages to topic channel
- src/utils.py — shared helpers, Dex rate limiter, dedupe (dexscreener.py was pruned in 353f4d9)
- src/dex_watcher.py / dex_watcher_evm.py — Dexscreener watchers (Solana / EVM)
- src/dex_milestone_tracker.py — milestone alerts
- src/filtered_forward.py — filter channel forwarding
- main.py — entrypoint, runs all scrapers + bot + cleanup (no peak tracker)

## Background jobs (in main.py)

- run_cleanup_loop() — prunes old mentions every 1h
- There is NO peak tracker (implemented in d6050f4, rolled back to 990d05a same day). peak_mc only updates when the same CA is re-posted in the same group — leaderboard understates pumps that aren't re-shared.
- run_discord_scraper() — now delegates entirely to discord_scraper.py's run_discord_scraper()

## Discord scraper (two accounts)

- Account 1: DISCORD_SELF_TOKEN + DISCORD_CHANNEL_IDS
- Account 2: DISCORD_SELF_TOKEN_2 + DISCORD_CHANNEL_IDS_2 (1246170346948661319,1351808209035333703,1303488698200883410)
- DiscordScraper class accepts optional channel_ids param, falls back to CHANNEL_IDS if not passed
- run_discord_scraper() in discord_scraper.py runs both accounts via asyncio.gather

## Mirror

- mirror.py mirrors all Telegram alpha group messages to topic channel
- Mirror group ID: -1003963742680 (t.me/c/3963742680)
- Message links format: https://t.me/c/3963742680/{topic_id}/{message_id}
- GROUP_TOPIC_MAP and GROUP_LINK_MAP in mirror.py map group names to topic IDs
- Rick is blocked from CA channel but shows in mirror
- wrap_cas_in_backticks is commented out — do not call it

## CA ping format

- Shows sender, group (hyperlinked to mirror topic), token name, mcap, age, FDV, ATH, scan history
- History block shows peak_mc multiplier (not current mcap)
- Axiom/Padre/GMGN links included
- mirror_link passed from mirror_message() return value to handle_ca_ping()

## Leaderboard

- Ranks groups (top 7) and users (top 10) by avg multiplier
- Uses peak_mc / first_mc for multiplier
- Only counts first call per user per CA (deduped)
- Best call shown with axiom link or shortened address link
- ticker saved in ca_history.json for each CA

## /pump command

- Reads ca_history.json, filters by timeframe
- Fetches current mcap + ticker from Dexscreener if not stored
- Uses peak_mc for multiplier calculation
- Shows top 10 ranked by peak multiplier with called time

## Lessons learned

- .env must never be committed to git
- data/ and **pycache**/ are gitignored
- GROUP IDs must be cast to int() in Telethon
- /etc/webhook.conf must point to /root/memecoin-bot-new
- deploy.sh logs to /root/deploy.log for debugging
- Restart bot with systemctl restart memebot — pkill no longer works (systemd re-spawns); switched after a 10h silent outage under nohup
- API key must be created after credits are added to Anthropic account
- send_ping.py must use load_dotenv() not hardcoded path
- Coding agent rate limits on large files — keep requests focused
- mirror.py must not call wrap_cas_in_backticks (commented out) — causes NameError
- dex_watcher(_evm) _send_telegram_alert: sendPhoto and the sendMessage fallback were in ONE try — a sendPhoto timeout skipped the text fallback entirely, so alerts landed on Discord but never Telegram (seen was marked, no retry). Fixed 2026-07-18: sendPhoto has its own try, sendMessage retries once on 429, plain-text (no parse_mode) last resort.
- current_mc in telegram_scraper was wrong variable name — should be mc
- sender_name=sender was wrong — should be sender_name=sender_name in store.add_message calls
- group_name was undefined in on_new_message — fixed with event.get_chat()

## Projects

### memecoin-bot

- Monitors Telegram groups + Discord channels for contract addresses
- Filters and ranks by mcap, volume via Dexscreener
- Sends instant CA pings with full token data
- No background peak tracking — peak_mc updates only on same-group re-posts
- Mirror feature forwards all messages to topic channel
- Blocked users: Rick (shows in mirror but not CA channel)
- Two Discord accounts running simultaneously

### coding-agent (Borz Agent)

- Telegram bot for Johan to request code changes
- Reads memory.md at start of every session
- Can run commands, read/write files on VPS, push to GitHub
- Updates memory.md after completing tasks
- Rate limit: avoid reading large files in one request

### fomo Discord research bot

- Local path: `C:\Users\mzshu\Downloads\memebot\fomo`; standalone real
  `discord.py` bot, separate from the main Discord self-bot and memebot service.
- Must run on borz/residential internet. FOMO Cloudflare blocks the Vultr VPS;
  browser/Playwright transport is the default.
- `/fomo <handle>` now presents a requester-only Compact/Wide layout selector.
  Compact shows only the profile image, identity, Social, Strategy, Portfolio,
  linked X account and linked wallets. Wide preserves the complete existing
  profile including Best trade, activity, ranked PnL and links. Both layouts use
  the same fetch/enrichment pipeline, and wallet edits preserve the selection.
  Compact displays `Querying ⏳` while wallet enrichment is pending, then edits
  to the discovered wallets or the final no-wallet state.
- `/wallet` reverse-searches both FOMO and Pump across Solana and EVM. `/untrack`
  and `/tracksettings` use Discord selection menus across both services; filter
  changes preserve the existing baseline and never replay hidden activity.
- `/token` returns DexScreener/Pump token metadata, image and top 5/10 holders
  across Solana, Ethereum, BSC, Base and Robinhood. Holder wallets are annotated
  with verified cached FOMO/Pump identities, and Pump Solana profiles are
  resolved live even when they are not tracked.
- Pump profile links are wallet-address based everywhere. When a Pump username
  and wallet are displayed together, both `@username` and the shortened wallet
  link to `https://pump.fun/profile/{FULL_SOLANA_WALLET}`. Never build a Pump
  profile URL from a username.
- Tracking JSON uses unique atomic temporary files with Windows lock retries and
  skips unchanged writes. `/token` splits top-ten rows across Discord-safe
  1,024-character fields. Pump RPC reads coalesce duplicate wallet subscriptions
  and both Solana paths use circuit breakers during an all-provider outage.
- Removed by request: trade/swap counts, win rate, generated image/PnL card and
  Solscan/BaseScan/BscScan links.
- FOMO's published `address` and `evmAddress` fields may be synthetic. Real
  Solana wallets are derived from sponsored transactions. EVM discovery first
  uses indexed identities, then transaction-backed evidence: older/lower-volume
  trades are matched by chain, direction, timestamp and token amount, the same
  wallet must explain multiple independent transactions, deployed code is
  verified, and the confirmed mapping is cached. Manual stale-index fallback:
  `evm_resolve.py --handle HANDLE --wallet 0x...`.
- Every FOMO API request must send
  `x-supported-chains: 1,56,143,4663,8453,1399811149`. Without it FOMO defaults
  to Solana-only data, so EVM wallets and activity cannot be recovered later by
  Helius, Alchemy or explorer fallbacks. The shared value is in
  `fomo_chains.py` and is used by both direct HTTP and browser transports.
- Live `0xOuroboros` proof (2026-08-19): profile user ID
  `d5b00d6a-3881-5ba0-805b-25bfa0371932` resolves to EVM smart wallet
  `0xb089d6ac26e0fe26e1a3a5076e4feaaf4d797180`. With the supported-chain header,
  its supplied Ethereum, BSC and Robinhood contracts appear in the FOMO feed;
  without the header the same API path contains zero EVM evidence.
- Known mappings: onmycheck EVM `0xb6e00e...e7ac` and Collectible EVM
  `0xfa2b3e...3111`; both remain cached in `wallet_cache.json`.
- The FomoScan/Railway identity service was completely removed on 2026-08-19:
  there is no endpoint, retry loop, cooldown or configuration dependency.
  FOMO EVM discovery now uses only cached mappings, corroborated historical
  transactions, exact current-balance fingerprints, or explicit manual mapping.
- FrankDeGods Solana is
  `498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ`, verified by one sell and two
  buys signed with FOMO sponsor `AgmL...N51`. Discovery is buy/sell-aware for
  all handles, uses 50 recent swaps, and preserves an existing EVM cache entry.
- FOMO `/swaps` is not a complete EVM history. `fomo_evm_activity.py` reads the
  verified wallet on Robinhood Chain Blockscout (chain 4663), rejects airdrops
  without a priced input, calculates buy size/entry MC and merges with FOMO buys.
- Solana discovery filters mixed-chain feeds to valid base58 Solana mints and
  network ID `1399811149` before calling Solana RPCs. JSON-RPC `-32602` is a
  caller/input error, not an all-provider outage, so it neither fails over nor
  starts the circuit breaker. EVM transfer candidate sorting uses scalar keys so
  equal-time matches cannot compare `EvmTransfer` objects.
- Collectible live proof (2026-08-18): WALL3 buys $5,688.55 at $3.83M and
  $2,370.34 at $3.65M from wallet `0xfa2b3e...3111`.
- `/fomotrack handle` and `/fomountrack handle` persist channel subscriptions in
  ignored `fomo_tracks.json`. FOMO polling is configured at 5s and Pump at 1s;
  loops are independent and non-overlapping. Robinhood buys are not yet
  included in FOMO tracking alerts.
- Slash commands are global-only. `setup_hook()` syncs the global tree;
  `on_ready()` syncs an empty tree to every connected guild once to delete
  server-specific registrations left by older releases. Never call
  `copy_global_to()` or restore `DISCORD_GUILD_ID`, because either recreates the
  duplicate command list.
- Current workspace changes remain uncommitted/not deployed; preserve the dirty
  worktree and inspect it before making unrelated edits.
- The project `.venv` is usable and points to Python 3.12. A non-escalated Codex
  shell can still report an inaccessible/missing interpreter because the base
  Python installation is outside the workspace sandbox; that is not evidence
  that the environment itself is broken.
- Verification: 78 conventional unit tests plus every `test_offline.py` Solana
  wallet regression pass. The current Discord tree imports and live modified
  FOMO client smoke tests return the expected multi-chain data.
- Remaining performance work: `/fomo` fetches 50 swaps in
  `profile_panels()` and the background Solana resolver fetches the identical
  uncached endpoint again. Pass `TraderStats.raw_swaps` into the resolver and
  only refetch when absent. This should reduce enrichment time and API pressure,
  but has not been implemented yet.
- Security: a Helius key was pasted into chat during debugging and should be rotated.

## Multi-wallet buy alerts (memebot, this session)

- New feature: several monitored wallets buying the same token inside a window
  posts to its own Telegram channel (`MULTIWALLET_CHANNEL_ID`, falls back to
  the owner DM). Default rule ≥3 wallets in 120 min, changeable at runtime with
  `/multirule 3 120`.
- Files: `src/multiwallet_store.py` (SQLite at `data/multiwallet.db`),
  `src/multiwallet_sources.py` (Solana `logsSubscribe` per wallet + EVM
  `eth_subscribe` logs per chain, both backed by a reconcile sweep),
  `src/multiwallet.py` (rule, message, loop). Commands `/add /remove /list
  /buys /multirule` live in `src/bot.py`; `main.py` runs the watcher in its
  gather.
- A buy = tokens in AND native/wrapped/stable out of the same wallet in the same
  tx, so airdrops and transfers never count. Market cap per buy line comes from
  the transaction (USD spent ÷ tokens × supply), not from a later quote.
- One post per wallet-count milestone (`mw_alerts.max_count`), ceiling 6, then
  24h quiet per token — this is also what keeps a restart or a sweep silent.
- Chain endpoints are read from `fomo/.env` as a fallback. That file is not in
  git and was missing on the VPS as of 2026-08-28 — without it Solana has no
  websocket and the EVM chains have no endpoint at all.
- Verify: `python3 tools/test_multiwallet.py` (offline, no keys) and
  `python3 tools/diag_multiwallet.py [wallet]` (live, on the box).
