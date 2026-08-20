"""
pump_profiles.py -- wallet <-> Pump.fun profile, cached.

What `/fomo` had to earn, `/pump` is given
-----------------------------------------

FOMO publishes four synthetic addresses per trader and none of them is the
trading wallet, so `fomo_wallet.py` has to *find* the wallet on chain -- a
sponsor index, a mint scan, sometimes a block scan -- and the result is cached
forever because it was proved, not asserted.

Pump.fun is the opposite shape. **The profile IS the wallet**: Pump keys a
profile by its Solana address, `GET /users/{address}` and `GET /users/{name}`
both return the same record, and `address` on that record is the canonical
identifier (session 23 already made every profile URL use it). So there is no
discovery stage, no corroboration gate and no ambiguity to reject. The whole
cost of `/pump` identity is one HTTP request per wallet.

Which is exactly why it needed a cache. `/token` renders up to ten holder rows
and every Solana row called `PumpClient.resolve()` live, on every invocation,
including the rows that have no Pump profile at all -- a `404` re-asked
forever. This module is that request paid once.

What is kept from `/fomo`
-------------------------

* one on-disk map, so a restart does not re-pay (`ProfileCache`);
* a per-key `asyncio.Lock` with a second cache read inside it, so two callers
  for the same wallet in one execution make one request (`WalletResolver`'s
  pattern, extracted into `wallet_profile_cache.KeyedLocks`);
* never raise -- an identity is a nice-to-have on a card that is useful
  without it, so every failure returns a result object saying so;
* refuse to send an address of the wrong shape to a source that cannot take it
  (session 20's `-32602` lesson, here as: an `0x...` term is never sent to
  Pump's Solana profile route).

What is deliberately different
------------------------------

* **TTL.** A FOMO wallet proved by an on-chain signature does not expire. A
  Pump profile is Pump's own claim about a *mutable* username, avatar and
  follower count, so positive entries expire (`PUMP_PROFILE_TTL`, 7 days) and
  a call site that needs current numbers asks for a shorter `max_age`.
* **Negative caching.** `/fomo` does not negative-cache, and should not: a
  miss there means an expensive scan did not reach far enough, which a later,
  cheaper run may fix. A Pump miss is an authoritative `404` from the only
  source of truth. It is cached, with its own shorter TTL
  (`PUMP_PROFILE_NEGATIVE_TTL`, 6 hours) because a wallet can create a profile
  later. A *transient* failure -- timeout, 5xx, transport error -- is never
  written as a negative; that distinction is what keeps a Pump outage from
  poisoning the cache for six hours.
* **The alias direction.** FOMO caches handle -> wallet. Pump's canonical key
  is the wallet, and the username is a mutable alias pointing at it, so
  `/pump zinc` and `/pump <zinc's wallet>` share one entry.

The separate Pump *EVM* wallet is not this module's job: it is not published
by Pump and has to be discovered from a balance fingerprint, which is
`pump_evm.py`. This module accepts a `PumpEvmResolver` and uses its cache only
to translate a `0x...` query into the Solana address to look up.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from pump_api import PumpError, PumpNotFound, PumpUser, pump_profile_url
from pump_evm import EVM_RE
from fomo_wallet import SOLANA_ADDRESS_RE
from wallet_profile_cache import CacheEntry, ProfileCache

log = logging.getLogger("pump.profiles")


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


CACHE_FILE = os.getenv("PUMP_PROFILE_CACHE_FILE", "pump_profile_cache.json")
# A username or avatar can change; the wallet -> profile edge effectively
# cannot. Seven days keeps holder labelling free while still converging.
PROFILE_TTL = _float_env("PUMP_PROFILE_TTL", 7 * 24 * 3600)
# Short enough that a trader who joins Pump today is named tomorrow, long
# enough that one /token render does not re-ask ten 404s.
NEGATIVE_TTL = _float_env("PUMP_PROFILE_NEGATIVE_TTL", 6 * 3600)
# What a *card* considers current, as opposed to what holder labelling does.
CARD_TTL = _float_env("PUMP_PROFILE_CARD_TTL", 300)
# Pump's public site API is not a rate-limit-documented developer API. Ten in
# flight is comfortably below what the site itself opens for one page.
BATCH_CONCURRENCY = max(1, _int_env("PUMP_PROFILE_CONCURRENCY", 8))

# Statuses a lookup can end in. The bot and the diagnostic both branch on
# these rather than on exception types.
CACHED = "cached"              # positive cache hit
CACHED_MISSING = "cached-missing"  # negative cache hit -- known to have none
RESOLVED = "resolved"          # live lookup succeeded
MISSING = "missing"            # live lookup: Pump has no profile (404)
UNAVAILABLE = "unavailable"    # live lookup failed transiently -- NOT cached
UNSUPPORTED = "unsupported"    # the term cannot address a Pump profile
DISABLED = "disabled"          # network lookups were not permitted by caller

FOUND_STATUSES = (CACHED, RESOLVED)
DEFINITIVE_MISS_STATUSES = (CACHED_MISSING, MISSING, UNSUPPORTED)


def normalize_term(term: str) -> str:
    """Canonical cache key for a term.

    Solana addresses are base58 and **case-sensitive** -- the same rule
    `find_cached_wallets()` applies -- so they are only stripped. Usernames and
    EVM addresses are case-insensitive and fold to lower case.
    """
    clean = (term or "").strip().strip("`").strip().lstrip("@").strip()
    if not clean:
        return ""
    if SOLANA_ADDRESS_RE.fullmatch(clean):
        return clean
    return clean.casefold()


def is_solana_address(term: str) -> bool:
    clean = (term or "").strip().strip("`").strip()
    return bool(SOLANA_ADDRESS_RE.fullmatch(clean)) and not clean.startswith("0x")


def is_evm_address(term: str) -> bool:
    return bool(EVM_RE.fullmatch((term or "").strip().strip("`").strip()))


@dataclass(frozen=True)
class PumpProfile:
    """A Pump profile, as cached. `address` is the canonical identifier."""

    address: str
    username: str
    profile_image: str | None = None
    header_image: str | None = None
    bio: str | None = None
    x_username: str | None = None
    followers: int = 0
    following: int = 0
    cached_at: int = 0
    source: str = ""

    @classmethod
    def from_user(cls, user: PumpUser, *, source: str = "api") -> "PumpProfile":
        return cls(
            address=user.address,
            username=user.username,
            profile_image=user.profile_image,
            header_image=user.header_image,
            bio=user.bio,
            x_username=user.x_username,
            followers=user.followers,
            following=user.following,
            source=source,
        )

    @classmethod
    def from_entry(cls, entry: CacheEntry) -> "PumpProfile | None":
        payload = entry.payload or {}
        address = str(payload.get("address") or "").strip()
        username = str(payload.get("username") or "").strip()
        if not address or not username:
            return None
        def _int(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
        return cls(
            address=address,
            username=username,
            profile_image=payload.get("profile_image") or None,
            header_image=payload.get("header_image") or None,
            bio=payload.get("bio") or None,
            x_username=payload.get("x_username") or None,
            followers=_int(payload.get("followers")),
            following=_int(payload.get("following")),
            cached_at=entry.at,
            source=entry.source or "cache",
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "username": self.username,
            "profile_image": self.profile_image,
            "header_image": self.header_image,
            "bio": self.bio,
            "x_username": self.x_username,
            "followers": self.followers,
            "following": self.following,
        }

    def to_user(self) -> PumpUser:
        """The shape the existing renderers already accept."""
        return PumpUser(
            address=self.address,
            username=self.username,
            profile_image=self.profile_image,
            header_image=self.header_image,
            bio=self.bio,
            x_username=self.x_username,
            followers=self.followers,
            following=self.following,
        )

    @property
    def profile_url(self) -> str:
        return pump_profile_url(self.address)


@dataclass(frozen=True)
class PumpLookup:
    """The outcome of one lookup, including *why* there is no profile.

    `/pumpwallet` has to tell "Pump has no profile for this wallet" apart from
    "Pump did not answer", and the diagnostic reports the stage that lost it,
    so a bare `None` is not enough.
    """

    term: str
    key: str
    status: str
    profile: PumpProfile | None = None
    error: str | None = None
    requests: int = 0

    @property
    def found(self) -> bool:
        return self.profile is not None

    @property
    def from_cache(self) -> bool:
        return self.status in (CACHED, CACHED_MISSING)

    @property
    def definitive_miss(self) -> bool:
        """True when Pump itself says there is no profile."""
        return self.status in DEFINITIVE_MISS_STATUSES

    @property
    def stage(self) -> str:
        return {
            CACHED: "cache",
            CACHED_MISSING: "cache",
            RESOLVED: "profile",
            MISSING: "profile",
            UNAVAILABLE: "transport",
            UNSUPPORTED: "input",
            DISABLED: "cache",
        }.get(self.status, "profile")


class PumpProfileResolver:
    """Wallet or username -> Pump profile, with one request per subject.

    Usage mirrors `WalletResolver`::

        resolver = PumpProfileResolver(pump_client, cache_path)
        profile = await resolver.resolve(wallet)        # None when there is none
        result  = await resolver.lookup(wallet)         # with the reason
        found   = await resolver.resolve_many(wallets)  # deduped, bounded

    `cached()` is the synchronous, network-free read for render paths that must
    not block, and is the reverse-lookup `/wallet` uses.
    """

    def __init__(
        self,
        pump: Any,
        cache_path: str | Path = CACHE_FILE,
        *,
        ttl: float = PROFILE_TTL,
        negative_ttl: float = NEGATIVE_TTL,
        concurrency: int = BATCH_CONCURRENCY,
        evm: Any = None,
        persist: bool = True,
    ) -> None:
        self.pump = pump
        self.evm = evm
        # `--dry-run` in the bulk tools keeps every learned mapping in memory
        # and writes nothing, so a rehearsal reports real numbers.
        self.persist = persist
        self.cache = ProfileCache(
            cache_path, ttl=ttl, negative_ttl=negative_ttl,
            normalize=normalize_term,
        )
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self.requests = 0

    # -- cache-only reads ------------------------------------------------

    def cached(self, term: str, *, max_age: float | None = None
               ) -> PumpProfile | None:
        """A live cached profile, or None. Never touches the network."""
        entry = self.cache.get(term, max_age=max_age)
        if entry is None or not entry.found:
            return None
        return PumpProfile.from_entry(entry)

    def known_missing(self, term: str) -> bool:
        """True when Pump has told us, recently, that there is no profile."""
        entry = self.cache.get(term)
        return entry is not None and not entry.found

    def counts(self) -> dict[str, int]:
        return self.cache.counts()

    # -- writes from elsewhere -------------------------------------------

    def adopt(self, user: PumpUser | PumpProfile, *, source: str = "api",
              aliases: tuple[str, ...] = (), save: bool = True) -> PumpProfile:
        """Record a profile this process obtained by some other route.

        `/pumptrack` and `/pump` already hold a full `PumpUser`; handing it
        here means the next `/token` that meets the same wallet pays nothing.
        The wallet is the entry; the username -- and whatever term the caller
        actually typed -- become aliases pointing at it, so one entry answers
        every spelling of the same trader.
        """
        profile = (user if isinstance(user, PumpProfile)
                   else PumpProfile.from_user(user, source=source))
        alias_terms = tuple(
            alias for alias in (profile.username, *aliases) if alias
        )
        self.cache.put(
            profile.address, profile.as_payload(), source=source,
            aliases=alias_terms, save=save and self.persist,
        )
        return profile

    def forget(self, term: str) -> bool:
        return self.cache.forget(term)

    # -- resolution ------------------------------------------------------

    def _translate(self, term: str) -> tuple[str, str | None]:
        """(term to look up, reason it cannot be looked up).

        An EVM address is never sent to Pump's Solana profile route. When
        `pump_evm.py` has already discovered which Solana profile owns it, the
        query is rewritten to that profile; otherwise the caller is told the
        term is unsupported rather than being charged a request that must 404.
        """
        clean = (term or "").strip().strip("`").strip().lstrip("@").strip()
        if not clean:
            return "", "empty term"
        if is_evm_address(clean):
            match = None
            if self.evm is not None:
                try:
                    match = self.evm.cached(clean)
                except Exception as exc:  # a broken cache is not a crash
                    log.debug("pump EVM cache lookup failed for %s: %s", clean, exc)
            if match is not None and getattr(match, "solana", ""):
                return str(match.solana), None
            return clean, "EVM wallet has no discovered Pump profile yet"
        return clean, None

    async def lookup(
        self,
        term: str,
        *,
        fresh: bool = False,
        max_age: float | None = None,
        allow_network: bool = True,
    ) -> PumpLookup:
        """Resolve one term. Never raises."""
        key = normalize_term(term)
        query, blocked = self._translate(term)
        if blocked:
            # Still honour a cached answer for the raw term if one exists --
            # `adopt()` may have stored the EVM address as an alias.
            entry = self.cache.get(term, max_age=max_age)
            if entry is not None and entry.found:
                profile = PumpProfile.from_entry(entry)
                if profile is not None:
                    return PumpLookup(term, key, CACHED, profile)
            return PumpLookup(term, key, UNSUPPORTED, error=blocked)

        query_key = normalize_term(query)
        if not fresh:
            hit = self._read(query, max_age)
            if hit is not None:
                return PumpLookup(term, query_key, hit[0], hit[1])
        if not allow_network:
            return PumpLookup(term, query_key, DISABLED)

        # Two commands for the same wallet at once would each pay the request.
        # Serialise per key, then read the cache again inside the lock so the
        # second one gets the first one's answer. This is the /fomo rule.
        lock = self.cache.locks(query_key)
        async with lock:
            if not fresh:
                hit = self._read(query, max_age)
                if hit is not None:
                    return PumpLookup(term, query_key, hit[0], hit[1])
            return await self._fetch(term, query, query_key)

    def _read(self, query: str, max_age: float | None
              ) -> tuple[str, PumpProfile | None] | None:
        entry = self.cache.get(query, max_age=max_age)
        if entry is None:
            return None
        if not entry.found:
            return (CACHED_MISSING, None)
        profile = PumpProfile.from_entry(entry)
        if profile is None:
            return None  # corrupt row: treat as a miss and re-fetch
        return (CACHED, profile)

    async def _fetch(self, term: str, query: str, query_key: str) -> PumpLookup:
        try:
            async with self._semaphore:
                self.requests += 1
                user = await self.pump.resolve(query)
        except PumpNotFound:
            # Authoritative: Pump has no profile for this wallet. Cache it so
            # the next /token does not ask again.
            self.cache.put_missing(query_key, source="pump-404",
                                   save=self.persist)
            log.debug("pump profile: no profile for %s (cached negative)", query)
            return PumpLookup(term, query_key, MISSING, requests=1)
        except (PumpError, asyncio.TimeoutError, OSError) as exc:
            # Transient. Never written as a negative -- a Pump outage must not
            # be believed for the negative TTL.
            log.debug("pump profile lookup failed for %s: %s", query, exc)
            return PumpLookup(term, query_key, UNAVAILABLE,
                              error=str(exc)[:200], requests=1)
        except Exception as exc:  # defensive: identity is never load-bearing
            log.warning("pump profile lookup errored for %s: %s", query, exc)
            return PumpLookup(term, query_key, UNAVAILABLE,
                              error=str(exc)[:200], requests=1)

        # One write: `/pump <username>` warms the wallet key and vice versa.
        profile = self.adopt(user, aliases=(query_key,))
        return PumpLookup(term, normalize_term(profile.address), RESOLVED,
                          profile, requests=1)

    async def resolve(self, term: str, *, fresh: bool = False,
                      max_age: float | None = None) -> PumpProfile | None:
        return (await self.lookup(term, fresh=fresh, max_age=max_age)).profile

    async def lookup_many(
        self,
        terms: Iterable[str],
        *,
        fresh: bool = False,
        max_age: float | None = None,
    ) -> dict[str, PumpLookup]:
        """Resolve many terms at once, deduplicated by canonical key.

        The same wallet appearing twice in one batch costs one request, both
        because the batch is deduped up front and because `lookup()` locks per
        key. Concurrency is bounded by the resolver's semaphore.
        """
        ordered: list[str] = []
        seen: set[str] = set()
        for term in terms:
            key = normalize_term(term)
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(term)
        if not ordered:
            return {}
        results = await asyncio.gather(*(
            self.lookup(term, fresh=fresh, max_age=max_age) for term in ordered
        ), return_exceptions=True)
        out: dict[str, PumpLookup] = {}
        for term, result in zip(ordered, results):
            if isinstance(result, BaseException):
                log.debug("pump batch lookup failed for %s: %s", term, result)
                out[normalize_term(term)] = PumpLookup(
                    term, normalize_term(term), UNAVAILABLE,
                    error=str(result)[:200])
                continue
            out[normalize_term(term)] = result
        return out

    async def resolve_many(
        self,
        terms: Iterable[str],
        *,
        fresh: bool = False,
        max_age: float | None = None,
    ) -> dict[str, PumpProfile]:
        """Only the terms that have a profile, keyed by canonical term."""
        looked = await self.lookup_many(terms, fresh=fresh, max_age=max_age)
        return {key: result.profile for key, result in looked.items()
                if result.profile is not None}

    async def prefetch(self, terms: Sequence[str], *,
                       max_age: float | None = None) -> int:
        """Warm the cache for a render path. Returns how many are now known.

        `/token` calls this once for the whole holder list so the per-row
        labelling that follows is pure cache reads.
        """
        looked = await self.lookup_many(terms, max_age=max_age)
        return sum(1 for result in looked.values() if result.found)
