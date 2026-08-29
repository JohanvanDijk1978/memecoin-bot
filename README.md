# Bot scripts

ssh root@209.250.245.16

## Commands

git pull origin main

When you come back, start the conversation with:

"Read the memory file"

And paste the contents of /root/memecoin-bot-new/memory.md so I have full context instantly.

- Read only
  cat /root/memecoin-bot-new/memory.md
- Edit
  nano /root/memecoin-bot-new/.env
- Pulls from github and deploys the bot
  /root/deploy.sh

- Run Github
  git add .
  git commit -m "what you changed"
  git push
- Kill the previous session and run the bot

kill $(pgrep -f main.py)
cd /root/memecoin-bot-new
nohup python3 main.py > data/bot.log 2>&1 &

Coding Agent:

nohup python3 /root/coding-agent/agent.py >> /root/coding-agent/agent.log 2>&1 &
echo "Agent started"

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

---

# 🚨 Multi-wallet buy alerts

Several wallets you track buying the same coin inside a window is the signal.
When it happens the bot posts to its own Telegram channel:

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

The market cap on each buy line is the market cap **at the moment of that buy**,
worked out from the transaction itself (USD spent ÷ tokens received × supply),
not from a later quote.

## Commands

| Command | What it does |
|---|---|
| `/add <wallet> <name>` | monitor a wallet — `/add 7abc...xyz rowdy` |
| `/remove <wallet-or-name>` | stop monitoring it |
| `/list` | monitored wallets, the active rule, which chains are live |
| `/buys` | the last 15 detected buys — how you tell "nothing is happening" from "detection is broken" |
| `/multirule 3 120` | set the rule: ≥3 wallets in 120 minutes. Optional 3rd/4th argument set the milestone ceiling and the per-token cooldown in hours. |

Solana and EVM addresses are both accepted; an EVM address is watched on every
configured EVM chain at once.

## Setup

1. Create a Telegram channel, add the bot as an **admin** with permission to
   post, then forward any message from it to
   [@username_to_id_bot](https://t.me/username_to_id_bot) to get its `-100…` id.
2. Put it in `.env` on the VPS:

   ```
   MULTIWALLET_CHANNEL_ID=-1001234567890
   ```

   Without it the alerts go to `YOUR_TELEGRAM_USER_ID` (your DM), so the feature
   works before the channel exists.
3. The chain endpoints come from `fomo/.env` (`SOLANA_RPC`, `ETH_RPC`,
   `BASE_RPC`, `BSC_RPC`, `ROBINHOOD_RPC` and the matching `*_WSS`), which the
   watcher reads without overriding anything already set in the bot's own
   `.env`. **That file is not deployed by git** — copy it once:

   ```
   scp C:\Users\mzshu\Downloads\memebot\fomo\.env root@209.250.245.16:/root/memecoin-bot-new/fomo/.env
   ```

   Without it Solana falls back to the public RPC (sweep only, no websocket)
   and the EVM chains have no endpoint at all.

## How detection works

* **Solana** — one websocket, one `logsSubscribe {mentions:[wallet]}` per
  monitored wallet. Sub-second, and adding a wallet is one more subscribe on
  the same connection.
* **EVM** — one `eth_subscribe("logs")` per chain, filtered on the ERC-20
  Transfer topic with every monitored wallet in the `to` slot. One subscription
  covers the whole list, so cost does not grow with the number of wallets.
* **Both** are backed by a reconcile sweep (`getSignaturesForAddress` per
  wallet, `eth_getLogs` per chain) on a slow timer, so a gap during a reconnect
  is filled rather than lost. A chain with no websocket URL degrades to that
  sweep alone instead of going dark.
* **A buy** is tokens going up while SOL/ETH/BNB, wrapped native or a stablecoin
  goes out of the same wallet in the same transaction — so airdrops, transfers
  in and failed swaps never count.
* **One post per milestone.** 3→4→5→6 wallets each post once (`mw_alerts`
  remembers the highest count already announced, which is also what keeps a
  restart silent), then the token is muted for 24h.

State lives in `data/multiwallet.db` (SQLite, gitignored). Deleting it loses
the wallet list.

## Checking it on the box

```bash
python3 tools/diag_multiwallet.py                    # endpoints, websockets, channel
python3 tools/diag_multiwallet.py <wallet> 50        # replay a wallet's recent txs
python3 tools/test_multiwallet.py                    # offline test, no network needed
```

Env vars, all optional: `MULTIWALLET_CHANNEL_ID`, `MULTIWALLET_EVM_CHAINS`
(default `ethereum,base,bsc,robinhood`), `MULTIWALLET_RECONCILE_SEC` (300),
`MULTIWALLET_POLL_SEC` (30, used when a chain has no websocket),
`MULTIWALLET_SYNC_SEC` (20), `MULTIWALLET_DB`.
