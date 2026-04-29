"""
dexscreener.py
──────────────
Fetches on-chain price/volume/mcap data from Dexscreener's free API.
Supports both Solana and Ethereum tokens.
"""

import asyncio
import aiohttp
from dataclasses import dataclass
from typing import Optional, List
import logging

logger = logging.getLogger(__name__)

DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"
DEXSCREENER_PAIR = "https://api.dexscreener.com/latest/dex/pairs"

MIN_VOLUME_USD = 200_000  # $200k minimum volume filter


@dataclass
class TokenData:
    address: str
    chain: str
    name: str
    symbol: str
    price_usd: float
    volume_24h: float
    market_cap: float
    liquidity_usd: float
    price_change_24h: float
    dex_url: str
    pair_address: str


async def fetch_token(session: aiohttp.ClientSession, address: str, chain: str) -> Optional[TokenData]:
    """Fetch token data from Dexscreener for a given contract address."""
    try:
        url = f"{DEXSCREENER_API}/{address}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        pairs = data.get("pairs", [])
        if not pairs:
            return None

        # Filter by chain and pick the pair with highest liquidity
        chain_map = {"SOL": "solana", "ETH": "ethereum"}
        chain_id = chain_map.get(chain, chain.lower())

        chain_pairs = [p for p in pairs if p.get("chainId", "").lower() == chain_id]
        if not chain_pairs:
            chain_pairs = pairs  # fallback: use all pairs

        # Pick best pair by liquidity
        best = max(chain_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))

        volume_24h = float(best.get("volume", {}).get("h24", 0) or 0)
        market_cap = float(best.get("marketCap", 0) or 0)
        liquidity  = float(best.get("liquidity", {}).get("usd", 0) or 0)
        price_usd  = float(best.get("priceUsd", 0) or 0)
        price_chg  = float(best.get("priceChange", {}).get("h24", 0) or 0)

        base_token = best.get("baseToken", {})
        name   = base_token.get("name", "Unknown")
        symbol = base_token.get("symbol", "???")

        dex_url      = best.get("url", f"https://dexscreener.com/{chain_id}/{best.get('pairAddress','')}")
        pair_address = best.get("pairAddress", "")

        return TokenData(
            address=address,
            chain=chain,
            name=name,
            symbol=symbol,
            price_usd=price_usd,
            volume_24h=volume_24h,
            market_cap=market_cap,
            liquidity_usd=liquidity,
            price_change_24h=price_chg,
            dex_url=dex_url,
            pair_address=pair_address,
        )

    except Exception as e:
        logger.warning(f"Dexscreener fetch failed for {address}: {e}")
        return None


async def fetch_tokens_batch(addresses_with_chains: List[tuple]) -> List[TokenData]:
    """
    Fetch multiple tokens concurrently.
    addresses_with_chains: list of (address, chain) tuples
    """
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_token(session, addr, chain) for addr, chain in addresses_with_chains]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    tokens = []
    for r in results:
        if isinstance(r, TokenData):
            tokens.append(r)

    return tokens


def passes_volume_filter(token: TokenData) -> bool:
    """Returns True if token meets the minimum volume threshold."""
    return token.volume_24h >= MIN_VOLUME_USD
