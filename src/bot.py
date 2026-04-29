"""
bot.py
──────
Telegram bot.
"""

import os
import logging
import time
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

ALLOWED_USERS = {1768528319,6717838435}  # replace with your Telegram ID

START_TIME = time.time()


def is_allowed(update: Update) -> bool:
    return update.effective_user.id in ALLOWED_USERS


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    uptime_secs = int(time.time() - START_TIME)
    hours, rem = divmod(uptime_secs, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    # Check log for recent activity
    try:
        with open("data/bot.log", "r") as f:
            lines = f.readlines()[-50:]
        log = "".join(lines)
        tg  = "✅" if "Telegram user account connected" in log or "Listening" in log else "❌"
        dc  = "✅" if "Discord self-bot connected" in log else "❌"
        mir = "✅" if "Mirror send failed" not in log else "⚠️"
    except Exception:
        tg = dc = mir = "❓"

    await update.message.reply_text(
        f"📡 *Bot Status*\n\n"
        f"⏱ Uptime: *{uptime_str}*\n\n"
        f"{tg} Telegram Scraper\n"
        f"{dc} Discord Scraper\n"
        f"{mir} Mirror",
        parse_mode="Markdown",
    )


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    from src.mention_store import store

    await update.message.reply_text("⏳ Generating leaderboard...")

    group_stats = store.get_leaderboard()

    if not group_stats:
        await update.message.reply_text("No data yet — wait for some CAs to be scanned.")
        return

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣"]

    def fmt_mc(mc):
        if mc >= 1_000_000: return f"${mc/1_000_000:.1f}M"
        if mc >= 1_000: return f"${mc/1_000:.0f}K"
        return f"${mc:.0f}"

    def axiom_link(addr, ticker):
        chain = "eth" if addr.startswith("0x") else "sol"
        return f"[${ticker}](https://axiom.trade/t/{addr}?chain={chain})"

    sorted_groups = sorted(
        group_stats.items(),
        key=lambda x: x[1]["total_mult"] / max(x[1]["calls"], 1),
        reverse=True
    )

    lines = ["🏆 *Group Leaderboard*\n"]
    for i, (group, stats) in enumerate(sorted_groups[:7]):
        avg_mult = stats["total_mult"] / max(stats["calls"], 1)
        peak     = stats["peak_mult"]
        medal    = medals[i] if i < len(medals) else f"{i+1}."
        best     = stats.get("best_call")

        ticker_str = f" — ${best['ticker']}" if best and best.get("ticker") else ""
        lines.append(f"{medal} *{group}* — {stats['calls']} calls | avg {avg_mult:.1f}x | best {peak:.1f}x{ticker_str}")

        if best:
            mc_str = fmt_mc(best["first_mc"])
            if best.get("ticker") and best.get("address"):
                link = axiom_link(best["address"], best["ticker"])
            elif best.get("address"):
                addr = best["address"]
                chain = "eth" if addr.startswith("0x") else "sol"
                link = f"[{addr[:8]}...](https://axiom.trade/t/{addr}?chain={chain})"
            else:
                link = ""
            lines.append(f"   ↳ {best['sender']} — {link}   {mc_str}({peak:.0f}x)")

    # ── User leaderboard ──────────────────────────────────────────────
    all_users = {}
    for group, stats in group_stats.items():
        for user, ustats in stats["callers"].items():
            if user not in all_users:
                all_users[user] = {"calls": 0, "total_mult": 0, "peak_mult": 0, "best_ticker": "", "best_address": "", "best_first_mc": 0, "best_group": ""}
            all_users[user]["calls"]      += ustats["calls"]
            all_users[user]["total_mult"] += ustats["total_mult"]
            if ustats["peak_mult"] > all_users[user]["peak_mult"]:
                all_users[user]["peak_mult"]    = ustats["peak_mult"]
                all_users[user]["best_ticker"]  = ustats["best_ticker"]
                all_users[user]["best_address"] = ustats["best_address"]
                all_users[user]["best_first_mc"]= ustats["best_first_mc"]
                all_users[user]["best_group"]   = ustats["best_group"]

    sorted_users = sorted(
        all_users.items(),
        key=lambda x: x[1]["total_mult"] / max(x[1]["calls"], 1),
        reverse=True
    )

    lines.append("\n👤 *User Leaderboard*\n")
    for i, (user, ustats) in enumerate(sorted_users[:10]):
        avg_mult = ustats["total_mult"] / max(ustats["calls"], 1)
        peak     = ustats["peak_mult"]
        medal    = medals[i] if i < len(medals) else f"{i+1}."
        ticker   = ustats.get("best_ticker", "")
        addr     = ustats.get("best_address", "")
        group    = ustats.get("best_group", "")
        if ticker and addr:
            link = axiom_link(addr, ticker)
        elif addr:
            chain = "eth" if addr.startswith("0x") else "sol"
            link = f"[{addr[:8]}...](https://axiom.trade/t/{addr}?chain={chain})"
        else:
            link = ""
        lines.append(f"{medal} *{user}* — {ustats['calls']} calls | avg {avg_mult:.1f}x | best {peak:.1f}x {link} | {group}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)

def build_bot_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    return app