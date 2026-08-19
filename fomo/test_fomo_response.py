from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from fomo_api import FomoUser
from fomo_bot import _enrich_fomo_message
from fomo_features import TraderStats


SOLANA_WALLET = "GxpWcYRz2nLcNxshch7yAhseZGtdgQz1nG8p5XxwQVdD"
EVM_WALLET = "0x0232b9afb9160fe479f25dade62fa60ef657bdc5"


def user(handle: str = "latencytest") -> FomoUser:
    return FomoUser({
        "id": "user-id",
        "userHandle": handle,
        "displayName": "Latency Test",
        "followers": 10,
        "following": 2,
        "totalVolume": 1000,
        "averageHoldTimeSeconds": 60,
    })


class WalletResolver:
    async def resolve(self, _fomo: object, _user: FomoUser) -> str:
        return SOLANA_WALLET

    async def resolve_from_balances(
        self, _user: FomoUser, _balances: object
    ) -> str:
        raise AssertionError("transaction resolver already returned a wallet")


class SlowWalletResolver:
    async def resolve(self, _fomo: object, _user: FomoUser) -> str:
        await asyncio.sleep(1)
        return SOLANA_WALLET


class EvmResolver:
    async def resolve(self, _user: FomoUser, **_kwargs: object) -> str:
        return EVM_WALLET


class ConcurrentWalletResolver(WalletResolver):
    def __init__(self, own: asyncio.Event, other: asyncio.Event) -> None:
        self.own = own
        self.other = other

    async def resolve(self, _fomo: object, _user: FomoUser) -> str:
        self.own.set()
        await asyncio.wait_for(self.other.wait(), timeout=0.2)
        return SOLANA_WALLET


class ConcurrentEvmResolver:
    def __init__(self, own: asyncio.Event, other: asyncio.Event) -> None:
        self.own = own
        self.other = other

    async def resolve(self, _user: FomoUser, **_kwargs: object) -> str:
        self.own.set()
        await asyncio.wait_for(self.other.wait(), timeout=0.2)
        return EVM_WALLET


class ActivityAwareWalletResolver(WalletResolver):
    def __init__(self, activity_started: asyncio.Event) -> None:
        self.activity_started = activity_started

    async def resolve(self, _fomo: object, _user: FomoUser) -> str:
        await asyncio.wait_for(self.activity_started.wait(), timeout=0.2)
        return SOLANA_WALLET


class Client:
    def __init__(self, wallets: object, evm_wallets: object = None) -> None:
        self.fomo = object()
        self.wallets = wallets
        self.evm_wallets = evm_wallets
        self._http = None


class Message:
    def __init__(self) -> None:
        self.embeds = []

    async def edit(self, *, embed: object) -> None:
        self.embeds.append(embed)


class FomoResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_evm_activity_does_not_wait_for_solana_discovery(self) -> None:
        activity_started = asyncio.Event()
        client = Client(ActivityAwareWalletResolver(activity_started), EvmResolver())
        client._http = object()
        message = Message()

        async def activity(_http: object, _wallet: str):
            activity_started.set()
            return (), ()

        with patch("fomo_bot.fetch_evm_activity", side_effect=activity):
            await _enrich_fomo_message(
                client,
                message,
                user("activity-concurrency-test"),
                TraderStats(),
                None,
                None,
                timeout=1,
            )
        self.assertEqual(len(message.embeds), 1)

    async def test_solana_and_evm_discovery_start_concurrently(self) -> None:
        sol_started = asyncio.Event()
        evm_started = asyncio.Event()
        message = Message()
        await _enrich_fomo_message(
            Client(
                ConcurrentWalletResolver(sol_started, evm_started),
                ConcurrentEvmResolver(evm_started, sol_started),
            ),
            message,
            user("concurrent-test"),
            TraderStats(),
            None,
            None,
            timeout=1,
        )
        self.assertEqual(len(message.embeds), 1)

    async def test_background_enrichment_edits_existing_profile(self) -> None:
        message = Message()
        await _enrich_fomo_message(
            Client(WalletResolver(), EvmResolver()),
            message,
            user(),
            TraderStats(raw_balances={"balances": []}),
            None,
            None,
            timeout=1,
        )
        self.assertEqual(len(message.embeds), 1)
        fields = {field.name: field.value for field in message.embeds[0].fields}
        self.assertIn(SOLANA_WALLET, fields["Solana wallet"])
        self.assertIn(EVM_WALLET, fields["EVM wallet"])

    async def test_background_deadline_does_not_delay_or_edit_without_results(self) -> None:
        message = Message()
        await _enrich_fomo_message(
            Client(SlowWalletResolver()),
            message,
            user("unique-timeout-test"),
            TraderStats(),
            None,
            None,
            timeout=0.01,
        )
        self.assertEqual(message.embeds, [])


if __name__ == "__main__":
    unittest.main()
