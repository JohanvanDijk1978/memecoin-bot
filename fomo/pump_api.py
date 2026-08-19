"""Small public Pump.fun API client used by the Discord bot.

The website endpoints are intentionally kept behind this adapter.  They are
public but not a versioned developer API, so callers should not depend on the
raw response shape outside this module.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote


CLIENT_URL = "https://frontend-api-v3.pump.fun"
PROFILE_URL = "https://profile-api.pump.fun"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

HEADERS = {
    "Accept": "application/json",
    "Origin": "https://pump.fun",
    "Referer": "https://pump.fun/",
    "User-Agent": "Mozilla/5.0 (compatible; FomoDiscordBot/1.0)",
}


class PumpError(RuntimeError):
    pass


class PumpNotFound(PumpError):
    pass


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def timestamp_iso(value: Any) -> str | None:
    """Convert Pump's millisecond timestamps to Discord-friendly ISO UTC."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


@dataclass(frozen=True)
class PumpUser:
    address: str
    username: str
    profile_image: str | None = None
    header_image: str | None = None
    bio: str | None = None
    x_username: str | None = None
    followers: int = 0
    following: int = 0

    @classmethod
    def from_raw(cls, raw: Any) -> "PumpUser":
        if not isinstance(raw, dict):
            raise PumpError("Pump returned an invalid user response")
        address = str(raw.get("address") or "").strip()
        username = str(raw.get("username") or "").strip().lstrip("@")
        if not address or not username:
            raise PumpNotFound("Pump profile not found")
        return cls(
            address=address,
            username=username,
            profile_image=_http_url(raw.get("profile_image")),
            header_image=_http_url(raw.get("header_image_url")),
            bio=str(raw.get("bio") or "").strip() or None,
            x_username=str(raw.get("x_username") or "").strip().lstrip("@") or None,
            followers=_integer(raw.get("followers")),
            following=_integer(raw.get("following")),
        )

    @property
    def profile_url(self) -> str:
        return f"https://pump.fun/profile/{quote(self.username, safe='')}"

    @property
    def x_url(self) -> str | None:
        return f"https://x.com/{quote(self.x_username, safe='')}" if self.x_username else None


@dataclass(frozen=True)
class PumpCoin:
    mint: str
    name: str
    symbol: str
    image_url: str | None = None
    creator: str | None = None
    created_at: str | None = None
    market_cap_usd: float | None = None
    quote_mint: str | None = None
    quote_decimals: int = 9
    protocol: str | None = None
    program: str | None = None
    complete: bool = False
    pool_address: str | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "PumpCoin":
        if not isinstance(raw, dict):
            raise PumpError("Pump returned invalid coin metadata")
        mint = str(raw.get("mint") or "").strip()
        if not mint:
            raise PumpNotFound("Pump coin not found")
        # `market_cap` can be quote-denominated.  The explicit USD fields are
        # the only safe values for Discord's MC label.
        market_cap = _number(raw.get("usd_market_cap"))
        if market_cap is None:
            market_cap = _number(raw.get("market_cap_usd"))
        return cls(
            mint=mint,
            name=str(raw.get("name") or raw.get("symbol") or "Unknown token").strip(),
            symbol=str(raw.get("symbol") or "TOKEN").strip().lstrip("$")[:40] or "TOKEN",
            image_url=_http_url(raw.get("image_uri")),
            creator=str(raw.get("creator") or "").strip() or None,
            created_at=timestamp_iso(raw.get("created_timestamp")),
            market_cap_usd=market_cap,
            quote_mint=str(raw.get("quote_mint") or "").strip() or None,
            quote_decimals=_integer(raw.get("quote_decimals"), 9),
            protocol=str(raw.get("protocol") or "").strip() or None,
            program=str(raw.get("program") or "").strip() or None,
            complete=bool(raw.get("complete")),
            pool_address=str(raw.get("pool_address") or "").strip() or None,
        )

    @property
    def pump_url(self) -> str:
        return f"https://pump.fun/coin/{quote(self.mint, safe='')}"


@dataclass(frozen=True)
class PumpHolding:
    mint: str
    symbol: str
    name: str
    value_usd: float | None
    balance: float | None
    pnl_usd: float | None
    pnl_percent: float | None
    image_url: str | None
    program: str | None

    @classmethod
    def from_raw(cls, raw: Any) -> "PumpHolding | None":
        if not isinstance(raw, dict) or not raw.get("token_mint"):
            return None
        pnl = raw.get("tokenPnL") if isinstance(raw.get("tokenPnL"), dict) else {}
        unrealized = _number(pnl.get("unrealized_usd"))
        realized = _number(pnl.get("realized_pnl_usd"))
        combined = None
        if unrealized is not None or realized is not None:
            combined = (unrealized or 0) + (realized or 0)
        return cls(
            mint=str(raw["token_mint"]),
            symbol=str(raw.get("token_symbol") or "TOKEN").strip().lstrip("$")[:40],
            name=str(raw.get("token_name") or "Unknown token").strip(),
            value_usd=_number(raw.get("value")),
            balance=_number(raw.get("balance")),
            pnl_usd=combined,
            pnl_percent=_number(pnl.get("percentage_usd") or pnl.get("percentage")),
            image_url=_http_url(raw.get("token_image")),
            program=str(raw.get("program") or "").strip() or None,
        )


@dataclass(frozen=True)
class PumpPortfolio:
    total_value: float | None = None
    native_balance: float | None = None
    token_count: int = 0
    cost_basis_usd: float | None = None
    unrealized_usd: float | None = None
    unrealized_percent: float | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "PumpPortfolio":
        data = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else raw
        if not isinstance(data, dict):
            return cls()
        pnl = data.get("portfolioPnL") if isinstance(data.get("portfolioPnL"), dict) else {}
        return cls(
            total_value=_number(data.get("total_value")),
            native_balance=_number(data.get("native_balance")),
            token_count=_integer(data.get("token_count")),
            cost_basis_usd=_number(pnl.get("total_cost_basis_usd")),
            unrealized_usd=_number(pnl.get("total_unrealized_usd")),
            unrealized_percent=_number(pnl.get("total_percentage")),
        )


@dataclass(frozen=True)
class PumpCallout:
    id: str
    user_id: str
    mint: str
    thesis: str
    created_at: str | None
    market_cap: float | None
    callout_price_usd: float | None
    likes: int = 0
    reposts: int = 0

    @classmethod
    def from_raw(cls, raw: Any) -> "PumpCallout | None":
        if not isinstance(raw, dict) or not raw.get("calloutId") or not raw.get("coinMint"):
            return None
        thesis = " ".join(str(raw.get("thesis") or "New callout").split())
        if len(thesis) > 1000:
            thesis = thesis[:997] + "…"
        return cls(
            id=str(raw["calloutId"]),
            user_id=str(raw.get("userId") or ""),
            mint=str(raw["coinMint"]),
            thesis=thesis,
            created_at=timestamp_iso(raw.get("createdAt")),
            market_cap=_number(raw.get("marketCap")),
            callout_price_usd=_number(raw.get("calloutPriceUsd")),
            likes=_integer(raw.get("likes")),
            reposts=_integer(raw.get("repostCount")),
        )


def _http_url(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.startswith(("https://", "http://")) else None


class PumpClient:
    def __init__(self, http: Any) -> None:
        self.http = http
        self._coin_cache: dict[str, tuple[float, PumpCoin]] = {}
        self._sol_price_cache: tuple[float, float | None] = (0, None)

    async def _get(self, base: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self.http.get(base + path, params=params, headers=HEADERS)
        except Exception as exc:
            raise PumpError(f"Pump request failed: {exc}") from exc
        status = int(getattr(response, "status_code", 200))
        if status == 404:
            raise PumpNotFound("Pump resource not found")
        if status >= 400:
            raise PumpError(f"Pump returned HTTP {status}")
        try:
            return response.json()
        except Exception as exc:
            raise PumpError("Pump returned invalid JSON") from exc

    async def resolve(self, term: str) -> PumpUser:
        clean = term.strip().strip("`").lstrip("@").strip()
        if not clean:
            raise PumpNotFound("Pump profile not found")
        raw = await self._get(CLIENT_URL, f"/users/{quote(clean, safe='')}")
        return PumpUser.from_raw(raw)

    async def coin(self, mint: str, *, fresh: bool = False) -> PumpCoin:
        clean = mint.strip()
        cached = self._coin_cache.get(clean)
        now = time.monotonic()
        if cached and not fresh and now - cached[0] < 60:
            return cached[1]
        raw = await self._get(CLIENT_URL, f"/coins/{quote(clean, safe='')}")
        coin = PumpCoin.from_raw(raw)
        self._coin_cache[clean] = (now, coin)
        return coin

    async def portfolio(self, wallet: str) -> PumpPortfolio:
        raw = await self._get(PROFILE_URL, f"/balance/summary/{quote(wallet, safe='')}")
        return PumpPortfolio.from_raw(raw)

    async def holdings(self, wallet: str, *, limit: int = 10) -> list[PumpHolding]:
        try:
            raw = await self._get(
                PROFILE_URL,
                f"/balance/tokens/{quote(wallet, safe='')}",
                params={"page": 1, "size": max(1, min(limit, 100))},
            )
        except PumpNotFound:
            return []
        data = raw.get("data") if isinstance(raw, dict) else None
        rows = data.get("tokens") if isinstance(data, dict) else []
        values = [PumpHolding.from_raw(row) for row in (rows or [])]
        return [value for value in values if value is not None]

    async def callouts(self, wallet: str, *, limit: int = 30) -> list[PumpCallout]:
        try:
            raw = await self._get(
                CLIENT_URL,
                f"/callout/list/{quote(wallet, safe='')}",
                params={"limit": max(1, min(limit, 100)), "sortBy": "TIMESTAMP", "sortOrder": "DESC"},
            )
        except PumpNotFound:
            return []
        rows = raw.get("callouts") if isinstance(raw, dict) else []
        values = [PumpCallout.from_raw(row) for row in (rows or [])]
        return [value for value in values if value is not None]

    async def created_coins(self, wallet: str, *, limit: int = 5) -> tuple[int, list[PumpCoin]]:
        try:
            raw = await self._get(
                CLIENT_URL,
                f"/coins-v2/user-created-coins/{quote(wallet, safe='')}",
                params={"limit": max(1, min(limit, 50)), "offset": 0},
            )
        except PumpNotFound:
            return 0, []
        rows = raw.get("coins") if isinstance(raw, dict) else []
        coins: list[PumpCoin] = []
        for row in rows or []:
            try:
                coins.append(PumpCoin.from_raw(row))
            except PumpError:
                continue
        return _integer(raw.get("count") if isinstance(raw, dict) else 0), coins

    async def sol_price(self) -> float | None:
        cached_at, cached_price = self._sol_price_cache
        now = time.monotonic()
        if now - cached_at < 30:
            return cached_price
        raw = await self._get(CLIENT_URL, "/sol-price")
        price = None
        if isinstance(raw, dict):
            for key in ("solPrice", "price", "usd", "sol_price"):
                price = _number(raw.get(key))
                if price is not None:
                    break
        self._sol_price_cache = (now, price)
        return price

    async def coins(self, mints: set[str]) -> dict[str, PumpCoin]:
        async def fetch(mint: str) -> tuple[str, PumpCoin | None]:
            try:
                return mint, await self.coin(mint)
            except PumpError:
                return mint, None

        pairs = await asyncio.gather(*(fetch(mint) for mint in mints))
        return {mint: coin for mint, coin in pairs if coin is not None}


def quote_value_usd(
    quote_mint: str | None,
    raw_amount: int,
    decimals: int,
    sol_price: float | None,
) -> float | None:
    """Convert an on-chain Pump quote amount to USD when the quote is known."""
    mint = (quote_mint or "").strip()
    if raw_amount < 0:
        raw_amount = abs(raw_amount)
    if mint in (USDC_MINT, USDT_MINT):
        return raw_amount / (10 ** max(0, decimals))
    # Legacy Pump events use the default pubkey for native SOL.
    if mint in ("", WSOL_MINT, "11111111111111111111111111111111"):
        return raw_amount / 1_000_000_000 * sol_price if sol_price is not None else None
    return None


def quote_value_sol(
    quote_mint: str | None,
    raw_amount: int,
    decimals: int,
    sol_price: float | None,
) -> float | None:
    """Express any supported Pump quote amount in SOL for alert display."""
    mint = (quote_mint or "").strip()
    amount = abs(raw_amount)
    if mint in ("", WSOL_MINT, "11111111111111111111111111111111"):
        return amount / 1_000_000_000
    usd_value = quote_value_usd(mint, amount, decimals, sol_price)
    if usd_value is None or sol_price is None or sol_price <= 0:
        return None
    return usd_value / sol_price
