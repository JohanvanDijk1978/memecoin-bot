"""Derived trader metrics for Discord embeds, cards, and alerts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from fomo_tracking import TrackEvent, detect_events


STABLE_SYMBOLS = {"USDC", "USDT", "USD", "USDG", "USDS", "DAI"}
STABLE_ADDRESSES = {
    # Solana USDC / USDT. EVM activity normally carries token metadata, but the
    # symbols above cover it when present.
    "epjfwdd5aufqssqem2qn1xzybapc8g4wegkzwydt1v",
    "es9vmfrzacermjfrf4h2fydmpqh9g2cybapfvr8ryj5",
    # Base / Ethereum / BNB Chain stablecoins used as FOMO quote assets.
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # Base USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # Base USDbC
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # Ethereum USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7",  # Ethereum USDT
    "0x55d398326f99059ff775485246999027b3197955",  # BSC USDT
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # BSC USDC
}


@dataclass(frozen=True)
class BestTrade:
    symbol: str
    pnl: float
    roi: float | None
    trade_id: str | None = None


@dataclass(frozen=True)
class OpenPosition:
    """One still-open trade, priced from FOMO's own trade row.

    PnL is derived per unit -- `amount x (current - entry)` -- rather than from
    `totalCostBasis`, because that basis covers the whole trade including any
    portion already sold. Per-unit keeps a partially-closed position honest.
    """
    symbol: str
    token_address: str
    network_id: Any
    amount: float
    entry_price: float | None
    current_price: float | None
    value_usd: float | None
    pnl_usd: float | None
    roi: float | None
    trade_id: str | None = None


@dataclass(frozen=True)
class LatestActivity:
    action: str
    symbol: str
    usd_value: float | None
    created_at: str | None
    activity_id: str | None = None
    chain: str = ""
    token_address: str = ""
    market_cap: float | None = None
    market_cap_estimated: bool = False


@dataclass(frozen=True)
class TraderStats:
    best_trade: BestTrade | None = None
    portfolio_value: float | None = None
    latest_buys: tuple[LatestActivity, ...] = ()
    latest_sells: tuple[TrackEvent, ...] = ()
    latest_theses: tuple[TrackEvent, ...] = ()
    open_positions: tuple[OpenPosition, ...] = ()
    raw_balances: Any = field(default=None, repr=False, compare=False)
    raw_trades: Any = field(default=None, repr=False, compare=False)
    raw_swaps: Any = field(default=None, repr=False, compare=False)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _trade_rows(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key in ("activeTrades", "closedTrades"):
        values = data.get(key)
        if isinstance(values, list):
            rows.extend(row for row in values if isinstance(row, dict))
    return rows


def _token_symbols(trades: Any) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for row in _trade_rows(trades):
        trade = row.get("trade") if isinstance(row.get("trade"), dict) else {}
        token = trade.get("tokenMetadata") if isinstance(trade.get("tokenMetadata"), dict) else {}
        address = trade.get("tokenAddress")
        network = trade.get("networkId")
        symbol = token.get("symbol")
        if isinstance(address, str) and isinstance(symbol, str) and symbol:
            result[(str(network), address.lower())] = symbol
    return result


def _portfolio_value(data: Any) -> float | None:
    if not isinstance(data, dict):
        return None
    total = 0.0
    found = False
    for row in data.get("balances") or []:
        if not isinstance(row, dict):
            continue
        balance = row.get("balance") if isinstance(row.get("balance"), dict) else {}
        token = row.get("tokenFilterResult") if isinstance(row.get("tokenFilterResult"), dict) else {}
        user_token = row.get("userToken") if isinstance(row.get("userToken"), dict) else {}
        amount = _number(balance.get("shiftedBalance"))
        if amount is None:
            amount = _number(user_token.get("humanAmountRemaining"))
        price = _number(token.get("priceUSD"))
        if amount is not None and price is not None:
            total += amount * price
            found = True

    # These rows vary by EVM provider. Prefer a supplied USD value and otherwise
    # fall back to amount * USD price without assuming decimals.
    for row in data.get("nativeEvmBalances") or []:
        if not isinstance(row, dict):
            continue
        direct = next(
            (_number(row.get(key)) for key in ("usdValue", "valueUsd", "balanceUsd")
             if _number(row.get(key)) is not None),
            None,
        )
        if direct is not None:
            total += direct
            found = True
            continue
        amount = next(
            (_number(row.get(key)) for key in ("shiftedBalance", "humanBalance", "balance")
             if _number(row.get(key)) is not None),
            None,
        )
        price = next(
            (_number(row.get(key)) for key in ("priceUsd", "priceUSD", "usdPrice")
             if _number(row.get(key)) is not None),
            None,
        )
        if amount is not None and price is not None:
            total += amount * price
            found = True
    return total if found else None


def _best_trade(data: Any) -> BestTrade | None:
    rows = data.get("bestTrades") if isinstance(data, dict) else None
    candidates: list[BestTrade] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        trade = row.get("trade") if isinstance(row.get("trade"), dict) else {}
        pnl = _number(trade.get("realizedPnlUsd"))
        if pnl is None:
            continue
        metadata = trade.get("tokenMetadata") if isinstance(trade.get("tokenMetadata"), dict) else {}
        symbol = str(metadata.get("symbol") or "Unknown")
        cost = _number(trade.get("totalCostBasis"))
        roi = (pnl / cost * 100) if cost and cost > 0 else None
        candidates.append(BestTrade(symbol, pnl, roi, trade.get("id")))
    return max(candidates, key=lambda item: item.pnl, default=None)


def _chain_name(network: Any, token_address: str) -> str:
    value = str(network or "").lower()
    if value in {"1399811149", "solana", "solana-mainnet"}:
        return "Solana"
    if value in {"8453", "eip155:8453", "base"}:
        return "Base"
    if value in {"56", "eip155:56", "bsc", "bnb"}:
        return "BSC"
    if value in {"1", "eip155:1", "ethereum"}:
        return "Ethereum"
    if value in {"4663", "eip155:4663", "robinhood", "robinhood-chain"}:
        return "Robinhood"
    return "EVM" if token_address.lower().startswith("0x") else "Solana"


def _market_lookup(balances: Any, trades: Any,
                   external: Any = None) -> dict[tuple[str, str], tuple[float, float]]:
    """Map (chain, token) -> (current market cap/FDV, current USD price)."""
    result: dict[tuple[str, str], tuple[float, float]] = {}
    if isinstance(external, dict):
        for key, value in external.items():
            if not isinstance(key, tuple) or len(key) != 2 or not isinstance(value, dict):
                continue
            market_cap = _number(value.get("marketCap") or value.get("fdv"))
            price = _number(value.get("priceUsd"))
            if market_cap and price and market_cap > 0 and price > 0:
                result[(str(key[0]), str(key[1]).lower())] = (market_cap, price)

    for row in _trade_rows(trades):
        trade = row.get("trade") if isinstance(row.get("trade"), dict) else {}
        metadata = trade.get("tokenMetadata") if isinstance(trade.get("tokenMetadata"), dict) else {}
        address = str(trade.get("tokenAddress") or "")
        chain = _chain_name(trade.get("networkId"), address)
        market_cap = _number(metadata.get("marketCap") or metadata.get("fdv"))
        price = _number(metadata.get("currentPrice") or metadata.get("priceUSD"))
        if address and market_cap and price and market_cap > 0 and price > 0:
            result.setdefault((chain, address.lower()), (market_cap, price))

    if isinstance(balances, dict):
        for row in balances.get("balances") or []:
            if not isinstance(row, dict):
                continue
            balance = row.get("balance") if isinstance(row.get("balance"), dict) else {}
            token_filter = row.get("tokenFilterResult") if isinstance(row.get("tokenFilterResult"), dict) else {}
            token = token_filter.get("token") if isinstance(token_filter.get("token"), dict) else {}
            user_token = row.get("userToken") if isinstance(row.get("userToken"), dict) else {}
            address = str(balance.get("tokenAddress") or token.get("address")
                          or user_token.get("tokenAddress") or "")
            network = user_token.get("networkId", token.get("networkId"))
            chain = _chain_name(network, address)
            market_cap = _number(token_filter.get("marketCap") or token_filter.get("fdv"))
            price = _number(token_filter.get("priceUSD"))
            if address and market_cap and price and market_cap > 0 and price > 0:
                result.setdefault((chain, address.lower()), (market_cap, price))
    return result


def _latest_buys(swaps: Any, trades: Any, balances: Any = None,
                 market_data: Any = None, limit: int = 3) -> tuple[LatestActivity, ...]:
    rows = swaps.get("swaps") if isinstance(swaps, dict) else None
    valid = [row for row in (rows or []) if isinstance(row, dict)]
    if not valid:
        return ()
    symbols = _token_symbols(trades)
    trade_by_id: dict[str, dict[str, Any]] = {}
    for row in _trade_rows(trades):
        trade = row.get("trade") if isinstance(row.get("trade"), dict) else {}
        if trade.get("id"):
            trade_by_id[str(trade["id"])] = trade
    markets = _market_lookup(balances, trades, market_data)
    grouped: dict[str, dict[str, Any]] = {}
    for swap in valid:
        in_address = str(swap.get("inTokenAddress") or "")
        out_address = str(swap.get("outTokenAddress") or "")
        in_network = swap.get("inNetworkId", swap.get("networkId"))
        out_network = swap.get("outNetworkId", swap.get("networkId"))
        in_symbol = symbols.get((str(in_network), in_address.lower()), "")
        out_symbol = symbols.get((str(out_network), out_address.lower()), "")
        in_stable = in_address.lower() in STABLE_ADDRESSES or in_symbol.upper() in STABLE_SYMBOLS
        out_stable = out_address.lower() in STABLE_ADDRESSES or out_symbol.upper() in STABLE_SYMBOLS
        # outTradeId is FOMO's strongest signal that the output token opens a
        # position. Stablecoin direction is the fallback for older swap rows.
        is_buy = bool(swap.get("outTradeId")) or (in_stable and not out_stable)
        if not is_buy:
            continue
        group_id = str(swap.get("outTradeId") or swap.get("id") or "")
        if not group_id:
            continue
        values = [_number(swap.get("humanUsdAmountIn")), _number(swap.get("humanUsdAmountOut"))]
        usd = max((value for value in values if value is not None), default=0.0)
        token_amount = _number(swap.get("outHumanAmount")) or 0.0
        created = str(swap.get("createdAt") or "")
        direct_market_cap = next(
            (_number(swap.get(key)) for key in
             ("marketCapAtSwap", "marketCapAtEntry", "entryMarketCap", "marketCap")
             if _number(swap.get(key)) is not None),
            None,
        )
        current = grouped.get(group_id)
        if current is None:
            grouped[group_id] = {
                "symbol": out_symbol or _short_token(out_address),
                "chain": _chain_name(out_network, out_address),
                "usd": usd,
                "created": created,
                "id": group_id,
                "address": out_address,
                "token_amount": token_amount,
                "direct_market_cap": direct_market_cap,
            }
        else:
            current["usd"] += usd
            current["token_amount"] += token_amount
            current["direct_market_cap"] = current["direct_market_cap"] or direct_market_cap
            if created > current["created"]:
                current["created"] = created

    ordered = sorted(grouped.values(), key=lambda item: item["created"], reverse=True)
    activities: list[LatestActivity] = []
    for item in ordered[:limit]:
        direct = item["direct_market_cap"]
        trade = trade_by_id.get(item["id"], {})
        if direct is None:
            direct = next(
                (_number(trade.get(key)) for key in
                 ("marketCapAtEntry", "entryMarketCap", "entryMarketCapUsd")
                 if _number(trade.get(key)) is not None),
                None,
            )
        estimated = False
        market_cap = direct
        current_market = markets.get((item["chain"], item["address"].lower()))
        trade_cost = _number(trade.get("totalCostBasis"))
        display_usd = trade_cost if trade_cost is not None and trade_cost > 0 else item["usd"]
        entry_price = _number(trade.get("avgEntryPrice") or trade.get("avgTransferInPrice"))
        if entry_price is None and item["usd"] > 0 and item["token_amount"] > 0:
            entry_price = item["usd"] / item["token_amount"]
        if market_cap is None and current_market and entry_price:
            current_cap, current_price = current_market
            market_cap = current_cap * entry_price / current_price
            estimated = True
        activities.append(LatestActivity(
            "Bought", item["symbol"], display_usd, item["created"], item["id"],
            item["chain"], item["address"], market_cap, estimated,
        ))
    return tuple(activities)


def fmt_price(value: float | None) -> str:
    """Token prices span nine orders of magnitude; $0.00 is not a price.

    `fmt_usd` rounds to cents, which erases every memecoin entry. This keeps
    four significant digits below a cent and stays readable above it.
    """
    if value is None:
        return "—"
    magnitude = abs(float(value))
    if magnitude == 0:
        return "$0"
    if magnitude >= 1:
        return f"${value:,.4f}".rstrip("0").rstrip(".")
    decimals = 4
    probe = magnitude
    while probe < 0.1 and decimals < 12:
        probe *= 10
        decimals += 1
    return f"${value:.{decimals}f}".rstrip("0").rstrip(".")


def open_positions(trades: Any, limit: int = 5) -> tuple[OpenPosition, ...]:
    """Active trades, largest position first.

    FOMO's trade row already carries the average entry, the amount still held
    and the token's current price, so the open book needs no extra request.
    """
    rows = trades.get("activeTrades") if isinstance(trades, dict) else None
    positions: list[OpenPosition] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        trade = row.get("trade") if isinstance(row.get("trade"), dict) else row
        if not isinstance(trade, dict):
            continue
        amount = _number(trade.get("humanTokenAmount"))
        if amount is None or amount <= 0:
            continue
        metadata = (trade.get("tokenMetadata")
                    if isinstance(trade.get("tokenMetadata"), dict) else {})
        entry = next(
            (_number(trade.get(key)) for key in ("avgEntryPrice", "avgTransferInPrice")
             if _number(trade.get(key))),
            None,
        )
        if entry is None:
            cost = _number(trade.get("totalCostBasis"))
            entry = cost / amount if cost and cost > 0 else None
        current = _number(metadata.get("currentPrice"))
        value = amount * current if current is not None else None
        pnl = amount * (current - entry) if (current is not None and entry) else None
        roi = ((current / entry) - 1) * 100 if (current is not None and entry) else None
        positions.append(OpenPosition(
            symbol=str(metadata.get("symbol") or "")
            or _short_token(str(trade.get("tokenAddress") or "")),
            token_address=str(trade.get("tokenAddress") or ""),
            network_id=trade.get("networkId"),
            amount=amount,
            entry_price=entry,
            current_price=current,
            value_usd=value,
            pnl_usd=pnl,
            roi=roi,
            trade_id=str(trade.get("id") or "") or None,
        ))
    positions.sort(key=lambda item: (item.value_usd is None, -(item.value_usd or 0.0)))
    return tuple(positions[:limit])


def _short_token(address: str) -> str:
    return f"{address[:5]}…{address[-4:]}" if len(address) > 12 else (address or "token")


def build_trader_stats(
    balances: Any = None,
    spotlight: Any = None,
    trades: Any = None,
    swaps: Any = None,
    market_data: Any = None,
) -> TraderStats:
    events = detect_events(swaps, trades, {}, 0)
    latest_sells = tuple(
        sorted(
            (event for event in events if event.kind == "sell"),
            key=lambda event: event.created_at or "",
            reverse=True,
        )[:3]
    )
    latest_theses = tuple(
        sorted(
            (event for event in events if event.kind == "thesis"),
            key=lambda event: event.created_at or "",
            reverse=True,
        )[:3]
    )
    return TraderStats(
        best_trade=_best_trade(spotlight),
        portfolio_value=_portfolio_value(balances),
        latest_buys=_latest_buys(swaps, trades, balances, market_data),
        latest_sells=latest_sells,
        latest_theses=latest_theses,
        open_positions=open_positions(trades),
        raw_balances=balances,
        raw_trades=trades,
        raw_swaps=swaps,
    )


async def fetch_trader_stats(client: Any, user_id: str) -> TraderStats:
    """Fetch independent enrichments; one failed panel never hides the rest."""
    if hasattr(client, "profile_panels"):
        results = await client.profile_panels(user_id)
    else:
        results = await asyncio.gather(
            client.balances(user_id),
            client.spotlight(user_id),
            client.trades(user_id),
            client.swaps(user_id, limit=50),
            return_exceptions=True,
        )
    clean = [None if isinstance(value, Exception) else value for value in results]
    preliminary = build_trader_stats(*clean)
    tokens = [(buy.chain, buy.token_address) for buy in preliminary.latest_buys
              if buy.token_address]
    try:
        market_data = await client.token_market_data(tokens)
    except Exception:
        market_data = {}
    return build_trader_stats(*clean, market_data=market_data)


def merge_latest_buys(stats: TraderStats, *feeds: tuple[LatestActivity, ...],
                      limit: int = 3) -> TraderStats:
    """Merge FOMO and on-chain feeds, de-duplicate, and keep newest first."""
    merged = list(stats.latest_buys)
    for feed in feeds:
        merged.extend(feed)
    unique: dict[tuple[str, str], LatestActivity] = {}
    for buy in merged:
        key = (buy.chain, buy.activity_id or f"{buy.token_address}:{buy.created_at}")
        unique[key] = buy
    ordered = sorted(unique.values(), key=lambda item: item.created_at or "", reverse=True)
    return replace(stats, latest_buys=tuple(ordered[:limit]))


def merge_latest_sells(
    stats: TraderStats, *feeds: tuple[TrackEvent, ...], limit: int = 3
) -> TraderStats:
    """Merge API and verified-wallet sells, de-duplicate, and keep newest."""
    merged = list(stats.latest_sells)
    for feed in feeds:
        merged.extend(feed)
    unique: dict[tuple[str, str, str, str], TrackEvent] = {}
    for sell in merged:
        key = (
            str(sell.network_id),
            sell.token_address.lower(),
            sell.created_at or "",
            str(sell.usd_value or ""),
        )
        unique[key] = sell
    ordered = sorted(unique.values(), key=lambda item: item.created_at or "", reverse=True)
    return replace(stats, latest_sells=tuple(ordered[:limit]))


def iso_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def iso_to_unix(value: str | None) -> int | None:
    parsed = iso_to_datetime(value)
    return int(parsed.timestamp()) if parsed is not None else None
