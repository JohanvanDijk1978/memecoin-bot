"""
fomo_hodlers.py -- FOMO's own top-holder list for a token, matched to wallets.

`GET /hodlers/top?tokens=[{"address":..,"networkId":..}]` is what fomo.family's
Holders tab calls. It is spelled **hodlers**, which is why every `/holders`
probe returned 404. Each row carries the full user object plus that trader's
position, entry, PnL and hold time:

    { user: {userHandle, displayName, address, evmAddress, ...},
      humanAmount, value, price, pnl, unrealizedPnl, realizedPnl,
      costBasis, averageEntryPrice, averageHoldTimeSeconds, isDev, tradeId }

`user.address` is FOMO's synthetic address and is NOT the trading wallet (see
FOMO_API.md section 10), so it cannot be matched against on-chain owners.
`humanAmount` can: FOMO reports the exact position, and the token's on-chain
owners are already computed for `/token`. One unambiguous amount match names a
wallet.

That match was validated against a known-good pair: `/token` named
`8f39Xh…tsEr` as @Quanterty from the wallet cache, and this endpoint
independently reports Quanterty holding 16,682,532.40 of the same mint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# FOMO network ids, by the chain names `TokenIntelligence` reports.
NETWORK_IDS = {
    "Solana": 1399811149,
    "Base": 8453,
    "BSC": 56,
    "Ethereum": 1,
    "Robinhood": 4663,
}

# FOMO rounds `humanAmount` for display, so an exact equality test would miss
# every row. The match still has to be unique to be accepted, which is what
# actually keeps it honest.
CHAIN_NAMES_BY_ID = {value: key for key, value in NETWORK_IDS.items()}

MATCH_RELATIVE_TOLERANCE = 1e-6
MATCH_ABSOLUTE_FLOOR = 0.01
# How much empty space a match needs around it before it may be persisted:
# the next-nearest balance must be this many tolerances away.
CACHE_SEPARATION = 50.0


@dataclass(frozen=True)
class FomoHolder:
    handle: str
    display_name: str
    user_id: str
    amount: float
    value_usd: float | None = None
    pnl_usd: float | None = None
    unrealized_pnl_usd: float | None = None
    cost_basis_usd: float | None = None
    entry_price: float | None = None
    hold_seconds: int | None = None
    is_dev: bool = False
    twitter: str | None = None
    trade_id: str | None = None

    @property
    def roi(self) -> float | None:
        if not self.cost_basis_usd or self.pnl_usd is None:
            return None
        return self.pnl_usd / self.cost_basis_usd * 100


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # drop NaN


def network_id_for(chain: str) -> int | None:
    return NETWORK_IDS.get(chain) or NETWORK_IDS.get(str(chain).title())


def holders_query(address: str, network_id: int) -> str:
    """The `tokens` parameter is a JSON array, so one call covers many tokens."""
    from json import dumps
    from urllib.parse import urlencode

    payload = dumps([{"address": address, "networkId": network_id}],
                    separators=(",", ":"))
    return f"/hodlers/top?{urlencode({'tokens': payload})}"


def parse_token_holders(payload: Any) -> tuple[list[FomoHolder], int | None]:
    """Return (holders, totalHolders) from a `/hodlers/top` response."""
    rows = payload
    if isinstance(rows, dict):
        rows = rows.get("responseObject", rows)
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return [], None

    holders: list[FomoHolder] = []
    total: int | None = None
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        count = _number(entry.get("totalHolders"))
        if count is not None:
            total = int(count)
        for row in entry.get("topHolders") or []:
            if not isinstance(row, dict):
                continue
            user = row.get("user") if isinstance(row.get("user"), dict) else {}
            amount = _number(row.get("humanAmount"))
            handle = str(user.get("userHandle") or "")
            if amount is None or amount <= 0 or not handle:
                continue
            hold = _number(row.get("averageHoldTimeSeconds"))
            holders.append(FomoHolder(
                handle=handle,
                display_name=str(user.get("displayName") or handle),
                user_id=str(user.get("id") or ""),
                amount=amount,
                value_usd=_number(row.get("value")),
                pnl_usd=_number(row.get("pnl")),
                unrealized_pnl_usd=_number(row.get("unrealizedPnl")),
                cost_basis_usd=_number(row.get("costBasis")),
                entry_price=_number(row.get("averageEntryPrice")),
                hold_seconds=int(hold) if hold is not None else None,
                is_dev=bool(row.get("isDev")),
                twitter=user.get("twitter") or None,
                trade_id=str(row.get("tradeId") or "") or None,
            ))
    return holders, total


def _tolerance(amount: float) -> float:
    return max(MATCH_ABSOLUTE_FLOOR, abs(amount) * MATCH_RELATIVE_TOLERANCE)


def match_holders_to_wallets(
    holders: Iterable[FomoHolder],
    onchain: Iterable[tuple[str, float]],
    separation: float = 0.0,
) -> dict[str, FomoHolder]:
    """Wallet -> FOMO holder, by unambiguous position match.

    `onchain` is (owner wallet, balance). A pairing is accepted only when the
    amount identifies exactly ONE wallet and that wallet matches exactly ONE
    FOMO holder; two traders holding near-identical amounts leave both
    unnamed rather than guessing between them.

    `separation` additionally demands empty space around the match: the
    runner-up balance must be at least this many tolerances away. Labelling a
    Discord row is reversible and wants every match it can get (0); writing a
    permanent cache entry is not, and uses `CACHE_SEPARATION`.
    """
    chain_rows = [(wallet, balance) for wallet, balance in onchain
                  if wallet and balance is not None]
    fomo_rows = list(holders)

    pairs: dict[str, list[FomoHolder]] = {}
    claims: dict[str, list[str]] = {}
    for holder in fomo_rows:
        window = _tolerance(holder.amount)
        distances = sorted(
            ((abs(balance - holder.amount), wallet) for wallet, balance in chain_rows),
            key=lambda item: item[0],
        )
        if not distances or distances[0][0] > window:
            continue
        if len(distances) > 1 and distances[1][0] <= window:
            continue  # two wallets inside the window -- ambiguous
        if separation and len(distances) > 1 and distances[1][0] < window * separation:
            continue  # a near neighbour: close enough to doubt, so do not commit
        wallet = distances[0][1]
        pairs.setdefault(wallet, []).append(holder)
        claims.setdefault(holder.handle.lower(), []).append(wallet)

    return {
        wallet: candidates[0]
        for wallet, candidates in pairs.items()
        if len(candidates) == 1
        and len(claims.get(candidates[0].handle.lower(), [])) == 1
    }


def confident_matches(
    holders: Iterable[FomoHolder],
    onchain: Iterable[tuple[str, float]],
) -> dict[str, FomoHolder]:
    """Matches strong enough to write to disk.

    Same rule as the display match plus a separation margin, because a cached
    wallet is permanent, is trusted by `/fomo` and `/wallet`, and is derived
    here from a figure FOMO rounded for display.
    """
    return match_holders_to_wallets(holders, onchain, separation=CACHE_SEPARATION)
