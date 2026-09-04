"""
main.py
───────
Entrypoint — runs all the bot's async loops concurrently:
  1. Telegram user account scraper (Telethon)
  2. Discord self-bot scraper (two accounts)
  3. Telegram bot for command handlers (/status, /leaderboard, /pump)
  4. Hourly cleanup of old mentions
  5. Dexscreener paid-boost + CTO watcher (Solana)
  6. Dexscreener paid-boost + CTO watcher (EVM: Ethereum, BSC, Robinhood)
  7. Milestone tracker replying to dex-update posts at 2x/3x/5x/10x/+5x
  8. Multi-wallet buy watcher — alerts when several monitored wallets buy the
     same token inside the configured window (Solana + EVM, own channel)
  9. Long.xyz watcher — alerts the moment a new stock becomes pairable on
     Long, and when Robinhood deploys a new tokenised stock upstream of it
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/bot.log"),
    ],
)
logger = logging.getLogger(__name__)


async def run_cleanup_loop():
    """Prune old mentions from the store every hour."""
    from src.mention_store import store
    while True:
        await asyncio.sleep(3600)
        store.clear_old(keep_hours=24)
        logger.info("🧹 Cleaned up old mentions from store")


async def run_telegram_scraper():
    """Start the Telegram user account scraper."""
    from src.telegram_scraper import TelegramScraper
    scraper = TelegramScraper()
    await scraper.start()

    


async def run_discord_scraper():
    """Start the Discord self-bot scrapers."""
    from src.discord_scraper import run_discord_scraper as _run
    await _run()
    


async def run_multiwallet():
    """Start the multi-wallet buy watcher (idle until wallets are /add-ed)."""
    from src.multiwallet import run_multiwallet_watcher
    await run_multiwallet_watcher()


async def run_long():
    """Start the Long.xyz new-stock watcher (own Discord webhook)."""
    from src.long_watcher import run_long_watcher
    await run_long_watcher()


async def run_bot():
    """Start the Telegram bot that handles /status, /leaderboard, /pump."""
    from src.bot import build_bot_app, register_commands
    app = build_bot_app()
    await app.initialize()
    # Explicit: post_init does NOT run under manual startup (PTB only calls it
    # from run_polling/run_webhook), so without this the / menu never updates.
    await register_commands(app)
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("✅ Telegram bot started — /status /leaderboard /pump /wallet "
                "/credits /add /remove /list /buys /multirule")

    # Keep running until cancelled
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


async def main():
    logger.info("🚀 Starting Memecoin Briefing Bot...")

    required = [
        "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_PHONE",
        "TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_USER_ID", "TELEGRAM_ALPHA_GROUP",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        logger.error(f"❌ Missing required env vars: {', '.join(missing)}")
        sys.exit(1)

    from src.dex_watcher import run_dex_watcher
    from src.dex_watcher_evm import run_dex_watcher_evm
    from src.dex_milestone_tracker import run_milestone_tracker

    results = await asyncio.gather(
        run_telegram_scraper(),
        run_discord_scraper(),
        run_bot(),
        run_cleanup_loop(),
        run_dex_watcher(),
        run_dex_watcher_evm(),
        run_milestone_tracker(),
        run_multiwallet(),
        run_long(),
        return_exceptions=True,
    )

    for name, result in zip(
        ["telegram_scraper", "discord_scraper", "bot", "cleanup", "dex_watcher",
         "dex_watcher_evm", "milestone_tracker", "multiwallet", "long_watcher"],
        results,
    ):
        if isinstance(result, Exception):
            logger.error(f"❌ {name} crashed: {result}", exc_info=result)


if __name__ == "__main__":
    asyncio.run(main())
