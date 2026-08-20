"""Persistent tracking subscriptions and change detection for FOMO traders."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote


@dataclass(frozen=True)
class TrackEvent:
    kind: str
    symbol: str
    token_address: str
    network_id: Any
    created_at: str | None = None
    usd_value: float | None = None
    native_value: float | None = None
    native_symbol: str | None = None
    value_label: str = "Value"
    provider: str | None = None
    detail: str | None = None
    image_url: str | None = None


_CHAIN_INFO = {
    "1399811149": ("Solana", "solana"),
    "solana": ("Solana", "solana"),
    "solana-mainnet": ("Solana", "solana"),
    "8453": ("Base", "base"),
    "eip155:8453": ("Base", "base"),
    "base": ("Base", "base"),
    "56": ("BSC", "bsc"),
    "eip155:56": ("BSC", "bsc"),
    "bsc": ("BSC", "bsc"),
    "bnb": ("BSC", "bsc"),
    "1": ("Ethereum", "ethereum"),
    "eip155:1": ("Ethereum", "ethereum"),
    "ethereum": ("Ethereum", "ethereum"),
}

_NATIVE_CURRENCIES = {
    "Solana": ("SOL", {
        "So11111111111111111111111111111111111111112",
        "11111111111111111111111111111111",
    }),
    "Ethereum": ("ETH", {
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "0x0000000000000000000000000000000000000000",
    }),
    "Base": ("ETH", {
        "0x4200000000000000000000000000000000000006",
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "0x0000000000000000000000000000000000000000",
    }),
    "BSC": ("BNB", {
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "0x0000000000000000000000000000000000000000",
    }),
}


def chain_name(network_id: Any) -> str:
    """Return a friendly chain name for an API network identifier."""
    key = str(network_id or "").strip().lower()
    if key in _CHAIN_INFO:
        return _CHAIN_INFO[key][0]
    return f"Chain {network_id}" if network_id not in (None, "") else "Unknown chain"


def native_currency(network_id: Any) -> str | None:
    info = _NATIVE_CURRENCIES.get(chain_name(network_id))
    return info[0] if info else None


def _swap_native_value(row: dict[str, Any], network_id: Any, side: str) -> float | None:
    info = _NATIVE_CURRENCIES.get(chain_name(network_id))
    if not info:
        return None
    side_network = row.get(f"{side}NetworkId")
    if side_network is not None and chain_name(side_network) != chain_name(network_id):
        return None
    address = str(row.get(f"{side}TokenAddress") or "")
    addresses = info[1]
    matches = address in addresses if chain_name(network_id) == "Solana" else address.lower() in addresses
    return _number(row.get(f"{side}HumanAmount")) if matches else None


def native_value_from_usd(usd_value: float | None, native_usd_price: float | None) -> float | None:
    if usd_value is None or native_usd_price is None or native_usd_price <= 0:
        return None
    return usd_value / native_usd_price


def fmt_native_amount(value: float | None, symbol: str | None) -> str:
    currency = symbol or "native"
    if value is None:
        return f"— {currency}"
    absolute = abs(value)
    decimals = 2 if absolute >= 1_000 else 4 if absolute >= 1 else 6
    number = f"{value:,.{decimals}f}".rstrip("0").rstrip(".")
    return f"{number} {currency}"


def padre_trade_url(network_id: Any, token_address: str) -> str | None:
    """Build a Padre token URL for chains Padre supports."""
    key = str(network_id or "").strip().lower()
    info = _CHAIN_INFO.get(key)
    address = token_address.strip()
    if not info or not address:
        return None
    return f"https://trade.padre.gg/trade/{info[1]}/{quote(address, safe='')}"


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trade_rows(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    rows: list[dict[str, Any]] = []
    for key in ("activeTrades", "closedTrades"):
        values = data.get(key)
        if isinstance(values, list):
            rows.extend(row for row in values if isinstance(row, dict))
    return rows


def _trade_index(trades: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in _trade_rows(trades):
        trade = row.get("trade") if isinstance(row.get("trade"), dict) else {}
        trade_id = str(trade.get("id") or "")
        if trade_id:
            result[trade_id] = trade
    return result


def _symbol(trade: dict[str, Any]) -> str:
    metadata = trade.get("tokenMetadata") if isinstance(trade.get("tokenMetadata"), dict) else {}
    value = " ".join(str(metadata.get("symbol") or "TOKEN").split()).lstrip("$")
    return value[:40] or "TOKEN"


def _image_url(trade: dict[str, Any]) -> str | None:
    metadata = trade.get("tokenMetadata") if isinstance(trade.get("tokenMetadata"), dict) else {}
    value = metadata.get("imageLargeUrl") or metadata.get("imageUrl")
    return str(value) if isinstance(value, str) and value.startswith(("https://", "http://")) else None


# How many event ids each subscription remembers. FOMO's trade response holds
# well under this, so an id only ages out once it has been absent for a long
# time.
SEEN_ID_LIMIT = 500


def _remember_ids(current: list[str], previous: Any,
                  limit: int = SEEN_ID_LIMIT) -> list[str]:
    """Merge this poll's ids into the ids already seen, newest first.

    ``/trades?userId=`` is unordered and its membership is not stable: a row
    drops out of one poll's response and comes back in the next. Rebuilding
    the baseline from the current response alone makes that reappearance look
    like new activity, so the same position is announced again every time it
    flaps. Keeping a bounded rolling memory makes a returning row a no-op.
    """
    known = [str(value) for value in previous] if isinstance(previous, list) else []
    return list(dict.fromkeys([*current, *known]))[:limit]


def snapshot(swaps: Any, trades: Any, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    swap_ids = [str(row["id"]) for row in ((swaps or {}).get("swaps") or [])
                if isinstance(row, dict) and row.get("id")]
    trade_ids: list[str] = []
    thesis_ids: list[str] = []
    previous_tokens = (previous or {}).get("tokens")
    tokens = dict(previous_tokens) if isinstance(previous_tokens, dict) else {}
    for row in _trade_rows(trades):
        trade = row.get("trade") if isinstance(row.get("trade"), dict) else {}
        comment = row.get("comment") if isinstance(row.get("comment"), dict) else {}
        if trade.get("id"):
            trade_id = str(trade["id"])
            trade_ids.append(trade_id)
            metadata = trade.get("tokenMetadata") if isinstance(trade.get("tokenMetadata"), dict) else {}
            tokens[trade_id] = {
                "tokenAddress": trade.get("tokenAddress"),
                "networkId": trade.get("networkId"),
                "tokenMetadata": {
                    "symbol": metadata.get("symbol"),
                    "imageLargeUrl": metadata.get("imageLargeUrl"),
                },
            }
        if comment.get("id"):
            thesis_ids.append(str(comment["id"]))
    # Keep enough history to identify a later sell even after the original
    # trade has fallen outside the current trades response.
    tokens = dict(list(tokens.items())[-250:])
    known = previous or {}
    return {
        "swapIds": _remember_ids(swap_ids, known.get("swapIds")),
        "tradeIds": _remember_ids(trade_ids, known.get("tradeIds")),
        "thesisIds": _remember_ids(thesis_ids, known.get("thesisIds")),
        "tokens": tokens,
    }


def detect_events(
    swaps: Any,
    trades: Any,
    previous: dict[str, Any],
    large_swap_usd: float,
) -> list[TrackEvent]:
    known_swaps = set(previous.get("swapIds") or [])
    known_trades = set(previous.get("tradeIds") or [])
    known_theses = set(previous.get("thesisIds") or [])
    events: list[TrackEvent] = []
    trades_by_id = _trade_index(trades)
    previous_tokens = previous.get("tokens")
    for trade_id, trade in (previous_tokens.items()
                            if isinstance(previous_tokens, dict) else []):
        if isinstance(trade, dict):
            trades_by_id.setdefault(str(trade_id), trade)
    swap_trade_ids: set[str] = set()

    for row in reversed(((swaps or {}).get("swaps") or [])):
        if not isinstance(row, dict) or not row.get("id") or str(row["id"]) in known_swaps:
            continue
        amounts = [_number(row.get("humanUsdAmountIn")), _number(row.get("humanUsdAmountOut"))]
        amount = max((value for value in amounts if value is not None), default=0.0)
        if amount < large_swap_usd:
            continue

        # FOMO attaches the tracked trade to the token side of a swap. An
        # outTradeId is a buy (the token was received); an inTradeId is a sell
        # (the token was sent). Prefer the destination for token-to-token swaps.
        out_trade_id = str(row.get("outTradeId") or "")
        in_trade_id = str(row.get("inTradeId") or "")
        if out_trade_id:
            kind = "buy"
            trade_id = out_trade_id
            token_address = str(row.get("outTokenAddress") or "")
            network_id = row.get("outNetworkId")
            usd_value = _number(row.get("humanUsdAmountIn")) or amount
            native_value = _swap_native_value(row, network_id, "in")
        elif in_trade_id:
            kind = "sell"
            trade_id = in_trade_id
            token_address = str(row.get("inTokenAddress") or "")
            network_id = row.get("inNetworkId")
            usd_value = _number(row.get("humanUsdAmountOut")) or amount
            native_value = _swap_native_value(row, network_id, "out")
        else:
            continue

        trade = trades_by_id.get(trade_id, {})
        swap_trade_ids.add(trade_id)
        events.append(TrackEvent(
            kind=kind,
            symbol=_symbol(trade),
            token_address=token_address or str(trade.get("tokenAddress") or ""),
            network_id=network_id if network_id is not None else trade.get("networkId"),
            created_at=row.get("createdAt"),
            usd_value=usd_value,
            native_value=native_value,
            native_symbol=native_currency(network_id),
            provider=str(row.get("provider") or "FOMO"),
            detail="Large swap",
            image_url=_image_url(trade),
        ))

    for row in reversed(_trade_rows(trades)):
        trade = row.get("trade") if isinstance(row.get("trade"), dict) else {}
        trade_id = str(trade.get("id") or "")
        token_address = str(trade.get("tokenAddress") or "")
        symbol = _symbol(trade)
        if (trade_id and token_address and trade_id not in known_trades
                and trade_id not in swap_trade_ids):
            cost = _number(trade.get("totalCostBasis"))
            events.append(TrackEvent(
                kind="buy",
                symbol=symbol,
                token_address=token_address,
                network_id=trade.get("networkId"),
                created_at=trade.get("createdAt"),
                usd_value=cost,
                native_symbol=native_currency(trade.get("networkId")),
                value_label="Cost basis",
                detail="New position",
                image_url=_image_url(trade),
            ))

        comment = row.get("comment") if isinstance(row.get("comment"), dict) else {}
        comment_id = str(comment.get("id") or "")
        if comment_id and comment_id not in known_theses:
            body = str(comment.get("comment") or "New thesis posted").replace("\n", " ").strip()
            if len(body) > 900:
                body = body[:897] + "…"
            events.append(TrackEvent(
                kind="thesis",
                symbol=symbol,
                token_address=token_address or str(comment.get("tokenAddress") or ""),
                network_id=trade.get("networkId") or comment.get("networkId"),
                created_at=comment.get("createdAt"),
                detail=body,
                image_url=_image_url(trade),
            ))

    return sorted(events, key=lambda event: event.created_at or "")


class TrackingStore:
    """Small atomic JSON store keyed by Discord channel and FOMO user id."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data = self._load()
        self._last_saved_payload = json.dumps(self._data, indent=1)

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"version": 1, "tracks": {}}
        except (OSError, ValueError):
            return {"version": 1, "tracks": {}}

    @property
    def tracks(self) -> dict[str, dict[str, Any]]:
        tracks = self._data.setdefault("tracks", {})
        if not isinstance(tracks, dict):
            tracks = {}
            self._data["tracks"] = tracks
        return tracks

    @staticmethod
    def key(channel_id: int, user_id: str) -> str:
        return f"{channel_id}:{user_id}"

    def add(
        self,
        channel_id: int,
        guild_id: int | None,
        user_id: str,
        handle: str,
        state: dict[str, Any],
        activity_filters: Any = "all",
    ) -> bool:
        key = self.key(channel_id, user_id)
        existed = key in self.tracks
        self.tracks[key] = {
            "channelId": channel_id,
            "guildId": guild_id,
            "userId": user_id,
            "handle": handle,
            "activityFilters": list(normalize_activity_filters(activity_filters)),
            **state,
        }
        self.save()
        return not existed

    def remove(self, channel_id: int, user_id: str) -> bool:
        removed = self.tracks.pop(self.key(channel_id, user_id), None) is not None
        if removed:
            self.save()
        return removed

    def set_activity_filters(
        self, channel_id: int, user_id: str, activity_filters: Any
    ) -> bool:
        """Update one subscription without replacing its event baseline."""
        entry = self.tracks.get(self.key(channel_id, user_id))
        if not isinstance(entry, dict):
            return False
        entry["activityFilters"] = list(normalize_activity_filters(activity_filters))
        entry.pop("activityFilter", None)
        self.save()
        return True

    def for_channel(self, channel_id: int) -> list[dict[str, Any]]:
        """Return this channel's subscriptions, ordered by handle."""
        entries = [
            entry for entry in self.tracks.values()
            if isinstance(entry, dict) and entry.get("channelId") == channel_id
        ]
        return sorted(entries, key=lambda entry: str(entry.get("handle") or "").casefold())

    def update_state(self, key: str, state: dict[str, Any]) -> None:
        entry = self.tracks.get(key)
        if entry is not None:
            entry.update(state)
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, indent=1)
        if payload == self._last_saved_payload:
            return
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(5):
                try:
                    os.replace(temporary, self.path)
                    self._last_saved_payload = payload
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    # Windows virus scanners and another just-finishing bot
                    # save can hold the destination for a few milliseconds.
                    time.sleep(0.025 * (2 ** attempt))
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


_ACTIVITY_FILTER_KINDS = {
    "buys": "buy",
    "sells": "sell",
    "theses": "thesis",
    "callouts": "callout",
}


def normalize_activity_filters(
    value: Any,
    allowed: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Normalize new multi-select values and legacy single-filter records."""
    raw = value if isinstance(value, (list, tuple, set)) else [value or "all"]
    selected = {str(item).lower() for item in raw}
    if "all" in selected:
        return allowed or ("all",)
    valid = tuple(
        item for item in (allowed or tuple(_ACTIVITY_FILTER_KINDS))
        if item in selected and item in _ACTIVITY_FILTER_KINDS
    )
    return valid or (allowed or ("all",))


def activity_allowed(activity_filters: Any, kind: str) -> bool:
    selected = normalize_activity_filters(activity_filters)
    return "all" in selected or any(
        _ACTIVITY_FILTER_KINDS.get(value) == kind for value in selected
    )


def activity_filter_label(activity_filters: Any) -> str:
    selected = normalize_activity_filters(activity_filters)
    if selected == ("all",):
        return "all activity"
    if len(selected) == 1:
        return f"{selected[0]} only"
    return " + ".join(selected)
