"""Discover Pump.fun EVM wallets from public portfolio balance fingerprints."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pump_api import CLIENT_URL, HEADERS, PumpUser
from rpc_config import env_rpc_urls, normalize_rpc_urls, rpc_display_name


log = logging.getLogger("pump.evm")


CMC_HOLDERS_URL = (
    "https://pro-api.coinmarketcap.com/public-api/v1/dex/holders/list"
)
EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
CMC_PLATFORMS = {1: "ethereum", 56: "bsc", 8453: "base"}
BLOCKSCOUT = {
    4663: "https://robinhoodchain.blockscout.com",
}
EVM_RPCS = {
    1: env_rpc_urls("ETH_RPC", "ETH_RPC_FALLBACKS"),
    56: env_rpc_urls(
        "BSC_RPC", "BSC_RPC_FALLBACKS", "https://bsc-dataseed.bnbchain.org"
    ),
    8453: env_rpc_urls("BASE_RPC", "BASE_RPC_FALLBACKS", "https://mainnet.base.org"),
    4663: env_rpc_urls(
        "ROBINHOOD_RPC",
        "ROBINHOOD_RPC_FALLBACKS",
        "https://rpc.mainnet.chain.robinhood.com",
    ),
}


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _same_balance(left: Decimal, right: Decimal) -> bool:
    # Pump serializes amountHeld as a JSON number, while holder indexes retain
    # all 18 decimals. This tolerance only covers JSON floating-point rounding.
    tolerance = max(abs(left) * Decimal("0.000000001"), Decimal("0.000001"))
    return abs(left - right) <= tolerance


@dataclass(frozen=True)
class PumpEvmMatch:
    solana: str
    handle: str
    evm: str
    chain_id: int
    token: str
    balance: str
    discovered_at: str
    verified_onchain: bool = False


@dataclass(frozen=True)
class _Position:
    token: str
    chain_id: int
    amount: Decimal
    value_usd: float
    has_transfers: bool
    has_callout: bool

    @classmethod
    def from_raw(cls, raw: Any) -> "_Position | None":
        if not isinstance(raw, dict):
            return None
        token = str(raw.get("coinMint") or "").strip().lower()
        amount = _decimal(raw.get("amountHeld"))
        try:
            chain_id = int(raw.get("chainId"))
            value_usd = float(raw.get("valueUsd") or 0)
        except (TypeError, ValueError):
            return None
        if not EVM_RE.fullmatch(token) or amount is None or amount <= 0:
            return None
        if chain_id not in CMC_PLATFORMS and chain_id not in BLOCKSCOUT:
            return None
        return cls(
            token=token,
            chain_id=chain_id,
            amount=amount,
            value_usd=value_usd,
            has_transfers=bool(raw.get("hasTransfers")),
            has_callout=isinstance(raw.get("callout"), dict),
        )


class PumpEvmResolver:
    """Resolve and cache the separate EVM account used by a Pump profile."""

    def __init__(
        self,
        http: Any,
        cache_file: Path,
        rpcs: dict[int, str | list[str]] | None = None,
    ) -> None:
        self.http = http
        self.cache_file = cache_file
        configured = EVM_RPCS if rpcs is None else rpcs
        self.rpcs: dict[int, list[str]] = {}
        for chain_id, urls in configured.items():
            normalized = normalize_rpc_urls(urls)
            if normalized:
                self.rpcs[chain_id] = normalized
        self._matches: dict[str, PumpEvmMatch] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        rows = raw.get("matches") if isinstance(raw, dict) else None
        if not isinstance(rows, dict):
            return
        for solana, row in rows.items():
            try:
                match = PumpEvmMatch(**row)
            except (TypeError, ValueError):
                continue
            if EVM_RE.fullmatch(match.evm):
                self._matches[str(solana)] = match

    def _save(self) -> None:
        payload = {
            "version": 1,
            "matches": {
                solana: asdict(match)
                for solana, match in sorted(self._matches.items())
            },
        }
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_file.with_suffix(self.cache_file.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.cache_file)

    def cached(self, wallet: str) -> PumpEvmMatch | None:
        clean = wallet.strip().lower()
        if EVM_RE.fullmatch(clean):
            return next(
                (match for match in self._matches.values() if match.evm.lower() == clean),
                None,
            )
        return self._matches.get(wallet.strip())

    async def _positions(self, solana: str) -> list[_Position]:
        response = await self.http.get(
            f"{CLIENT_URL}/user-portfolio/{quote(solana, safe='')}",
            params={
                "filter": "open",
                "page": 0,
                "pageSize": 100,
                "sortBy": "POSITION_SIZE",
            },
            headers=HEADERS,
        )
        if int(getattr(response, "status_code", 200)) >= 400:
            return []
        raw = response.json()
        rows = raw.get("positions") if isinstance(raw, dict) else []
        positions = [_Position.from_raw(row) for row in (rows or [])]
        usable = [position for position in positions if position is not None]
        # Stable balances and authored callouts are stronger fingerprints. USD
        # value is a useful proxy for appearing in a top-holder index.
        usable.sort(
            key=lambda item: (
                item.has_transfers,
                not item.has_callout,
                -item.value_usd,
            )
        )
        return usable

    async def _cmc_holders(self, position: _Position) -> list[tuple[str, Decimal]]:
        platform = CMC_PLATFORMS.get(position.chain_id)
        if not platform:
            return []
        response = await self.http.post(
            CMC_HOLDERS_URL,
            json={
                "tokenAddress": position.token,
                "platform": platform,
                "tag": "tag_all",
            },
            headers={"Accept": "application/json"},
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
            balance = _decimal(row.get("balance"))
            if EVM_RE.fullmatch(address) and balance is not None:
                result.append((address, balance))
        return result

    async def _blockscout_holders(
        self, position: _Position, *, max_pages: int = 5
    ) -> list[tuple[str, Decimal]]:
        base = BLOCKSCOUT.get(position.chain_id)
        if not base:
            return []
        params: dict[str, Any] = {}
        result: list[tuple[str, Decimal]] = []
        for _ in range(max_pages):
            response = await self.http.get(
                f"{base}/api/v2/tokens/{position.token}/holders",
                params=params,
                headers={"Accept": "application/json"},
            )
            if int(getattr(response, "status_code", 200)) >= 400:
                break
            raw = response.json()
            rows = raw.get("items") if isinstance(raw, dict) else []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                holder = row.get("address") or row.get("address_hash")
                if isinstance(holder, dict):
                    holder = holder.get("hash")
                address = str(holder or "").strip().lower()
                raw_value = _decimal(row.get("value"))
                if not EVM_RE.fullmatch(address) or raw_value is None:
                    continue
                # Blockscout holder values are integer token units.
                decimals = int((raw.get("token") or {}).get("decimals") or 18)
                result.append((address, raw_value / (Decimal(10) ** decimals)))
            next_page = raw.get("next_page_params") if isinstance(raw, dict) else None
            if not isinstance(next_page, dict) or not next_page:
                break
            params = next_page
        return result

    async def _verify_balance(
        self, position: _Position, address: str
    ) -> tuple[bool, Decimal | None]:
        urls = self.rpcs.get(position.chain_id, [])
        if not urls:
            return False, None
        balance_data = "0x70a08231" + address[2:].rjust(64, "0")
        for url in urls:
            try:
                decimals_response = await self.http.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_call",
                        "params": [{"to": position.token, "data": "0x313ce567"}, "latest"],
                    },
                    timeout=20,
                )
                balance_response = await self.http.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "eth_call",
                        "params": [{"to": position.token, "data": balance_data}, "latest"],
                    },
                    timeout=20,
                )
                if (int(getattr(decimals_response, "status_code", 200)) >= 400
                        or int(getattr(balance_response, "status_code", 200)) >= 400):
                    raise RuntimeError("RPC returned an HTTP error")
                decimals_payload = decimals_response.json()
                balance_payload = balance_response.json()
                if not isinstance(decimals_payload, dict) or decimals_payload.get("error"):
                    raise RuntimeError("decimals eth_call failed")
                if not isinstance(balance_payload, dict) or balance_payload.get("error"):
                    raise RuntimeError("balanceOf eth_call failed")
                decimals = int(str(decimals_payload.get("result") or "0x12"), 16)
                raw_balance = int(str(balance_payload.get("result") or "0x0"), 16)
                balance = Decimal(raw_balance) / (Decimal(10) ** decimals)
                if _same_balance(position.amount, balance):
                    return True, balance
            except Exception as exc:
                # Endpoint labels are intentionally secret-safe; API keys live
                # in URL paths on several providers.
                log.debug("balance verification failed via %s: %s",
                          rpc_display_name(url), exc)
                continue
        return False, None

    async def resolve(self, user: PumpUser, *, fresh: bool = False) -> PumpEvmMatch | None:
        if not fresh:
            cached = self._matches.get(user.address)
            if cached and cached.verified_onchain:
                return cached
        try:
            positions = await self._positions(user.address)
        except Exception:
            return None
        for position in positions[:8]:
            try:
                holders = (
                    await self._cmc_holders(position)
                    if position.chain_id in CMC_PLATFORMS
                    else await self._blockscout_holders(position)
                )
            except Exception:
                continue
            matches = [
                (address, balance)
                for address, balance in holders
                if _same_balance(position.amount, balance)
            ]
            if len(matches) != 1:
                continue
            address, indexed_balance = matches[0]
            verified, onchain_balance = await self._verify_balance(position, address)
            if not verified or onchain_balance is None:
                continue
            match = PumpEvmMatch(
                solana=user.address,
                handle=user.username,
                evm=address,
                chain_id=position.chain_id,
                token=position.token,
                balance=str(onchain_balance or indexed_balance),
                discovered_at=datetime.now(timezone.utc).isoformat(),
                verified_onchain=True,
            )
            self._matches[user.address] = match
            try:
                self._save()
            except OSError:
                pass
            return match
        return None
