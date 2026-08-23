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
from pickle import dumps
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


def holders_query_many(tokens: Iterable[tuple[str, int]]) -> str:
    """`tokens` is a JSON array, so one call covers many tokens.

    That matters for wallet resolution rather than for `/token`: a trader's
    whole position list can be asked about in a single request, and only the
    tokens whose holder list actually names them cost an on-chain query
    afterwards.
    """
    from json import dumps
    from urllib.parse import urlencode

    payload = dumps(
        [{"address": address, "networkId": network_id}
         for address, network_id in tokens],
         separators=(",", ":"),
         )
    return f"/hodlers/top?{urlencode({'tokens': payload, 'limit': 1000})}"


def holders_query(address: str, network_id: int) -> str:
    return holders_query_many([(address, network_id)])


@dataclass(frozen=True)
class TokenHolderGroup:
    """One token's holder list, kept apart from the others in a batch reply."""
    address: str
    network_id: int | None
    total: int | None
    holders: tuple[FomoHolder, ...]


def _parse_holder_rows(entry: dict[str, Any]) -> list[FomoHolder]:
    holders: list[FomoHolder] = []
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
    return holders


def parse_holder_groups(payload: Any) -> list[TokenHolderGroup]:
    """Split a `/hodlers/top` reply into one group per token.

    `parse_token_holders` flattens every token's rows together, which is right
    for `/token` (it asks about one mint) and wrong for wallet resolution,
    which asks about a trader's whole position list at once and has to know
    which amount belongs to which token.
    """
    rows = payload
    if isinstance(rows, dict):
        rows = rows.get("responseObject", rows)
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        return []

    groups: list[TokenHolderGroup] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        count = _number(entry.get("totalHolders"))
        network = _number(entry.get("networkId"))
        address = str(entry.get("tokenAddress") or "")
        holders = tuple(_parse_holder_rows(entry))
        # A token with no FOMO holders is a real answer; a dict that names no
        # token and carries no rows is just an unrecognised shape.
        if not address and not holders:
            continue
        groups.append(TokenHolderGroup(
            address=address,
            network_id=int(network) if network is not None else None,
            total=int(count) if count is not None else None,
            holders=holders,
        ))
    return groups


def parse_token_holders(payload: Any) -> tuple[list[FomoHolder], int | None]:
    """Return (holders, totalHolders) from a `/hodlers/top` response."""
    groups = parse_holder_groups(payload)
    holders = [holder for group in groups for holder in group.holders]
    totals = [group.total for group in groups if group.total is not None]
    return holders, (totals[-1] if totals else None)


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


# ---------------------------------------------------------------- theses ----
# A holder's written thesis is the comment attached to their trade on this
# token. Two routes carry it and neither is a superset of the other:
#
#   /feed/token/sortedThesis  one request, already ranked by position
#   /hodlers/top + /trades/{tradeId}   verified, but one request per holder
#
# `parse_thesis_feed` reads the first; `theses_from_trades` reassembles the
# second from rows this module already parses. Both produce `HolderThesis`, so
# `/thesis` renders one shape whichever route answered.

THESIS_TEXT_KEYS = ("comment", "thesis", "text", "body", "content", "message")
THESIS_ROW_KEYS = ("theses", "thesis", "items", "rows", "feed", "results", "data")


@dataclass(frozen=True)
class HolderThesis:
    handle: str
    display_name: str
    text: str
    value_usd: float | None = None
    pnl_usd: float | None = None
    hold_seconds: int | None = None
    twitter: str | None = None
    is_dev: bool = False
    trade_id: str | None = None

    @property
    def sort_key(self) -> float:
        """Rank by position size; an unpriced position sorts last, not first."""
        return self.value_usd if self.value_usd is not None else float("-inf")


def thesis_feed_query(address: str, network_id: int, limit: int = 50) -> str:
    from urllib.parse import urlencode

    return "/feed/token/sortedThesis?" + urlencode({
        "tokenAddress": address,
        "networkId": network_id,
        "limit": limit,
    })


def _thesis_text(source: Any) -> str:
    """Pull the written thesis out of whatever shape carries it.

    FOMO nests the text one level deep on the trade-detail route (`comment` is
    an object whose `comment` is the string) and flat on the feed. Both are
    handled here so neither caller has to guess.
    """
    if isinstance(source, str):
        return source.strip()
    if not isinstance(source, dict):
        return ""
    for key in THESIS_TEXT_KEYS:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _thesis_text(value)
            if nested:
                return nested
    return ""


def _thesis_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in THESIS_ROW_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        payload = payload.get("responseObject", payload)
        if isinstance(payload, dict):
            for key in THESIS_ROW_KEYS:
                value = payload.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
            return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def parse_thesis_feed(payload: Any) -> list[HolderThesis]:
    """Theses from `/feed/token/sortedThesis`.

    This route was recorded off the wire but never probed, so every field is
    read defensively: a shape that does not match returns [] and `/thesis`
    falls back to the verified holder + trade-detail path rather than showing
    an empty card.
    """
    out: list[HolderThesis] = []
    for row in _thesis_rows(payload):
        user = row.get("user") or row.get("author") or {}
        if not isinstance(user, dict):
            user = {}
        handle = str(user.get("userHandle") or row.get("userHandle") or "")
        text = _thesis_text(row.get("comment")) or _thesis_text(row)
        if not handle or not text:
            continue
        trade = row.get("authorTrade") or row.get("trade") or {}
        if not isinstance(trade, dict):
            trade = {}
        value = _number(row.get("equity"))
        if value is None:
            value = _number(trade.get("value") or row.get("value"))
        pnl = _number(row.get("pnl"))
        if pnl is None:
            pnl = _number(trade.get("pnl") or trade.get("realizedPnlUsd"))
        hold = _number(
            row.get("averageHoldTimeSeconds")
            or trade.get("averageHoldTimeSeconds")
        )
        out.append(HolderThesis(
            handle=handle,
            display_name=str(
                user.get("displayName") or row.get("displayName") or handle
            ),
            text=text,
            value_usd=value,
            pnl_usd=pnl,
            hold_seconds=int(hold) if hold is not None else None,
            twitter=user.get("twitter") or row.get("twitter") or None,
            is_dev=bool(row.get("isDev") or trade.get("isDev")),
            trade_id=str(row.get("tradeId") or trade.get("id") or "") or None,
        ))
    return out


def theses_from_trades(
    holders: Iterable[FomoHolder], details: Iterable[Any]
) -> list[HolderThesis]:
    """Pair `/hodlers/top` rows with their `/trades/{tradeId}` comments.

    `details` may contain exceptions -- `_get_many` returns them inline -- and
    a trade with no comment simply means that holder never wrote a thesis.
    Both are skipped rather than raised: a card missing one holder is better
    than no card.
    """
    texts: dict[str, str] = {}
    for detail in details:
        if not isinstance(detail, dict):
            continue
        trade = detail.get("trade") if isinstance(detail.get("trade"), dict) else {}
        trade_id = str(trade.get("id") or detail.get("id") or "")
        text = _thesis_text(detail.get("comment"))
        if trade_id and text:
            texts[trade_id] = text

    out: list[HolderThesis] = []
    for holder in holders:
        text = texts.get(holder.trade_id or "")
        if not text:
            continue
        out.append(HolderThesis(
            handle=holder.handle,
            display_name=holder.display_name,
            text=text,
            value_usd=holder.value_usd,
            pnl_usd=holder.pnl_usd,
            hold_seconds=holder.hold_seconds,
            twitter=holder.twitter,
            is_dev=holder.is_dev,
            trade_id=holder.trade_id,
        ))
    return out


def rank_theses(theses: Iterable[HolderThesis]) -> list[HolderThesis]:
    """Largest position first, one entry per handle."""
    best: dict[str, HolderThesis] = {}
    for thesis in theses:
        key = thesis.handle.casefold()
        current = best.get(key)
        if current is None or thesis.sort_key > current.sort_key:
            best[key] = thesis
    return sorted(best.values(), key=lambda item: item.sort_key, reverse=True)
