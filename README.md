# 🤖 Memecoin Briefing Bot

## ⚡ Setup

### 1. Clone and install

```bash
cd memecoin-bot
pip install -r requirements.txt
mkdir -p data
```

### 🔑 Getting your credentials

#### Telegram API ID & Hash

1. Go to [https://my.telegram.org/apps](https://my.telegram.org/apps)
2. Log in with your phone number
3. Create a new app (any name/description)
4. Copy `App api_id` → `TELEGRAM_API_ID`
5. Copy `App api_hash` → `TELEGRAM_API_HASH`

#### Telegram Bot Token

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the token → `TELEGRAM_BOT_TOKEN`

#### Your Telegram User ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. It will reply with your user ID
3. Copy it → `YOUR_TELEGRAM_USER_ID`

#### Telegram Alpha Group

- If the group has a public username: use `groupusername` (without @)
- If it's a private group: use the numeric ID like `-1001234567890`
- To get a group ID: forward a message from the group to [@username_to_id_bot](https://t.me/username_to_id_bot)

#### Discord User Token ⚠️

> **Warning:** Using a self-bot violates Discord's ToS. Use a secondary account if possible.

1. Open Discord in your **browser** (not the app)
2. Press `F12` to open DevTools
3. Go to the **Network** tab
4. Send any message in any channel
5. Look for a request to `discord.com/api`
6. Click on it → **Headers** → find `Authorization`
7. That value is your token → `DISCORD_SELF_TOKEN`

#### Discord Channel IDs

1. In Discord, go to **Settings → Advanced → Enable Developer Mode**
2. Right-click any channel you want to monitor → **Copy Channel ID**
3. Add multiple IDs comma-separated: `DISCORD_CHANNEL_IDS=123456789,987654321`

---

#### Test

- test
- test

### 3. Run the bot

- Run Github
  git add .
  git commit -m "what you changed"
  git push
- Kill the previous session and run the bot

kill $(pgrep -f main.py)
cd /root/memecoin-bot
nohup python3 main.py > data/bot.log 2>&1 &

### Test scripts

- Test Discord
  1. grep -i discord /root/memecoin-bot/data/bot.log
- Test Telegram
  1. grep -i telegram /root/memecoin-bot/data/bot.log
- Test error logs in Mirror
  1. tail -50 /root/memecoin-bot/data/bot.log | grep -i mirror

- Check for running bots

1.  pgrep -f main.py

# Agent Memory

## Who I am working with

- Name: Johan
- Location: Amsterdam, NL
- VPS: Vultr, IP 209.250.245.16
- Bot folder: /root/memecoin-bot-new
- Editor: VS Code (local), deploys via git push to VPS

## Stack

- Python 3
- Telegram bot (python-telegram-bot + Telethon)
- Discord self-bot
- Solana memecoin signals bot

## Deploy process

- Edit in VS Code → git add/commit/push → webhook auto-pulls → bot restarts
- Restart command: kill $(pgrep -f main.py) cd /root/memecoin-bot-new nohup python3 main.py > data/bot.log 2>&1 &
- Logs: tail -f /root/memecoin-bot-new/data/bot.log

## Preferences

- Always use VS Code solutions, never terminal-only edits
- Never use pm2, always use nohup restart command above
- Keep code clean and simple

## Lessons learned

- .env must never be committed to git
- data/ and **pycache**/ are gitignored
- GROUP IDs must be cast to int() in Telethon

## Projects

### memecoin-bot

- Monitors Telegram + Discord for contract addresses
- Mirrors Telegram Channels to the Mirror channel

EOF

- Download backup:
  scp -r root@209.250.245.16:/root/memecoin-bot/ C:\Users\mzshu\Downloads\memebot\

- upload:
  scp C:\Users\mzshu\Downloads\memebot\src\telegram_scraper.py root@209.250.245.16:/root/memecoin-bot/src/

- Env Upload:
  scp C:\Users\mzshu\Downloads\memebot\.env root@209.250.245.16:/root/memecoin-bot/

`telegram_scraper.py `
python3 -c "import py_compile; py_compile.compile('/root/memecoin-bot/src/telegram_scraper.py', doraise=True); print('✓ telegram_scraper OK')"

`discord_scraper.py`
python3 -c "import py_compile; py_compile.compile('/root/memecoin-bot/src/discord_scraper.py', doraise=True); print('✓ discord_scraper OK')"

`mirror.py`
python3 -c "import py_compile; py_compile.compile('/root/memecoin-bot/src/mirror.py', doraise=True); print('✓ mirror OK')"

`test`

cd /root/memecoin-bot && python3 -c "
import asyncio
from src.mirror import mirror_message
async def test():
link = await mirror_message('test message 123', 'underground', 'TestUser', 'testusername')
print(f'Mirror link: {link}')
asyncio.run(test())
"

---
