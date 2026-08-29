"""Discover Pump.fun EVM wallets from public portfolio balance fingerprints."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import asyncio
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
# Display only -- `pump_resolve_diag.py` names the chain a fingerprint
# came from, and Pump mixes Solana rows into the same portfolio payload.
CHAIN_NAMES = {1: "Ethereum", 56: "BSC", 8453: "Base", 4663: "Robinhood"}
SOLANA_CHAIN_ID = 1399811149
# How many of a profile's ordered positions discovery is willing to spend
# requests on. The diagnostic walks the same slice.
EXAMINED_POSITIONS = 8
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


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


# Session 42: this was 5 pages -- 250 holders -- and that was the whole reason
# `/pump eth` had no EVM wallet. Pump's published fingerprint for that profile
# was a 372.5228803259225 dust position, and its owner sits at holder rank
# ~1211 of 2493. The fingerprint was perfect; discovery just never paged far
# enough to meet it. Depth is a correctness parameter here, not a budget.
HOLDER_PAGES = max(1, _int_env("PUMP_EVM_HOLDER_PAGES", 40))
# ...but a Discord card cannot spend 40 pages x 8 positions on a public
# explorer that answers in about two seconds. Depth belongs to the tools that
# can afford it: `pump_resolve_diag.py` and `pump_map_top.py` page deep and
# write the result, and `/pump` reads what they cached. A card that discovers
# nothing costs one field; a card that takes 80 seconds costs the command.
HOLDER_PAGES_CARD = max(1, _int_env("PUMP_EVM_HOLDER_PAGES_CARD", 6))
# Blockscout throttles a tight loop, and a throttled page reads as an empty
# holder index -- which looks exactly like "this token has no holders".
HOLDER_PAGE_DELAY = _float_env("PUMP_EVM_HOLDER_PAGE_DELAY", 0.15)
HOLDER_RETRIES = max(1, _int_env("PUMP_EVM_HOLDER_RETRIES", 3))
# Measured: Blockscout answers a holder page in about 6.5 seconds, so 40 pages
# is four minutes for ONE token. Depth without a clock is how a tool stops
# looking like it is working and starts looking like it has hung.
HOLDER_SECONDS = _float_env("PUMP_EVM_HOLDER_SECONDS", 300)
CARD_SECONDS = _float_env("PUMP_EVM_CARD_SECONDS", 8)
# Cloudflare gives 180 requests per window and a ~40 minute lockout after it.
# Stopping just short of the wall keeps the NEXT run possible.
HOLDER_RATE_FLOOR = max(0, _int_env("PUMP_EVM_HOLDER_RATE_FLOOR", 8))

# Blockscout sits behind Cloudflare (confirmed: `server: cloudflare`,
# `x-ratelimit-limit: 180`). A request carrying httpx's default
# `python-httpx/x.y` User-Agent is refused, and that refusal arrives here as
# an EMPTY holder list -- indistinguishable from "this token has no holders".
#
# That asymmetry is why `/pump eth` still had no EVM wallet after the depth
# fix: `_positions` sends `pump_api.HEADERS`, which carry a UA, so the
# portfolio call succeeded and the positions were found; every holder call
# sent `Accept` alone and never got past the edge. `token_intelligence.py`
# already learned this on HyperEVM in session 39 -- the lesson just never
# reached the other explorer calls.
EXPLORER_USER_AGENT = os.getenv(
    "EXPLORER_USER_AGENT",
    "Mozilla/5.0 (compatible; fomo-bot/1.0; +https://fomo.family)",
)
EXPLORER_HEADERS = {
    "Accept": "application/json",
    "User-Agent": EXPLORER_USER_AGENT,
}


@dataclass(frozen=True)
class HolderIndex:
    """A holder list, and how much of one it actually is.

    `complete` is the field that matters: "no address holds that balance" and
    "no address in the first N holders holds that balance" are different
    answers, and only one of them is a reason to give up.
    """

    holders: list[tuple[str, Decimal]]
    source: str
    complete: bool = False
    pages: int = 0
    status: int | None = None
    error: str = ""
    stopped: str = ""  # "budget" / "rate-limit" / "" -- why paging ended

    def __bool__(self) -> bool:
        return bool(self.holders)


def _rate_remaining(response: Any) -> int | None:
    """Requests left in the explorer's current rate-limit window, if it says."""
    try:
        value = response.headers.get("x-ratelimit-remaining")
    except Exception:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
    # How many OTHER published positions the same address also matched. Zero
    # is still a verified match; two or more is the kind of evidence that
    # settled this wallet by hand.
    corroborations: int = 0


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


PORTFOLIO_PARAMS = {
    "filter": "open",
    "page": 0,
    "pageSize": 100,
    "sortBy": "POSITION_SIZE",
}


def order_positions(rows: Any) -> list[_Position]:
    """Parse raw portfolio rows into the order discovery examines them in.

    Split out of `PumpEvmResolver._positions` so the diagnostic can walk the
    same candidates, in the same order, from one fetch of the same payload --
    a gate report that examined a different slice than `/pump` did would be
    worse than no gate report.
    """
    parsed = [_Position.from_raw(row) for row in (rows or [])]
    usable = [position for position in parsed if position is not None]
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
        self._decimals: dict[tuple[int, str], int] = {}
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

    async def portfolio_rows(self, solana: str) -> list[Any]:
        """Pump's published open positions for a profile, unparsed.

        Public because `pump_resolve_diag.py` needs the rows discovery threw
        away -- a Solana-only portfolio and a portfolio on an unsupported
        chain are the same empty candidate list here, and a very different
        answer to "why is there no EVM wallet?".
        """
        response = await self.http.get(
            f"{CLIENT_URL}/user-portfolio/{quote(solana, safe='')}",
            params=dict(PORTFOLIO_PARAMS),
            headers=HEADERS,
        )
        if int(getattr(response, "status_code", 200)) >= 400:
            return []
        raw = response.json()
        rows = raw.get("positions") if isinstance(raw, dict) else []
        return list(rows or [])

    async def _positions(self, solana: str) -> list[_Position]:
        return order_positions(await self.portfolio_rows(solana))

    async def _cmc_holders(self, position: _Position) -> HolderIndex:
        platform = CMC_PLATFORMS.get(position.chain_id)
        if not platform:
            return HolderIndex([], "CMC", error="chain not on CMC")
        response = await self.http.post(
            CMC_HOLDERS_URL,
            json={
                "tokenAddress": position.token,
                "platform": platform,
                "tag": "tag_all",
            },
            headers=EXPLORER_HEADERS,
        )
        status = int(getattr(response, "status_code", 200))
        if status >= 400:
            log.debug("CMC holder index refused %s: HTTP %s", position.token, status)
            return HolderIndex([], "CMC", status=status,
                               error=f"HTTP {status}")
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
        # CMC returns one ranked page and does not paginate here, so this is
        # never a complete holder set for a token with a long tail.
        return HolderIndex(result, "CMC", complete=False, pages=1, status=status)

    async def _blockscout_holders(
        self,
        position: _Position,
        *,
        max_pages: int | None = None,
        budget: float | None = None,
    ) -> HolderIndex:
        """Page a Blockscout holder list, deeply, politely, and on a clock.

        Holders come back ranked by balance, so the interesting owner of a
        *dust* fingerprint is deep in the tail. Stopping at 250 rows was
        indistinguishable from an empty index and lost real wallets.
        """
        base = BLOCKSCOUT.get(position.chain_id)
        if not base:
            return HolderIndex([], "Blockscout", error="chain not on Blockscout")
        limit = HOLDER_PAGES if max_pages is None else max_pages
        seconds = HOLDER_SECONDS if budget is None else budget
        deadline = time.monotonic() + seconds if seconds else None
        # A token's own decimals, read once -- the holder rows do not carry it.
        decimals = await self._token_decimals(position)
        params: dict[str, Any] = {}
        result: list[tuple[str, Decimal]] = []
        pages = 0
        status: int | None = None
        error = ""
        complete = False
        stopped = ""
        while pages < limit:
            if deadline is not None and time.monotonic() > deadline:
                stopped = "budget"
                log.info("holder search for %s stopped after %.0fs (%s rows)",
                         position.token, seconds, len(result))
                break
            response = None
            for attempt in range(HOLDER_RETRIES):
                response = await self.http.get(
                    f"{base}/api/v2/tokens/{position.token}/holders",
                    params=params,
                    headers=EXPLORER_HEADERS,
                )
                status = int(getattr(response, "status_code", 200))
                if status < 400:
                    break
                # 429 and 5xx are the throttle telling us to slow down, not
                # the chain telling us the token has no holders.
                error = f"HTTP {status}"
                await asyncio.sleep(HOLDER_PAGE_DELAY * (2 ** attempt) + 0.2)
                response = None
            if response is None:
                log.debug("Blockscout holder page %s for %s failed: %s",
                          pages, position.token, error)
                break
            error = ""
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
                result.append((address, raw_value / (Decimal(10) ** decimals)))
            pages += 1
            # Progress belongs on the wire, not in a comment: at ~6.5s a page
            # a silent tool is indistinguishable from a hung one.
            if pages % 5 == 0:
                log.info("%s holders read for %s (page %s/%s)",
                         len(result), position.token, pages, limit)
            remaining = _rate_remaining(response)
            if remaining is not None and remaining <= HOLDER_RATE_FLOOR:
                stopped = "rate-limit"
                log.info("stopping the holder search for %s: %s request(s) "
                         "left in this rate-limit window", position.token,
                         remaining)
                break
            next_page = raw.get("next_page_params") if isinstance(raw, dict) else None
            if not isinstance(next_page, dict) or not next_page:
                complete = True
                break
            params = next_page
            if HOLDER_PAGE_DELAY:
                await asyncio.sleep(HOLDER_PAGE_DELAY)
        return HolderIndex(result, "Blockscout", complete=complete, pages=pages,
                           status=status, error=error, stopped=stopped)

    async def _token_decimals(self, position: _Position) -> int:
        """ERC-20 decimals for a token, cached for the life of the resolver."""
        key = (position.chain_id, position.token)
        if key in self._decimals:
            return self._decimals[key]
        decimals = 18
        base = BLOCKSCOUT.get(position.chain_id)
        if base:
            try:
                response = await self.http.get(
                    f"{base}/api/v2/tokens/{position.token}",
                    headers=EXPLORER_HEADERS,
                )
                if int(getattr(response, "status_code", 200)) < 400:
                    raw = response.json()
                    if isinstance(raw, dict) and raw.get("decimals") is not None:
                        decimals = int(raw["decimals"])
            except Exception as exc:
                log.debug("token decimals lookup failed for %s: %s",
                          position.token, exc)
        self._decimals[key] = decimals
        return decimals

    async def holder_index(
        self,
        position: _Position,
        *,
        pages: int | None = None,
        budget: float | None = None,
    ) -> HolderIndex:
        """The holder index discovery would consult for this position."""
        if position.chain_id in CMC_PLATFORMS:
            return await self._cmc_holders(position)
        return await self._blockscout_holders(
            position, max_pages=pages, budget=budget
        )

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

    async def corroborate(
        self,
        address: str,
        positions: list[_Position],
        exclude: _Position | None = None,
    ) -> int:
        """How many of the profile's OTHER published balances this address also holds.

        One exact balance match is a coincidence worth checking; three in a
        row is the wallet. This is what settled `eth` by hand -- 372.5228803259225,
        50 and 25 of three different Robinhood tokens, all on one address.
        """
        score = 0
        for position in positions:
            if exclude is not None and position is exclude:
                continue
            try:
                verified, _balance = await self._verify_balance(position, address)
            except Exception:
                continue
            if verified:
                score += 1
        return score

    async def adopt(
        self, user: PumpUser, address: str, *, require: int = 1
    ) -> PumpEvmMatch | None:
        """Accept a supplied EVM address -- but only if the chain agrees.

        Discovery's expensive half is *searching* a holder index for an
        address holding Pump's exact published balance. When the address is
        already known, that search is skippable; the proof is not. This asks
        the chain directly, with `balanceOf`, whether the supplied address
        holds at least `require` of the exact balances Pump publishes for this
        profile. A wrong address fails every one of them, so this is a
        shortcut past the search, never past the evidence.
        """
        clean = (address or "").strip().lower()
        if not EVM_RE.fullmatch(clean):
            return None
        try:
            positions = await self._positions(user.address)
        except Exception:
            return None
        proofs: list[tuple[_Position, Decimal]] = []
        for position in positions[:EXAMINED_POSITIONS]:
            try:
                verified, balance = await self._verify_balance(position, clean)
            except Exception:
                continue
            if verified and balance is not None:
                proofs.append((position, balance))
        if len(proofs) < max(1, require):
            log.debug("adopt refused %s for %s: %s of %s balance(s) matched",
                      clean, user.username, len(proofs), require)
            return None
        position, balance = proofs[0]
        match = PumpEvmMatch(
            solana=user.address,
            handle=user.username,
            evm=clean,
            chain_id=position.chain_id,
            token=position.token,
            balance=str(balance),
            discovered_at=datetime.now(timezone.utc).isoformat(),
            verified_onchain=True,
            corroborations=len(proofs) - 1,
        )
        self._matches[user.address] = match
        try:
            self._save()
        except OSError:
            pass
        return match

    async def resolve(
        self,
        user: PumpUser,
        *,
        fresh: bool = False,
        pages: int | None = None,
        budget: float | None = None,
    ) -> PumpEvmMatch | None:
        """Discover the profile's EVM wallet.

        `pages` is the holder-index depth. Callers that render a card pass
        `HOLDER_PAGES_CARD`; the offline tools leave it at the deep default.
        """
        if not fresh:
            cached = self._matches.get(user.address)
            if cached and cached.verified_onchain:
                return cached
        try:
            positions = await self._positions(user.address)
        except Exception:
            return None
        examined = positions[:EXAMINED_POSITIONS]
        for position in examined:
            try:
                index = await self.holder_index(
                    position, pages=pages, budget=budget
                )
            except Exception as exc:
                log.debug("holder index failed for %s: %s", position.token, exc)
                continue
            matches = [
                (address, balance)
                for address, balance in index.holders
                if _same_balance(position.amount, balance)
            ]
            if not matches:
                if not index.complete:
                    log.debug(
                        "no holder at %s of %s in the first %s row(s) -- index "
                        "was truncated, not exhausted",
                        position.amount, position.token, len(index.holders),
                    )
                continue
            if len(matches) > 1:
                # Ambiguity used to end the position. It does not have to:
                # the profile publishes other balances, and a coincidence
                # rarely survives a second one.
                survivors = []
                for address, balance in matches:
                    if await self.corroborate(address, examined, exclude=position):
                        survivors.append((address, balance))
                if len(survivors) != 1:
                    log.debug("%s addresses hold %s of %s; refusing to guess",
                              len(matches), position.amount, position.token)
                    continue
                matches = survivors
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
                corroborations=await self.corroborate(
                    address, examined, exclude=position
                ),
            )
            self._matches[user.address] = match
            try:
                self._save()
            except OSError:
                pass
            return match
        return None
