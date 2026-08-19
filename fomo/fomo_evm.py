"""Resolve a FOMO handle to its verified EVM smart-contract wallet.

FOMO's public ``evmAddress`` user field is not the trading wallet.  For the
known Konito sample it has no code and no nonce on Base or BNB Chain.  The
public FomoScan identity index returns a different address marked ``verified``;
that address is a deployed ERC-4337 smart wallet on both chains and its paired
Solana address matches the wallet independently proved by ``fomo_wallet.py``.

Automatic resolution accepts only FomoScan's verified EVM result, then checks
its deployment against official public Base/BSC RPCs. An explicit manual
mapping can also be deployment-checked and cached for profiles absent from the
index. Results share the existing wallet cache under a separate ``evmWallet``
key.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fomo_wallet import CACHE, _load_cache, _save_cache
from rpc_config import (
    env_rpc_urls,
    normalize_rpc_urls,
    rpc_display_name,
    unique_urls,
)

log = logging.getLogger("fomo.evm")

FOMOSCAN_URL = os.getenv(
    "FOMOSCAN_PUBLIC_URL", "https://api-production-9541.up.railway.app"
).rstrip("/")
FOMOSCAN_URLS = unique_urls([
    FOMOSCAN_URL,
    *os.getenv("FOMOSCAN_FALLBACK_URLS", "").split(","),
])
try:
    FOMOSCAN_COOLDOWN = max(
        1.0, float(os.getenv("FOMOSCAN_COOLDOWN_SECONDS", "900"))
    )
except ValueError:
    FOMOSCAN_COOLDOWN = 900.0
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
BLOCKSCOUT = {4663: "https://robinhoodchain.blockscout.com"}


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _same_balance(left: Decimal, right: Decimal) -> bool:
    tolerance = max(abs(left) * Decimal("0.000000001"), Decimal("0.000001"))
    return abs(left - right) <= tolerance


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


class FomoIndexUnavailable(RuntimeError):
    def __init__(self, message: str, *, report: bool) -> None:
        super().__init__(message)
        self.report = report


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
    """Handle -> verified EVM smart wallet, cached permanently.

    Failures return ``None`` so EVM enrichment never breaks the profile embed.
    Empty results are not cached because the public identity index may verify a
    trader later.
    """

    def __init__(
        self,
        http: Any,
        index_url: str | list[str] = FOMOSCAN_URLS,
        rpcs: dict[str, str | list[str]] | None = None,
        cache_path: str | Path = CACHE,
        index_retry_delays: tuple[float, ...] = (0.0,),
        index_cooldown: float = FOMOSCAN_COOLDOWN,
    ) -> None:
        self.http = http
        self.index_urls = [url.rstrip("/") for url in normalize_rpc_urls(index_url)]
        if not self.index_urls:
            raise ValueError("at least one FomoScan index URL is required")
        self.index_url = self.index_urls[0]
        self.index_retry_delays = index_retry_delays or (0.0,)
        self.index_cooldown = max(1.0, index_cooldown)
        self._index_open_until = 0.0
        configured = EVM_RPCS if rpcs is None else rpcs
        self.rpcs: dict[str, list[str]] = {}
        for name, urls in configured.items():
            normalized = normalize_rpc_urls(urls)
            if normalized:
                self.rpcs[name] = normalized
        self.cache_path = Path(cache_path)
        self._locks: dict[str, asyncio.Lock] = {}

    async def resolve(
        self, user: Any, use_cache: bool = True, balances: Any = None
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
                if balances is not None:
                    discovered = await self._resolve_from_balances(handle, balances)
                    if discovered:
                        return discovered
                return await self._resolve(handle)
            except FomoIndexUnavailable as exc:
                if exc.report:
                    log.info(
                        "EVM identity index unavailable; using cached wallets for %.0fs",
                        self.index_cooldown,
                    )
                return None
            except Exception as exc:
                log.warning("EVM wallet resolution failed for %s: %s", handle, exc)
                return None

    async def _resolve_from_balances(self, handle: str, balances: Any) -> str | None:
        """Discover the smart wallet without FomoScan from exact token ownership."""
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

        This is an explicit escape hatch for a verified wallet missing from the
        public identity index.  Contract deployment proves that the address is
        a live smart wallet, not that it belongs to ``handle``; the caller is
        therefore responsible for the handle/address association.
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

    async def _resolve(self, handle: str) -> str | None:
        response = await self._index_get(f"/get-user/{quote(handle, safe='')}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        user = payload.get("user") if isinstance(payload, dict) else None
        wallets = (user or {}).get("wallets") if isinstance(user, dict) else None
        evm = (wallets or {}).get("evm") if isinstance(wallets, dict) else None
        address = (evm or {}).get("address") if isinstance(evm, dict) else None
        status = str((evm or {}).get("status") or "").lower()
        if status != "verified" or not isinstance(address, str) or not EVM_RE.fullmatch(address):
            return None
        address = address.lower()

        deployed, checked = await self._deployed_chains(address)
        # If reachable chains unanimously say this is an unused address, reject
        # it. If every public RPC is temporarily unavailable, retain the
        # identity index's explicit verified result rather than hiding it.
        if checked and not deployed:
            log.warning("verified index address for %s has no EVM code on %s",
                        handle, ", ".join(checked))
            return None

        self._save(handle, address, deployed, (evm or {}).get("verifiedAt"),
                   source="fomoscan")
        log.info("resolved EVM %s -> %s (%s)", handle, address,
                 ", ".join(deployed) or "index verified; RPC unavailable")
        return address

    async def _index_get(self, path: str) -> Any:
        now = time.monotonic()
        if now < self._index_open_until:
            raise FomoIndexUnavailable("circuit breaker open", report=False)

        last_error: Exception | None = None
        for attempt, delay in enumerate(self.index_retry_delays):
            if delay:
                await asyncio.sleep(delay)
            for base in self.index_urls:
                try:
                    response = await self.http.get(base + path, timeout=20)
                    status = int(getattr(response, "status_code", 200))
                    if status == 404:
                        self._index_open_until = 0.0
                        return response
                    if status in (429, 500, 502, 503, 504):
                        last_error = RuntimeError(
                            f"{rpc_display_name(base)} returned HTTP {status}"
                        )
                        continue
                    response.raise_for_status()
                    self._index_open_until = 0.0
                    return response
                except Exception as exc:
                    last_error = exc
                    log.debug("FomoScan request failed via %s (attempt %d): %s",
                              rpc_display_name(base), attempt + 1, exc)

        self._index_open_until = time.monotonic() + self.index_cooldown
        raise FomoIndexUnavailable(str(last_error or "all indexes failed"), report=True)

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

    def _save(self, handle: str, address: str, chains: list[str],
              verified_at: str | None, source: str) -> None:
        # Use the configured path in tests/custom deployments; the default path
        # shares fomo_wallet's helpers and preserves its Solana entry.
        if self.cache_path == Path(CACHE):
            cache = _load_cache()
            entry = cache.get(handle)
            if not isinstance(entry, dict):
                entry = {}
            entry.update({
                "evmWallet": address,
                "evmStatus": "verified",
                "evmChains": chains,
                "evmSource": source,
                "evmVerifiedAt": verified_at,
                "evmResolvedAt": int(time.time()),
            })
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
        entry.update({
            "evmWallet": address,
            "evmStatus": "verified",
            "evmChains": chains,
            "evmSource": source,
            "evmVerifiedAt": verified_at,
            "evmResolvedAt": int(time.time()),
        })
        cache[handle] = entry
        self.cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
