"""Resolve a FOMO handle to its verified EVM smart-contract wallet.

FOMO's public ``evmAddress`` user field is not assumed to be the trading wallet.

Automatic resolution first correlates multiple historical FOMO trades with
token transfers on the corresponding EVM chains.  The same address must explain
at least two independent transactions and have deployed code on an evidence
chain. Exact current-balance matching remains a fallback. An explicit manual
mapping can also be deployment-checked and cached. Results share the existing
wallet cache under a separate ``evmWallet`` key.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fomo_wallet import CACHE, _load_cache, _save_cache
from rpc_config import (
    env_rpc_urls,
    normalize_rpc_urls,
    rpc_display_name,
)

log = logging.getLogger("fomo.evm")

EVM_RPCS = {
    "base": env_rpc_urls("BASE_RPC", "BASE_RPC_FALLBACKS", "https://mainnet.base.org"),
    "bsc": env_rpc_urls(
        "BSC_RPC", "BSC_RPC_FALLBACKS", "https://bsc-dataseed.bnbchain.org"
    ),
    "ethereum": env_rpc_urls("ETH_RPC", "ETH_RPC_FALLBACKS"),
    "robinhood": env_rpc_urls(
        "ROBINHOOD_RPC",
        "ROBINHOOD_RPC_FALLBACKS",
        "https://rpc.mainnet.chain.robinhood.com",
    ),
}
EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
CMC_HOLDERS_URL = "https://pro-api.coinmarketcap.com/public-api/v1/dex/holders/list"
CHAIN_IDS = {"ethereum": 1, "bsc": 56, "base": 8453, "robinhood": 4663}
CHAIN_NAMES = {value: key for key, value in CHAIN_IDS.items()}
CMC_PLATFORMS = {1: "ethereum", 56: "bsc", 8453: "base"}
BLOCKSCOUT = {
    1: "https://eth.blockscout.com",
    8453: "https://base.blockscout.com",
    4663: "https://robinhoodchain.blockscout.com",
}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
STABLE_SYMBOLS = {"USDC", "USDT", "USD", "USDG", "USDS", "DAI"}
STABLE_DECIMALS = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 6,
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,
    "0x55d398326f99059ff775485246999027b3197955": 18,
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 18,
}
try:
    EVM_DISCOVERY_TOKENS = max(2, int(os.getenv("FOMO_EVM_DISCOVERY_TOKENS", "6")))
except ValueError:
    EVM_DISCOVERY_TOKENS = 6
try:
    EVM_DISCOVERY_PAGES = max(1, int(os.getenv("FOMO_EVM_DISCOVERY_PAGES", "4")))
except ValueError:
    EVM_DISCOVERY_PAGES = 4
# Seconds of slack added to each side of a trade's match window before it is
# converted into a block range.
TRANSFER_WINDOW_MARGIN = 300
# Rough seconds per block, used only to seed the block search.
BLOCK_TIME_HINTS = {1: 12.0, 56: 0.75, 8453: 2.0, 4663: 2.0}
BLOCK_SEARCH_PROBES = 12


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _same_balance(left: Decimal, right: Decimal) -> bool:
    tolerance = max(abs(left) * Decimal("0.000000001"), Decimal("0.000001"))
    return abs(left - right) <= tolerance


def _relays_amount(
    siblings: list["EvmTransfer"],
    wallet: str,
    amount: Decimal,
    tolerance: Decimal,
    direction: str,
) -> bool:
    """True when ``wallet`` passes the traded amount through this transaction.

    A router, an ERC-4337 bundler or a pool that sits between the trader and
    the liquidity both receives and forwards an amount indistinguishable from
    the trade, so it matches the same fingerprint as the trader and ties with
    them in the ranking. The trader is the endpoint of that chain: on a buy
    they only receive the amount, on a sell they only send it. Checking the
    opposite leg inside the same transaction removes every intermediary using
    transfers that have already been fetched.
    """
    opposite = "sender" if direction == "buy" else "recipient"
    return any(
        getattr(row, opposite) == wallet
        and abs(row.token_amount - amount) <= tolerance
        for row in siblings
    )


def _chain_id(value: Any) -> int | None:
    text = str(value or "").strip().lower()
    if text.startswith("eip155:"):
        text = text.split(":", 1)[1]
    aliases = {"ethereum": 1, "eth": 1, "bsc": 56, "bnb": 56,
               "base": 8453, "robinhood": 4663, "robinhood-chain": 4663}
    try:
        return int(text)
    except ValueError:
        return aliases.get(text)


@dataclass(frozen=True)
class EvmBalancePosition:
    token: str
    chain_id: int
    amounts: tuple[Decimal, ...]
    value_usd: float


@dataclass(frozen=True)
class EvmTradeEvidence:
    token: str
    chain_id: int
    direction: str
    created_at: int
    token_amount: Decimal
    usd_amount: Decimal | None
    evidence_id: str
    aggregate: bool = False
    liquidity: float = 0.0


@dataclass(frozen=True)
class EvmTransfer:
    token: str
    chain_id: int
    transaction: str
    sender: str
    recipient: str
    created_at: int
    token_amount: Decimal


@dataclass(frozen=True)
class EvmTransactionMatch:
    evidence: EvmTradeEvidence
    transfer: EvmTransfer
    wallet: str
    usd_matched: bool | None


def _iso_unix(value: Any) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError):
        return None


def _address(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("hash") or value.get("address")
    text = str(value or "").strip().lower()
    return text if EVM_RE.fullmatch(text) else ""


def _trade_objects(trades: Any, details: Any = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(trades, dict):
        for key in ("activeTrades", "closedTrades"):
            for row in trades.get(key) or []:
                if not isinstance(row, dict):
                    continue
                trade = row.get("trade") if isinstance(row.get("trade"), dict) else row
                if isinstance(trade, dict):
                    result.append(trade)
    for detail in details or []:
        if not isinstance(detail, dict):
            continue
        trade = detail.get("trade")
        if isinstance(trade, dict):
            result.append(trade)
    return result


def evm_trade_ids(trades: Any, limit: int = EVM_DISCOVERY_TOKENS) -> list[str]:
    """Prefer low-liquidity, older EVM positions for detailed swap evidence."""
    candidates: list[tuple[float, int, str]] = []
    for trade in _trade_objects(trades):
        token = str(trade.get("tokenAddress") or "").strip().lower()
        chain_id = _chain_id(trade.get("networkId"))
        trade_id = str(trade.get("id") or "")
        if not trade_id or not EVM_RE.fullmatch(token) or chain_id not in CHAIN_NAMES:
            continue
        metadata = (trade.get("tokenMetadata")
                    if isinstance(trade.get("tokenMetadata"), dict) else {})
        try:
            liquidity = float(metadata.get("liquidity") or float("inf"))
        except (TypeError, ValueError):
            liquidity = float("inf")
        candidates.append((liquidity, _iso_unix(trade.get("createdAt")) or 0, trade_id))
    candidates.sort()
    return list(dict.fromkeys(item[2] for item in candidates))[:limit]


def evm_trade_evidence(
    swaps: Any = None, trades: Any = None, details: Any = None,
) -> list[EvmTradeEvidence]:
    """Extract exact EVM trade fingerprints from profile and detail payloads."""
    trade_objects = _trade_objects(trades, details)
    trade_tokens: dict[str, tuple[str, int]] = {}
    for trade in trade_objects:
        trade_id = str(trade.get("id") or "")
        token = str(trade.get("tokenAddress") or "").strip().lower()
        chain_id = _chain_id(trade.get("networkId"))
        if trade_id and EVM_RE.fullmatch(token) and chain_id in CHAIN_NAMES:
            trade_tokens[trade_id] = (token, chain_id)

    swap_rows: list[dict[str, Any]] = []
    if isinstance(swaps, dict):
        swap_rows.extend(row for row in swaps.get("swaps") or [] if isinstance(row, dict))
    for detail in details or []:
        if isinstance(detail, dict):
            swap_rows.extend(row for row in detail.get("swaps") or [] if isinstance(row, dict))

    evidence: list[EvmTradeEvidence] = []
    represented_trades: set[str] = set()
    seen_swaps: set[tuple[str, str, str]] = set()
    for index, row in enumerate(swap_rows):
        created_at = _iso_unix(row.get("createdAt"))
        if created_at is None:
            continue
        swap_id = str(row.get("id") or f"swap-{index}")
        legs = (
            ("buy", "out", str(row.get("outTradeId") or "")),
            ("sell", "in", str(row.get("inTradeId") or "")),
        )
        for direction, side, trade_id in legs:
            token = str(row.get(f"{side}TokenAddress") or "").strip().lower()
            chain_id = _chain_id(row.get(f"{side}NetworkId") or row.get("networkId"))
            known_trade = trade_tokens.get(trade_id)
            if not trade_id and (token, chain_id) not in trade_tokens.values():
                continue
            if known_trade and known_trade != (token, chain_id):
                continue
            amount = _decimal(row.get(f"{side}HumanAmount"))
            if (not EVM_RE.fullmatch(token) or chain_id not in CHAIN_NAMES
                    or amount is None or amount <= 0):
                continue
            key = (swap_id, direction, token)
            if key in seen_swaps:
                continue
            seen_swaps.add(key)
            represented_trades.add(trade_id)
            usd = _decimal(
                row.get("humanUsdAmountIn" if direction == "buy" else "humanUsdAmountOut")
            )
            evidence.append(EvmTradeEvidence(
                token, chain_id, direction, created_at, amount,
                usd if usd is not None and usd > 0 else None,
                f"swap:{swap_id}:{direction}",
            ))

    # A trade row is an aggregate fingerprint. It is weaker than a detailed
    # swap, but remains useful when FOMO omitted the EVM swap from the profile
    # feed and the detail endpoint has no rows.
    seen_trades: set[str] = set()
    for trade in trade_objects:
        trade_id = str(trade.get("id") or "")
        if not trade_id or trade_id in represented_trades or trade_id in seen_trades:
            continue
        seen_trades.add(trade_id)
        token = str(trade.get("tokenAddress") or "").strip().lower()
        chain_id = _chain_id(trade.get("networkId"))
        created_at = _iso_unix(trade.get("createdAt"))
        amount = _decimal(trade.get("humanTokenAmount"))
        if (not EVM_RE.fullmatch(token) or chain_id not in CHAIN_NAMES
                or created_at is None or amount is None or amount <= 0):
            continue
        usd = _decimal(trade.get("totalCostBasis"))
        metadata = (trade.get("tokenMetadata")
                    if isinstance(trade.get("tokenMetadata"), dict) else {})
        try:
            liquidity = float(metadata.get("liquidity") or 0)
        except (TypeError, ValueError):
            liquidity = 0.0
        evidence.append(EvmTradeEvidence(
            token, chain_id, "buy", created_at, amount,
            usd if usd is not None and usd > 0 else None,
            f"trade:{trade_id}", aggregate=True, liquidity=liquidity,
        ))

    evidence.sort(key=lambda item: (
        item.aggregate,
        item.liquidity if item.liquidity > 0 else float("inf"),
        item.created_at,
    ))
    return evidence


def match_window(item: EvmTradeEvidence) -> int:
    """Seconds either side of a trade in which its transfer may appear."""
    return 900 if item.aggregate else 240


def match_tolerance(item: EvmTradeEvidence) -> Decimal:
    """Token amount difference still considered the same transfer."""
    return max(
        item.token_amount * (Decimal("0.05") if item.aggregate else Decimal("0.01")),
        Decimal("0.000001"),
    )


def select_evidence_groups(
    evidence: list[EvmTradeEvidence], limit: int = EVM_DISCOVERY_TOKENS,
) -> dict[tuple[int, str], list[EvmTradeEvidence]]:
    """The tokens worth searching, most corroborating evidence first.

    Two independent transactions are required, so a token the trader touched
    five times is worth more than five tokens touched once. Ties break towards
    recency. Taking the first tokens in evidence order instead selects
    oldest-first, which starves an active trader's recent tokens of any search.
    """
    grouped: dict[tuple[int, str], list[EvmTradeEvidence]] = defaultdict(list)
    for item in evidence:
        grouped[(item.chain_id, item.token)].append(item)
    ordered = sorted(
        grouped.items(),
        key=lambda entry: (
            sum(not item.aggregate for item in entry[1]),
            max(item.created_at for item in entry[1]),
        ),
        reverse=True,
    )
    return dict(ordered[:limit])


def evidence_windows(
    groups: dict[tuple[int, str], list[EvmTradeEvidence]],
    margin: int = TRANSFER_WINDOW_MARGIN,
) -> list[tuple[tuple[int, str], int, int]]:
    """Merged time ranges to search, one per cluster of trades.

    Only the minutes around a trade can ever match it, and paging a busy token
    backwards from the chain head cannot reach a trade that is hours old.
    """
    windows: list[tuple[tuple[int, str], int, int]] = []
    for key, items in groups.items():
        spans = sorted(
            (item.created_at - match_window(item) - margin,
             item.created_at + match_window(item) + margin)
            for item in items
        )
        start, end = spans[0]
        for next_start, next_end in spans[1:]:
            if next_start <= end:
                end = max(end, next_end)
            else:
                windows.append((key, start, end))
                start, end = next_start, next_end
        windows.append((key, start, end))
    return windows


def transfer_candidates(
    evidence: list[EvmTradeEvidence],
    transfers_by_token: dict[tuple[int, str], list[EvmTransfer]],
    limit: int = 4,
) -> list[tuple[EvmTradeEvidence, EvmTransfer, str]]:
    """Addresses that could own each trade, closest transfer first.

    Repeated round-number transfers are noisy, so only the closest few
    candidates per trade are returned; repeated agreement across independent
    trades is what actually disambiguates.
    """
    siblings: dict[tuple[int, str, str], list[EvmTransfer]] = defaultdict(list)
    for (chain_id, token), rows in transfers_by_token.items():
        for transfer in rows:
            siblings[(chain_id, token, transfer.transaction)].append(transfer)

    result: list[tuple[EvmTradeEvidence, EvmTransfer, str]] = []
    for item in evidence:
        rows = transfers_by_token.get((item.chain_id, item.token), [])
        window = match_window(item)
        tolerance = match_tolerance(item)
        nearby: list[tuple[int, EvmTransfer, str]] = []
        for transfer in rows:
            if abs(transfer.created_at - item.created_at) > window:
                continue
            wallet = (transfer.recipient if item.direction == "buy"
                      else transfer.sender)
            if not wallet:
                continue
            if abs(transfer.token_amount - item.token_amount) > tolerance:
                continue
            # Routers and bundlers relay the exact traded amount, so they match
            # this fingerprint as strongly as the trader does. Only the
            # endpoint of the transfer chain can own the position.
            if _relays_amount(
                siblings[(item.chain_id, item.token, transfer.transaction)],
                wallet, item.token_amount, tolerance, item.direction,
            ):
                continue
            nearby.append(
                (abs(transfer.created_at - item.created_at), transfer, wallet)
            )
        ordered = sorted(
            nearby,
            key=lambda candidate: (
                candidate[0],
                candidate[1].created_at,
                candidate[1].transaction,
                candidate[2],
            ),
        )
        result.extend((item, transfer, wallet)
                      for _, transfer, wallet in ordered[:limit])
    return result


def evm_balance_positions(payload: Any) -> list[EvmBalancePosition]:
    """Extract ownership fingerprints from FOMO's multi-chain balance rows."""
    rows = payload.get("balances") if isinstance(payload, dict) else None
    positions: list[EvmBalancePosition] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        balance = row.get("balance") if isinstance(row.get("balance"), dict) else {}
        token_filter = (row.get("tokenFilterResult")
                        if isinstance(row.get("tokenFilterResult"), dict) else {})
        token = token_filter.get("token") if isinstance(token_filter.get("token"), dict) else {}
        user_token = row.get("userToken") if isinstance(row.get("userToken"), dict) else {}
        address = str(
            balance.get("tokenAddress") or token.get("address")
            or user_token.get("tokenAddress") or ""
        ).strip().lower()
        chain_id = _chain_id(
            user_token.get("networkId") or token.get("networkId")
            or balance.get("networkId")
        )
        if not EVM_RE.fullmatch(address) or chain_id not in CHAIN_NAMES:
            continue
        amounts: list[Decimal] = []
        for value in (balance.get("shiftedBalance"), user_token.get("humanAmountRemaining")):
            amount = _decimal(value)
            if amount is not None and amount > 0 and amount not in amounts:
                amounts.append(amount)
        if not amounts:
            continue
        try:
            price = float(token_filter.get("priceUSD") or 0)
            value_usd = max(float(amount) * price for amount in amounts)
        except (TypeError, ValueError, OverflowError):
            value_usd = 0.0
        positions.append(EvmBalancePosition(address, chain_id, tuple(amounts), value_usd))
    positions.sort(key=lambda position: position.value_usd, reverse=True)
    return positions


def cached_evm_wallet(handle: str, cache_path: str | Path = CACHE) -> str | None:
    try:
        import json

        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        return None
    entry = cache.get(handle.lower())
    address = entry.get("evmWallet") if isinstance(entry, dict) else None
    return address if isinstance(address, str) and EVM_RE.fullmatch(address) else None


class EvmWalletResolver:
    """Handle -> corroborated EVM smart wallet, cached permanently.

    Failures return ``None`` so EVM enrichment never breaks the profile embed.
    Empty results are not cached because later trades or balances may provide
    enough independent evidence to resolve the trader.
    """

    def __init__(
        self,
        http: Any,
        rpcs: dict[str, str | list[str]] | None = None,
        cache_path: str | Path = CACHE,
    ) -> None:
        self.http = http
        configured = EVM_RPCS if rpcs is None else rpcs
        self.rpcs: dict[str, list[str]] = {}
        for name, urls in configured.items():
            normalized = normalize_rpc_urls(urls)
            if normalized:
                self.rpcs[name] = normalized
        self.cache_path = Path(cache_path)
        self._locks: dict[str, asyncio.Lock] = {}
        self._quote_cache: dict[tuple[int, str], tuple[Decimal, ...]] = {}
        self._block_times: dict[int, dict[int, int]] = {}
        self._head_blocks: dict[int, tuple[int, int, float]] = {}

    async def resolve(
        self,
        user: Any,
        use_cache: bool = True,
        balances: Any = None,
        swaps: Any = None,
        trades: Any = None,
        trade_details: Any = None,
    ) -> str | None:
        handle = (getattr(user, "handle", "") or "").lstrip("@").strip().lower()
        if not handle:
            return None
        if use_cache and (hit := cached_evm_wallet(handle, self.cache_path)):
            return hit

        lock = self._locks.setdefault(handle, asyncio.Lock())
        async with lock:
            if use_cache and (hit := cached_evm_wallet(handle, self.cache_path)):
                return hit
            try:
                evidence = evm_trade_evidence(swaps, trades, trade_details)
                if len(evidence) >= 2:
                    discovered = await self._resolve_from_transactions(handle, evidence)
                    if discovered:
                        return discovered
                if balances is not None:
                    discovered = await self._resolve_from_balances(handle, balances)
                    if discovered:
                        return discovered
                return None
            except Exception as exc:
                log.warning("EVM wallet resolution failed for %s: %s", handle, exc)
                return None

    async def _resolve_from_transactions(
        self, handle: str, evidence: list[EvmTradeEvidence]
    ) -> str | None:
        """Resolve a wallet only when independent historical trades agree."""
        groups = select_evidence_groups(evidence)
        if not groups:
            return None
        searched = {(item.chain_id, item.token) for item in evidence}
        if len(searched) > len(groups):
            log.debug(
                "EVM discovery for %s searches %d of %d token(s)",
                handle, len(groups), len(searched),
            )

        async def fetch_window(
            key: tuple[int, str], earliest: int, latest: int
        ) -> tuple[tuple[int, str], list[EvmTransfer]]:
            try:
                transfers = await self._transfers_for_token(
                    key[0], key[1], earliest, latest
                )
            except Exception as exc:
                log.debug(
                    "EVM transfer search failed for %s on %s: %s",
                    key[1], CHAIN_NAMES.get(key[0], key[0]), exc,
                )
                transfers = []
            return key, transfers

        fetched = await asyncio.gather(
            *(fetch_window(key, start, end)
              for key, start, end in evidence_windows(groups))
        )
        transfers_by_token: dict[tuple[int, str], list[EvmTransfer]] = defaultdict(list)
        for key, rows in fetched:
            transfers_by_token[key].extend(rows)
        for key, rows in transfers_by_token.items():
            unique = {
                (row.transaction, row.sender, row.recipient, row.token_amount): row
                for row in rows
            }
            transfers_by_token[key] = list(unique.values())
        for key in groups:
            if not transfers_by_token.get(key):
                log.debug(
                    "no EVM transfers found for %s on %s",
                    key[1], CHAIN_NAMES.get(key[0], key[0]),
                )

        preliminary = transfer_candidates(evidence, transfers_by_token)

        async def validate(
            item: EvmTradeEvidence, transfer: EvmTransfer, wallet: str
        ) -> EvmTransactionMatch | None:
            usd_matched: bool | None = None
            if item.usd_amount is not None:
                values = await self._transaction_quote_values(
                    item.chain_id, transfer.transaction
                )
                if values:
                    tolerance = max(Decimal("10"), item.usd_amount * Decimal("0.20"))
                    usd_matched = any(
                        abs(value - item.usd_amount) <= tolerance for value in values
                    )
                    if not usd_matched:
                        return None
            return EvmTransactionMatch(item, transfer, wallet, usd_matched)

        checked = await asyncio.gather(
            *(validate(item, transfer, wallet)
              for item, transfer, wallet in preliminary),
            return_exceptions=True,
        )
        matches = [item for item in checked if isinstance(item, EvmTransactionMatch)]
        by_wallet: dict[str, list[EvmTransactionMatch]] = defaultdict(list)
        for match in matches:
            by_wallet[match.wallet].append(match)

        ranked: list[tuple[tuple[int, int, int], str, list[EvmTransactionMatch]]] = []
        for wallet, wallet_matches in by_wallet.items():
            transactions = {match.transfer.transaction for match in wallet_matches}
            if len(transactions) < 2:
                continue
            tokens = {(match.evidence.chain_id, match.evidence.token)
                      for match in wallet_matches}
            usd_matches = sum(match.usd_matched is True for match in wallet_matches)
            ranked.append(((len(tokens), usd_matches, len(transactions)),
                           wallet, wallet_matches))
        ranked.sort(reverse=True)
        if not ranked:
            log.info(
                "no transaction-backed EVM wallet for %s: %d evidence item(s) "
                "produced %d candidate(s), none explaining two transactions",
                handle, len(evidence), len(by_wallet),
            )
            return None
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            log.info(
                "ambiguous transaction-backed EVM wallet for %s across %d evidence "
                "item(s): %s and %s both score %s",
                handle, len(evidence), ranked[0][1], ranked[1][1], ranked[0][0],
            )
            return None

        _score, wallet, wallet_matches = ranked[0]
        deployed, checked_chains = await self._deployed_chains(wallet)
        evidence_chains = {
            CHAIN_NAMES[match.evidence.chain_id] for match in wallet_matches
        }
        if not checked_chains or not evidence_chains.intersection(deployed):
            log.info(
                "transaction candidate %s for %s is not deployed on an evidence chain",
                wallet, handle,
            )
            return None

        transactions = {match.transfer.transaction for match in wallet_matches}
        tokens = {match.evidence.token for match in wallet_matches}
        self._save(
            handle, wallet, deployed, None, source="transactions+rpc",
            confirmations=len(transactions), evidence_tokens=sorted(tokens),
        )
        log.info(
            "resolved EVM %s -> %s from %d transaction(s) across %d token(s)",
            handle, wallet, len(transactions), len(tokens),
        )
        return wallet

    async def _transfers_for_token(
        self, chain_id: int, token: str, earliest: int, latest: int
    ) -> list[EvmTransfer]:
        chain = CHAIN_NAMES[chain_id]
        transfers: list[EvmTransfer] = []
        for url in self.rpcs.get(chain, []):
            if "alchemy.com" not in url.lower():
                continue
            try:
                transfers = await self._alchemy_token_transfers(
                    url, chain_id, token, earliest, latest
                )
            except Exception as exc:
                log.debug(
                    "Alchemy transfer search failed via %s: %s",
                    rpc_display_name(url), exc,
                )
                continue
            if any(earliest - 1200 <= row.created_at <= latest + 1200
                   for row in transfers):
                break
        if not any(earliest - 1200 <= row.created_at <= latest + 1200
                   for row in transfers):
            transfers.extend(await self._blockscout_token_transfers(
                chain_id, token, earliest
            ))
        unique = {
            (row.transaction, row.sender, row.recipient, row.token_amount): row
            for row in transfers
        }
        return list(unique.values())

    async def _alchemy_token_transfers(
        self, url: str, chain_id: int, token: str, earliest: int,
        latest: int | None = None,
    ) -> list[EvmTransfer]:
        page_key: str | None = None
        result: list[EvmTransfer] = []
        if latest is None:
            latest = earliest
        # A busy token produces thousands of transfers per minute, so a
        # descending scan from the chain head never reaches an older trade.
        # Ask for the trade's own block range instead and walk it forwards.
        from_block = await self._block_number_at(chain_id, earliest)
        to_block = (await self._block_number_at(chain_id, latest)
                    if from_block is not None else None)
        ascending = from_block is not None
        for _ in range(EVM_DISCOVERY_PAGES):
            params: dict[str, Any] = {
                "fromBlock": hex(from_block) if ascending else "0x0",
                "toBlock": hex(to_block) if to_block is not None else "latest",
                "contractAddresses": [token], "category": ["erc20"],
                "withMetadata": True, "excludeZeroValue": True,
                "maxCount": "0x3e8", "order": "asc" if ascending else "desc",
            }
            if page_key:
                params["pageKey"] = page_key
            response = await self.http.post(
                url,
                json={"jsonrpc": "2.0", "id": 1,
                      "method": "alchemy_getAssetTransfers", "params": [params]},
                timeout=25,
            )
            if int(getattr(response, "status_code", 200)) >= 400:
                break
            payload = response.json()
            rpc_result = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(rpc_result, dict) or payload.get("error"):
                break
            rows = rpc_result.get("transfers") or []
            timestamps: list[int] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                created_at = _iso_unix(metadata.get("blockTimestamp"))
                raw = row.get("rawContract") if isinstance(row.get("rawContract"), dict) else {}
                row_token = str(raw.get("address") or token).lower()
                amount = _decimal(row.get("value"))
                if amount is None:
                    raw_value = str(raw.get("value") or "")
                    try:
                        decimals = int(str(raw.get("decimal") or "0x12"), 16)
                        amount = Decimal(int(raw_value, 16)) / (Decimal(10) ** decimals)
                    except (InvalidOperation, TypeError, ValueError):
                        amount = None
                transaction = str(row.get("hash") or "")
                if (created_at is None or amount is None or amount <= 0 or not transaction
                        or row_token != token):
                    continue
                timestamps.append(created_at)
                result.append(EvmTransfer(
                    token, chain_id, transaction, _address(row.get("from")),
                    _address(row.get("to")), created_at, amount,
                ))
            page_key = str(rpc_result.get("pageKey") or "") or None
            if not page_key:
                break
            if not timestamps:
                continue
            if ascending and max(timestamps) > latest + 1200:
                break
            if not ascending and min(timestamps) < earliest - 1200:
                break
        return result

    async def _chain_rpc(self, chain_id: int, method: str, params: list[Any]) -> Any:
        """First configured endpoint for a chain that answers, else ``None``."""
        for url in self.rpcs.get(CHAIN_NAMES.get(chain_id, ""), []):
            try:
                response = await self.http.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params},
                    timeout=20,
                )
                if int(getattr(response, "status_code", 200)) >= 400:
                    continue
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("error"):
                    continue
                return payload.get("result")
            except Exception as exc:
                log.debug("%s failed via %s: %s", method, rpc_display_name(url), exc)
        return None

    async def _block_time(self, chain_id: int, number: int) -> int | None:
        cache = self._block_times.setdefault(chain_id, {})
        if number in cache:
            return cache[number]
        block = await self._chain_rpc(
            chain_id, "eth_getBlockByNumber", [hex(number), False]
        )
        if not isinstance(block, dict):
            return None
        try:
            timestamp = int(str(block.get("timestamp")), 16)
        except (TypeError, ValueError):
            return None
        cache[number] = timestamp
        return timestamp

    async def _head_block(self, chain_id: int) -> tuple[int, int] | None:
        cached = self._head_blocks.get(chain_id)
        if cached and time.time() - cached[2] < 30:
            return cached[0], cached[1]
        try:
            number = int(str(await self._chain_rpc(chain_id, "eth_blockNumber", [])), 16)
        except (TypeError, ValueError):
            return None
        timestamp = await self._block_time(chain_id, number)
        if timestamp is None:
            return None
        self._head_blocks[chain_id] = (number, timestamp, time.time())
        return number, timestamp

    async def _block_number_at(self, chain_id: int, when: int) -> int | None:
        """Highest block at or before ``when``, or ``None`` if unavailable.

        Block production is near linear in time, so interpolating between two
        known samples converges in a few probes. Samples are cached per chain
        and shared by every later lookup, and any RPC gap simply returns
        ``None`` so the caller falls back to an unbounded scan.
        """
        head = await self._head_block(chain_id)
        if head is None:
            return None
        high, high_time = head
        if when >= high_time:
            return high
        # Assuming blocks four times faster than the hint gives a lower bound
        # that is safely at or before the target.
        hint = BLOCK_TIME_HINTS.get(chain_id, 2.0)
        low = max(1, high - int((high_time - when) / (hint / 4)))
        low_time = await self._block_time(chain_id, low)
        if low_time is not None and low_time > when:
            low, low_time = 1, None
        for _ in range(BLOCK_SEARCH_PROBES):
            if high - low <= 1:
                break
            if low_time is not None and high_time > low_time:
                reach = (when - low_time) / (high_time - low_time)
                probe = min(max(low + int((high - low) * reach), low + 1), high - 1)
            else:
                probe = (low + high) // 2
            probe_time = await self._block_time(chain_id, probe)
            if probe_time is None:
                break
            if probe_time <= when:
                low, low_time = probe, probe_time
                if when - probe_time <= hint * 2:
                    break
            else:
                high, high_time = probe, probe_time
        return low

    async def _blockscout_token_transfers(
        self, chain_id: int, token: str, earliest: int
    ) -> list[EvmTransfer]:
        base = BLOCKSCOUT.get(chain_id)
        if not base:
            return []
        params: dict[str, Any] = {}
        result: list[EvmTransfer] = []
        for _ in range(EVM_DISCOVERY_PAGES):
            response = await self.http.get(
                f"{base}/api/v2/tokens/{token}/transfers",
                params=params, headers={"Accept": "application/json"}, timeout=20,
            )
            if int(getattr(response, "status_code", 200)) >= 400:
                break
            payload = response.json()
            rows = payload.get("items") if isinstance(payload, dict) else []
            timestamps: list[int] = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                created_at = _iso_unix(row.get("timestamp"))
                total = row.get("total") if isinstance(row.get("total"), dict) else {}
                amount = _decimal(total.get("value"))
                try:
                    decimals = int(total.get("decimals") or 18)
                except (TypeError, ValueError):
                    decimals = 18
                if amount is not None:
                    amount /= Decimal(10) ** decimals
                transaction = str(row.get("transaction_hash") or "")
                if created_at is None or amount is None or amount <= 0 or not transaction:
                    continue
                timestamps.append(created_at)
                result.append(EvmTransfer(
                    token, chain_id, transaction, _address(row.get("from")),
                    _address(row.get("to")), created_at, amount,
                ))
            next_page = payload.get("next_page_params") if isinstance(payload, dict) else None
            if (not isinstance(next_page, dict) or not next_page
                    or (timestamps and min(timestamps) < earliest - 1200)):
                break
            params = next_page
        return result

    async def _transaction_quote_values(
        self, chain_id: int, transaction: str
    ) -> tuple[Decimal, ...]:
        key = (chain_id, transaction.lower())
        if key in self._quote_cache:
            return self._quote_cache[key]
        values = await self._blockscout_quote_values(chain_id, transaction)
        if not values:
            values = await self._rpc_quote_values(chain_id, transaction)
        output = tuple(value for value in values if value > 0)
        self._quote_cache[key] = output
        return output

    async def _blockscout_quote_values(
        self, chain_id: int, transaction: str
    ) -> list[Decimal]:
        base = BLOCKSCOUT.get(chain_id)
        if not base:
            return []
        try:
            response = await self.http.get(
                f"{base}/api/v2/transactions/{transaction}/token-transfers",
                headers={"Accept": "application/json"}, timeout=20,
            )
            if int(getattr(response, "status_code", 200)) >= 400:
                return []
            payload = response.json()
        except Exception:
            return []
        values: list[Decimal] = []
        for row in (payload.get("items") if isinstance(payload, dict) else []) or []:
            if not isinstance(row, dict):
                continue
            token = row.get("token") if isinstance(row.get("token"), dict) else {}
            if str(token.get("symbol") or "").upper() not in STABLE_SYMBOLS:
                continue
            total = row.get("total") if isinstance(row.get("total"), dict) else {}
            amount = _decimal(total.get("value"))
            try:
                decimals = int(total.get("decimals") or token.get("decimals") or 18)
            except (TypeError, ValueError):
                decimals = 18
            if amount is None:
                continue
            rate = _decimal(token.get("exchange_rate")) or Decimal(1)
            values.append(amount / (Decimal(10) ** decimals) * rate)
        return values

    async def _rpc_quote_values(
        self, chain_id: int, transaction: str
    ) -> list[Decimal]:
        chain = CHAIN_NAMES[chain_id]
        for url in self.rpcs.get(chain, []):
            try:
                response = await self.http.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 2,
                          "method": "eth_getTransactionReceipt", "params": [transaction]},
                    timeout=20,
                )
                if int(getattr(response, "status_code", 200)) >= 400:
                    continue
                payload = response.json()
                receipt = payload.get("result") if isinstance(payload, dict) else None
                if not isinstance(receipt, dict):
                    continue
                values: list[Decimal] = []
                for row in receipt.get("logs") or []:
                    if not isinstance(row, dict):
                        continue
                    topics = row.get("topics") if isinstance(row.get("topics"), list) else []
                    token = str(row.get("address") or "").lower()
                    if (not topics or str(topics[0]).lower() != TRANSFER_TOPIC
                            or token not in STABLE_DECIMALS):
                        continue
                    try:
                        raw = int(str(row.get("data") or "0x0"), 16)
                    except ValueError:
                        continue
                    values.append(Decimal(raw) / (Decimal(10) ** STABLE_DECIMALS[token]))
                return values
            except Exception as exc:
                log.debug("receipt lookup failed via %s: %s", rpc_display_name(url), exc)
        return []

    async def _resolve_from_balances(self, handle: str, balances: Any) -> str | None:
        """Discover the smart wallet from exact token ownership."""
        for position in evm_balance_positions(balances)[:8]:
            try:
                holders = await self._holders(position)
                matching = {
                    address: indexed
                    for address, indexed in holders
                    if any(_same_balance(amount, indexed) for amount in position.amounts)
                }
                if len(matching) != 1:
                    continue
                address = next(iter(matching))
                verified = await self._verify_balance(position, address)
                if verified is None:
                    continue
                deployed, checked = await self._deployed_chains(address)
                if checked and not deployed:
                    continue
                if not deployed:
                    # An exact balance is strong ownership evidence, but FOMO's
                    # documented EVM account is a smart wallet. Require code so
                    # an unrelated EOA with the same rounded balance is rejected.
                    continue
                self._save(
                    handle, address, deployed, None, source="balance+rpc"
                )
                log.info(
                    "discovered EVM %s from exact %s balance on %s",
                    handle,
                    position.token,
                    CHAIN_NAMES[position.chain_id],
                )
                return address
            except Exception as exc:
                log.debug(
                    "EVM balance discovery failed for %s on %s: %s",
                    handle,
                    CHAIN_NAMES.get(position.chain_id, position.chain_id),
                    exc,
                )
        return None

    async def _holders(
        self, position: EvmBalancePosition
    ) -> list[tuple[str, Decimal]]:
        platform = CMC_PLATFORMS.get(position.chain_id)
        if platform:
            response = await self.http.post(
                CMC_HOLDERS_URL,
                json={
                    "tokenAddress": position.token,
                    "platform": platform,
                    "tag": "tag_all",
                },
                headers={"Accept": "application/json"},
                timeout=20,
            )
            if int(getattr(response, "status_code", 200)) >= 400:
                return []
            raw = response.json()
            data = raw.get("data") if isinstance(raw, dict) else None
            rows = data.get("holders") if isinstance(data, dict) else []
            result: list[tuple[str, Decimal]] = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                address = str(row.get("walletAddress") or "").strip().lower()
                amount = _decimal(row.get("balance"))
                if EVM_RE.fullmatch(address) and amount is not None:
                    result.append((address, amount))
            return result

        base = BLOCKSCOUT.get(position.chain_id)
        if not base:
            return []
        params: dict[str, Any] = {}
        result = []
        for _ in range(5):
            response = await self.http.get(
                f"{base}/api/v2/tokens/{position.token}/holders",
                params=params,
                headers={"Accept": "application/json"},
                timeout=20,
            )
            if int(getattr(response, "status_code", 200)) >= 400:
                break
            raw = response.json()
            rows = raw.get("items") if isinstance(raw, dict) else []
            token = raw.get("token") if isinstance(raw, dict) else None
            try:
                decimals = int((token or {}).get("decimals") or 18)
            except (TypeError, ValueError):
                decimals = 18
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                holder = row.get("address") or row.get("address_hash")
                if isinstance(holder, dict):
                    holder = holder.get("hash")
                address = str(holder or "").strip().lower()
                raw_amount = _decimal(row.get("value"))
                if EVM_RE.fullmatch(address) and raw_amount is not None:
                    result.append(
                        (address, raw_amount / (Decimal(10) ** decimals))
                    )
            next_page = raw.get("next_page_params") if isinstance(raw, dict) else None
            if not isinstance(next_page, dict) or not next_page:
                break
            params = next_page
        return result

    async def _verify_balance(
        self, position: EvmBalancePosition, address: str
    ) -> Decimal | None:
        chain = CHAIN_NAMES[position.chain_id]
        urls = self.rpcs.get(chain, [])
        balance_data = "0x70a08231" + address[2:].rjust(64, "0")
        for url in urls:
            try:
                decimals_response, balance_response = await asyncio.gather(
                    self.http.post(
                        url,
                        json={
                            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                            "params": [{"to": position.token, "data": "0x313ce567"}, "latest"],
                        },
                        timeout=20,
                    ),
                    self.http.post(
                        url,
                        json={
                            "jsonrpc": "2.0", "id": 2, "method": "eth_call",
                            "params": [{"to": position.token, "data": balance_data}, "latest"],
                        },
                        timeout=20,
                    ),
                )
                if (int(getattr(decimals_response, "status_code", 200)) >= 400
                        or int(getattr(balance_response, "status_code", 200)) >= 400):
                    continue
                decimals_payload = decimals_response.json()
                balance_payload = balance_response.json()
                if (not isinstance(decimals_payload, dict) or decimals_payload.get("error")
                        or not isinstance(balance_payload, dict) or balance_payload.get("error")):
                    continue
                decimals = int(str(decimals_payload.get("result") or "0x12"), 16)
                raw_balance = int(str(balance_payload.get("result") or "0x0"), 16)
                actual = Decimal(raw_balance) / (Decimal(10) ** decimals)
                if any(_same_balance(amount, actual) for amount in position.amounts):
                    return actual
            except Exception as exc:
                log.debug("balanceOf failed via %s: %s", rpc_display_name(url), exc)
        return None

    async def verify_and_cache(self, handle: str, address: str) -> str | None:
        """Validate a user-supplied mapping on-chain and cache it.

        Contract deployment proves that the address is a live smart wallet, not
        that it belongs to ``handle``; the caller is therefore responsible for
        the handle/address association.
        """
        handle = (handle or "").lstrip("@").strip().lower()
        address = (address or "").strip().lower()
        if not handle or not EVM_RE.fullmatch(address):
            return None

        try:
            deployed, checked = await self._deployed_chains(address)
        except Exception as exc:
            log.warning("manual EVM wallet verification failed for %s: %s", handle, exc)
            return None

        # Unlike an indexed result, a manual mapping has no second source of
        # validation. At least one reachable chain must contain contract code.
        if not checked or not deployed:
            log.warning("manual EVM wallet for %s was not deployed on checked chains", handle)
            return None

        self._save(handle, address, deployed, None, source="manual+rpc")
        log.info("cached manual EVM %s -> %s (%s)", handle, address, ", ".join(deployed))
        return address

    async def _deployed_chains(self, address: str) -> tuple[list[str], list[str]]:
        async def probe(name: str, urls: list[str]) -> tuple[str, bool]:
            error: Exception | None = None
            for url in urls:
                try:
                    response = await self.http.post(
                        url,
                        json={"jsonrpc": "2.0", "id": 1, "method": "eth_getCode",
                              "params": [address, "latest"]},
                        timeout=20,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or payload.get("error"):
                        detail = payload.get("error") if isinstance(payload, dict) else payload
                        raise RuntimeError(f"{name} eth_getCode: {detail}")
                    code = payload.get("result")
                    return name, isinstance(code, str) and code not in ("", "0x", "0x0")
                except Exception as exc:
                    error = exc
                    log.debug("%s RPC probe failed via %s: %s",
                              name, rpc_display_name(url), exc)
            raise RuntimeError(f"{name} RPCs failed: {error}")

        results = await asyncio.gather(
            *(probe(name, urls) for name, urls in self.rpcs.items()),
            return_exceptions=True,
        )
        deployed: list[str] = []
        checked: list[str] = []
        for result in results:
            if isinstance(result, Exception):
                log.debug("EVM RPC probe failed: %s", result)
                continue
            name, has_code = result
            checked.append(name)
            if has_code:
                deployed.append(name)
        return deployed, checked

    def _save(
        self,
        handle: str,
        address: str,
        chains: list[str],
        verified_at: str | None,
        source: str,
        confirmations: int | None = None,
        evidence_tokens: list[str] | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "evmWallet": address,
            "evmStatus": "verified",
            "evmChains": chains,
            "evmSource": source,
            "evmVerifiedAt": verified_at,
            "evmResolvedAt": int(time.time()),
        }
        if confirmations is not None:
            fields["evmConfirmed"] = confirmations
        if evidence_tokens:
            fields["evmEvidenceTokens"] = evidence_tokens
        # Use the configured path in tests/custom deployments; the default path
        # shares fomo_wallet's helpers and preserves its Solana entry.
        if self.cache_path == Path(CACHE):
            cache = _load_cache()
            entry = cache.get(handle)
            if not isinstance(entry, dict):
                entry = {}
            entry.update(fields)
            cache[handle] = entry
            _save_cache(cache)
            return

        import json

        try:
            cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        entry = cache.get(handle)
        if not isinstance(entry, dict):
            entry = {}
        entry.update(fields)
        cache[handle] = entry
        self.cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
