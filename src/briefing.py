"""
briefing.py
───────────
The brain of the bot.
Combines mention signals from Telegram/Discord with on-chain volume data
from Dexscreener, filters by $200k+ volume, and returns a ranked top-10 list.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional

from .mention_store import store, CoinSignal
from .dexscreener import fetch_tokens_batch, TokenData, passes_volume_filter

logger = logging.getLogger(__name__)

MAX_RESULTS = 10
CONCURRENT_FETCH_LIMIT = 20  # max addresses to query Dexscreener for at once


@dataclass
class BriefingItem:
    signal: CoinSignal
    token: TokenData
    score: float  # composite rank score


def _compute_score(signal: CoinSignal, token: TokenData) -> float:
    """
    Composite score = weighted blend of:
    - Mention count (social signal)
    - 24h volume (on-chain activity)

    Both are normalized so neither dominates by raw magnitude.
    """
    mention_weight = 0.4
    volume_weight  = 0.6

    # Normalize volume: $200k = 1.0, $1M = 5.0, $10M = 50.0
    norm_volume  = token.volume_24h / 200_000
    norm_mention = signal.mention_count  # raw count is fine for relative ranking

    return (norm_mention * mention_weight) + (norm_volume * volume_weight)


async def generate_briefing(hours: float) -> List[BriefingItem]:
    """
    Generate a briefing for the given look-back window (in hours).
    Returns up to MAX_RESULTS items, sorted by composite score.
    """
    # 1. Get all signals from the mention store for the window
    signals = store.get_signals(since_hours=hours)
    if not signals:
        logger.info("No signals found in the mention store.")
        return []

    # 2. Take top candidates by mention count (cap at CONCURRENT_FETCH_LIMIT)
    top_signals = signals[:CONCURRENT_FETCH_LIMIT]
    addresses   = [(sig.address, sig.chain) for sig in top_signals]

    logger.info(f"Fetching on-chain data for {len(addresses)} addresses...")

    # 3. Fetch on-chain data from Dexscreener
    tokens = await fetch_tokens_batch(addresses)
    token_map = {t.address: t for t in tokens}

    # 4. Filter by $200k+ volume and build briefing items
    items: List[BriefingItem] = []
    for sig in top_signals:
        token = token_map.get(sig.address)
        if not token:
            continue  # couldn't fetch on-chain data
        if not passes_volume_filter(token):
            continue  # below $200k volume floor

        score = _compute_score(sig, token)
        items.append(BriefingItem(signal=sig, token=token, score=score))

    # 5. Sort by composite score, return top N
    items.sort(key=lambda x: x.score, reverse=True)
    return items[:MAX_RESULTS]


def format_briefing_message(items: List[BriefingItem], hours: float) -> str:
    """
    Format the briefing as a clean Telegram message.
    """
    if not items:
        return (
            f"😴 *No coins cleared $200k volume in the last {hours:.0f}h*\n\n"
            "Either it was a quiet period, or the groups didn't mention any "
            "tracked contracts. Try a longer window."
        )

    chain_emoji = {"SOL": "◎", "ETH": "Ξ"}
    source_emoji = {"telegram": "📱", "discord": "🎮"}

    lines = [
        f"🔍 *Briefing — Last {hours:.0f}h* ({len(items)} coins)\n",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for i, item in enumerate(items, 1):
        t = item.token
        s = item.signal

        chain_icon = chain_emoji.get(t.chain, "🔗")
        sources    = " ".join(source_emoji.get(src, "•") for src in s.sources)

        # Price change indicator
        chg = t.price_change_24h
        chg_icon = "🟢" if chg >= 0 else "🔴"
        chg_str  = f"{chg:+.1f}%"

        # Volume formatting
        vol = t.volume_24h
        if vol >= 1_000_000:
            vol_str = f"${vol/1_000_000:.1f}M"
        else:
            vol_str = f"${vol/1_000:.0f}K"

        # Market cap formatting
        mc = t.market_cap
        if mc >= 1_000_000:
            mc_str = f"${mc/1_000_000:.1f}M"
        elif mc > 0:
            mc_str = f"${mc/1_000:.0f}K"
        else:
            mc_str = "N/A"

        ticker = f"${t.symbol}" if t.symbol else ""

        lines += [
            f"\n*{i}. {chain_icon} {t.name} {ticker}*",
            f"   💬 Mentions: *{s.mention_count}x* {sources}",
            f"   📊 Vol 24h: *{vol_str}* | MC: {mc_str}",
            f"   {chg_icon} Price: ${t.price_usd:.6g} ({chg_str})",
            f"   📋 `{t.address[:8]}...{t.address[-6:]}`",
            f"   🔗 [Chart]({t.dex_url})",
        ]

    lines += [
        "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "_Sources: Telegram alpha + Discord + Dexscreener_",
    ]

    return "\n".join(lines)
