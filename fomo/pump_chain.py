"""Read exact Pump/PumpSwap trades from Solana transaction event logs."""

from __future__ import annotations

import base64
import hashlib
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from rpc_config import normalize_rpc_urls, rpc_display_name

from pump_api import WSOL_MINT


PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class PumpRpcError(RuntimeError):
    pass


@dataclass(frozen=True)
class PumpChainTrade:
    id: str
    signature: str
    kind: str
    user: str
    mint: str
    created_at: str | None
    base_amount: int
    quote_amount: int
    quote_mint: str | None
    source: str


def _event_discriminator(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


TRADE_EVENT = _event_discriminator("TradeEvent")
AMM_BUY_EVENT = _event_discriminator("BuyEvent")
AMM_SELL_EVENT = _event_discriminator("SellEvent")


def b58encode(value: bytes) -> str:
    zeroes = len(value) - len(value.lstrip(b"\0"))
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _B58[remainder] + encoded
    return "1" * zeroes + encoded


class _Reader:
    def __init__(self, data: bytes, position: int = 0) -> None:
        self.data = data
        self.position = position

    @property
    def remaining(self) -> int:
        return len(self.data) - self.position

    def take(self, size: int) -> bytes:
        end = self.position + size
        if end > len(self.data):
            raise ValueError("truncated Pump event")
        value = self.data[self.position:end]
        self.position = end
        return value

    def u8(self) -> int:
        return self.take(1)[0]

    def bool(self) -> bool:
        return bool(self.u8())

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def pubkey(self) -> str:
        return b58encode(self.take(32))

    def string(self) -> str:
        return self.take(self.u32()).decode("utf-8", errors="replace")


def _iso_timestamp(value: Any) -> str | None:
    try:
        timestamp = int(value)
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _decode_bonding_trade(data: bytes) -> tuple[str, str, int, int, int, str | None]:
    reader = _Reader(data, 8)
    mint = reader.pubkey()
    sol_amount = reader.u64()
    token_amount = reader.u64()
    is_buy = reader.bool()
    user = reader.pubkey()
    timestamp = reader.i64()
    quote_mint: str | None = None
    quote_amount = sol_amount

    # These fields were appended over time.  Old events remain valid and fall
    # back to native SOL without losing the trade itself.
    try:
        for _ in range(4):
            reader.u64()
        reader.pubkey()
        reader.u64()
        reader.u64()
        reader.pubkey()
        reader.u64()
        reader.u64()
        reader.bool()
        reader.u64()
        reader.u64()
        reader.u64()
        reader.i64()
        reader.string()
        reader.bool()
        reader.u64()
        reader.u64()
        reader.u64()
        reader.u64()
        shareholder_count = reader.u32()
        if shareholder_count > 100:
            raise ValueError("invalid shareholder count")
        for _ in range(shareholder_count):
            reader.pubkey()
            reader.u16()
        quote_mint = reader.pubkey()
        quote_amount = reader.u64()
    except ValueError:
        quote_mint = WSOL_MINT
        quote_amount = sol_amount

    return ("buy" if is_buy else "sell", user, token_amount, quote_amount,
            timestamp, quote_mint)


def _decode_amm_trade(data: bytes, kind: str) -> tuple[str, str, int, int, int]:
    reader = _Reader(data, 8)
    timestamp = reader.i64()
    base_amount = reader.u64()
    reader.u64()  # max quote in / min quote out
    reader.u64()  # user base reserves
    reader.u64()  # user quote reserves
    reader.u64()  # pool base reserves
    reader.u64()  # pool quote reserves
    reader.u64()  # quote amount before the remaining fee presentation
    reader.u64()  # LP fee bps
    reader.u64()  # LP fee
    reader.u64()  # protocol fee bps
    reader.u64()  # protocol fee
    reader.u64()  # quote amount with/without LP fee
    user_quote_amount = reader.u64()
    reader.pubkey()  # pool
    user = reader.pubkey()
    return kind, user, base_amount, user_quote_amount, timestamp


def _raw_token_amount(row: dict[str, Any]) -> int:
    amount = row.get("uiTokenAmount")
    if isinstance(amount, dict):
        try:
            return int(amount.get("amount") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _wallet_token_deltas(transaction: dict[str, Any], wallet: str) -> dict[str, int]:
    meta = transaction.get("meta") if isinstance(transaction.get("meta"), dict) else {}
    pre_rows = meta.get("preTokenBalances") if isinstance(meta.get("preTokenBalances"), list) else []
    post_rows = meta.get("postTokenBalances") if isinstance(meta.get("postTokenBalances"), list) else []
    entries: dict[tuple[int, str], dict[str, Any]] = {}
    for side, rows in (("pre", pre_rows), ("post", post_rows)):
        for row in rows:
            if not isinstance(row, dict) or not row.get("mint"):
                continue
            try:
                index = int(row.get("accountIndex"))
            except (TypeError, ValueError):
                continue
            key = (index, str(row["mint"]))
            entry = entries.setdefault(key, {"owner": None, "pre": 0, "post": 0})
            if row.get("owner"):
                entry["owner"] = str(row["owner"])
            entry[side] = _raw_token_amount(row)
    deltas: dict[str, int] = {}
    for (_index, mint), entry in entries.items():
        if entry.get("owner") != wallet:
            continue
        deltas[mint] = deltas.get(mint, 0) + int(entry["post"]) - int(entry["pre"])
    return deltas


def _amm_base_mint(transaction: dict[str, Any], wallet: str, kind: str, amount: int) -> str:
    deltas = _wallet_token_deltas(transaction, wallet)
    direction = 1 if kind == "buy" else -1
    candidates = [(mint, delta) for mint, delta in deltas.items() if delta * direction > 0]
    if not candidates:
        return ""
    exact = [mint for mint, delta in candidates if abs(delta) == amount]
    if exact:
        return exact[0]
    return max(candidates, key=lambda item: abs(item[1]))[0]


def parse_pump_trades(transaction: Any, tracked_wallet: str) -> list[PumpChainTrade]:
    if not isinstance(transaction, dict):
        return []
    meta = transaction.get("meta") if isinstance(transaction.get("meta"), dict) else {}
    if meta.get("err") is not None:
        return []
    tx = transaction.get("transaction") if isinstance(transaction.get("transaction"), dict) else {}
    signatures = tx.get("signatures") if isinstance(tx.get("signatures"), list) else []
    signature = str(signatures[0]) if signatures else ""
    logs = meta.get("logMessages") if isinstance(meta.get("logMessages"), list) else []
    trades: list[PumpChainTrade] = []
    for log_index, line in enumerate(logs):
        if not isinstance(line, str) or not line.startswith("Program data: "):
            continue
        try:
            data = base64.b64decode(line[14:], validate=True)
        except (ValueError, base64.binascii.Error):
            continue
        if len(data) < 8:
            continue
        try:
            if data[:8] == TRADE_EVENT:
                kind, user, base_amount, quote_amount, timestamp, quote_mint = _decode_bonding_trade(data)
                mint = b58encode(data[8:40])
                source = "Pump"
            elif data[:8] in (AMM_BUY_EVENT, AMM_SELL_EVENT):
                kind = "buy" if data[:8] == AMM_BUY_EVENT else "sell"
                kind, user, base_amount, quote_amount, timestamp = _decode_amm_trade(data, kind)
                mint = _amm_base_mint(transaction, user, kind, base_amount)
                quote_mint = None
                source = "PumpSwap"
            else:
                continue
        except (ValueError, struct.error):
            continue
        if user != tracked_wallet or not mint:
            continue
        created_at = _iso_timestamp(timestamp) or _iso_timestamp(transaction.get("blockTime"))
        trades.append(PumpChainTrade(
            id=f"{signature}:{log_index}",
            signature=signature,
            kind=kind,
            user=user,
            mint=mint,
            created_at=created_at,
            base_amount=base_amount,
            quote_amount=quote_amount,
            quote_mint=quote_mint,
            source=source,
        ))
    return trades


class PumpChainClient:
    def __init__(self, http: Any, rpc_url: str | list[str]) -> None:
        self.http = http
        self.rpc_urls = normalize_rpc_urls(rpc_url)
        if not self.rpc_urls:
            raise ValueError("at least one Solana RPC URL is required")
        self.rpc_url = self.rpc_urls[0]
        self._request_id = 0
        self._failure_streak = 0
        self._cooldown_until = 0.0
        self._signature_cache: dict[
            tuple[str, str | None, int], tuple[float, list[dict[str, Any]]]
        ] = {}

    def _id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            raise PumpRpcError(f"Solana RPC cooling down for {remaining:.1f}s")
        request_id = self._id()
        error: Exception | None = None
        for url in self.rpc_urls:
            try:
                response = await self.http.post(
                    url,
                    json={"jsonrpc": "2.0", "id": request_id,
                          "method": method, "params": params},
                )
                status = int(getattr(response, "status_code", 200))
                if status >= 400:
                    raise PumpRpcError(f"HTTP {status}")
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("error"):
                    detail = payload.get("error") if isinstance(payload, dict) else payload
                    raise PumpRpcError(str(detail))
                self._failure_streak = 0
                self._cooldown_until = 0.0
                return payload.get("result")
            except Exception as exc:
                error = exc
                continue
        endpoints = ", ".join(rpc_display_name(url) for url in self.rpc_urls)
        self._failure_streak += 1
        cooldown = min(30.0, 2.0 ** min(self._failure_streak, 5))
        self._cooldown_until = time.monotonic() + cooldown
        raise PumpRpcError(f"Solana RPC {method} failed via {endpoints}: {error}")

    async def _signature_rows(
        self, wallet: str, options: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Share near-simultaneous reads for a wallet tracked in many channels."""
        before = str(options.get("before")) if options.get("before") else None
        limit = int(options.get("limit") or 100)
        key = (wallet, before, limit)
        cached = self._signature_cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < 0.9:
            return cached[1]
        rows = await self._rpc("getSignaturesForAddress", [wallet, options])
        result = [row for row in (rows or []) if isinstance(row, dict)]
        self._signature_cache[key] = (now, result)
        if len(self._signature_cache) > 250:
            self._signature_cache = {
                cache_key: value
                for cache_key, value in self._signature_cache.items()
                if now - value[0] < 5
            }
        return result

    async def recent_signature_ids(self, wallet: str, *, limit: int = 100) -> list[str]:
        rows = await self._signature_rows(
            wallet,
            {"limit": max(1, min(limit, 1000)), "commitment": "confirmed"},
        )
        return [str(row["signature"]) for row in (rows or [])
                if isinstance(row, dict) and row.get("signature")]

    async def signatures_since(
        self, wallet: str, known_ids: set[str], *, pages: int = 4
    ) -> list[str]:
        """Return unseen signatures newest-first, paging until a known cursor."""
        unseen: list[str] = []
        before: str | None = None
        reached_known = False
        for _ in range(max(1, pages)):
            options: dict[str, Any] = {"limit": 100, "commitment": "confirmed"}
            if before:
                options["before"] = before
            rows = await self._signature_rows(wallet, options)
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                if not isinstance(row, dict) or not row.get("signature"):
                    continue
                signature = str(row["signature"])
                if signature in known_ids:
                    reached_known = True
                    break
                unseen.append(signature)
            if reached_known or len(rows) < 100:
                break
            before = str(rows[-1].get("signature") or "")
            if not before:
                break
        return unseen

    async def transactions(self, signatures: list[str]) -> list[dict[str, Any]]:
        if not signatures:
            return []
        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            raise PumpRpcError(f"Solana RPC cooling down for {remaining:.1f}s")
        results: dict[str, dict[str, Any]] = {}
        for start in range(0, len(signatures), 20):
            chunk = signatures[start:start + 20]
            requests = []
            id_to_signature: dict[int, str] = {}
            for signature in chunk:
                request_id = self._id()
                id_to_signature[request_id] = signature
                requests.append({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "getTransaction",
                    "params": [signature, {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    }],
                })
            payload: Any = None
            error: Exception | None = None
            for url in self.rpc_urls:
                try:
                    response = await self.http.post(url, json=requests)
                    status = int(getattr(response, "status_code", 200))
                    if status >= 400:
                        raise PumpRpcError(f"HTTP {status}")
                    payload = response.json()
                    if isinstance(payload, dict) and payload.get("error"):
                        raise PumpRpcError(str(payload.get("error")))
                    if not isinstance(payload, (list, dict)):
                        raise PumpRpcError("invalid batch response")
                    self._failure_streak = 0
                    self._cooldown_until = 0.0
                    break
                except Exception as exc:
                    error = exc
                    payload = None
            if payload is None:
                endpoints = ", ".join(rpc_display_name(url) for url in self.rpc_urls)
                self._failure_streak += 1
                cooldown = min(30.0, 2.0 ** min(self._failure_streak, 5))
                self._cooldown_until = time.monotonic() + cooldown
                raise PumpRpcError(
                    f"Solana RPC getTransaction batch failed via {endpoints}: {error}"
                )
            rows = payload if isinstance(payload, list) else [payload]
            for row in rows:
                if not isinstance(row, dict) or row.get("error"):
                    continue
                signature = id_to_signature.get(int(row.get("id") or -1))
                result = row.get("result")
                if signature and isinstance(result, dict):
                    results[signature] = result
        # Signatures arrive newest-first.  Discord should receive alerts in
        # chronological order.
        return [results[signature] for signature in reversed(signatures) if signature in results]

    async def new_trades(
        self, wallet: str, known_ids: set[str]
    ) -> tuple[list[PumpChainTrade], list[str]]:
        signatures = await self.signatures_since(wallet, known_ids)
        transactions = await self.transactions(signatures)
        trades = [trade for transaction in transactions
                  for trade in parse_pump_trades(transaction, wallet)]
        return trades, signatures

    async def recent_trades(
        self, wallet: str, *, signature_limit: int = 40
    ) -> list[PumpChainTrade]:
        """Return recent decoded Pump/PumpSwap trades, newest first."""
        signatures = await self.recent_signature_ids(wallet, limit=signature_limit)
        transactions = await self.transactions(signatures)
        trades = [trade for transaction in transactions
                  for trade in parse_pump_trades(transaction, wallet)]
        return sorted(trades, key=lambda trade: trade.created_at or "", reverse=True)
