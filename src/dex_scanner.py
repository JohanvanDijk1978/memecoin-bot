"""
dex_scanner.py
──────────────
Pulls top volume coins directly from Dexscreener for SOL and ETH.
No addresses needed — queries Dexscreener's search/latest endpoints
and filters by volume + time window.
"""

import aiohttp
import asyncio
import logging
from dataclasses import dataclass
from typing import List
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MIN_VOLUME_USD = 200_000  # $200k floor

DEXSCREENER_SEARCH = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_BOOSTED = "https://api.dexscreener.com/token-boosts/latest/v1"

# Direct chain latest pairs endpoints
CHAIN_ENDPOINTS = {
    "SOL": "https://api.dexscreener.com/latest/dex/pairs/solana",
    "ETH": "https://api.dexscreener.com/latest/dex/pairs/ethereum",
}

# Search by volume using Dexscreener's rankBy parameter
VOLUME_SEARCH = "https://api.dexscreener.com/latest/dex/search?q=SOL&rankBy=volume&order=desc"


@dataclass
class DexToken:
    address: str
    chain: str
    name: str
    symbol: str
    price_usd: float
    volume_24h: float
    volume_6h: float
    market_cap: float
    liquidity_usd: float
    price_change_24h: float
    price_change_6h: float
    created_at: float  # unix timestamp, 0 if unknown
    dex_url: str


def _parse_pair(pair: dict, chain: str) -> DexToken:
    base = pair.get("baseToken", {})
    vol  = pair.get("volume", {})
    chg  = pair.get("priceChange", {})
    liq  = pair.get("liquidity", {})

    # Try to get creation time
    created_at = 0.0
    if pair.get("pairCreatedAt"):
        created_at = pair["pairCreatedAt"] / 1000  # ms to seconds

    return DexToken(
        address=base.get("address", ""),
        chain=chain,
        name=base.get("name", "Unknown"),
        symbol=base.get("symbol", "???"),
        price_usd=float(pair.get("priceUsd", 0) or 0),
        volume_24h=float(vol.get("h24", 0) or 0),
        volume_6h=float(vol.get("h6", 0) or 0),
        market_cap=float(pair.get("marketCap", 0) or 0),
        liquidity_usd=float(liq.get("usd", 0) or 0),
        price_change_24h=float(chg.get("h24", 0) or 0),
        price_change_6h=float(chg.get("h6", 0) or 0),
        created_at=created_at,
        dex_url=pair.get("url", ""),
    )


async def fetch_top_volume_coins(hours: float) -> List[DexToken]:
    """
    Fetch top volume coins for SOL and ETH from Dexscreener.
    Filters by minimum volume and time window.
    Returns sorted by volume (highest first).
    """
    all_tokens: List[DexToken] = []

    async with aiohttp.ClientSession() as session:
        for chain, endpoint in CHAIN_ENDPOINTS.items():
            try:
                async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.warning(f"Dexscreener {chain} returned {resp.status}")
                        continue
                    data = await resp.json()

                pairs = data.get("pairs", [])
                if not pairs:
                    continue

                for pair in pairs:
                    token = _parse_pair(pair, chain)

                    # Pick volume based on window
                    vol = token.volume_6h if hours <= 6 else token.volume_24h
                    if vol < MIN_VOLUME_USD:
                        continue

                    # Filter by creation time if window < 24h
                    if hours < 24 and token.created_at > 0:
                        age_hours = (datetime.now(timezone.utc).timestamp() - token.created_at) / 3600
                        # Include if created within window OR has high volume in window
                        # (don't exclude old coins that pumped recently)

                    all_tokens.append(token)

            except Exception as e:
                logger.error(f"Error fetching {chain} pairs: {e}")

        # Also search for trending tokens
        try:
            for query in ["solana meme", "ethereum meme"]:
                url = f"https://api.dexscreener.com/latest/dex/search?q={query.replace(' ', '%20')}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pairs = data.get("pairs", [])
                        for pair in pairs:
                            chain_id = pair.get("chainId", "").lower()
                            chain = "SOL" if chain_id == "solana" else "ETH" if chain_id == "ethereum" else None
                            if not chain:
                                continue
                            token = _parse_pair(pair, chain)
                            vol = token.volume_6h if hours <= 6 else token.volume_24h
                            if vol >= MIN_VOLUME_USD:
                                all_tokens.append(token)
        except Exception as e:
            logger.warning(f"Trending search failed: {e}")

    # Deduplicate by address
    seen = set()
    unique = []
    for t in all_tokens:
        if t.address not in seen and t.address:
            seen.add(t.address)
            unique.append(t)

    # Sort by volume
    vol_key = (lambda t: t.volume_6h) if hours <= 6 else (lambda t: t.volume_24h)
    unique.sort(key=vol_key, reverse=True)

    return unique[:20]  # return top 20, briefing will trim to 10


def format_dex_message(tokens: List[DexToken], hours: float) -> str:
    """Format the pure dex briefing message."""
    if not tokens:
        return (
            f"😴 *No coins found with $200k+ volume in the last {hours:.0f}h*\n\n"
            "Market might be quiet. Try a longer window."
        )

    chain_emoji = {"SOL": "◎", "ETH": "Ξ"}
    top = tokens[:10]

    lines = [
        f"📊 *Top Volume Coins — Last {hours:.0f}h* ({len(top)} coins)\n",
        "_Pure on-chain — no social filter_",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for i, t in enumerate(top, 1):
        chain_icon = chain_emoji.get(t.chain, "🔗")
        vol = t.volume_6h if hours <= 6 else t.volume_24h
        chg = t.price_change_6h if hours <= 6 else t.price_change_24h

        if vol >= 1_000_000:
            vol_str = f"${vol/1_000_000:.1f}M"
        else:
            vol_str = f"${vol/1_000:.0f}K"

        mc = t.market_cap
        mc_str = f"${mc/1_000_000:.1f}M" if mc >= 1_000_000 else (f"${mc/1_000:.0f}K" if mc > 0 else "N/A")

        chg_icon = "🟢" if chg >= 0 else "🔴"
        ticker = f"${t.symbol}" if t.symbol else ""

        lines += [
            f"\n*{i}. {chain_icon} {t.name} {ticker}*",
            f"   📊 Vol: *{vol_str}* | MC: {mc_str}",
            f"   {chg_icon} Price: ${t.price_usd:.6g} ({chg:+.1f}%)",
            f"   💧 Liq: ${t.liquidity_usd/1_000:.0f}K",
            f"   📋 `{t.address[:8]}...{t.address[-6:]}`",
            f"   🔗 [Chart]({t.dex_url})",
        ]

    lines += [
        "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "_Source: Dexscreener_",
    ]

    return "\n".join(lines)
