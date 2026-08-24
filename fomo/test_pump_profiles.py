"""Regression tests for the shared profile cache and Pump profile resolution.

The properties these lock down are the ones sessions 26-32 established for
`/fomo` and this work carries over to `/pump`:

* a wallet is asked about once, ever (per TTL) -- across calls, across
  concurrent callers in one execution, and across process restarts;
* a wallet with no Pump profile is remembered as such, so `/token` does not
  re-ask a known 404 on every render;
* a *transient* failure is never mistaken for an absence;
* an address of the wrong shape is never sent to a source that cannot take it;
* nothing here can raise into a Discord command.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from pump_api import PumpError, PumpNotFound, PumpUser
from pump_profiles import (
    CACHED,
    CACHED_MISSING,
    MISSING,
    RESOLVED,
    UNAVAILABLE,
    UNSUPPORTED,
    PumpProfile,
    PumpProfileResolver,
    normalize_term,
)
from wallet_profile_cache import KeyedLocks, ProfileCache, write_json_atomic

WALLET = "4y2T1ghykCTq4EddoXjptZamk4qAsqcZw6eKxS8jdvE1"
OTHER = "5f1AoBaqeBZ3sQhNVQp7xYANb7ykj4xzYBh8eW5RYyFE"
EVM = "0x1160079f1463dc5f9f20b1f1b9cf628718649c18"

USER = PumpUser(address=WALLET, username="hdegroot", followers=12,
                x_username="hdegroot_x", profile_image="https://img/1.png")


class FakePump:
    """`PumpClient.resolve()`'s contract: the profile, or PumpNotFound."""

    def __init__(self, known: dict[str, PumpUser] | None = None,
                 error: Exception | None = None, delay: float = 0) -> None:
        self.known = known or {}
        self.error = error
        self.delay = delay
        self.calls: list[str] = []

    async def resolve(self, term: str) -> PumpUser:
        self.calls.append(term)
        if self.delay:
            await asyncio.sleep(self.delay)
        else:
            await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        if term in self.known:
            return self.known[term]
        raise PumpNotFound("Pump resource not found")


class FakeEvmMatch:
    def __init__(self, solana: str) -> None:
        self.solana = solana


class FakeEvmCache:
    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = mapping or {}

    def cached(self, wallet: str):
        target = self.mapping.get(wallet.strip().lower())
        return FakeEvmMatch(target) if target else None


def _resolver(tmp: str, name: str = "cache.json", **kwargs) -> PumpProfileResolver:
    pump = kwargs.pop("pump", None) or FakePump({WALLET: USER, "hdegroot": USER})
    return PumpProfileResolver(pump, os.path.join(tmp, name), **kwargs)


# ------------------------------------------------------------------ cache


class ProfileCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "c.json")

    def test_a_positive_entry_survives_a_restart(self) -> None:
        cache = ProfileCache(self.path)
        cache.put("k", {"address": "k", "username": "u"})
        reopened = ProfileCache(self.path)
        entry = reopened.get("k")
        self.assertIsNotNone(entry)
        self.assertTrue(entry.found)
        self.assertEqual(entry.payload["username"], "u")

    def test_a_negative_entry_is_a_hit_not_an_absence(self) -> None:
        """`get()` returns the record; `found` is what distinguishes it."""
        cache = ProfileCache(self.path)
        cache.put_missing("k")
        entry = ProfileCache(self.path).get("k")
        self.assertIsNotNone(entry)
        self.assertFalse(entry.found)

    def test_positive_and_negative_entries_expire_on_their_own_clocks(self) -> None:
        cache = ProfileCache(self.path, ttl=1000, negative_ttl=1)
        cache.put("keep", {"address": "keep", "username": "u"})
        cache.put_missing("drop")
        # Rewrite both as if they were written 10 seconds ago.
        raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
        for row in raw["entries"].values():
            row["at"] = int(time.time()) - 10
        write_json_atomic(self.path, raw)
        reopened = ProfileCache(self.path, ttl=1000, negative_ttl=1)
        self.assertIsNotNone(reopened.get("keep"))
        self.assertIsNone(reopened.get("drop"))

    def test_a_caller_can_demand_a_shorter_max_age_than_the_ttl(self) -> None:
        cache = ProfileCache(self.path, ttl=10_000)
        cache.put("k", {"address": "k", "username": "u"})
        raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
        raw["entries"]["k"]["at"] = int(time.time()) - 600
        write_json_atomic(self.path, raw)
        reopened = ProfileCache(self.path, ttl=10_000)
        self.assertIsNotNone(reopened.get("k"))
        self.assertIsNone(reopened.get("k", max_age=300))

    def test_an_alias_points_at_the_canonical_entry(self) -> None:
        cache = ProfileCache(self.path, normalize=normalize_term)
        cache.put(WALLET, {"address": WALLET, "username": "hdegroot"},
                  aliases=("hdegroot",))
        self.assertEqual(cache.key_for("@HDEGROOT"), WALLET)
        self.assertIsNotNone(cache.get("HDEGroot"))

    def test_a_recorded_absence_drops_stale_aliases(self) -> None:
        cache = ProfileCache(self.path, normalize=normalize_term)
        cache.put(WALLET, {"address": WALLET, "username": "hdegroot"},
                  aliases=("hdegroot",))
        cache.put_missing(WALLET)
        self.assertEqual(cache.aliases(), {})

    def test_an_unreadable_cache_file_is_not_an_error(self) -> None:
        Path(self.path).write_text("{not json", encoding="utf-8")
        cache = ProfileCache(self.path)
        self.assertEqual(len(cache), 0)

    def test_a_write_is_atomic_and_leaves_no_temporary_behind(self) -> None:
        self.assertTrue(write_json_atomic(self.path, {"a": 1}))
        self.assertEqual(json.loads(Path(self.path).read_text()), {"a": 1})
        self.assertFalse(Path(self.path + ".tmp").exists())

    def test_an_unwritable_path_is_reported_not_raised(self) -> None:
        self.assertFalse(write_json_atomic("/proc/one/two/three.json", {"a": 1}))

    def test_prune_drops_only_expired_rows(self) -> None:
        cache = ProfileCache(self.path, ttl=10_000, negative_ttl=1)
        cache.put("keep", {"address": "keep", "username": "u"})
        cache.put_missing("drop")
        raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
        raw["entries"]["drop"]["at"] = int(time.time()) - 10
        write_json_atomic(self.path, raw)
        reopened = ProfileCache(self.path, ttl=10_000, negative_ttl=1)
        self.assertEqual(reopened.prune(), 1)
        self.assertEqual(len(reopened), 1)


class KeyedLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_same_key_serialises_and_different_keys_do_not(self) -> None:
        locks = KeyedLocks()
        self.assertIs(locks("a"), locks("a"))
        self.assertIsNot(locks("a"), locks("b"))


# -------------------------------------------------------------- resolution


class PumpProfileResolverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    async def test_an_uncached_wallet_is_fetched_and_a_cached_one_is_not(self) -> None:
        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump)
        first = await resolver.lookup(WALLET)
        second = await resolver.lookup(WALLET)
        self.assertEqual(first.status, RESOLVED)
        self.assertEqual(second.status, CACHED)
        self.assertEqual(second.profile.username, "hdegroot")
        self.assertEqual(len(pump.calls), 1)

    async def test_a_wallet_with_no_profile_is_asked_about_once(self) -> None:
        """The /token saving: a 404 is authoritative, so it is remembered."""
        pump = FakePump({})
        resolver = _resolver(self.tmp, pump=pump)
        first = await resolver.lookup(OTHER)
        second = await resolver.lookup(OTHER)
        self.assertEqual(first.status, MISSING)
        self.assertEqual(second.status, CACHED_MISSING)
        self.assertIsNone(second.profile)
        self.assertTrue(second.definitive_miss)
        self.assertEqual(len(pump.calls), 1)

    async def test_a_transient_failure_is_never_cached_as_an_absence(self) -> None:
        """A Pump outage must not be believed for the negative TTL."""
        pump = FakePump(error=PumpError("Pump returned HTTP 502"))
        resolver = _resolver(self.tmp, pump=pump)
        result = await resolver.lookup(WALLET)
        self.assertEqual(result.status, UNAVAILABLE)
        self.assertFalse(result.definitive_miss)
        self.assertIsNone(resolver.cache.peek(WALLET))
        self.assertFalse(resolver.known_missing(WALLET))
        await resolver.lookup(WALLET)
        self.assertEqual(len(pump.calls), 2)  # retried, not written off

    async def test_a_timeout_is_transient_too(self) -> None:
        resolver = _resolver(self.tmp, pump=FakePump(error=asyncio.TimeoutError()))
        self.assertEqual((await resolver.lookup(WALLET)).status, UNAVAILABLE)
        self.assertIsNone(resolver.cache.peek(WALLET))

    async def test_an_unexpected_error_never_reaches_the_caller(self) -> None:
        resolver = _resolver(self.tmp, pump=FakePump(error=ValueError("boom")))
        result = await resolver.lookup(WALLET)
        self.assertEqual(result.status, UNAVAILABLE)
        self.assertIsNone(await resolver.resolve(WALLET))

    async def test_concurrent_lookups_for_one_wallet_make_one_request(self) -> None:
        """`WalletResolver`'s per-handle lock, applied per wallet."""
        pump = FakePump({WALLET: USER}, delay=0.02)
        resolver = _resolver(self.tmp, pump=pump)
        results = await asyncio.gather(*(resolver.lookup(WALLET) for _ in range(6)))
        self.assertEqual(len(pump.calls), 1)
        self.assertEqual(sum(1 for r in results if r.status == RESOLVED), 1)
        self.assertEqual(sum(1 for r in results if r.status == CACHED), 5)

    async def test_a_batch_deduplicates_repeats_of_the_same_wallet(self) -> None:
        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump)
        results = await resolver.lookup_many(
            [WALLET, f" {WALLET} ", f"`{WALLET}`", OTHER, OTHER]
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(pump.calls), sorted([WALLET, OTHER]))

    async def test_a_batch_survives_a_failure_on_one_member(self) -> None:
        class Flaky(FakePump):
            async def resolve(self, term: str) -> PumpUser:
                if term == OTHER:
                    raise PumpError("boom")
                return await super().resolve(term)

        resolver = _resolver(self.tmp, pump=Flaky({WALLET: USER}))
        results = await resolver.lookup_many([WALLET, OTHER])
        self.assertEqual(results[WALLET].status, RESOLVED)
        self.assertEqual(results[OTHER].status, UNAVAILABLE)

    async def test_prefetch_then_label_costs_nothing_extra(self) -> None:
        """The /token shape: one batch, then per-row reads."""
        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump)
        found = await resolver.prefetch([WALLET, OTHER])
        self.assertEqual(found, 1)
        before = len(pump.calls)
        self.assertIsNotNone(await resolver.resolve(WALLET))
        self.assertIsNone(await resolver.resolve(OTHER))
        self.assertEqual(len(pump.calls), before)

    async def test_a_username_and_its_wallet_share_one_entry(self) -> None:
        pump = FakePump({"hdegroot": USER})
        resolver = _resolver(self.tmp, pump=pump)
        first = await resolver.lookup("@hdegroot")
        self.assertEqual(first.status, RESOLVED)
        self.assertEqual(first.profile.address, WALLET)
        # The wallet now answers from cache even though it was never asked.
        self.assertEqual((await resolver.lookup(WALLET)).status, CACHED)
        self.assertEqual((await resolver.lookup("HDEGROOT")).status, CACHED)
        self.assertEqual(len(pump.calls), 1)

    async def test_an_evm_wallet_is_never_sent_to_the_solana_profile_route(self) -> None:
        """Session 20's lesson: do not ask a source for a shape it cannot take."""
        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump)
        result = await resolver.lookup(EVM)
        self.assertEqual(result.status, UNSUPPORTED)
        self.assertEqual(pump.calls, [])
        self.assertTrue(result.definitive_miss)

    async def test_a_discovered_evm_wallet_resolves_through_its_solana_profile(self) -> None:
        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump, evm=FakeEvmCache({EVM: WALLET}))
        result = await resolver.lookup(EVM)
        self.assertEqual(result.status, RESOLVED)
        self.assertEqual(result.profile.address, WALLET)
        self.assertEqual(pump.calls, [WALLET])

    async def test_a_broken_evm_cache_does_not_break_the_lookup(self) -> None:
        class Broken:
            def cached(self, wallet: str):
                raise RuntimeError("corrupt")

        resolver = _resolver(self.tmp, pump=FakePump({WALLET: USER}), evm=Broken())
        self.assertEqual((await resolver.lookup(EVM)).status, UNSUPPORTED)

    async def test_the_cache_survives_a_restart(self) -> None:
        path = "restart.json"
        first = _resolver(self.tmp, path, pump=FakePump({WALLET: USER}))
        await first.lookup(WALLET)
        await first.lookup(OTHER)
        # Writes inside one event-loop burst are coalesced, so the flush that
        # `atexit` would run on the way out has to be asked for explicitly --
        # this line is the process exit the test is pretending to have.
        first.cache.flush()
        second = _resolver(self.tmp, path, pump=FakePump({}))
        self.assertEqual(second.cached(WALLET).username, "hdegroot")
        self.assertTrue(second.known_missing(OTHER))
        self.assertEqual((await second.lookup(WALLET)).status, CACHED)

    async def test_fresh_bypasses_the_cache_and_rewrites_it(self) -> None:
        renamed = PumpUser(address=WALLET, username="hdegroot2")
        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump)
        await resolver.lookup(WALLET)
        pump.known[WALLET] = renamed
        again = await resolver.lookup(WALLET, fresh=True)
        self.assertEqual(again.status, RESOLVED)
        self.assertEqual(resolver.cached(WALLET).username, "hdegroot2")
        self.assertEqual(len(pump.calls), 2)

    async def test_a_card_can_demand_fresher_data_than_holder_labelling(self) -> None:
        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump)
        await resolver.lookup(WALLET)
        # A zero-second freshness bar forces a refetch; the default does not.
        self.assertEqual((await resolver.lookup(WALLET)).status, CACHED)
        self.assertEqual((await resolver.lookup(WALLET, max_age=0.0)).status,
                         RESOLVED)
        self.assertEqual(len(pump.calls), 2)

    async def test_allow_network_false_never_makes_a_request(self) -> None:
        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump)
        result = await resolver.lookup(WALLET, allow_network=False)
        self.assertFalse(result.found)
        self.assertEqual(pump.calls, [])

    async def test_dry_run_learns_in_memory_and_writes_nothing(self) -> None:
        path = os.path.join(self.tmp, "dry.json")
        resolver = PumpProfileResolver(FakePump({WALLET: USER}), path, persist=False)
        await resolver.lookup(WALLET)
        await resolver.lookup(OTHER)
        self.assertEqual(resolver.counts()["total"], 2)
        self.assertFalse(Path(path).exists())

    async def test_an_empty_term_is_refused_without_a_request(self) -> None:
        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump)
        self.assertEqual((await resolver.lookup("   ")).status, UNSUPPORTED)
        self.assertEqual(pump.calls, [])

    async def test_a_corrupt_row_is_refetched_rather_than_believed(self) -> None:
        path = os.path.join(self.tmp, "corrupt.json")
        write_json_atomic(path, {
            "version": 1,
            "entries": {WALLET: {"found": True, "at": int(time.time()),
                                 "payload": {"address": "", "username": ""}}},
            "aliases": {},
        })
        pump = FakePump({WALLET: USER})
        resolver = PumpProfileResolver(pump, path)
        self.assertIsNone(resolver.cached(WALLET))
        self.assertEqual((await resolver.lookup(WALLET)).status, RESOLVED)

    async def test_adopt_records_a_profile_obtained_elsewhere(self) -> None:
        pump = FakePump({})
        resolver = _resolver(self.tmp, pump=pump)
        resolver.adopt(USER)
        self.assertEqual((await resolver.lookup(WALLET)).status, CACHED)
        self.assertEqual((await resolver.lookup("hdegroot")).status, CACHED)
        self.assertEqual(pump.calls, [])


class HolderLabellingTests(unittest.IsolatedAsyncioTestCase):
    """`/token`'s holder rows, which is where the repeated lookups were."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def _holder(self, address: str):
        from decimal import Decimal
        from token_intelligence import TokenHolder
        return TokenHolder(address, Decimal("24339588.53"), 2.54)

    def _stub(self, resolver, pump_evm=None):
        return type("B", (), {
            "fomo": None, "pump": None, "pump_evm": pump_evm,
            "pump_profiles": resolver, "wallets": None, "evm_wallets": None,
        })()

    async def test_a_pump_holder_is_named_and_linked_by_wallet(self) -> None:
        import fomo_bot
        from unittest.mock import patch

        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump)
        with patch.object(fomo_bot, "bot", self._stub(resolver)):
            line = await fomo_bot._holder_label(self._holder(WALLET), "Solana", None)
        self.assertIn(f"https://pump.fun/profile/{WALLET}", line)
        self.assertIn("@hdegroot", line)

    async def test_a_holder_without_a_pump_profile_keeps_its_address(self) -> None:
        import fomo_bot
        from unittest.mock import patch

        pump = FakePump({})
        resolver = _resolver(self.tmp, pump=pump)
        with patch.object(fomo_bot, "bot", self._stub(resolver)):
            line = await fomo_bot._holder_label(self._holder(OTHER), "Solana", None)
        self.assertNotIn("pump.fun/profile", line)
        self.assertIn("2.54%", line)

    async def test_rendering_the_same_card_twice_asks_pump_once(self) -> None:
        """The regression this whole change exists for."""
        import fomo_bot
        from unittest.mock import patch

        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump)
        holders = [self._holder(WALLET), self._holder(OTHER)]
        with patch.object(fomo_bot, "bot", self._stub(resolver)):
            for _ in range(3):
                await resolver.prefetch([holder.address for holder in holders])
                await asyncio.gather(*(
                    fomo_bot._holder_label(holder, "Solana", None)
                    for holder in holders
                ))
        self.assertEqual(sorted(pump.calls), sorted([WALLET, OTHER]))

    async def test_a_pump_outage_does_not_break_the_holder_row(self) -> None:
        import fomo_bot
        from unittest.mock import patch

        resolver = _resolver(self.tmp, pump=FakePump(error=PumpError("HTTP 503")))
        with patch.object(fomo_bot, "bot", self._stub(resolver)):
            line = await fomo_bot._holder_label(self._holder(WALLET), "Solana", None)
        self.assertIn("2.54%", line)
        self.assertNotIn("pump.fun/profile", line)

    async def test_an_evm_holder_never_reaches_the_solana_profile_route(self) -> None:
        import fomo_bot
        from unittest.mock import patch

        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump)
        holder = self._holder(EVM)
        with patch.object(fomo_bot, "bot", self._stub(resolver)):
            line = await fomo_bot._holder_label(holder, "BSC", None)
        self.assertEqual(pump.calls, [])
        self.assertNotIn("pump.fun/profile", line)


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send(self, content: str = "", *, ephemeral: bool = False,
                   embed: object = None) -> None:
        self.messages.append((content, ephemeral))


class FakeInteraction:
    def __init__(self) -> None:
        self.followup = FakeFollowup()


class PumpCommandPathTests(unittest.IsolatedAsyncioTestCase):
    """`_resolve_pump_user` is the only way a Pump command gets a profile."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def _stub(self, resolver, pump_evm=None):
        return type("B", (), {
            "fomo": None, "pump": object(), "pump_evm": pump_evm,
            "pump_profiles": resolver, "wallets": None, "evm_wallets": None,
        })()

    async def _call(self, resolver, term, pump_evm=None, **kwargs):
        import fomo_bot
        from unittest.mock import patch
        interaction = FakeInteraction()
        with patch.object(fomo_bot, "bot", self._stub(resolver, pump_evm)):
            user = await fomo_bot._resolve_pump_user(interaction, term, **kwargs)
        return user, interaction.followup.messages

    async def test_a_known_wallet_returns_a_pump_user_and_says_nothing(self) -> None:
        resolver = _resolver(self.tmp, pump=FakePump({WALLET: USER}))
        user, messages = await self._call(resolver, WALLET)
        self.assertIsInstance(user, PumpUser)
        self.assertEqual(user.username, "hdegroot")
        self.assertEqual(messages, [])

    async def test_a_second_command_for_the_same_wallet_makes_no_request(self) -> None:
        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump)
        await self._call(resolver, WALLET)
        await self._call(resolver, WALLET)
        self.assertEqual(len(pump.calls), 1)

    async def test_the_card_demands_fresher_data_than_the_holder_rows(self) -> None:
        pump = FakePump({WALLET: USER})
        resolver = _resolver(self.tmp, pump=pump)
        await self._call(resolver, WALLET)
        await self._call(resolver, WALLET, max_age=0.0)
        self.assertEqual(len(pump.calls), 2)

    async def test_a_wallet_with_no_profile_gets_the_not_found_reply(self) -> None:
        resolver = _resolver(self.tmp, pump=FakePump({}))
        user, messages = await self._call(resolver, OTHER)
        self.assertIsNone(user)
        self.assertIn("No Pump.fun profile found", messages[0][0])

    async def test_an_undiscovered_evm_wallet_gets_its_own_explanation(self) -> None:
        resolver = _resolver(self.tmp, pump=FakePump({}))
        user, messages = await self._call(resolver, EVM)
        self.assertIsNone(user)
        self.assertIn("has not been discovered yet", messages[0][0])

    async def test_a_pump_outage_is_reported_as_a_failure_not_an_absence(self) -> None:
        resolver = _resolver(self.tmp, pump=FakePump(error=PumpError("HTTP 502")))
        user, messages = await self._call(resolver, WALLET)
        self.assertIsNone(user)
        self.assertIn("lookup failed", messages[0][0])
        self.assertNotIn("No Pump.fun profile found", messages[0][0])

    async def test_no_resolver_at_all_is_reported_rather_than_crashing(self) -> None:
        user, messages = await self._call(None, WALLET)
        self.assertIsNone(user)
        self.assertIn("unavailable", messages[0][0])


class PumpProfileModelTests(unittest.TestCase):
    def test_a_profile_round_trips_through_the_cache_payload(self) -> None:
        profile = PumpProfile.from_user(USER)
        payload = profile.as_payload()
        self.assertEqual(payload["address"], WALLET)
        self.assertEqual(payload["x_username"], "hdegroot_x")

    def test_a_profile_renders_as_the_pump_user_the_embeds_expect(self) -> None:
        user = PumpProfile.from_user(USER).to_user()
        self.assertIsInstance(user, PumpUser)
        self.assertEqual(user.profile_url, f"https://pump.fun/profile/{WALLET}")

    def test_the_profile_url_is_built_from_the_wallet_not_the_username(self) -> None:
        """Session 23's rule, preserved."""
        self.assertEqual(PumpProfile.from_user(USER).profile_url,
                         f"https://pump.fun/profile/{WALLET}")

    def test_solana_keys_are_case_sensitive_and_usernames_are_not(self) -> None:
        self.assertEqual(normalize_term(f" `{WALLET}` "), WALLET)
        self.assertNotEqual(normalize_term(WALLET), normalize_term(WALLET.lower()))
        self.assertEqual(normalize_term("@HDEGroot"), "hdegroot")


if __name__ == "__main__":
    unittest.main()
