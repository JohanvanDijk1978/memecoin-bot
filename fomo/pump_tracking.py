"""Pump.fun subscription state and alert normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fomo_tracking import TrackingStore
from pump_api import PumpCallout, PumpCoin
from pump_chain import PumpChainTrade


@dataclass(frozen=True)
class PumpAlert:
    id: str
    kind: str
    mint: str
    symbol: str
    created_at: str | None
    usd_value: float | None = None
    native_value: float | None = None
    market_cap: float | None = None
    detail: str | None = None
    image_url: str | None = None
    source: str = "Pump"


def pump_snapshot(
    signature_ids: list[str],
    callouts: list[PumpCallout],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    old_signatures = (previous or {}).get("signatureIds")
    old_callouts = (previous or {}).get("calloutIds")
    signatures = list(dict.fromkeys(signature_ids + (
        [str(value) for value in old_signatures] if isinstance(old_signatures, list) else []
    )))[:250]
    callout_ids = list(dict.fromkeys(
        [callout.id for callout in callouts] +
        ([str(value) for value in old_callouts] if isinstance(old_callouts, list) else [])
    ))[:250]
    return {"signatureIds": signatures, "calloutIds": callout_ids}


def new_callouts(callouts: list[PumpCallout], previous: dict[str, Any]) -> list[PumpCallout]:
    known = set(str(value) for value in (previous.get("calloutIds") or []))
    return sorted(
        [callout for callout in callouts if callout.id not in known],
        key=lambda callout: callout.created_at or "",
    )


def trade_alert(
    trade: PumpChainTrade,
    coin: PumpCoin | None,
    usd_value: float | None,
    native_value: float | None = None,
) -> PumpAlert:
    return PumpAlert(
        id=trade.id,
        kind=trade.kind,
        mint=trade.mint,
        symbol=coin.symbol if coin else "TOKEN",
        created_at=trade.created_at,
        usd_value=usd_value,
        native_value=native_value,
        market_cap=coin.market_cap_usd if coin else None,
        image_url=coin.image_url if coin else None,
        source=trade.source,
    )


def callout_alert(callout: PumpCallout, coin: PumpCoin | None) -> PumpAlert:
    return PumpAlert(
        id=callout.id,
        kind="callout",
        mint=callout.mint,
        symbol=coin.symbol if coin else "TOKEN",
        created_at=callout.created_at,
        market_cap=callout.market_cap,
        detail=callout.thesis,
        image_url=coin.image_url if coin else None,
        source="Pump callout",
    )


class PumpTrackingStore(TrackingStore):
    """A separate JSON store with wallet-backed Pump identities."""
