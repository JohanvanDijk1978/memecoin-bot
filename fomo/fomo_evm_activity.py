"""Read verified-wallet EVM buys and sells omitted by FOMO's activity feed."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from fomo_features import LatestActivity, STABLE_ADDRESSES, STABLE_SYMBOLS
from fomo_tracking import TrackEvent
from rpc_config import env_rpc_urls


EXPLORERS = {
    1: os.getenv("ETH_EXPLORER_API", "https://eth.blockscout.com/api/v2").rstrip("/"),
    8453: os.getenv("BASE_EXPLORER_API", "https://base.blockscout.com/api/v2").rstrip("/"),
    4663: os.getenv(
        "ROBINHOOD_EXPLORER_API", "https://robinhoodchain.blockscout.com/api/v2"
    ).rstrip("/"),
}
CHAIN_NAMES = {1: "Ethereum", 56: "BSC", 8453: "Base", 4663: "Robinhood"}
ALCHEMY_RPCS = {
    1: env_rpc_urls("ETH_RPC", "ETH_RPC_FALLBACKS"),
    56: env_rpc_urls("BSC_RPC", "BSC_RPC_FALLBACKS"),
    8453: env_rpc_urls("BASE_RPC", "BASE_RPC_FALLBACKS"),
    4663: env_rpc_urls("ROBINHOOD_RPC", "ROBINHOOD_RPC_FALLBACKS"),
}
STABLE_DECIMALS = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 6,
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,
    "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,
    "0x55d398326f99059ff775485246999027b3197955": 18,
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 18,
}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_CACHE: dict[
    str, tuple[float, tuple[LatestActivity, ...], tuple[TrackEvent, ...]]
] = {}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _address(value: Any) -> str:
    return str(value.get("hash") or "") if isinstance(value, dict) else ""


def _token_address(item: dict[str, Any]) -> str:
    token = item.get("token") if isinstance(item.get("token"), dict) else {}
    return str(token.get("address_hash") or token.get("address") or "")


def _human_amount(item: dict[str, Any]) -> float | None:
    total = item.get("total") if isinstance(item.get("total"), dict) else {}
    value = _number(total.get("value"))
    decimals = _number(total.get("decimals"))
    if value is None or decimals is None:
        return None
    return value / (10 ** int(decimals))


def _usd_value(item: dict[str, Any]) -> float | None:
    token = item.get("token") if isinstance(item.get("token"), dict) else {}
    symbol = str(token.get("symbol") or "").upper()
    amount = _human_amount(item)
    if amount is None:
        return None
    rate = _number(token.get("exchange_rate"))
    if symbol in STABLE_SYMBOLS:
        return amount * (rate if rate and rate > 0 else 1.0)
    return amount * rate if rate and rate > 0 else None


def _activity(
    candidate: dict[str, Any], transfers: Any, wallet: str, chain_id: int,
) -> LatestActivity | TrackEvent | None:
    rows = transfers.get("items") if isinstance(transfers, dict) else None
    if not isinstance(rows, list):
        return None
    wallet = wallet.lower()
    incoming = _address(candidate.get("to")).lower() == wallet
    outgoing = _address(candidate.get("from")).lower() == wallet
    if not incoming and not outgoing:
        return None
    token = candidate.get("token") if isinstance(candidate.get("token"), dict) else {}
    traded_address = _token_address(candidate).lower()
    quote_values: list[float] = []
    for row in rows:
        if not isinstance(row, dict) or _token_address(row).lower() == traded_address:
            continue
        row_token = row.get("token") if isinstance(row.get("token"), dict) else {}
        if str(row_token.get("symbol") or "").upper() not in STABLE_SYMBOLS:
            continue
        value = _usd_value(row)
        if value is not None and value > 0:
            quote_values.append(value)
    usd = max(quote_values, default=0.0)
    if usd <= 0:
        return None
    transaction = str(candidate.get("transaction_hash") or "")
    symbol = str(token.get("symbol") or "TOKEN")
    timestamp = candidate.get("timestamp")
    if outgoing:
        return TrackEvent(
            kind="sell",
            symbol=symbol,
            token_address=_token_address(candidate),
            network_id=chain_id,
            created_at=timestamp,
            usd_value=usd,
            provider="On-chain",
            detail="EVM swap",
        )

    output_amount = _human_amount(candidate)
    decimals = _number(token.get("decimals"))
    raw_supply = _number(token.get("total_supply"))
    supply = (
        raw_supply / (10 ** int(decimals))
        if raw_supply is not None and decimals is not None else None
    )
    market_cap = usd / output_amount * supply if output_amount and supply else None
    return LatestActivity(
        action="Bought",
        symbol=symbol,
        usd_value=usd,
        created_at=timestamp,
        activity_id=transaction,
        chain=CHAIN_NAMES[chain_id],
        token_address=_token_address(candidate),
        market_cap=market_cap,
        market_cap_estimated=False,
    )


async def _blockscout_activity(
    http: Any, wallet: str, chain_id: int, candidate_limit: int,
) -> tuple[list[LatestActivity], list[TrackEvent]]:
    base = EXPLORERS[chain_id]
    response = await http.get(
        f"{base}/addresses/{wallet}/token-transfers",
        params={"type": "ERC-20"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("items") if isinstance(payload, dict) else None
    candidates: list[dict[str, Any]] = []
    seen_transactions: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        token = row.get("token") if isinstance(row.get("token"), dict) else {}
        transaction = str(row.get("transaction_hash") or "")
        involves_wallet = wallet.lower() in {
            _address(row.get("from")).lower(), _address(row.get("to")).lower()
        }
        if (not involves_wallet or not transaction or transaction in seen_transactions
                or str(token.get("symbol") or "").upper() in STABLE_SYMBOLS
                or not _token_address(row)):
            continue
        seen_transactions.add(transaction)
        candidates.append(row)
        if len(candidates) >= candidate_limit:
            break

    async def detail(row: dict[str, Any]) -> LatestActivity | TrackEvent | None:
        response = await http.get(
            f"{base}/transactions/{row['transaction_hash']}/token-transfers",
            timeout=20,
        )
        response.raise_for_status()
        return _activity(row, response.json(), wallet, chain_id)

    results = await asyncio.gather(*(detail(row) for row in candidates),
                                   return_exceptions=True)
    buys = [item for item in results if isinstance(item, LatestActivity)]
    sells = [item for item in results if isinstance(item, TrackEvent)]
    return buys, sells


async def _alchemy_transfer_rows(http: Any, url: str, wallet: str) -> list[dict[str, Any]]:
    async def direction(field: str) -> list[dict[str, Any]]:
        response = await http.post(
            url,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "alchemy_getAssetTransfers",
                "params": [{
                    "fromBlock": "0x0", field: wallet,
                    "category": ["erc20"], "withMetadata": True,
                    "excludeZeroValue": True, "maxCount": "0x64", "order": "desc",
                }],
            },
            timeout=30,
        )
        if int(getattr(response, "status_code", 200)) >= 400:
            return []
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("error"):
            return []
        result = payload.get("result")
        rows = result.get("transfers") if isinstance(result, dict) else []
        return [row for row in (rows or []) if isinstance(row, dict)]

    incoming, outgoing = await asyncio.gather(
        direction("toAddress"), direction("fromAddress")
    )
    unique: dict[str, dict[str, Any]] = {}
    for row in incoming + outgoing:
        key = str(row.get("uniqueId") or (
            f"{row.get('hash')}:{row.get('from')}:{row.get('to')}:"
            f"{(row.get('rawContract') or {}).get('address')}:{row.get('value')}"
        ))
        unique[key] = row
    return list(unique.values())


def _alchemy_activities(
    rows: list[dict[str, Any]], wallet: str, chain_id: int,
) -> tuple[list[LatestActivity], list[TrackEvent]]:
    wallet = wallet.lower()
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        tx_hash = str(row.get("hash") or "")
        if tx_hash:
            by_hash.setdefault(tx_hash, []).append(row)
    buys: list[LatestActivity] = []
    sells: list[TrackEvent] = []
    for tx_hash, transfers in by_hash.items():
        inbound = [row for row in transfers if str(row.get("to") or "").lower() == wallet]
        outbound = [row for row in transfers if str(row.get("from") or "").lower() == wallet]
        inbound_stable = [row for row in inbound
                          if str(row.get("asset") or "").upper() in STABLE_SYMBOLS]
        outbound_stable = [row for row in outbound
                           if str(row.get("asset") or "").upper() in STABLE_SYMBOLS]
        inbound_tokens = [row for row in inbound
                          if str(row.get("asset") or "").upper() not in STABLE_SYMBOLS]
        outbound_tokens = [row for row in outbound
                           if str(row.get("asset") or "").upper() not in STABLE_SYMBOLS]
        buy_usd = max((_number(row.get("value")) or 0 for row in outbound_stable), default=0)
        sell_usd = max((_number(row.get("value")) or 0 for row in inbound_stable), default=0)
        for row in inbound_tokens if buy_usd > 0 else []:
            raw = row.get("rawContract") if isinstance(row.get("rawContract"), dict) else {}
            token = str(raw.get("address") or "")
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            if token:
                buys.append(LatestActivity(
                    action="Bought", symbol=str(row.get("asset") or "TOKEN"),
                    usd_value=buy_usd, created_at=metadata.get("blockTimestamp"),
                    activity_id=tx_hash, chain=CHAIN_NAMES[chain_id],
                    token_address=token,
                ))
        for row in outbound_tokens if sell_usd > 0 else []:
            raw = row.get("rawContract") if isinstance(row.get("rawContract"), dict) else {}
            token = str(raw.get("address") or "")
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            if token:
                sells.append(TrackEvent(
                    kind="sell", symbol=str(row.get("asset") or "TOKEN"),
                    token_address=token, network_id=chain_id,
                    created_at=metadata.get("blockTimestamp"), usd_value=sell_usd,
                    provider="On-chain", detail="EVM swap",
                ))
    return buys, sells


async def _alchemy_activity(
    http: Any, wallet: str, chain_id: int, candidate_limit: int,
) -> tuple[list[LatestActivity], list[TrackEvent]]:
    for url in ALCHEMY_RPCS.get(chain_id, []):
        if "alchemy.com" not in url.lower():
            continue
        try:
            rows = await _alchemy_transfer_rows(http, url, wallet)
            candidates: list[dict[str, Any]] = []
            seen: set[str] = set()
            for row in rows:
                tx_hash = str(row.get("hash") or "")
                token = str((row.get("rawContract") or {}).get("address") or "").lower()
                symbol = str(row.get("asset") or "").upper()
                involves_wallet = wallet.lower() in {
                    str(row.get("from") or "").lower(),
                    str(row.get("to") or "").lower(),
                }
                if (not involves_wallet or not tx_hash or tx_hash in seen or not token
                        or symbol in STABLE_SYMBOLS or token in STABLE_ADDRESSES):
                    continue
                seen.add(tx_hash)
                candidates.append(row)
                if len(candidates) >= candidate_limit:
                    break

            async def detail(row: dict[str, Any]) -> LatestActivity | TrackEvent | None:
                response = await http.post(
                    url,
                    json={
                        "jsonrpc": "2.0", "id": 2,
                        "method": "eth_getTransactionReceipt",
                        "params": [row["hash"]],
                    },
                    timeout=20,
                )
                if int(getattr(response, "status_code", 200)) >= 400:
                    return None
                payload = response.json()
                receipt = payload.get("result") if isinstance(payload, dict) else None
                logs = receipt.get("logs") if isinstance(receipt, dict) else []
                stable_values: list[float] = []
                for log_row in logs or []:
                    if not isinstance(log_row, dict):
                        continue
                    topics = log_row.get("topics") if isinstance(log_row.get("topics"), list) else []
                    stable = str(log_row.get("address") or "").lower()
                    if (not topics or str(topics[0]).lower() != TRANSFER_TOPIC
                            or stable not in STABLE_DECIMALS):
                        continue
                    try:
                        raw_value = int(str(log_row.get("data") or "0x0"), 16)
                    except ValueError:
                        continue
                    stable_values.append(raw_value / (10 ** STABLE_DECIMALS[stable]))
                usd = max(stable_values, default=0.0)
                if usd <= 0:
                    return None
                raw = row.get("rawContract") if isinstance(row.get("rawContract"), dict) else {}
                token = str(raw.get("address") or "")
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                incoming = str(row.get("to") or "").lower() == wallet.lower()
                if incoming:
                    return LatestActivity(
                        action="Bought", symbol=str(row.get("asset") or "TOKEN"),
                        usd_value=usd, created_at=metadata.get("blockTimestamp"),
                        activity_id=str(row.get("hash") or ""),
                        chain=CHAIN_NAMES[chain_id], token_address=token,
                    )
                return TrackEvent(
                    kind="sell", symbol=str(row.get("asset") or "TOKEN"),
                    token_address=token, network_id=chain_id,
                    created_at=metadata.get("blockTimestamp"), usd_value=usd,
                    provider="On-chain", detail="EVM swap",
                )

            results = await asyncio.gather(*(detail(row) for row in candidates),
                                           return_exceptions=True)
            return (
                [item for item in results if isinstance(item, LatestActivity)],
                [item for item in results if isinstance(item, TrackEvent)],
            )
        except Exception:
            continue
    return [], []


async def fetch_evm_activity(
    http: Any, wallet: str, limit: int = 3, candidate_limit: int = 15,
    cache_ttl: float = 60.0,
) -> tuple[tuple[LatestActivity, ...], tuple[TrackEvent, ...]]:
    """Return recent verified-wallet swaps from available EVM explorers."""
    if not wallet or not wallet.lower().startswith("0x"):
        return (), ()
    cache_key = wallet.lower()
    hit = _CACHE.get(cache_key)
    if hit and hit[0] > time.monotonic():
        return hit[1][:limit], hit[2][:limit]
    tasks = []
    for chain_id in CHAIN_NAMES:
        has_alchemy = any("alchemy.com" in url.lower()
                          for url in ALCHEMY_RPCS.get(chain_id, []))
        if has_alchemy and chain_id in (1, 56):
            tasks.append(_alchemy_activity(http, wallet, chain_id, candidate_limit))
        elif chain_id in EXPLORERS:
            tasks.append(_blockscout_activity(http, wallet, chain_id, candidate_limit))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    buys: list[LatestActivity] = []
    sells: list[TrackEvent] = []
    for result in results:
        if isinstance(result, tuple):
            buys.extend(result[0])
            sells.extend(result[1])
    unique_buys = {
        (item.chain, item.activity_id or "", item.token_address.lower()): item
        for item in buys
    }
    unique_sells = {
        (str(item.network_id), item.created_at or "", item.token_address.lower()): item
        for item in sells
    }
    buys = sorted(unique_buys.values(), key=lambda item: item.created_at or "", reverse=True)
    sells = sorted(unique_sells.values(), key=lambda item: item.created_at or "", reverse=True)
    output = (tuple(buys[:limit]), tuple(sells[:limit]))
    _CACHE[cache_key] = (time.monotonic() + cache_ttl, *output)
    return output


async def fetch_robinhood_buys(
    http: Any, wallet: str, limit: int = 3, candidate_limit: int = 12,
    cache_ttl: float = 60.0,
) -> tuple[LatestActivity, ...]:
    """Backward-compatible Robinhood-only buy helper."""
    if not wallet or not wallet.lower().startswith("0x"):
        return ()
    try:
        buys, _sells = await _blockscout_activity(http, wallet, 4663, candidate_limit)
    except Exception:
        return ()
    buys.sort(key=lambda item: item.created_at or "", reverse=True)
    return tuple(buys[:limit])
