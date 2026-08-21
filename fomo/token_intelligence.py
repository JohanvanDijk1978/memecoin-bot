"""Cross-chain token metadata and top-holder intelligence."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlsplit

from rpc_config import env_rpc_urls, normalize_rpc_urls, rpc_display_name
from token_traders import (
    EVM_QUOTE_ASSETS,
    SOLANA_STABLE_MINTS,
    STABLE_SYMBOLS,
    WSOL_MINT,
    QuoteFlow,
    TokenFlow,
    TokenTrader,
    aggregate_traders,
    attach_quote_values,
    candidate_pool,
    infrastructure_addresses,
    parse_alchemy_quote_flows,
    parse_alchemy_transfers,
    parse_blockscout_transfers,
    parse_helius_transactions,
    parse_rpc_transactions,
    rank_traders,
    sampled_window,
)


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

# `getTokenLargestAccounts` returns at most 20 token accounts, and several of
# those collapse into one owner, so it cannot answer a top-50 question. Helius
# DAS `getTokenAccounts` pages the full holder set instead -- the same reason
# `fomo_map_top.py` prefers it.
LARGEST_ACCOUNTS_CAP = 20
DAS_PAGE_LIMIT = 1000
DAS_MAX_PAGES = 3
MAX_HOLDERS = 50

# ------------------------------------------------------------- traders ----
# Top Traders reads the same chains the holder list does, but a holder list is
# one ranked query while traders have to be aggregated out of transfer
# history. These bound that: the ranking covers the window the pages reach and
# says so on the card rather than implying a lifetime total.
HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
HELIUS_TX_LIMIT = 100
EVM_TRANSFER_PAGE = "0x3e8"  # 1000, Alchemy's per-page maximum
MAX_TRADERS = 50


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    try:
        return max(low, min(int(os.getenv(name, str(default))), high))
    except ValueError:
        return default


# The budget is what decides whether the ranking is *right*, not merely how
# long it takes. A memecoin that launched today has its winners' entries at the
# very start of its history -- they bought at a $23K market cap and sold at
# $133K -- so a sample that reaches only the last few hundred transactions sees
# nothing but the tail: recent buyers, all at nearly the same entry, all up the
# same few percent. 30 Helius pages is 3,000 parsed transactions, which covers
# a young token's entire life; paging stops early the moment history runs out,
# so a quiet token costs no more than it used to.
SOLANA_TRADER_PAGES = _env_int("TOKEN_TRADER_SOLANA_PAGES", 30, low=1, high=200)
EVM_TRADER_PAGES = _env_int("TOKEN_TRADER_EVM_PAGES", 5, low=1, high=50)
# Paging is sequential, so the page budget alone cannot bound the wait. This
# does: whichever limit is reached first stops the sample, and the card says
# the sample was cut short either way.
TRADER_BUDGET_SECONDS = _env_int(
    "TOKEN_TRADER_BUDGET_SECONDS", 60, low=5, high=600
)
# The raw-RPC fallback pays one HTTP request per batch of transactions, so it
# is held to a much smaller sample than the parsed route.
RPC_TRADER_SIGNATURES = _env_int(
    "TOKEN_TRADER_RPC_SIGNATURES", 400, low=20, high=5000
)
RPC_TRADER_BATCH = 10
# Trader aggregates are expensive and change slowly relative to a Discord
# card, so a repeat /token inside this window costs nothing.
TRADER_CACHE_TTL = 300.0
# The card can be re-sorted between PnL, ROI and volume without another
# request, so the rows it holds have to be the right rows for any of the three:
# the client keeps the union of the top `limit` under each ranking.
TRADER_POOL_MULTIPLIER = 3
# Pricing an EVM sample means reading the venue's own quote-asset leg. Pools
# are ranked by how much of the sample they touch, and only the busiest few are
# worth a query -- the long tail of one-swap pools prices almost nothing.
EVM_QUOTE_VENUES = 2
# A quote price is a market number, not a per-token one, so one lookup serves
# every /token on that chain for this long.
QUOTE_PRICE_TTL = 600.0


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


@dataclass(frozen=True)
class TokenTraders:
    """A ranked trader list plus what the sample behind it actually covers.

    `priced` is how many of those rows the ledger could put a dollar figure on.
    A card that says "ranked by PnL" over rows with no PnL would be lying by
    omission, so the count travels with the list.
    """

    traders: tuple[TokenTrader, ...]
    transactions: int
    earliest: int | None
    latest: int | None
    source: str
    truncated: bool = False
    priced: int = 0
    current_price: Decimal | None = None


class TokenIntelligenceError(RuntimeError):
    pass


class TokenIntelligenceClient:
    def __init__(
        self,
        http: Any,
        solana_rpcs: list[str],
        evm_rpcs: dict[str, list[str]] | None = None,
    ) -> None:
        self.http = http
        self.solana_rpcs = normalize_rpc_urls(solana_rpcs)
        # Holders on EVM come from CMC and Blockscout, which need no endpoint
        # of ours. Traders need transfer history, and Alchemy already answers
        # that for `fomo_evm.py` -- read the same variables rather than asking
        # for a second set of keys.
        self.evm_rpcs = evm_rpcs if evm_rpcs is not None else {
            "Ethereum": env_rpc_urls("ETH_RPC", "ETH_RPC_FALLBACKS"),
            "BSC": env_rpc_urls("BSC_RPC", "BSC_RPC_FALLBACKS"),
            "Base": env_rpc_urls("BASE_RPC", "BASE_RPC_FALLBACKS"),
            "Robinhood": env_rpc_urls("ROBINHOOD_RPC", "ROBINHOOD_RPC_FALLBACKS"),
        }
        self._trader_cache: dict[tuple[str, str], tuple[float, TokenTraders]] = {}
        self._quote_price_cache: dict[str, tuple[float, dict[str, Decimal]]] = {}
        # The flows behind the most recent trader sample, kept so
        # `token_traders_diag.py` can show one wallet's trades without paging
        # the history a second time. Nothing in the bot reads it.
        self.last_sample: list[TokenFlow] = []

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
        limit = max(1, min(int(limit), MAX_HOLDERS))

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

    def _helius_urls(self) -> list[str]:
        return [url for url in self.solana_rpcs if "helius" in url.lower()]

    async def _das_holders(
        self, mint: str, decimals: int, supply: Decimal | None, limit: int
    ) -> list[TokenHolder]:
        """Owner totals from Helius DAS, which pages past the 20-account cap.

        Returns [] when no Helius endpoint is configured or every one of them
        fails, so the caller can fall back to `getTokenLargestAccounts` rather
        than reporting a token with no holders at all.
        """
        scale = Decimal(10) ** int(decimals)
        for url in self._helius_urls():
            totals: dict[str, Decimal] = {}
            try:
                for page in range(1, DAS_MAX_PAGES + 1):
                    response = await self.http.post(
                        url,
                        json={
                            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccounts",
                            "params": {
                                "mint": mint, "page": page, "limit": DAS_PAGE_LIMIT,
                            },
                        },
                        timeout=30,
                    )
                    if int(getattr(response, "status_code", 200)) >= 400:
                        raise TokenIntelligenceError("DAS returned an HTTP error")
                    payload = response.json()
                    if not isinstance(payload, dict) or payload.get("error"):
                        raise TokenIntelligenceError(
                            str((payload or {}).get("error") or "invalid DAS response")
                        )
                    accounts = (payload.get("result") or {}).get("token_accounts") or []
                    for account in accounts:
                        if not isinstance(account, dict):
                            continue
                        owner = str(account.get("owner") or "")
                        raw = _decimal(account.get("amount"))
                        if not owner or raw is None:
                            continue
                        totals[owner] = totals.get(owner, Decimal(0)) + raw
                    if len(accounts) < DAS_PAGE_LIMIT:
                        break
            except Exception as exc:
                log.debug("DAS holder query failed via %s: %s",
                          rpc_display_name(url), exc)
                continue
            holders = [
                TokenHolder(
                    owner,
                    raw / scale,
                    float(raw / scale / supply * 100) if supply and supply > 0 else None,
                )
                for owner, raw in totals.items()
            ]
            return sorted(holders, key=lambda holder: holder.balance, reverse=True)[:limit]
        return []

    async def _solana_holders(self, mint: str, limit: int) -> list[TokenHolder]:
        supply_raw = await self._solana_call("getTokenSupply", [mint])
        supply_value = supply_raw.get("value") if isinstance(supply_raw, dict) else {}
        supply = _decimal((supply_value or {}).get("uiAmountString"))
        if limit > LARGEST_ACCOUNTS_CAP:
            decimals = (supply_value or {}).get("decimals")
            if decimals is not None:
                deep = await self._das_holders(mint, int(decimals), supply, limit)
                if deep:
                    return deep
            log.info(
                "top-%d holders for %s fell back to getTokenLargestAccounts "
                "(no Helius DAS endpoint answered); at most %d owners are reachable",
                limit, mint, LARGEST_ACCOUNTS_CAP,
            )
        largest_raw = await self._solana_call("getTokenLargestAccounts", [mint])
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


    # ------------------------------------------------------------ traders --

    async def top_traders(
        self, address: str, chain: str, *,
        limit: int = MAX_TRADERS, price_usd: float | Decimal | None = None,
    ) -> TokenTraders:
        """This token's best-performing traders recently.

        The sample is transfer history; the ranking is a cost-basis ledger over
        it -- weighted-average entry, realised PnL on every disposal, unrealised
        PnL on what is still held at `price_usd`. It used to be `bought + sold`,
        which ranked the wallets that moved the most tokens and had nothing to
        say about whether any of them made money.

        Cached for `TRADER_CACHE_TTL`, because `/token` renders every page of
        the card up front and a second invocation on a trending mint would
        otherwise re-page the same history. A provider that fails returns an
        empty result rather than raising: the holders card must still render.
        """
        clean = address.strip().strip("`").strip()
        current = _decimal(price_usd) if price_usd is not None else None
        key = (clean.lower(), chain)
        hit = self._trader_cache.get(key)
        if hit and hit[0] > time.monotonic() and hit[1].current_price == current:
            return hit[1]

        try:
            flows, source, truncated = await self._trader_flows(clean, chain)
        except Exception as exc:
            log.info("trader lookup failed for %s on %s: %s", clean, chain, exc)
            flows, source, truncated = [], "unavailable", False

        self.last_sample = flows
        earliest, latest = sampled_window(flows)
        ledgers = aggregate_traders(
            flows,
            exclude={clean, clean.lower()},
            limit=None,
            current_price=current,
        )
        pool = rank_traders(
            candidate_pool(ledgers, limit=limit),
            key="pnl",
            limit=limit * TRADER_POOL_MULTIPLIER,
        )
        result = TokenTraders(
            traders=tuple(pool),
            transactions=len({flow.reference for flow in flows if flow.reference}),
            earliest=earliest,
            latest=latest,
            source=source,
            truncated=truncated,
            priced=sum(1 for trader in pool if trader.has_pnl),
            current_price=current,
        )
        log.info(
            "top traders for %s on %s: %d address(es) (%d priced) from "
            "%d transaction(s) via %s",
            clean, chain, len(result.traders), result.priced,
            result.transactions, source,
        )
        self._trader_cache[key] = (time.monotonic() + TRADER_CACHE_TTL, result)
        return result

    async def _trader_flows(
        self, address: str, chain: str
    ) -> tuple[list[TokenFlow], str, bool]:
        if chain == "Solana":
            return await self._solana_trader_flows(address)
        if chain in self.evm_rpcs or chain in BLOCKSCOUT:
            return await self._evm_trader_flows(address, chain)
        return [], "unsupported", False

    # ------------------------------------------------------- quote prices --

    async def _quote_prices(self, chain: str, token: str) -> dict[str, Decimal]:
        """USD per unit of the assets trades on this chain are quoted in.

        A stablecoin is a dollar and needs no request. The chain's own coin
        needs exactly one, from the DEX Screener endpoint the card's market cap
        already comes from, and it is cached per chain rather than per token
        because it is a market price rather than a property of this mint.

        The traded token is never in the result: a token cannot price itself.
        """
        cached = self._quote_price_cache.get(chain)
        if cached and cached[0] > time.monotonic():
            prices = dict(cached[1])
        else:
            prices = {}
            if chain == "Solana":
                prices = {mint: Decimal(1) for mint in SOLANA_STABLE_MINTS}
                native = await self._native_price(WSOL_MINT)
                if native:
                    prices[WSOL_MINT] = native
            elif chain in EVM_QUOTE_ASSETS:
                assets = EVM_QUOTE_ASSETS[chain]
                prices = {
                    contract: Decimal(1)
                    for contract, symbol in assets.items()
                    if symbol in STABLE_SYMBOLS
                }
                for contract, symbol in assets.items():
                    if symbol in STABLE_SYMBOLS:
                        continue
                    native = await self._native_price(contract)
                    if native:
                        prices[contract] = native
                        # A swap paid for in the chain's own coin never touches
                        # the wrapped contract, so the native leg shares its
                        # price under a key the parser recognises.
                        prices.setdefault("native", native)
            if prices:
                self._quote_price_cache[chain] = (
                    time.monotonic() + QUOTE_PRICE_TTL, dict(prices)
                )
        prices.pop(token, None)
        prices.pop(token.lower(), None)
        return prices

    async def _native_price(self, address: str) -> Decimal | None:
        """USD per unit of a quote asset, from its deepest pair.

        `priceUsd` is the *base* token's price, and the deepest pair for a
        chain's own coin is usually COIN/USDC with the coin as base -- but not
        always, and reading a stablecoin's price as SOL's would scale every PnL
        on the card by a couple of hundred. When the asset is the quote side
        instead, `priceUsd / priceNative` inverts the pair exactly.
        """
        pair = await self._dex_pair(address)
        if not pair:
            return None
        wanted = address.lower()
        base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        quote = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}
        price = _decimal(pair.get("priceUsd"))
        if not price or price <= 0:
            return None
        if str(base.get("address") or "").lower() == wanted:
            return price
        if str(quote.get("address") or "").lower() == wanted:
            native = _decimal(pair.get("priceNative"))
            if native and native > 0:
                return price / native
        return None

    # ------------------------------------------------------------- solana --

    def _helius_key(self) -> str | None:
        """The API key already configured in `SOLANA_RPC`, if it is Helius.

        The parsed-transaction route lives on `api.helius.xyz` rather than on
        the RPC host, so it needs the key rather than the endpoint. Nothing new
        has to be configured for it.
        """
        for url in self._helius_urls():
            key = parse_qs(urlsplit(url).query).get("api-key", [""])[0].strip()
            if key:
                return key
        return None

    async def _solana_trader_flows(
        self, mint: str
    ) -> tuple[list[TokenFlow], str, bool]:
        prices = await self._quote_prices("Solana", mint)
        key = self._helius_key()
        if key:
            flows, truncated = await self._helius_trader_flows(mint, key, prices)
            if flows or truncated:
                # `truncated` with nothing to show means the budget ran out
                # rather than the route being wrong: starting the slower
                # fallback would spend time the caller has already used up.
                return flows, "helius", truncated
            log.info(
                "Helius parsed history returned nothing for %s; "
                "falling back to raw RPC", mint,
            )
        else:
            log.info(
                "no Helius api-key in SOLANA_RPC; top traders for %s uses the "
                "smaller raw-RPC sample", mint,
            )
        flows, truncated = await self._rpc_trader_flows(mint, prices)
        return flows, "rpc", truncated

    async def _helius_trader_flows(
        self, mint: str, key: str, prices: dict[str, Decimal] | None = None,
    ) -> tuple[list[TokenFlow], bool]:
        """Parsed transaction pages for the mint, newest first.

        `tokenTransfers` names owner accounts, so no token account ever has to
        be resolved to its owner -- the same reason `_das_holders` prefers
        Helius over `getTokenLargestAccounts`.

        Paging continues until the mint's history runs out, the page budget is
        spent or the wall-clock budget is. Reaching the end matters more here
        than it looks: a token's best traders bought at its beginning, so a
        sample that stops short does not merely cover less, it systematically
        excludes the winners and ranks the tail.
        """
        flows: list[TokenFlow] = []
        before = ""
        truncated = False
        deadline = time.monotonic() + TRADER_BUDGET_SECONDS
        for page in range(SOLANA_TRADER_PAGES):
            if time.monotonic() > deadline:
                log.info("trader sample for %s stopped after %d page(s): "
                         "%ds budget spent", mint, page, TRADER_BUDGET_SECONDS)
                truncated = True
                break
            params: dict[str, Any] = {"api-key": key, "limit": HELIUS_TX_LIMIT}
            if before:
                params["before"] = before
            try:
                response = await self.http.get(
                    HELIUS_TX_URL.format(address=mint),
                    params=params,
                    headers={"Accept": "application/json"},
                    timeout=30,
                )
                if int(getattr(response, "status_code", 200)) >= 400:
                    raise TokenIntelligenceError(
                        f"HTTP {getattr(response, 'status_code', '?')}"
                    )
                payload = response.json()
            except Exception as exc:
                log.debug("Helius parsed history page %d failed for %s: %s",
                          page + 1, mint, exc)
                # A page that failed is a sample cut short, not a finished
                # history -- the card must not claim full coverage.
                truncated = page > 0
                break
            if not isinstance(payload, list) or not payload:
                break
            flows.extend(parse_helius_transactions(payload, mint, prices=prices))
            last = payload[-1] if isinstance(payload[-1], dict) else {}
            before = str(last.get("signature") or "")
            if len(payload) < HELIUS_TX_LIMIT or not before:
                break   # the mint's history ended: the sample is complete
            truncated = page == SOLANA_TRADER_PAGES - 1
        return flows, truncated

    async def _rpc_trader_flows(
        self, mint: str, prices: dict[str, Decimal] | None = None,
    ) -> tuple[list[TokenFlow], bool]:
        """Balance-delta flows from raw `getTransaction`, batched.

        Rate limits count HTTP requests, not calls, so transactions go out
        `RPC_TRADER_BATCH` to a request -- the same trick `fomo_wallet.Rpc`
        uses. It is still far more expensive per transaction than the parsed
        route, which is why the sample it takes is much smaller.
        """
        signatures_raw = await self._solana_call(
            "getSignaturesForAddress", [mint, {"limit": RPC_TRADER_SIGNATURES}]
        )
        rows = [row for row in (signatures_raw or []) if isinstance(row, dict)]
        signatures = [
            str(row.get("signature") or "") for row in rows if not row.get("err")
        ]
        signatures = [signature for signature in signatures if signature]
        if not signatures:
            return [], False

        options = {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}
        flows: list[TokenFlow] = []
        for start in range(0, len(signatures), RPC_TRADER_BATCH):
            chunk = signatures[start:start + RPC_TRADER_BATCH]
            payload = [
                {"jsonrpc": "2.0", "id": index, "method": "getTransaction",
                 "params": [signature, options]}
                for index, signature in enumerate(chunk)
            ]
            results: list[Any] = []
            for url in self.solana_rpcs:
                try:
                    response = await self.http.post(url, json=payload, timeout=30)
                    if int(getattr(response, "status_code", 200)) >= 400:
                        continue
                    body = response.json()
                    if not isinstance(body, list):
                        continue
                    results = [item.get("result") for item in body
                               if isinstance(item, dict)]
                    break
                except Exception as exc:
                    log.debug("batched getTransaction failed via %s: %s",
                              rpc_display_name(url), exc)
            if not results:
                break
            flows.extend(parse_rpc_transactions(results, mint, prices=prices))
        return flows, len(signatures) >= RPC_TRADER_SIGNATURES

    # ---------------------------------------------------------------- evm --

    async def _evm_trader_flows(
        self, token: str, chain: str
    ) -> tuple[list[TokenFlow], str, bool]:
        for url in self.evm_rpcs.get(chain, []):
            if "alchemy.com" not in url.lower():
                continue
            try:
                flows, truncated = await self._alchemy_trader_flows(url, token)
            except Exception as exc:
                log.debug("Alchemy trader search failed via %s: %s",
                          rpc_display_name(url), exc)
                continue
            if flows:
                flows = await self._price_evm_flows(url, chain, token, flows)
                return flows, "alchemy", truncated
        if chain in BLOCKSCOUT:
            flows = await self._blockscout_trader_flows(token, chain)
            if flows:
                return flows, "blockscout", False
        log.info("no EVM transfer source answered for %s on %s", token, chain)
        return [], "unavailable", False

    async def _alchemy_trader_flows(
        self, url: str, token: str
    ) -> tuple[list[TokenFlow], bool]:
        """`alchemy_getAssetTransfers` for the token, newest first.

        Descending from the head is right here and wrong in `fomo_evm.py`:
        that module hunts one historical transaction and has to anchor on its
        block, while this one wants the most recent activity and nothing else.
        """
        flows: list[TokenFlow] = []
        page_key: str | None = None
        truncated = False
        deadline = time.monotonic() + TRADER_BUDGET_SECONDS
        for page in range(EVM_TRADER_PAGES):
            if time.monotonic() > deadline:
                truncated = True
                break
            params: dict[str, Any] = {
                "fromBlock": "0x0", "toBlock": "latest",
                "contractAddresses": [token], "category": ["erc20"],
                "withMetadata": True, "excludeZeroValue": True,
                "maxCount": EVM_TRANSFER_PAGE, "order": "desc",
            }
            if page_key:
                params["pageKey"] = page_key
            response = await self.http.post(
                url,
                json={"jsonrpc": "2.0", "id": 1,
                      "method": "alchemy_getAssetTransfers", "params": [params]},
                timeout=30,
            )
            if int(getattr(response, "status_code", 200)) >= 400:
                break
            payload = response.json()
            result = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(result, dict) or payload.get("error"):
                break
            flows.extend(parse_alchemy_transfers(result, token))
            page_key = str(result.get("pageKey") or "") or None
            if not page_key:
                break
            truncated = page == EVM_TRADER_PAGES - 1
        return flows, truncated

    async def _price_evm_flows(
        self, url: str, chain: str, token: str, flows: list[TokenFlow],
    ) -> list[TokenFlow]:
        """Put a USD value on EVM trades by reading the venue's own money leg.

        An EVM token page carries only that token, so unlike the Solana routes
        the money side of a swap is not already in hand. It is one join away:
        the pool is on the other side of every swap in the sample, its USDC or
        WETH movement in a transaction *is* the size of that swap, and the
        transaction hash matches the two exactly.

        Only the busiest venues are queried, and a failure leaves the flows
        unpriced rather than raising -- a trader list with no PnL is still a
        trader list, and the card says which it is.
        """
        prices = await self._quote_prices(chain, token)
        venues = infrastructure_addresses(flows)
        if not prices or not venues:
            return flows
        weight: dict[str, set[str]] = {}
        for flow in flows:
            if flow.address in venues and flow.reference:
                weight.setdefault(flow.address, set()).add(flow.reference)
        busiest = sorted(
            venues, key=lambda address: len(weight.get(address, ())), reverse=True
        )[:EVM_QUOTE_VENUES]
        contracts = [key for key in prices if key != "native"]
        quotes: list[QuoteFlow] = []
        # One `seen` set across every query: a venue's transfer must not be
        # counted once as its sender and again as its recipient.
        seen: set[str] = set()
        for venue in busiest:
            for direction in ("fromAddress", "toAddress"):
                try:
                    quotes.extend(
                        await self._alchemy_quote_page(url, venue, direction,
                                                       contracts, prices, seen)
                    )
                except Exception as exc:
                    log.debug("quote leg %s=%s failed on %s: %s",
                              direction, venue, chain, exc)
        if not quotes:
            log.info("no quote-asset movement found for %s on %s; "
                     "trader rows stay unpriced", token, chain)
            return flows
        return attach_quote_values(flows, quotes, venues=venues)

    async def _alchemy_quote_page(
        self, url: str, venue: str, direction: str,
        contracts: list[str], prices: dict[str, Decimal],
        seen: set[str] | None = None,
    ) -> list[QuoteFlow]:
        """One page of a venue's quote-asset transfers, newest first."""
        params: dict[str, Any] = {
            "fromBlock": "0x0", "toBlock": "latest",
            "contractAddresses": contracts, "category": ["erc20"],
            "withMetadata": False, "excludeZeroValue": True,
            "maxCount": EVM_TRANSFER_PAGE, "order": "desc",
            direction: venue,
        }
        response = await self.http.post(
            url,
            json={"jsonrpc": "2.0", "id": 1,
                  "method": "alchemy_getAssetTransfers", "params": [params]},
            timeout=30,
        )
        if int(getattr(response, "status_code", 200)) >= 400:
            return []
        payload = response.json()
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict) or payload.get("error"):
            return []
        return parse_alchemy_quote_flows(result, prices, seen=seen)

    async def _blockscout_trader_flows(
        self, token: str, chain: str
    ) -> list[TokenFlow]:
        response = await self.http.get(
            f"{BLOCKSCOUT[chain]}/api/v2/tokens/{token}/transfers",
            headers={"Accept": "application/json"},
            timeout=20,
        )
        if int(getattr(response, "status_code", 200)) >= 400:
            return []
        return parse_blockscout_transfers(response.json(), token)
