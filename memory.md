# Agent Memory

## Who I am working with
- Name: Johan
- Location: Amsterdam, NL
- VPS: Vultr, IP 209.250.245.16
- Bot folder: /root/memecoin-bot-new
- Agent folder: /root/coding-agent
- Editor: VS Code (local), deploys via git push to VPS
- GitHub: github.com/JohanvanDijk1978/memecoin-bot (public)

## Stack
- Python 3
- Telegram bot (python-telegram-bot + Telethon)
- Discord self-bot
- Solana/ETH memecoin signals bot
- Anthropic API (claude-sonnet-4-5) for coding agent

## Deploy process
- Edit in VS Code → git add/commit/push → webhook auto-pulls → bot + agent restart
- Deploy script: /root/deploy.sh
- Deploy logs: /root/deploy.log
- Bot logs: tail -f /root/memecoin-bot-new/data/bot.log
- Agent logs: tail -f /root/coding-agent/agent.log
- Webhook config: /etc/webhook.conf (must point to /root/memecoin-bot-new)

## Running services
- Bot: /root/memecoin-bot-new/main.py
- Coding agent: /root/coding-agent/agent.py
- Webhook: systemctl status webhook (port 9000)
- Both restart on reboot via /etc/rc.local

## Preferences
- Always VS Code solutions, never terminal-only edits
- Never use pm2, always use nohup
- Use pkill -f "python3 main.py" for clean restarts
- Keep code clean and simple
- Emoji: use 🪙 for coins, not 👤

## Bot commands
- /status — uptime and scraper status
- /leaderboard — group and user leaderboard by avg multiplier
- /pump — top 10 pumping coins with 4 timeframe buttons (1h/6h/12h/24h)

## File structure
- src/bot.py — Telegram bot commands
- src/telegram_scraper.py — monitors Telegram groups, sends CA pings
- src/discord_scraper.py — monitors Discord channels
- src/mention_store.py — stores CA history in data/ca_history.json
- src/send_ping.py — sends alerts to Telegram (uses load_dotenv(), NOT hardcoded path)
- src/mirror.py — mirrors messages to topic channel
- src/dexscreener.py — fetches token data from Dexscreener API
- main.py — entrypoint, runs all scrapers + bot + cleanup + peak tracker

## Background jobs (in main.py)
- run_cleanup_loop() — prunes old mentions every 1h
- run_peak_tracker() — checks all CAs from last 24h every 30min, updates peak_mc in ca_history.json

## CA ping format
- Shows sender, group, token name, mcap, age, FDV, ATH, scan history
- History block shows peak_mc multiplier (not current mcap)
- Axiom/Padre/GMGN links included

## /pump command
- Reads ca_history.json, filters by timeframe
- Fetches current mcap + ticker from Dexscreener if not stored
- Uses peak_mc for multiplier calculation
- Shows top 10 ranked by peak multiplier with called time

## Leaderboard
- Ranks groups and users by avg multiplier
- Uses peak_mc / first_mc for multiplier

## Lessons learned
- .env must never be committed to git
- data/ and __pycache__/ are gitignored
- GROUP IDs must be cast to int() in Telethon
- /etc/webhook.conf must point to /root/memecoin-bot-new
- deploy.sh logs to /root/deploy.log for debugging
- Use pkill -f "python3 main.py" not kill $(pgrep) for clean restarts
- API key must be created after credits are added to Anthropic account
- send_ping.py must use load_dotenv() not hardcoded path
- Coding agent rate limits on large files — keep requests focused

## Projects
### memecoin-bot
- Monitors Telegram groups + Discord channels for contract addresses
- Filters and ranks by mcap, volume via Dexscreener
- Sends instant CA pings with full token data
- Tracks peak mcap every 30 minutes
- Mirror feature forwards all messages to topic channel
- Blocked users: Rick (shows in mirror but not CA channel)

### coding-agent (Borz Agent)
- Telegram bot for Johan to request code changes
- Reads memory.md at start of every session
- Can run commands, read/write files on VPS, push to GitHub
- Updates memory.md after completing tasks
- Rate limit: avoid reading large files in one request
