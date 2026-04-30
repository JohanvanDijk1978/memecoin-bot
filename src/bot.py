"""
bot.py
──────
Telegram bot.
"""

import os
import json
import logging
import time
import asyncio
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

ALLOWED_USERS = {1768528319, 6717838435}

START_TIME = time.time()

CA_HISTORY_FILE = "data/ca_history.json"


def is_allowed(update: Update) -> bool:
    return update.effective_user.id in ALLOWED_USERS


def fmt_mc(mc):
    if mc >= 1_000_000:
        return f"${mc/1_000_000:.1f}M"
    if mc >= 1_000:
        return f"${mc/1_000:.0f}K"
    return f"${mc:.0f}"


def axiom_link(addr, ticker):
    chain = "eth" if addr.startswith("0x") else "sol"
    return f"[${ticker}](https://axiom.trade/t/{addr}?chain={chain})"


async def fetch_token_data(session: aiohttp.ClientSession, address: str) -> dict:
    """Fetch current market cap and ticker from Dexscreener."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return {"mcap": 0, "ticker": ""}
            data = await resp.json()
        pairs = data.get("pairs", [])
        if not pairs:
            return {"mcap": 0, "ticker": ""}
        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        mcap = float(best.get("marketCap", 0) or 0)
        ticker = best.get("baseToken", {}).get("symbol", "")
        return {"mcap": mcap, "ticker": ticker}
    except Exception:
        return {"mcap": 0, "ticker": ""}


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    uptime_secs = int(time.time() - START_TIME)
    hours, rem = divmod(uptime_secs, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

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


async def cmd_pump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    keyboard = [
        [
            InlineKeyboardButton("1h", callback_data="pump_1"),
            InlineKeyboardButton("6h", callback_data="pump_6"),
            InlineKeyboardButton("12h", callback_data="pump_12"),
            InlineKeyboardButton("24h", callback_data="pump_24"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📊 *Pump Tracker*\nChoose a timeframe:",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )


async def pump_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in ALLOWED_USERS:
        return

    hours = int(query.data.split("_")[1])
    cutoff = time.time() - (hours * 3600)

    await query.edit_message_text(f"⏳ Fetching top pumps for last {hours}h...")

    # Load CA history
    try:
        with open(CA_HISTORY_FILE, "r") as f:
            ca_history = json.load(f)
    except Exception as e:
        await query.edit_message_text(f"❌ Could not load CA history: {e}")
        return

    # Collect CAs called within the timeframe
    candidates = []
    for address, entries in ca_history.items():
        if not isinstance(entries, list):
            entries = [entries]
        for entry in entries:
            ts = entry.get("timestamp", 0)
            if ts >= cutoff:
                first_mc = entry.get("market_cap") or entry.get("first_mc") or 0
                if first_mc and first_mc > 0:
                    candidates.append({
                        "address": address,
                        "first_mc": first_mc,
                        "peak_mc": entry.get("peak_mc", 0),
                        "ticker": entry.get("ticker", ""),
                        "group_name": entry.get("group_name", "Unknown"),
                        "sender_name": entry.get("sender_name", "Unknown"),
                        "timestamp": ts,
                    })
                break  # only take first detection per CA

    if not candidates:
        await query.edit_message_text(f"No CAs detected in the last {hours}h.")
        return

    # Fetch current mcap + ticker for all CAs concurrently
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_token_data(session, c["address"]) for c in candidates]
        results = await asyncio.gather(*tasks)

    for c, result in zip(candidates, results):
        if not c["peak_mc"]:
            c["peak_mc"] = result["mcap"]
        if not c["ticker"]:
            c["ticker"] = result["ticker"]

    # Calculate multiplier and sort
    for c in candidates:
        peak = c["peak_mc"] or c["first_mc"]
        c["multiplier"] = peak / c["first_mc"] if c["first_mc"] > 0 else 0

    top10 = sorted(candidates, key=lambda x: x["multiplier"], reverse=True)[:10]

    # Format message
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = [f"🪙 *Coins that pumped in the last {hours}h*\n"]

    for i, c in enumerate(top10):
        medal = medals[i] if i < len(medals) else f"{i+1}."
        ticker = c.get("ticker") or ""
        addr = c["address"]
        mult = c["multiplier"]
        group = c["group_name"]
        sender = c["sender_name"]
        called_time = time.strftime("%H:%M", time.localtime(c["timestamp"]))

        if ticker:
            link = axiom_link(addr, ticker)
        else:
            chain = "eth" if addr.startswith("0x") else "sol"
            link = f"[{addr[:8]}...](https://axiom.trade/t/{addr}?chain={chain})"

        lines.append(
            f"{medal} {link} — *{fmt_mc(c['peak_mc'] or c['first_mc'])}* — {group} — {sender} — {fmt_mc(c['first_mc'])} (*{mult:.1f}x*) — {called_time}"
        )

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True,
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
    app.add_handler(CommandHandler("pump", cmd_pump))
    app.add_handler(CallbackQueryHandler(pump_callback, pattern="^pump_"))

    async def set_commands(application):
        from telegram import BotCommand
        await application.bot.set_my_commands([
            BotCommand("status", "Check bot status and uptime"),
            BotCommand("leaderboard", "Show group and user leaderboard"),
            BotCommand("pump", "Top pumping coins by timeframe"),
        ])

    app.post_init = set_commands
    return app
