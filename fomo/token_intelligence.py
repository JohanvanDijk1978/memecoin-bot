"""Cross-chain token metadata and top-holder intelligence."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from rpc_config import normalize_rpc_urls, rpc_display_name


log = logging.getLogger("token.intelligence")

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens"
CMC_HOLDERS_URL = "https://pro-api.coinmarketcap.com/public-api/v1/dex/holders/list"
EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

CHAIN_NAMES = {
    "solana": "Solana",
    "ethereum": "Ethereum",
    "bsc": "BSC",
    "base": "Base",
    "robinhood": "Robinhood",
    "robinhoodchain": "Robinhood",
}
CMC_PLATFORMS = {
    "Ethereum": "ethereum",
    "BSC": "bsc",
    "Base": "base",
}
BLOCKSCOUT = {
    "Robinhood": "https://robinhoodchain.blockscout.com",
}


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


@dataclass(frozen=True)
class TokenHolder:
    address: str
    balance: Decimal
    percentage: float | None = None


@dataclass(frozen=True)
class TokenIntelligence:
    address: str
    chain: str
    name: str
    symbol: str
    market_cap: float | None
    fdv: float | None
    price_usd: float | None
    image_url: str | None
    dex_url: str | None
    holders: tuple[TokenHolder, ...]


class TokenIntelligenceError(RuntimeError):
    pass


class TokenIntelligenceClient:
    def __init__(self, http: Any, solana_rpcs: list[str]) -> None:
        self.http = http
        self.solana_rpcs = normalize_rpc_urls(solana_rpcs)

    async def lookup(
        self,
        address: str,
        *,
        limit: int = 5,
        pump_coin: Any = None,
    ) -> TokenIntelligence:
        clean = address.strip().strip("`").strip()
        if not clean:
            raise TokenIntelligenceError("A token address is required")
        limit = 10 if limit >= 10 else 5

        pair = await self._dex_pair(clean)
        chain = self._pair_chain(pair)
        if chain == "Unknown" and pump_coin is not None:
            chain = "Solana"
        if chain == "Unknown" and not EVM_RE.fullmatch(clean):
            chain = "Solana"

        base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        quote = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}
        token_side = base
        if str(base.get("address") or "").lower() != clean.lower():
            token_side = quote if str(quote.get("address") or "").lower() == clean.lower() else base
        info = pair.get("info") if isinstance(pair.get("info"), dict) else {}

        name = str(token_side.get("name") or getattr(pump_coin, "name", "Unknown token"))
        symbol = str(token_side.get("symbol") or getattr(pump_coin, "symbol", "TOKEN"))
        image = info.get("imageUrl") or getattr(pump_coin, "image_url", None)
        market_cap = _float(pair.get("marketCap"))
        if market_cap is None:
            market_cap = _float(getattr(pump_coin, "market_cap_usd", None))
        fdv = _float(pair.get("fdv"))
        price_usd = _float(pair.get("priceUsd"))
        dex_url = str(pair.get("url") or "").strip() or None

        try:
            holders = await self._holders(clean, chain, limit)
        except Exception as exc:
            log.info("holder lookup failed for %s on %s: %s", clean, chain, exc)
            holders = []

        return TokenIntelligence(
            address=clean,
            chain=chain,
            name=name,
            symbol=symbol.lstrip("$")[:40] or "TOKEN",
            market_cap=market_cap,
            fdv=fdv,
            price_usd=price_usd,
            image_url=str(image) if isinstance(image, str) and image.startswith(("http://", "https://")) else None,
            dex_url=dex_url,
            holders=tuple(holders[:limit]),
        )

    async def _dex_pair(self, address: str) -> dict[str, Any]:
        try:
            response = await self.http.get(
                f"{DEXSCREENER_TOKEN_URL}/{address}",
                headers={"Accept": "application/json"},
                timeout=20,
            )
            if int(getattr(response, "status_code", 200)) >= 400:
                return {}
            raw = response.json()
        except Exception:
            return {}
        pairs = raw.get("pairs") if isinstance(raw, dict) else []
        candidates = [pair for pair in (pairs or []) if isinstance(pair, dict)]
        if not candidates:
            return {}

        def score(pair: dict[str, Any]) -> float:
            liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
            return _float(liquidity.get("usd")) or 0.0

        matching = []
        for pair in candidates:
            base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
            quote = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}
            addresses = {str(base.get("address") or "").lower(), str(quote.get("address") or "").lower()}
            if address.lower() in addresses:
                matching.append(pair)
        return max(matching or candidates, key=score)

    @staticmethod
    def _pair_chain(pair: dict[str, Any]) -> str:
        key = str(pair.get("chainId") or "").lower().replace("-", "")
        return CHAIN_NAMES.get(key, "Unknown")

    async def _holders(self, address: str, chain: str, limit: int) -> list[TokenHolder]:
        if chain == "Solana":
            return await self._solana_holders(address, limit)
        if chain in CMC_PLATFORMS:
            return await self._cmc_holders(address, chain, limit)
        if chain in BLOCKSCOUT:
            return await self._blockscout_holders(address, chain, limit)
        return []

    async def _solana_call(self, method: str, params: list[Any]) -> Any:
        last_error: Exception | None = None
        for url in self.solana_rpcs:
            try:
                response = await self.http.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=20,
                )
                if int(getattr(response, "status_code", 200)) >= 400:
                    raise TokenIntelligenceError("RPC returned an HTTP error")
                raw = response.json()
                if not isinstance(raw, dict) or raw.get("error"):
                    raise TokenIntelligenceError(str((raw or {}).get("error") or "invalid RPC response"))
                return raw.get("result")
            except Exception as exc:
                last_error = exc
                log.debug("Solana holder RPC failed via %s: %s", rpc_display_name(url), exc)
        raise TokenIntelligenceError(f"Every Solana RPC failed: {last_error}")

    async def _solana_holders(self, mint: str, limit: int) -> list[TokenHolder]:
        supply_raw = await self._solana_call("getTokenSupply", [mint])
        largest_raw = await self._solana_call("getTokenLargestAccounts", [mint])
        supply_value = supply_raw.get("value") if isinstance(supply_raw, dict) else {}
        supply = _decimal((supply_value or {}).get("uiAmountString"))
        rows = largest_raw.get("value") if isinstance(largest_raw, dict) else []
        rows = [row for row in (rows or []) if isinstance(row, dict)][: max(20, limit * 2)]
        accounts = [str(row.get("address") or "") for row in rows if row.get("address")]
        if not accounts:
            return []
        account_raw = await self._solana_call(
            "getMultipleAccounts",
            [accounts, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        account_values = account_raw.get("value") if isinstance(account_raw, dict) else []
        totals: dict[str, Decimal] = {}
        for row, account in zip(rows, account_values or []):
            data = account.get("data") if isinstance(account, dict) else None
            parsed = data.get("parsed") if isinstance(data, dict) else None
            info = parsed.get("info") if isinstance(parsed, dict) else None
            owner = str((info or {}).get("owner") or "")
            balance = _decimal(row.get("uiAmountString"))
            if owner and balance is not None:
                totals[owner] = totals.get(owner, Decimal(0)) + balance
        holders = [
            TokenHolder(
                owner,
                balance,
                float(balance / supply * 100) if supply and supply > 0 else None,
            )
            for owner, balance in totals.items()
        ]
        return sorted(holders, key=lambda holder: holder.balance, reverse=True)[:limit]

    async def _cmc_holders(self, token: str, chain: str, limit: int) -> list[TokenHolder]:
        response = await self.http.post(
            CMC_HOLDERS_URL,
            json={
                "tokenAddress": token,
                "platform": CMC_PLATFORMS[chain],
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
        holders = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            wallet = str(row.get("walletAddress") or "").strip().lower()
            balance = _decimal(row.get("balance"))
            percentage = _float(row.get("percentage") or row.get("percent"))
            if EVM_RE.fullmatch(wallet) and balance is not None:
                holders.append(TokenHolder(wallet, balance, percentage))
        return sorted(holders, key=lambda holder: holder.balance, reverse=True)[:limit]

    async def _blockscout_holders(self, token: str, chain: str, limit: int) -> list[TokenHolder]:
        response = await self.http.get(
            f"{BLOCKSCOUT[chain]}/api/v2/tokens/{token}/holders",
            headers={"Accept": "application/json"},
            timeout=20,
        )
        if int(getattr(response, "status_code", 200)) >= 400:
            return []
        raw = response.json()
        rows = raw.get("items") if isinstance(raw, dict) else []
        token_data = raw.get("token") if isinstance(raw, dict) else {}
        try:
            decimals = int((token_data or {}).get("decimals") or 18)
        except (TypeError, ValueError):
            decimals = 18
        raw_supply = _decimal((token_data or {}).get("total_supply"))
        supply = raw_supply / (Decimal(10) ** decimals) if raw_supply is not None else None
        holders = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            holder = row.get("address") or row.get("address_hash")
            if isinstance(holder, dict):
                holder = holder.get("hash")
            wallet = str(holder or "").strip().lower()
            raw_balance = _decimal(row.get("value"))
            if not EVM_RE.fullmatch(wallet) or raw_balance is None:
                continue
            balance = raw_balance / (Decimal(10) ** decimals)
            percentage = float(balance / supply * 100) if supply and supply > 0 else None
            holders.append(TokenHolder(wallet, balance, percentage))
        return sorted(holders, key=lambda holder: holder.balance, reverse=True)[:limit]

