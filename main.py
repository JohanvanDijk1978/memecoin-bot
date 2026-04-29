"""
main.py
───────
Entrypoint — runs all three components concurrently:
  1. Telegram user account scraper (Telethon)
  2. Discord self-bot scraper
  3. Telegram bot (for your /briefing commands)

Also runs an hourly cleanup of old mentions.
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
    """Start the Discord self-bot scraper."""
    discord_token = os.getenv("DISCORD_SELF_TOKEN", "")
    if not discord_token:
        logger.warning("⚠️  DISCORD_SELF_TOKEN not set — Discord scraper skipped")
        return

    from src.discord_scraper import DiscordScraper
    client = DiscordScraper()

    client = DiscordScraper()
    await client.start(discord_token)
    


async def run_bot():
    """Start the Telegram bot that handles your /briefing commands."""
    from src.bot import build_bot_app
    app = build_bot_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("✅ Telegram briefing bot started — send /briefing to get started")

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

    results = await asyncio.gather(
        run_telegram_scraper(),
        run_discord_scraper(),
        run_bot(),
        run_cleanup_loop(),
        return_exceptions=True,
    )

    for name, result in zip(["telegram_scraper", "discord_scraper", "bot", "cleanup"], results):
        if isinstance(result, Exception):
            logger.error(f"❌ {name} crashed: {result}", exc_info=result)


if __name__ == "__main__":
    asyncio.run(main())
