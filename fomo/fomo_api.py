"""
fomo_api.py — async client for FOMO's private app API (prod-api.fomo.family).

Runs on a residential IP (borz). Cloudflare 403s every datacenter IP, so this
will NOT work from the Vultr VPS — see FOMO_API.md §1. NOTE: residential is
necessary but may not be sufficient; CF also scores TLS fingerprint and browser
headers. describe_403() classifies which kind of 403 you actually got.

Auth is a Privy access JWT with a 60-minute lifetime, minted from a refresh
token. Privy ROTATES the refresh token on every use, so the rotated value is
persisted to disk (FOMO_SESSION_FILE) and reused on the next start.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from fomo_chains import SUPPORTED_CHAINS_HEADER
from fomo_hodlers import holders_query

log = logging.getLogger("fomo")

API_BASE = "https://prod-api.fomo.family"
DEXSCREENER_API = "https://api.dexscreener.com"
PRIVY_SESSIONS_URL = "https://auth.privy.io/api/v1/sessions"
PRIVY_APP_ID = os.getenv("PRIVY_APP_ID", "cm6h485o300n3zj9yl6vpedq7")

# The WAF wants requests that look like they came from the web app.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
BROWSERISH = {
    "User-Agent": UA,
    "Origin": "https://fomo.family",
    "Referer": "https://fomo.family/",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua-platform": '"Windows"',
    "x-supported-chains": SUPPORTED_CHAINS_HEADER,
}

# Cloudflare fingerprints in a 403 body. A WAF block is HTML and carries cf-ray;
# an app-level 403 is our usual JSON envelope. Telling them apart matters: the
# first means "this client looks like a bot", the second means "your token is
# wrong / you lack permission" — opposite fixes.
_CF_BODY_MARKERS = (
    "cf-error-details",
    "cf-browser-verification",
    "__cf_chl",
    "Attention Required",
    "Just a moment",
    "cloudflare",
    "Enable JavaScript and cookies",
)


def describe_403(path: str, body: str, headers: dict[str, str]) -> str:
    """
    Build an actionable message for a 403 instead of assuming it is an IP block.

    Cloudflare tells on itself: `cf-ray` is present on every edge response, and
    `cf-mitigated: challenge` marks a bot-management block specifically.
    """
    cf_ray = headers.get("cf-ray")
    cf_mitigated = headers.get("cf-mitigated")
    server = (headers.get("server") or "").lower()
    ctype = (headers.get("content-type") or "").lower()

    # An app-level 403 comes back as the normal {success, message, ...} envelope.
    # Check this FIRST: the whole site is behind Cloudflare, so `server: cloudflare`
    # and `cf-ray` are present on *every* response including ordinary app errors.
    # Only an HTML challenge page or an explicit cf-mitigated means the WAF ate it.
    app_message = None
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and ("success" in parsed or "message" in parsed):
        app_message = parsed.get("message") or parsed.get("responseObject")

    body_looks_cf = any(m.lower() in body.lower() for m in _CF_BODY_MARKERS)
    is_cf = app_message is None and (
        bool(cf_mitigated) or "html" in ctype or body_looks_cf
    )

    bits = [f"403 from {path}"]
    if is_cf:
        bits.append(
            "blocked by Cloudflare's WAF, NOT by the app. A residential IP alone is "
            "not enough — CF also scores TLS/JA3 fingerprint, sec-fetch-*/sec-ch-ua "
            "headers and the cf_clearance cookie, none of which a bare aiohttp "
            "client has. See FOMO_API.md §1."
        )
    elif app_message:
        bits.append(f"app-level refusal: {str(app_message)[:200]}")
    else:
        bits.append("origin refused it (no Cloudflare markers found)")

    detail = []
    if cf_ray:
        detail.append(f"cf-ray={cf_ray}")
    if cf_mitigated:
        detail.append(f"cf-mitigated={cf_mitigated}")
    if server:
        detail.append(f"server={server}")
    if ctype:
        detail.append(f"content-type={ctype.split(';')[0]}")
    if detail:
        bits.append(" ".join(detail))

    snippet = " ".join(body.split())[:300]
    if snippet:
        bits.append(f"body: {snippet}")

    msg = " | ".join(bits)
    log.warning("403 detail — %s", msg)
    return msg


SESSION_FILE = Path(os.getenv("FOMO_SESSION_FILE", ".fomo_session.json"))

# "browser" drives a real Chrome (beats Cloudflare, no Privy juggling);
# "http" is the old aiohttp path, kept for the day the WAF rule relaxes.
TRANSPORT = os.getenv("FOMO_TRANSPORT", "browser").strip().lower()


class FomoError(RuntimeError):
    pass


class FomoNotFound(FomoError):
    pass


class FomoBlocked(FomoError):
    """A 403. Could be Cloudflare's WAF or the app itself — see describe_403()."""


class FomoAuthError(FomoError):
    pass


@dataclass
class FomoUser:
    """The 26-key user object from /v2/users/userHandle/{handle}."""

    raw: dict[str, Any] = field(repr=False)

    @property
    def id(self) -> str: return self.raw["id"]
    @property
    def handle(self) -> str: return self.raw.get("userHandle") or ""
    @property
    def display_name(self) -> str: return self.raw.get("displayName") or self.handle
    @property
    def description(self) -> str | None: return self.raw.get("description") or None
    @property
    def sol_address(self) -> str | None: return self.raw.get("address")
    @property
    def evm_address(self) -> str | None: return self.raw.get("evmAddress")
    @property
    def twitter(self) -> str | None: return self.raw.get("twitter")
    @property
    def avatar(self) -> str | None: return self.raw.get("profilePictureLink")
    @property
    def followers(self) -> int: return self.raw.get("followers") or 0
    @property
    def following(self) -> int: return self.raw.get("following") or 0
    @property
    def swap_count(self) -> int: return self.raw.get("swapCount") or 0
    @property
    def num_trades(self) -> int: return self.raw.get("numTrades") or 0
    @property
    def total_volume(self) -> float: return self.raw.get("totalVolume") or 0.0
    @property
    def avg_hold_seconds(self) -> int | None: return self.raw.get("averageHoldTimeSeconds")
    @property
    def created_at(self) -> str | None: return self.raw.get("createdAt")
    @property
    def is_private(self) -> bool: return bool(self.raw.get("private"))
    @property
    def is_restricted(self) -> bool: return bool(self.raw.get("isRestricted"))
    @property
    def clan_name(self) -> str | None:
        clan = self.raw.get("clan") or {}
        return clan.get("name")
    @property
    def clan_icon(self) -> str | None:
        clan = self.raw.get("clan") or {}
        return clan.get("iconLink")

    @property
    def profile_url(self) -> str:
        return f"https://fomo.family/profile/{self.handle}"

    # Present only on the /leaderboard variant of the object.
    def rank(self, period: str = "") -> dict[str, Any] | None:
        key = {"": "rank", "24h": "rank24h", "7d": "rank7d", "30d": "rank30d"}[period]
        val = self.raw.get(key)
        return val if isinstance(val, dict) and val else None


class FomoClient:
    """
    Usage:
        async with FomoClient(refresh_token=...) as fomo:
            user = await fomo.user_by_handle("Binkieee")   # includes ranks
    """

    def __init__(
        self,
        refresh_token: str | None = None,
        access_token: str | None = None,
        app_id: str = PRIVY_APP_ID,
        session_file: Path = SESSION_FILE,
        cache_ttl: float = 60.0,
        transport: str = TRANSPORT,
    ) -> None:
        self._app_id = app_id
        self._session_file = session_file
        self._cache_ttl = cache_ttl
        self._transport = transport
        self._browser: Any = None

        stored = self._load_session()
        self._refresh_token = refresh_token or stored.get("refresh_token")
        self._access_token = access_token or stored.get("access_token")
        self._access_exp: float = float(stored.get("access_exp") or 0)

        # In browser mode the page's own live Privy token is used, read fresh
        # from localStorage on every call — no refresh, no rotation race.
        if transport != "browser" and not self._refresh_token and not self._access_token:
            raise FomoAuthError(
                "No FOMO credentials. Set FOMO_PRIVY_REFRESH_TOKEN in .env "
                "(localStorage key 'privy:refresh_token' on fomo.family)."
            )

        self._http: aiohttp.ClientSession | None = None
        self._auth_lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}

    # ---------------- lifecycle ----------------

    async def __aenter__(self) -> "FomoClient":
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20), headers=BROWSERISH
        )
        if self._transport == "browser":
            from fomo_browser import BrowserTransport

            self._browser = BrowserTransport()
            await self._browser.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._browser:
            try:
                await self._browser.close()
            finally:
                self._browser = None
        if self._http:
            await self._http.close()
            self._http = None

    # ---------------- session persistence ----------------

    def _load_session(self) -> dict[str, Any]:
        try:
            return json.loads(self._session_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save_session(self) -> None:
        try:
            self._session_file.write_text(
                json.dumps(
                    {
                        "refresh_token": self._refresh_token,
                        "access_token": self._access_token,
                        "access_exp": self._access_exp,
                    },
                    indent=1,
                )
            )
            os.chmod(self._session_file, 0o600)
        except OSError as exc:  # non-fatal: we just re-auth next start
            log.warning("could not persist FOMO session: %s", exc)

    # ---------------- auth ----------------

    @staticmethod
    def _jwt_exp(token: str) -> float:
        import base64

        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
        except Exception:
            return time.time() + 300  # unknown -> assume short

    async def _ensure_token(self, force: bool = False) -> str:
        async with self._auth_lock:
            fresh = self._access_token and time.time() < self._access_exp - 90
            if fresh and not force:
                return self._access_token  # type: ignore[return-value]
            if not self._refresh_token:
                raise FomoAuthError(
                    "Access token expired and no refresh token available. "
                    "Re-copy 'privy:refresh_token' from the browser."
                )
            await self._refresh()
            return self._access_token  # type: ignore[return-value]

    async def _refresh(self) -> None:
        assert self._http is not None, "use FomoClient as an async context manager"
        headers = {"Content-Type": "application/json", "privy-app-id": self._app_id}
        async with self._http.post(
            PRIVY_SESSIONS_URL, headers=headers,
            json={"refresh_token": self._refresh_token},
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise FomoAuthError(
                    f"Privy refresh failed ({resp.status}): {body[:200]}. "
                    "The refresh token was probably rotated out from under us — "
                    "log in on fomo.family and re-copy it."
                )
            data = json.loads(body)

        token = data.get("token") or data.get("access_token")
        if not token:
            raise FomoAuthError(f"Privy returned no access token: {list(data)}")
        self._access_token = token
        self._access_exp = self._jwt_exp(token)
        # Privy rotates the refresh token — keep the new one or we lock ourselves out.
        self._refresh_token = data.get("refresh_token") or self._refresh_token
        self._save_session()
        log.info("refreshed FOMO token, valid until %s",
                 time.strftime("%H:%M:%S", time.localtime(self._access_exp)))

    # ---------------- transport ----------------

    async def _get(
        self,
        path: str,
        *,
        cache: bool = True,
        _retry: bool = True,
        lane: str = "foreground",
    ) -> Any:
        assert self._http is not None, "use FomoClient as an async context manager"

        if cache and (hit := self._cache.get(path)) and hit[0] > time.time():
            return hit[1]

        if self._browser is not None:
            status, body, resp_headers = await self._browser.get(
                API_BASE + path, lane=lane
            )
        else:
            token = await self._ensure_token()
            async with self._http.get(
                API_BASE + path, headers={"Authorization": f"Bearer {token}"}
            ) as resp:
                body = await resp.text()
                status = resp.status
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}

        return await self._decode_response(
            path, status, body, resp_headers,
            cache=cache, retry=_retry, lane=lane,
        )

    async def _decode_response(
        self,
        path: str,
        status: int,
        body: str,
        resp_headers: dict[str, str],
        *,
        cache: bool,
        retry: bool,
        lane: str,
    ) -> Any:
        """Apply auth, envelope and cache semantics to one transport response."""

        if status == 403:
            raise FomoBlocked(describe_403(path, body, resp_headers))
        if status == 401 and retry:
            if self._browser is not None:
                # Nothing for us to refresh — the app owns the token. Reload the
                # page so the SPA mints a new one, then try once more.
                log.info("401 from FOMO, reloading the app page to re-mint the token")
                await self._browser.reload()
                return await self._get(
                    path, cache=cache, _retry=False, lane=lane
                )
            log.info("401 from FOMO, forcing token refresh")
            await self._ensure_token(force=True)
            return await self._get(
                path, cache=cache, _retry=False, lane=lane
            )

        try:
            data = json.loads(body)
        except ValueError:
            raise FomoError(f"{status} non-JSON from {path}: {body[:200]}") from None

        if status == 404:
            raise FomoNotFound(data.get("message") or "not found")
        if status == 401:
            raise FomoAuthError(data.get("message") or "unauthorized")
        if status >= 400 or not data.get("success"):
            detail = data.get("responseObject") or data.get("message")
            raise FomoError(f"{status} from {path}: {str(detail)[:300]}")

        obj = data.get("responseObject")
        if cache:
            self._cache[path] = (time.time() + self._cache_ttl, obj)
        return obj

    async def _get_many(
        self, paths: tuple[str, ...], *, lane: str = "foreground"
    ) -> tuple[Any, ...]:
        """Apply normal cache/auth semantics to one parallel browser batch."""
        results: dict[str, Any] = {}
        missing: list[str] = []
        now = time.time()
        for path in paths:
            hit = self._cache.get(path)
            if hit and hit[0] > now:
                results[path] = hit[1]
            else:
                missing.append(path)

        if missing and self._browser is not None:
            urls = [API_BASE + path for path in missing]
            try:
                responses = await self._browser.get_many(urls, lane=lane)
            except Exception as exc:
                responses = {url: exc for url in urls}
            for path, url in zip(missing, urls):
                response = responses.get(url)
                if isinstance(response, Exception):
                    results[path] = response
                    continue
                if not isinstance(response, tuple):
                    results[path] = FomoError(f"no browser response for {path}")
                    continue
                status, body, headers = response
                try:
                    results[path] = await self._decode_response(
                        path, status, body, headers,
                        cache=True, retry=True, lane=lane,
                    )
                except Exception as exc:
                    results[path] = exc
        elif missing:
            fetched = await asyncio.gather(
                *(self._get(path, lane=lane) for path in missing),
                return_exceptions=True,
            )
            results.update(zip(missing, fetched))

        return tuple(results.get(path) for path in paths)  # type: ignore[return-value]

    async def profile_panels(self, user_id: str) -> tuple[Any, Any, Any, Any]:
        """Fetch the four `/fomo` panels in one parallel in-browser batch."""
        paths = (
            f"/v2/users/{user_id}/balances",
            f"/v2/users/{user_id}/spotlight",
            f"/trades?{urlencode({'userId': user_id})}",
            f"/v2/users/{user_id}/swaps?limit=50",
        )
        return await self._get_many(paths)  # type: ignore[return-value]

    async def token_holders(
        self, address: str, network_id: int, *, background: bool = False
    ) -> Any:
        """FOMO's own top-holder list -- what the token page's Holders tab calls.

        Spelled `hodlers`. Rows carry the full user object plus that trader's
        position, entry, PnL and hold time; see `fomo_hodlers.py`.
        """
        return await self._get(
            holders_query(address, network_id),
            lane="background" if background else "foreground",
        )

    async def trade_details(
        self, trade_ids: list[str], *, background: bool = True
    ) -> tuple[Any, ...]:
        """Fetch immutable trade histories together for wallet discovery."""
        paths = tuple(f"/trades/{trade_id}" for trade_id in dict.fromkeys(trade_ids)
                      if trade_id)
        if not paths:
            return ()
        return await self._get_many(
            paths, lane="background" if background else "foreground"
        )

    # ---------------- public API ----------------

    async def user_by_handle(self, handle: str, with_ranks: bool = True) -> FomoUser:
        """
        Handle lookup. Case-insensitive. Raises FomoNotFound for unknown handles.

        With with_ranks=True this costs a second call to /v2/users/{id}/leaderboard,
        which returns the same user object plus rank/rank24h/rank7d/rank30d.
        """
        handle = handle.lstrip("@").strip()
        if not handle:
            raise FomoNotFound("empty handle")
        obj = await self._get(f"/v2/users/userHandle/{handle}")
        if not with_ranks:
            return FomoUser(obj)
        try:
            ranked = await self._get(f"/v2/users/{obj['id']}/leaderboard")
            return FomoUser({**obj, **ranked})
        except FomoError as exc:  # ranks are a nice-to-have, never fatal
            log.warning("rank lookup failed for %s: %s", handle, exc)
            return FomoUser(obj)

    async def user_by_id(self, user_id: str) -> FomoUser:
        return FomoUser(await self._get(f"/v2/users/{user_id}"))

    async def search(self, term: str, limit: int = 5) -> list[FomoUser]:
        """Fuzzy handle/display-name search. Note the param is searchTerm, not query."""
        obj = await self._get(f"/v2/users/fuzzy-search?searchTerm={term}&limit={limit}")
        return [FomoUser(u) for u in (obj or {}).get("users", [])]

    async def resolve(self, term: str) -> FomoUser:
        """Exact handle first, then fall back to the top fuzzy-search hit."""
        try:
            return await self.user_by_handle(term)
        except FomoNotFound:
            hits = await self.search(term, limit=1)
            if not hits:
                raise
            return await self.user_by_handle(hits[0].handle)

    async def swaps(self, user_id: str, limit: int = 10,
                    fresh: bool = False,
                    background: bool = False) -> dict[str, Any]:
        """{'swaps': [...], 'hasNextPage': bool}. Pagination cursor is still unknown."""
        return await self._get(f"/v2/users/{user_id}/swaps?limit={limit}",
                               cache=not fresh,
                               lane="background" if background else "foreground")

    async def balances(self, user_id: str) -> dict[str, Any]:
        return await self._get(f"/v2/users/{user_id}/balances")

    async def spotlight(self, user_id: str) -> dict[str, Any]:
        return await self._get(f"/v2/users/{user_id}/spotlight")

    async def trades(self, user_id: str, order_by: str | None = None,
                     fresh: bool = False,
                     background: bool = False) -> dict[str, Any]:
        """Active/closed trades; no order keeps the API's recent-trade order."""
        query = {"userId": user_id}
        if order_by:
            query["orderBy"] = order_by
        return await self._get(
            f"/trades?{urlencode(query)}",
            cache=not fresh,
            lane="background" if background else "foreground",
        )

    async def token_market_data(
        self, tokens: list[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Current price/market cap for buy-time market-cap reconstruction."""
        if self._http is None:
            return {}
        chain_ids = {
            "Solana": "solana",
            "Base": "base",
            "BSC": "bsc",
            "Ethereum": "ethereum",
            "Robinhood": "robinhood",
        }
        grouped: dict[str, list[str]] = {}
        for chain, address in tokens:
            if chain in chain_ids and address:
                values = grouped.setdefault(chain, [])
                if address.lower() not in {item.lower() for item in values}:
                    values.append(address)

        async def fetch(chain: str, addresses: list[str]) -> tuple[str, Any]:
            encoded = ",".join(quote(address, safe="") for address in addresses[:30])
            url = f"{DEXSCREENER_API}/tokens/v1/{chain_ids[chain]}/{encoded}"
            try:
                async with self._http.get(url) as response:
                    if response.status != 200:
                        return chain, []
                    return chain, await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return chain, []

        responses = await asyncio.gather(
            *(fetch(chain, addresses) for chain, addresses in grouped.items())
        )
        result: dict[tuple[str, str], dict[str, Any]] = {}
        requested = {(chain, address.lower()) for chain, address in tokens}
        scores: dict[tuple[str, str], float] = {}
        for chain, pairs in responses:
            for pair in pairs if isinstance(pairs, list) else []:
                if not isinstance(pair, dict):
                    continue
                base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
                quote_token = pair.get("quoteToken") \
                    if isinstance(pair.get("quoteToken"), dict) else {}
                base_address = str(base.get("address") or "").lower()
                quote_address = str(quote_token.get("address") or "").lower()
                base_key = (chain, base_address)
                quote_key = (chain, quote_address)
                if base_key in requested:
                    key = base_key
                    market_cap = pair.get("marketCap")
                    fdv = pair.get("fdv")
                    price_usd = pair.get("priceUsd")
                elif quote_key in requested:
                    key = quote_key
                    market_cap = None
                    fdv = None
                    try:
                        price_usd = float(pair.get("priceUsd")) / float(pair.get("priceNative"))
                    except (TypeError, ValueError, ZeroDivisionError):
                        continue
                else:
                    continue
                liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
                try:
                    score = float(liquidity.get("usd") or 0)
                except (TypeError, ValueError):
                    score = 0.0
                if key not in result or score > scores[key]:
                    result[key] = {
                        "marketCap": market_cap,
                        "fdv": fdv,
                        "priceUsd": price_usd,
                    }
                    scores[key] = score
        return result

    async def leaderboard(self, period: str | None = None, limit: int = 10) -> list[FomoUser]:
        """period: None (all-time) or '24h'. limit is REQUIRED by the API."""
        path = f"/v2/leaderboard/{period}?limit={limit}" if period else f"/v2/leaderboard?limit={limit}"
        obj = await self._get(path)
        return [FomoUser(u) for u in (obj or {}).get("leaderboard", [])]


# ---------------- formatting helpers ----------------

def fmt_usd(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    v = abs(float(value))
    for cutoff, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= cutoff:
            return f"{sign}${v / cutoff:.2f}{suffix}"
    return f"{sign}${v:,.2f}"


def fmt_count(value: int | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    for cutoff, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= cutoff:
            return f"{v / cutoff:.1f}{suffix}".replace(".0", "")
    return f"{int(v):,}"


def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "—"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def short_addr(addr: str | None, head: int = 4, tail: int = 4) -> str:
    if not addr:
        return "—"
    return addr if len(addr) <= head + tail + 3 else f"{addr[:head]}…{addr[-tail:]}"
