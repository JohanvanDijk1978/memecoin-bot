"""
wallet_profile_cache.py -- the part of wallet identity caching that belongs to
neither FOMO nor Pump.

`fomo_wallet.py` learned the shape of this the expensive way (sessions 26-32):

* an on-disk JSON map is the only thing that stops a costly lookup from being
  paid twice across process restarts;
* a *per-key* `asyncio.Lock` with a **second** cache read inside it is what
  stops two concurrent commands for the same subject from both paying;
* a cache write that is not atomic loses the whole map when the process dies
  mid-write, and the map is expensive to rebuild;
* persistence is best-effort -- a cache that raises is worse than no cache,
  because the caller's real job (rendering a card) does not need it.

FOMO's own records stay in `fomo_wallet.py` because they carry a different
guarantee: a wallet proved by an on-chain transaction does not expire, so that
cache has no TTL and no negative entries. A Pump profile is an *assertion by
Pump* about a mutable username, and "this wallet has no profile" is a cheap,
definitive `404` that `/token` would otherwise re-ask on every invocation. So
this layer adds the two things FOMO does not need -- expiry and negative
caching -- and keeps everything the two flows genuinely share.

Nothing in here knows what a Pump profile or a FOMO handle is. Source-specific
resolution lives in `pump_profiles.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

log = logging.getLogger("wallet.cache")

CACHE_VERSION = 1

# A record with no `at` is treated as written at the epoch, so it expires
# immediately rather than living forever with an unknown age.
_UNKNOWN_TIME = 0


# ------------------------------------------------------------------ json io


def read_json(path: str | Path) -> Any:
    """Read JSON, or return None. A cache that cannot be read is not an error."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def write_json_atomic(path: str | Path, payload: Any, *, indent: int = 1) -> bool:
    """Write via a temporary file and replace, so a crash cannot truncate it.

    Returns whether the write happened. Never raises: the caller's real work
    does not depend on the cache reaching disk.
    """
    destination = Path(path)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        if destination.parent and str(destination.parent) not in ("", "."):
            destination.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=indent, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, destination)
        return True
    except OSError as exc:
        log.warning("could not write %s: %s", destination, exc)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


# ----------------------------------------------------------------- locking


class KeyedLocks:
    """One `asyncio.Lock` per key, created on demand.

    This is `WalletResolver._locks` extracted verbatim in behaviour. Two
    lookups for the same subject serialise; the second one then finds the
    first one's cache entry and pays nothing. Lookups for *different* subjects
    never wait on each other.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def __call__(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def __len__(self) -> int:
        return len(self._locks)

    def discard(self, key: str) -> None:
        lock = self._locks.get(key)
        if lock is not None and not lock.locked():
            self._locks.pop(key, None)


# ------------------------------------------------------------------ entries


@dataclass(frozen=True)
class CacheEntry:
    """One cached answer: a payload, or a recorded absence."""

    key: str
    found: bool
    payload: dict[str, Any]
    at: int
    source: str = ""

    def age(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.at)

    def expired(self, ttl: float | None, now: float | None = None) -> bool:
        """`ttl` of None or a negative value means "never expires"; a `ttl`
        of exactly 0 means "must have been written this instant", which is how
        a caller demands a live value without passing `fresh=True`."""
        if ttl is None or ttl < 0:
            return False
        return self.age(now) >= ttl


class ProfileCache:
    """A keyed, expiring, negative-caching JSON store.

    Layout on disk::

        {"version": 1,
         "entries": {"<key>": {"found": true, "at": 1755, "source": "api",
                               "payload": {...}}},
         "aliases": {"<alias>": "<key>"}}

    `aliases` exists because the canonical key and the term a user types are
    not always the same thing -- a Pump profile is keyed by its Solana wallet,
    but `/pump zinc` arrives as a username. Aliases are stored alongside the
    entries rather than in a second file so one save keeps them consistent.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        ttl: float | None = 7 * 24 * 3600,
        negative_ttl: float | None = 6 * 3600,
        normalize: Callable[[str], str] | None = None,
        indent: int = 1,
    ) -> None:
        self.path = Path(path)
        # A negative TTL (or None) disables expiry, which is what a cache with
        # `/fomo`'s permanence guarantee would want.
        self.ttl = None if ttl is None else float(ttl)
        self.negative_ttl = None if negative_ttl is None else float(negative_ttl)
        self.indent = indent
        self._normalize = normalize or (lambda value: (value or "").strip())
        self._entries: dict[str, CacheEntry] = {}
        self._aliases: dict[str, str] = {}
        self._dirty = False
        self.locks = KeyedLocks()
        self.load()

    # -- persistence ----------------------------------------------------

    def load(self) -> None:
        raw = read_json(self.path)
        if not isinstance(raw, dict):
            return
        entries = raw.get("entries")
        if isinstance(entries, dict):
            for key, row in entries.items():
                if not isinstance(row, dict):
                    continue
                payload = row.get("payload")
                try:
                    at = int(row.get("at") or _UNKNOWN_TIME)
                except (TypeError, ValueError):
                    at = _UNKNOWN_TIME
                self._entries[str(key)] = CacheEntry(
                    key=str(key),
                    found=bool(row.get("found")),
                    payload=payload if isinstance(payload, dict) else {},
                    at=at,
                    source=str(row.get("source") or ""),
                )
        aliases = raw.get("aliases")
        if isinstance(aliases, dict):
            for alias, key in aliases.items():
                if isinstance(key, str) and key in self._entries:
                    self._aliases[str(alias)] = key

    def save(self, *, force: bool = False) -> bool:
        if not self._dirty and not force:
            return True
        payload = {
            "version": CACHE_VERSION,
            "entries": {
                key: {
                    "found": entry.found,
                    "at": entry.at,
                    **({"source": entry.source} if entry.source else {}),
                    **({"payload": entry.payload} if entry.payload else {}),
                }
                for key, entry in sorted(self._entries.items())
            },
            "aliases": dict(sorted(self._aliases.items())),
        }
        written = write_json_atomic(self.path, payload, indent=self.indent)
        if written:
            self._dirty = False
        return written

    # -- reads ----------------------------------------------------------

    def key_for(self, term: str) -> str:
        """The canonical key for a term, following an alias when one exists."""
        clean = self._normalize(term)
        return self._aliases.get(clean, clean)

    def peek(self, term: str) -> CacheEntry | None:
        """The stored entry regardless of age. `get()` is the one to use."""
        return self._entries.get(self.key_for(term))

    def get(self, term: str, *, max_age: float | None = None) -> CacheEntry | None:
        """A live entry, or None when absent or too old to trust.

        A negative entry is returned like any other -- the caller distinguishes
        by `entry.found`. Expiry is what makes a negative entry safe: a wallet
        with no profile today may have one tomorrow.
        """
        entry = self._entries.get(self.key_for(term))
        if entry is None:
            return None
        ttl = (self.ttl if entry.found else self.negative_ttl) if max_age is None \
            else max_age
        if entry.expired(ttl):
            return None
        return entry

    def entries(self) -> Iterator[CacheEntry]:
        return iter(list(self._entries.values()))

    def aliases(self) -> dict[str, str]:
        return dict(self._aliases)

    def __len__(self) -> int:
        return len(self._entries)

    def counts(self) -> dict[str, int]:
        found = sum(1 for entry in self._entries.values() if entry.found)
        return {"total": len(self._entries), "found": found,
                "missing": len(self._entries) - found,
                "aliases": len(self._aliases)}

    # -- writes ---------------------------------------------------------

    def put(self, key: str, payload: dict[str, Any], *, source: str = "",
            aliases: tuple[str, ...] = (), save: bool = True) -> CacheEntry:
        clean = self._normalize(key)
        entry = CacheEntry(key=clean, found=True, payload=dict(payload),
                           at=int(time.time()), source=source)
        self._entries[clean] = entry
        for alias in aliases:
            alias_key = self._normalize(alias)
            if alias_key and alias_key != clean:
                self._aliases[alias_key] = clean
        self._dirty = True
        if save:
            self.save()
        return entry

    def put_missing(self, key: str, *, source: str = "",
                    save: bool = True) -> CacheEntry:
        """Record a definitive absence. Only ever call this for a *definitive*
        negative -- a transient failure recorded here would be believed for the
        whole negative TTL."""
        clean = self._normalize(key)
        entry = CacheEntry(key=clean, found=False, payload={},
                           at=int(time.time()), source=source)
        self._entries[clean] = entry
        # A key that has no profile cannot be the target of an alias any more.
        self._aliases = {alias: target for alias, target in self._aliases.items()
                         if target != clean}
        self._dirty = True
        if save:
            self.save()
        return entry

    def forget(self, key: str, *, save: bool = True) -> bool:
        clean = self._normalize(key)
        removed = self._entries.pop(clean, None) is not None
        self._aliases = {alias: target for alias, target in self._aliases.items()
                         if target != clean and alias != clean}
        if removed:
            self._dirty = True
            if save:
                self.save()
        return removed

    def prune(self, *, save: bool = True) -> int:
        """Drop expired entries. Purely housekeeping -- `get()` already
        ignores them."""
        now = time.time()
        stale = [key for key, entry in self._entries.items()
                 if entry.expired(self.ttl if entry.found else self.negative_ttl, now)]
        for key in stale:
            self._entries.pop(key, None)
        if stale:
            self._aliases = {alias: target for alias, target in self._aliases.items()
                             if target in self._entries}
            self._dirty = True
            if save:
                self.save()
        return len(stale)
