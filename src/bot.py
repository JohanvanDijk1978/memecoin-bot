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

from src.utils import escape_md, fmt_usd, dex_wait

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

ALLOWED_USERS = {1768528319, 6717838435,7801901063}

START_TIME = time.time()

CA_HISTORY_FILE = "data/ca_history.json"

# fmt_mc is the pre-existing name callers use throughout this module. Keep it
# as an alias so we don't have to churn every call site.
fmt_mc = fmt_usd


def is_allowed(update: Update) -> bool:
    return update.effective_user.id in ALLOWED_USERS


def axiom_link(addr, ticker):
    chain = "eth" if addr.startswith("0x") else "sol"
    return f"[${escape_md(ticker)}](https://axiom.trade/t/{addr}?chain={chain})"


async def fetch_token_data(session: aiohttp.ClientSession, address: str) -> dict:
    """Fetch current market cap and ticker from Dexscreener."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        await dex_wait()
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

    # Fetch current mcap + ticker for all CAs concurrently (capped to avoid Dexscreener overload on 24h)
    async with aiohttp.ClientSession() as session:
        sem = asyncio.Semaphore(20)

        async def fetch_with_sem(addr):
            async with sem:
                return await fetch_token_data(session, addr)

        tasks = [fetch_with_sem(c["address"]) for c in candidates]
        try:
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=60)
        except asyncio.TimeoutError:
            logger.warning(f"/pump {hours}h: Dexscreener fetch timed out with {len(candidates)} candidates")
            results = [{"mcap": 0, "ticker": ""} for _ in candidates]

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
            f"{medal} {link} — *{fmt_mc(c['peak_mc'] or c['first_mc'])}* — {escape_md(group)} — {escape_md(sender)} — {fmt_mc(c['first_mc'])} (*{mult:.1f}x*) — {called_time}"
        )

    text = "\n".join(lines)
    try:
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"/pump {hours}h: Markdown edit failed ({e}); retrying as plain text")
        plain = text
        for ch in ("\\_", "\\*", "\\`", "\\[", "\\]", "*"):
            plain = plain.replace(ch, ch[-1] if ch.startswith("\\") else "")
        await query.edit_message_text(plain, disable_web_page_preview=True)


# ── /wallet ───────────────────────────────────────────────────────────────
# Frontrun bills in credits (associated-wallets is 400 a call, and Gold only
# includes 100k/month), so src.frontrun caches every read. Nothing here should
# call the API twice for the same input.

def solscan_link(address: str, chain: str) -> str:
    if (chain or "").upper() == "EVM" or address.startswith("0x"):
        return f"https://etherscan.io/address/{address}"
    return f"https://solscan.io/account/{address}"


def format_wallet(w: dict) -> list:
    """Render one Frontrun wallet object as Markdown lines."""
    from src.frontrun import has_fomo_tag, fomo_username

    address = str(w.get("address") or "")
    chain = str(w.get("chain") or "")
    name = w.get("name") or w.get("primaryLabel") or ""
    handle = w.get("twitterUsername") or ""
    fomo_user = fomo_username(w)
    smart = w.get("smartFollowersCount")
    followers = w.get("followersCount")

    badges = []
    # The Fomo username is the field that matters for /wallet — show it first
    # and distinctly from the X handle, which is often but not always the same.
    if fomo_user:
        badges.append(f"🔥 fomo: *{escape_md(fomo_user)}*")
    elif has_fomo_tag(w):
        badges.append("🔥 FOMO")
    for label in (w.get("labels") or []):
        text = label.get("name") if isinstance(label, dict) else label
        if text:
            badges.append(escape_md(str(text)))

    header = f"🪙 `{address}`"
    lines = [header]

    ident = []
    if name:
        ident.append(f"*{escape_md(str(name))}*")
    if handle:
        ident.append(f"[@{escape_md(str(handle))}](https://x.com/{handle})")
    if ident:
        lines.append("   " + " — ".join(ident))

    stats = []
    if smart is not None:
        stats.append(f"{smart:,} smart followers")
    if followers is not None:
        stats.append(f"{followers:,} followers")
    if chain:
        stats.append(escape_md(chain.title()))
    if stats:
        lines.append("   " + " | ".join(stats))

    if badges:
        lines.append("   " + " · ".join(badges))

    # Tags are the noisy field (top-holder markers, manual notes) — cap them so
    # a heavily-tagged whale doesn't blow past Telegram's 4096-char limit.
    tags = [
        str(t.get("name")) for t in (w.get("tags") or [])
        if isinstance(t, dict) and t.get("name")
        and str(t.get("name")).strip().upper() != "FOMO"
    ]
    if tags:
        shown = ", ".join(escape_md(t) for t in tags[:6])
        if len(tags) > 6:
            shown += f" +{len(tags) - 6} more"
        lines.append(f"   🏷 {shown}")

    if address:
        lines.append(f"   [Solscan]({solscan_link(address, chain)})")

    return lines


# Telegram's command menu can't show argument variants, so each mode gets its
# own command. They all funnel into _wallet_lookup.

async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/wallet — Fomo-linked wallets only (the default)."""
    await _wallet_lookup(update, context, default_mode="fomo")


async def cmd_wallet_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/walletall — every wallet linked to the X account, Fomo or not."""
    await _wallet_lookup(update, context, default_mode="all")


async def cmd_wallet_mentioned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/walletmentions — wallets the account has tweeted about."""
    await _wallet_lookup(update, context, default_mode="mentioned")


async def _wallet_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         default_mode: str = "fomo"):
    if not is_allowed(update):
        return

    from src import frontrun

    if not context.args:
        await update.message.reply_text(
            "🪙 *Wallet lookup*\n\n"
            "`/wallet @handle` — 🔥 Fomo wallets only\n"
            "`/walletall @handle` — every linked wallet\n"
            "`/walletmentions @handle` — wallets they've tweeted about\n"
            "`/wallet <address>` — labels and identity for one wallet\n\n"
            "_Token contract addresses aren't supported — Frontrun has no CA endpoint._",
            parse_mode="Markdown",
        )
        return

    if not frontrun.is_configured():
        await update.message.reply_text(
            "❌ Frontrun is not configured — add `FRONTRUN_API_KEY` to .env and restart.",
            parse_mode="Markdown",
        )
        return

    query = context.args[0].strip()
    rest = [a.lower() for a in context.args[1:]]
    # `fresh` / `nocache` forces a re-query — costs credits, but it's the way
    # out when a bad response got cached.
    fresh = any(a in ("fresh", "nocache", "refresh") for a in rest)
    if fresh:
        frontrun.clear_cache(query)
    override = next((a for a in rest if a not in ("fresh", "nocache", "refresh")), "")
    # A trailing word still overrides, so `/wallet foo all` keeps working.
    mode = override or default_mode
    chain = frontrun.looks_like_address(query)

    msg = await update.message.reply_text("⏳ Querying Frontrun...")

    # ── Address input ─────────────────────────────────────────────────
    # A Solana mint and a Solana wallet are the same format, so we can't tell
    # a token CA from a wallet here. Send it anyway: an unmatched lookup is
    # 5 credits, and "no match" is itself the answer.
    if chain:
        wallets, err = await frontrun.wallets_batch_query([query], chain=chain)
        if err:
            await msg.edit_text(f"⚠️ Frontrun lookup failed — {escape_md(err)}",
                                parse_mode="Markdown")
            return
        if not wallets:
            await msg.edit_text(
                f"🪙 No wallet data for `{query}`\n\n"
                "_Frontrun has no identity for this address. If it's a token "
                "contract rather than a wallet, that's expected._",
                parse_mode="Markdown",
            )
            return
        lines = ["🪙 *Wallet*\n"]
        for w in wallets:
            lines += format_wallet(w) + [""]
        await _send_wallet_result(msg, lines)
        return

    # ── Username input ────────────────────────────────────────────────
    # The FOMO username is the identifier. The X handle is not consulted as a
    # source of truth — see frontrun.fomo_wallets().
    handle = frontrun.clean_handle(query)
    if not handle:
        await msg.edit_text("❌ Could not read that as a username or address.")
        return

    source = ""
    if mode.startswith("mention"):
        wallets, err = await frontrun.mentioned_wallets(handle)
        title = f"🪙 *Wallets mentioned by* @{escape_md(handle)}"
    elif mode == "all":
        # Explicit escape hatch: every wallet Frontrun links to the X account,
        # Fomo or not. Only reachable via /walletall.
        wallets, err = await frontrun.linked_wallets(handle)
        title = f"🪙 *All wallets linked to* @{escape_md(handle)}"
    else:
        wallets, err, source = await frontrun.fomo_wallets(handle)
        title = f"🔥 *Fomo wallets for* `{handle}`"

    # Say what actually went wrong. "No wallets found" for an auth failure or a
    # changed response shape is worse than useless — it looks like an answer.
    if err:
        await msg.edit_text(
            f"⚠️ Lookup failed for `{handle}`\n\n"
            f"*{escape_md(err)}*\n\n"
            f"Try `/walletall {handle}` to see everything Frontrun links to "
            f"the X account, or run `python3 tools/probe_fomo.py {handle}` "
            "on the VPS to find the working Fomo route.",
            parse_mode="Markdown",
        )
        return

    if not wallets:
        await msg.edit_text(
            f"🔥 No Fomo wallets found for `{handle}`\n\n"
            f"No fomo.family profile resolved, and nothing Frontrun links to "
            f"that name carries the Fomo username.\n\n"
            f"`/walletall {handle}` shows every X-linked wallet instead.",
            parse_mode="Markdown",
        )
        return

    lines = [title, ""]
    if source:
        # Be explicit about provenance — "from fomo.family" and "inferred from a
        # Frontrun label" are very different confidence levels.
        note = {
            "fomo.family":     f"{len(wallets)} wallet(s) from fomo.family",
            "frontrun-label":  f"{len(wallets)} wallet(s) — Frontrun label matched `{handle}` on Fomo",
            "fomo-tag-only":   f"⚠️ {len(wallets)} FOMO-tagged wallet(s), but none names `{handle}` — verify before trusting",
            "cache":           f"{len(wallets)} wallet(s) (cached)",
        }.get(source, f"{len(wallets)} wallet(s) via {source}")
        lines.append(note + "\n")

    for w in wallets:
        lines += format_wallet(w) + [""]

    await _send_wallet_result(msg, lines)


async def cmd_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Frontrun credit balance. Worth watching: a /wallet lookup on a handle
    with 2 linked wallets costs 600 credits (400 + 2x100)."""
    if not is_allowed(update):
        return

    from src import frontrun

    if not frontrun.is_configured():
        await update.message.reply_text("❌ FRONTRUN_API_KEY not set.")
        return

    data = await frontrun.credits_remaining()
    if not isinstance(data, dict):
        await update.message.reply_text("⚠️ Could not read Frontrun credit balance.")
        return

    current = data.get("currentPoints")
    try:
        current = int(current)
    except (TypeError, ValueError):
        current = None

    lines = ["💳 *Frontrun credits*\n"]
    if current is not None:
        lines.append(f"*{current:,}* remaining")
        # Rough guide, using the observed 2-wallet case as the unit.
        lines.append(f"≈ {current // 600} more `/wallet` lookups")
    else:
        lines.append(f"`{json.dumps(data)[:300]}`")
    lines.append(
        "\n_400 per handle + 100 per matched wallet._\n"
        "_Cached results are free — only `fresh` re-bills._"
    )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _send_wallet_result(msg, lines: list):
    """Edit the placeholder with the result, truncating to Telegram's 4096-char
    limit and falling back to plain text if Markdown fails to parse."""
    text = "\n".join(lines).rstrip()
    if len(text) > 4000:
        text = text[:3950].rsplit("\n", 1)[0] + "\n\n_…truncated_"
    try:
        await msg.edit_text(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        logger.warning(f"/wallet: Markdown edit failed ({e}); retrying as plain text")
        plain = text.replace("\\", "").replace("*", "").replace("`", "")
        await msg.edit_text(plain[:4000], disable_web_page_preview=True)


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

    # fmt_mc and axiom_link are module-level (from utils + defined at top of file)
    # — no local shadows needed.

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


# ── Multi-wallet watcher commands ─────────────────────────────────────────
# The wallet list, the rule and the detected buys all live in
# src/multiwallet_store.py; these handlers are a thin Telegram surface over it
# so the watcher owns its own state and nothing here has to know about chains.

def _mw_short(address: str) -> str:
    return f"{address[:4]}…{address[-4:]}" if len(address) > 10 else address


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/add <wallet> <name> — monitor a wallet for multi-wallet buy alerts."""
    if not is_allowed(update):
        return
    from src import multiwallet as mw
    from src import multiwallet_store as mw_store

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/add <wallet> <name>`\n"
            "Example: `/add 7abc...xyz rowdy`",
            parse_mode="Markdown")
        return

    address, name = args[0], " ".join(args[1:])
    result = mw_store.add_wallet(address, name)
    if result["status"] == "invalid":
        await update.message.reply_text(
            "❌ That doesn't look like a Solana or EVM address.")
        return

    wallet = result["wallet"]
    chain = "Solana" if wallet["kind"] == "sol" else "EVM (ETH/Base/BSC/Robinhood)"
    verb = {"added": "Now watching", "renamed": "Renamed",
            "exists": "Already watching"}[result["status"]]
    total = len(mw_store.list_wallets())
    await update.message.reply_text(
        f"🪙 *{verb}* {mw.strip_md(wallet['name'])} — `{wallet['address']}`\n"
        f"{chain} · {total} wallet(s) monitored",
        parse_mode="Markdown", disable_web_page_preview=True)


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/remove <wallet-or-name> — stop monitoring a wallet."""
    if not is_allowed(update):
        return
    from src import multiwallet as mw
    from src import multiwallet_store as mw_store

    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: `/remove <wallet-or-name>`",
                                        parse_mode="Markdown")
        return
    removed = mw_store.remove_wallet(" ".join(args))
    if not removed:
        await update.message.reply_text("🤷 No monitored wallet matched that.")
        return
    names = ", ".join(mw.strip_md(r["name"] or _mw_short(r["address"])) for r in removed)
    await update.message.reply_text(
        f"🗑 Removed *{names}* — {len(mw_store.list_wallets())} wallet(s) left",
        parse_mode="Markdown")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list — the monitored wallets, the active rule and the live chains."""
    if not is_allowed(update):
        return
    from src import multiwallet as mw
    from src import multiwallet_store as mw_store

    wallets = mw_store.list_wallets()
    if not wallets:
        await update.message.reply_text(
            "No wallets monitored yet.\nAdd one with `/add <wallet> <name>`.",
            parse_mode="Markdown")
        return

    lines = [f"🪙 *Monitored wallets* ({len(wallets)})", ""]
    for kind, label in (("sol", "Solana"), ("evm", "EVM")):
        group = [w for w in wallets if w["kind"] == kind]
        if not group:
            continue
        lines.append(f"*{label}*")
        for w in group:
            lines.append(f"• *{mw.strip_md(w['name'] or '?')}* — `{w['address']}`")
        lines.append("")
    lines += mw.status_lines()
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950].rsplit("\n", 1)[0] + "\n\n_…truncated_"
    await update.message.reply_text(text, parse_mode="Markdown",
                                    disable_web_page_preview=True)


async def cmd_buys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/buys — the last detected buys, so silence is explainable."""
    if not is_allowed(update):
        return
    from src import multiwallet as mw
    from src import multiwallet_store as mw_store

    buys = mw_store.recent_buys(limit=15)
    if not buys:
        lines = ["🛍 No buys detected yet.", ""] + mw.status_lines()
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    names = mw_store.wallet_names()
    lines = [f"🛍 *Last {len(buys)} detected buys*", ""]
    for b in buys:
        who = mw.strip_md(names.get(b["wallet"], _mw_short(b["wallet"])))
        symbol = mw.strip_md(b["symbol"] or b["token"][:6])
        link = mw.explorer_tx(b["chain"], b["tx"])
        stamp = mw.utc_time(b["ts"])
        tx = f"[TX]({link})" if link else "tx"
        spent = f" · {mw.fmt_mcap(b['spent_usd'])}" if b["spent_usd"] else ""
        lines.append(f"• *{who}* · {mw.fmt_amount(b['amount'])} {symbol}"
                     f"{spent} · {tx} ({stamp})")
    lines += [""] + mw.status_lines()
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown",
                                    disable_web_page_preview=True)


async def cmd_multirule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/multirule [min] [window_min] [ceiling] [cooldown_h] — read or set the rule."""
    if not is_allowed(update):
        return
    from src import multiwallet_store as mw_store

    args = context.args or []
    if args:
        fields = ["min_wallets", "window_min", "max_wallets", "cooldown_h"]
        changes = {}
        for field, raw in zip(fields, args):
            try:
                value = int(raw)
            except ValueError:
                await update.message.reply_text(
                    "Usage: `/multirule <min wallets> <window min> [ceiling] [cooldown h]`\n"
                    "Example: `/multirule 3 120`", parse_mode="Markdown")
                return
            if value <= 0:
                await update.message.reply_text("Every value has to be at least 1.")
                return
            changes[field] = value
        rule = mw_store.set_rule(**changes)
    else:
        rule = mw_store.get_rule()

    await update.message.reply_text(
        f"📋 *Multi-wallet rule*\n\n"
        f"≥*{rule['min_wallets']}* wallets in *{rule['window_min']}* min\n"
        f"Milestones post up to *{rule['max_wallets']}* wallets, "
        f"then *{rule['cooldown_h']}h* quiet per token\n\n"
        f"_Change with_ `/multirule 3 120`",
        parse_mode="Markdown")


def build_bot_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("pump", cmd_pump))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("walletall", cmd_wallet_all))
    app.add_handler(CommandHandler("walletmentions", cmd_wallet_mentioned))
    app.add_handler(CommandHandler("credits", cmd_credits))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("buys", cmd_buys))
    app.add_handler(CommandHandler("multirule", cmd_multirule))
    app.add_handler(CallbackQueryHandler(pump_callback, pattern="^pump_"))

    # post_init is ONLY invoked by run_polling()/run_webhook(). main.py starts
    # the app manually (initialize/start/start_polling) so it can run under
    # asyncio.gather with the scrapers — which means post_init never fires and
    # the command menu never updates. Kept for correctness if this app is ever
    # started the normal way; main.py calls register_commands() explicitly.
    app.post_init = register_commands
    return app


BOT_COMMANDS = [
    ("status",         "Check bot status and uptime"),
    ("leaderboard",    "Show group and user leaderboard"),
    ("pump",           "Top pumping coins by timeframe"),
    ("wallet",         "🔥 Fomo wallets for an X handle"),
    ("walletall",      "All wallets linked to an X handle"),
    ("walletmentions", "Wallets an X account tweeted about"),
    ("credits",        "Frontrun API credits remaining"),
    ("add",            "🪙 Monitor a wallet: /add <wallet> <name>"),
    ("remove",         "Stop monitoring a wallet"),
    ("list",           "Monitored wallets and the multi-wallet rule"),
    ("buys",           "Last detected buys from monitored wallets"),
    ("multirule",      "Read or set the multi-wallet rule"),
]


async def register_commands(app: Application) -> None:
    """Push the command list to Telegram so it shows in the / menu.

    Must be awaited explicitly after app.initialize() — see the note in
    build_bot_app about post_init not running under manual startup.
    """
    from telegram import BotCommand
    try:
        await app.bot.set_my_commands(
            [BotCommand(name, desc) for name, desc in BOT_COMMANDS]
        )
        logger.info(f"✅ Registered {len(BOT_COMMANDS)} bot commands with Telegram")
    except Exception as e:
        logger.warning(f"Could not register bot commands: {e}")
