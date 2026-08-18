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
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fomo_wallet import CACHE, _load_cache, _save_cache

log = logging.getLogger("fomo.evm")

FOMOSCAN_URL = os.getenv(
    "FOMOSCAN_PUBLIC_URL", "https://api-production-9541.up.railway.app"
).rstrip("/")
EVM_RPCS = {
    "base": os.getenv("BASE_RPC", "https://mainnet.base.org"),
    "bsc": os.getenv("BSC_RPC", "https://bsc-dataseed.bnbchain.org"),
}
EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


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
        index_url: str = FOMOSCAN_URL,
        rpcs: dict[str, str] | None = None,
        cache_path: str | Path = CACHE,
    ) -> None:
        self.http = http
        self.index_url = index_url.rstrip("/")
        self.rpcs = dict(EVM_RPCS if rpcs is None else rpcs)
        self.cache_path = Path(cache_path)
        self._locks: dict[str, asyncio.Lock] = {}

    async def resolve(self, user: Any, use_cache: bool = True) -> str | None:
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
                return await self._resolve(handle)
            except Exception as exc:
                log.warning("EVM wallet resolution failed for %s: %s", handle, exc)
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
        response = await self.http.get(
            f"{self.index_url}/get-user/{quote(handle, safe='')}", timeout=20
        )
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

    async def _deployed_chains(self, address: str) -> tuple[list[str], list[str]]:
        async def probe(name: str, url: str) -> tuple[str, bool]:
            response = await self.http.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_getCode",
                      "params": [address, "latest"]},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("error"):
                raise RuntimeError(f"{name} eth_getCode: {payload.get('error')}")
            code = payload.get("result")
            return name, isinstance(code, str) and code not in ("", "0x", "0x0")

        results = await asyncio.gather(
            *(probe(name, url) for name, url in self.rpcs.items()),
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
