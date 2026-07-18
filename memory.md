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
