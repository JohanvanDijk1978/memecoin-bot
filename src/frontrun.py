"""
frontrun.py
───────────
Thin async client for the Frontrun.pro paid data API (api.frontrun.pro).

Docs: https://turquoise-earl-bf5.notion.site/Frontrun-API-2428bbcdc225806186dfe420734fad66

Why this module is defensive
────────────────────────────
Frontrun bills in CREDITS, not requests, and the calls we care about are the
expensive ones:

    associated-wallets   400 credits
    mentioned-wallets    500 credits
    wallets-batch-query  100 credits per MATCHED wallet, 5 per unmatched
    smart-followers/count  3 credits

The Extension Gold plan includes 100,000 credits/month — that is only ~250
associated-wallets lookups. A single impatient user spamming /wallet could burn
a month of budget in an afternoon. So every read goes through an on-disk cache
first, and the cache is written even for empty results (a handle with no
wallets today still costs 400 credits to re-ask).

Everything degrades to a clean "not configured" instead of raising when
FRONTRUN_API_KEY is absent, so the bot boots fine without it.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

API_KEY = os.getenv("FRONTRUN_API_KEY", "").strip()
BASE_URL = "https://api.frontrun.pro/api/v1"

# fomo.family's own API. Unofficial, sits behind Cloudflare, and is only used
# as a fallback when a Fomo username has no linked X account. Off by default —
# set FOMO_API_ENABLED=1 in .env to try it.
FOMO_API_ENABLED = os.getenv("FOMO_API_ENABLED", "").strip() in ("1", "true", "yes")
FOMO_BASE_URL = "https://prod-api.fomo.family/v2"

CACHE_FILE = "data/frontrun_cache.json"

# Documented default limits: 60 req/min, 100k req/day. We sit well under.
_RPM = 50

# Cache TTLs, in seconds. Wallet↔identity mappings barely change, so these are
# deliberately long — the constraint is credits, not freshness.
TTL_ASSOCIATED = 7 * 86400   # 400 credits a pop
TTL_MENTIONED = 7 * 86400    # 500 credits a pop
TTL_BATCH = 3 * 86400        # 100 credits per matched wallet
TTL_SMART_COUNT = 86400      # 3 credits, cheap, but no reason to re-ask hourly

_SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def is_configured() -> bool:
    return bool(API_KEY)


def looks_like_address(text: str) -> Optional[str]:
    """Return 'SOLANA' / 'EVM' if `text` is shaped like an on-chain address,
    else None.

    NOTE: a Solana token mint (CA) and a Solana wallet are the SAME format —
    base58, 32-44 chars. There is no way to tell them apart from the string
    alone. Callers should send it to Frontrun and treat "no match" as
    "probably a token CA, not a wallet". An unmatched lookup costs 5 credits,
    so guessing wrong here is cheap.
    """
    text = (text or "").strip()
    if _EVM_RE.match(text):
        return "EVM"
    if _SOL_RE.match(text):
        return "SOLANA"
    return None


def clean_handle(text: str) -> str:
    """Normalise an X / Fomo handle: strip @, URL wrappers, trailing slashes."""
    h = (text or "").strip()
    for prefix in (
        "https://x.com/", "https://twitter.com/",
        "http://x.com/", "http://twitter.com/",
        "x.com/", "twitter.com/",
        "https://fomo.family/", "fomo.family/",
    ):
        if h.lower().startswith(prefix):
            h = h[len(prefix):]
            break
    h = h.split("?")[0].split("/")[0]
    return h.lstrip("@").strip()


# ── Rate limiting ─────────────────────────────────────────────────────────
class _RateLimiter:
    def __init__(self, calls_per_minute: int):
        self.interval = 60.0 / max(calls_per_minute, 1)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            elapsed = time.time() - self._last
            if elapsed < self.interval:
                await asyncio.sleep(self.interval - elapsed)
            self._last = time.time()


_limiter = _RateLimiter(_RPM)


# ── Cache ─────────────────────────────────────────────────────────────────
_cache: Optional[Dict[str, Any]] = None
_cache_lock = asyncio.Lock()


def _load_cache() -> Dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(CACHE_FILE, "r") as f:
            _cache = json.load(f)
    except Exception:
        _cache = {}
    return _cache


def _cache_get(key: str, ttl: int) -> Optional[Any]:
    entry = _load_cache().get(key)
    if not isinstance(entry, dict):
        return None
    if time.time() - entry.get("ts", 0) > ttl:
        return None
    return entry.get("value")


async def _cache_put(key: str, value: Any) -> None:
    async with _cache_lock:
        cache = _load_cache()
        cache[key] = {"ts": time.time(), "value": value}
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            tmp = CACHE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cache, f)
            os.replace(tmp, CACHE_FILE)
        except Exception as e:
            logger.warning(f"frontrun: cache write failed: {e}")


# ── HTTP ──────────────────────────────────────────────────────────────────
def _headers() -> Dict[str, str]:
    return {
        "accept": "application/json",
        "Authorization": f"Bearer {API_KEY}",
        "X-Copilot-Client-Language": "en",
        "X-Copilot-Client-Platform": "CHROME_EXTENSION",
        "X-Copilot-Client-Version": "1.0.0",
    }


async def _request(method: str, path: str, payload: Optional[dict] = None,
                   timeout: int = 20) -> Tuple[Any, Optional[str]]:
    """One Frontrun call.

    Returns `(data, None)` on success or `(None, reason)` on failure. The two
    are kept distinct on purpose: an empty result and a failed call look
    identical downstream otherwise, which makes a broken key or a changed
    response shape indistinguishable from "this handle has no wallets" — and
    it means we'd cache a failure as if it were an answer.

    Never raises. A dead API must not take a command handler down.
    """
    if not API_KEY:
        return None, "FRONTRUN_API_KEY not set"

    url = f"{BASE_URL}{path}"
    await _limiter.wait()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, url,
                headers=_headers(),
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text()
                if resp.status in (401, 403):
                    logger.error(f"frontrun: auth rejected ({resp.status}) on {path}: {text[:300]}")
                    return None, f"auth rejected (HTTP {resp.status}) — check FRONTRUN_API_KEY"
                if resp.status == 402:
                    logger.error(f"frontrun: out of credits on {path}: {text[:300]}")
                    return None, "out of credits (HTTP 402)"
                if resp.status == 404:
                    logger.warning(f"frontrun: 404 on {path}: {text[:300]}")
                    return None, "not found (HTTP 404) — handle unknown to Frontrun"
                if resp.status == 429:
                    logger.warning(f"frontrun: rate limited on {path}")
                    return None, "rate limited (HTTP 429) — try again shortly"
                if resp.status != 200:
                    logger.warning(f"frontrun: HTTP {resp.status} on {path}: {text[:300]}")
                    return None, f"HTTP {resp.status}"
                body = json.loads(text)
    except asyncio.TimeoutError:
        logger.warning(f"frontrun: timeout on {path}")
        return None, f"timeout after {timeout}s"
    except Exception as e:
        logger.warning(f"frontrun: request failed on {path}: {e}")
        return None, f"request failed: {type(e).__name__}"

    if not isinstance(body, dict):
        logger.warning(f"frontrun: non-dict body on {path}: {str(body)[:300]}")
        return None, "unexpected response body"
    if body.get("status") is False:
        logger.warning(f"frontrun: API error on {path}: {body.get('message')}")
        return None, f"API error: {body.get('message')}"

    data = body.get("data")
    # A 200 whose payload we can't find is a shape change, not an empty result.
    # Log the whole envelope — that's the only way to fix the parser.
    if data is None and "data" not in body:
        logger.warning(f"frontrun: no 'data' key on {path}; body={json.dumps(body)[:600]}")
        return None, "response had no 'data' field"
    return data, None


def _extract_wallets(data: Any, _depth: int = 0) -> List[dict]:
    """Pull the wallet list out of a response.

    The docs publish sample JSON for smart-followers and high-pnl but not for
    associated-wallets / mentioned-wallets, so the exact key is unconfirmed.
    We accept a bare list or any of the plausible wrapper keys rather than
    hard-coding a guess that silently returns nothing.
    """
    if data is None:
        return []
    if isinstance(data, list):
        return [w for w in data if isinstance(w, dict)]
    if isinstance(data, dict):
        # A single wallet object returned bare.
        if "address" in data:
            return [data]
        for key in ("wallets", "associatedWallets", "mentionedWallets",
                    "associated_wallets", "mentioned_wallets",
                    "list", "items", "results", "records", "rows", "data"):
            val = data.get(key)
            if isinstance(val, list):
                return [w for w in val if isinstance(w, dict)]
            # One more level of nesting, e.g. {"data": {"wallets": [...]}}.
            if isinstance(val, dict):
                nested = _extract_wallets(val, _depth + 1)
                if nested:
                    return nested
        # Grouped by chain: {"SOLANA": [...], "EVM": [...]}.
        grouped: List[dict] = []
        for key, val in data.items():
            if isinstance(val, list) and val and isinstance(val[0], dict) \
                    and "address" in val[0]:
                for w in val:
                    if isinstance(w, dict):
                        grouped.append({"chain": key.upper(), **w})
        if grouped:
            return grouped
        # Last resort: recurse into any remaining dict value. The real response
        # shape for associated-wallets isn't published, so rather than fail on
        # an unexpected wrapper key we go looking for anything with an
        # `address` field. Depth-limited to avoid pathological nesting.
        if _depth < 4:
            for val in data.values():
                if isinstance(val, (dict, list)):
                    found = _extract_wallets(val, _depth + 1)
                    if found:
                        return found
    return []


def has_fomo_tag(wallet: dict) -> bool:
    """True if Frontrun tagged this wallet as trading via fomo.family."""
    for tag in wallet.get("tags") or []:
        name = tag.get("name") if isinstance(tag, dict) else tag
        if str(name or "").strip().upper() == "FOMO":
            return True
    return False


# ── Public API ────────────────────────────────────────────────────────────
async def associated_wallets(handle: str, use_cache: bool = True
                             ) -> Tuple[List[dict], Optional[str]]:
    """Wallets publicly linked to an X account. 400 credits — always cached.

    Returns `(wallets, error)`. On error the result is NOT cached, so a
    transient failure can't poison the handle for the next 7 days.
    """
    handle = clean_handle(handle)
    if not handle:
        return [], "empty handle"
    key = f"assoc:{handle.lower()}"
    if use_cache:
        cached = _cache_get(key, TTL_ASSOCIATED)
        if cached is not None:
            return cached, None

    data, err = await _request("GET", f"/pro/twitter/{handle}/associated-wallets")
    if err:
        return [], err
    wallets = _extract_wallets(data)
    if not wallets and data:
        # 200 with a payload we couldn't parse. Don't cache — log the shape so
        # the extractor can be fixed against real data.
        logger.warning(
            f"frontrun: associated-wallets for {handle} returned unparsed data: "
            f"{json.dumps(data)[:600]}"
        )
        return [], "response shape not recognised (logged for debugging)"
    await _cache_put(key, wallets)
    return wallets, None


async def mentioned_wallets(handle: str, use_cache: bool = True
                            ) -> Tuple[List[dict], Optional[str]]:
    """Wallets referenced in the account's tweets. 500 credits — always cached."""
    handle = clean_handle(handle)
    if not handle:
        return [], "empty handle"
    key = f"mention:{handle.lower()}"
    if use_cache:
        cached = _cache_get(key, TTL_MENTIONED)
        if cached is not None:
            return cached, None

    data, err = await _request("GET", f"/pro/twitter/{handle}/mentioned-wallets")
    if err:
        return [], err
    wallets = _extract_wallets(data)
    if not wallets and data:
        logger.warning(
            f"frontrun: mentioned-wallets for {handle} returned unparsed data: "
            f"{json.dumps(data)[:600]}"
        )
        return [], "response shape not recognised (logged for debugging)"
    await _cache_put(key, wallets)
    return wallets, None


async def wallets_batch_query(addresses: List[str], chain: Optional[str] = None,
                              use_cache: bool = True
                              ) -> Tuple[List[dict], Optional[str]]:
    """Label lookup for on-chain addresses.

    100 credits per MATCHED wallet, 5 per unmatched — so passing a token CA by
    mistake costs almost nothing. `chain` is auto-detected per address when not
    given ('SOLANA' or 'EVM').
    """
    wanted = [a.strip() for a in addresses if a and a.strip()]
    if not wanted:
        return [], None

    results: List[dict] = []
    to_fetch: List[dict] = []
    for addr in wanted:
        detected = chain or looks_like_address(addr)
        if not detected:
            continue
        if use_cache:
            cached = _cache_get(f"wallet:{addr}", TTL_BATCH)
            if cached is not None:
                if cached:
                    results.append(cached)
                continue
        to_fetch.append({"chain": detected, "address": addr})

    if to_fetch:
        data, err = await _request(
            "POST", "/pro/twitter/wallets-batch-query",
            payload={"wallets": to_fetch},
        )
        if err:
            return results, err
        fetched = _extract_wallets(data)
        by_addr = {str(w.get("address", "")): w for w in fetched}
        for item in to_fetch:
            addr = item["address"]
            hit = by_addr.get(addr)
            # Cache misses too — otherwise every unmatched CA re-bills 5 credits.
            await _cache_put(f"wallet:{addr}", hit or {})
            if hit:
                results.append(hit)

    return results, None


async def smart_follower_count(handle: str, use_cache: bool = True) -> Optional[int]:
    """Smart-follower count for a KOL. 3 credits.

    Returns None when the API says the count isn't final yet (`resolved: false`)
    so callers can render 'pending' rather than a misleading number.
    """
    handle = clean_handle(handle)
    if not handle:
        return None
    key = f"sfcount:{handle.lower()}"
    if use_cache:
        cached = _cache_get(key, TTL_SMART_COUNT)
        if cached is not None:
            return cached

    data, err = await _request("GET", f"/pro/twitter/{handle}/smart-followers/count")
    if err or not isinstance(data, dict):
        return None
    meta = data.get("meta") or {}
    if meta.get("resolved") is False:
        return None
    count = data.get("totalCount")
    if count is None:
        return None
    await _cache_put(key, count)
    return count


async def credits_remaining() -> Optional[dict]:
    """Current credit balance. Not cached — the whole point is a live figure."""
    if not API_KEY:
        return None
    data, err = await _request("GET", f"/user/paid-api/points/{API_KEY}")
    return None if err else data


def clear_cache(handle_or_address: str = "") -> int:
    """Drop cached entries so the next lookup re-queries Frontrun.

    With no argument, clears everything. Returns the number of entries removed.
    Costs credits on the next lookup — that's the point.
    """
    global _cache
    cache = _load_cache()
    needle = clean_handle(handle_or_address).lower()
    if needle:
        doomed = [k for k in cache if needle in k.lower()]
    else:
        doomed = list(cache)
    for k in doomed:
        cache.pop(k, None)
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        logger.warning(f"frontrun: cache clear failed: {e}")
    return len(doomed)


async def fomo_profile(username: str) -> Optional[dict]:
    """fomo.family profile lookup — fallback for Fomo users with no linked X.

    Unofficial and Cloudflare-protected; disabled unless FOMO_API_ENABLED=1.
    Returns None on any failure, including a Cloudflare challenge.
    """
    if not FOMO_API_ENABLED:
        return None
    username = clean_handle(username)
    if not username:
        return None

    key = f"fomo:{username.lower()}"
    cached = _cache_get(key, TTL_ASSOCIATED)
    if cached is not None:
        return cached or None

    url = f"{FOMO_BASE_URL}/users/{username}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={
                    "accept": "application/json",
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"
                    ),
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.info(f"fomo: HTTP {resp.status} for {username}")
                    return None
                body = await resp.json(content_type=None)
    except Exception as e:
        logger.info(f"fomo: lookup failed for {username}: {e}")
        return None

    if not isinstance(body, dict):
        return None
    profile = body.get("data") if isinstance(body.get("data"), dict) else body
    await _cache_put(key, profile)
    return profile


def fomo_addresses(profile: dict) -> List[dict]:
    """Best-effort extraction of wallet addresses from a fomo.family profile.

    The response shape is unverified (Cloudflare blocks anonymous probes), so
    this walks the known-plausible keys instead of assuming one.
    """
    if not isinstance(profile, dict):
        return []
    out: List[dict] = []
    for key, chain in (
        ("solanaAddress", "SOLANA"), ("solana_address", "SOLANA"),
        ("solAddress", "SOLANA"), ("walletAddress", "SOLANA"),
        ("evmAddress", "EVM"), ("evm_address", "EVM"),
        ("ethAddress", "EVM"),
    ):
        val = profile.get(key)
        if isinstance(val, str) and looks_like_address(val):
            out.append({"chain": chain, "address": val})

    for key in ("wallets", "addresses", "linkedWallets"):
        val = profile.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and looks_like_address(item):
                    out.append({"chain": looks_like_address(item), "address": item})
                elif isinstance(item, dict):
                    addr = item.get("address") or item.get("publicKey")
                    if isinstance(addr, str) and looks_like_address(addr):
                        out.append({
                            "chain": item.get("chain") or looks_like_address(addr),
                            "address": addr,
                        })

    seen, deduped = set(), []
    for w in out:
        if w["address"] not in seen:
            seen.add(w["address"])
            deduped.append(w)
    return deduped
