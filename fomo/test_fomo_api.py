from __future__ import annotations

import json
import unittest
from pathlib import Path

from fomo_api import API_BASE, BROWSERISH, FomoClient
from fomo_chains import SUPPORTED_CHAINS_HEADER


class FakeBrowser:
    def __init__(self) -> None:
        self.batches: list[tuple[list[str], str]] = []
        self.singles: list[tuple[str, str]] = []

    @staticmethod
    def response(url: str) -> tuple[int, str, dict[str, str]]:
        payload = {
            "success": True,
            "responseObject": {"url": url},
        }
        return 200, json.dumps(payload), {"content-type": "application/json"}

    async def get_many(
        self, urls: list[str], *, lane: str = "foreground"
    ) -> dict[str, tuple[int, str, dict[str, str]]]:
        self.batches.append((list(urls), lane))
        return {url: self.response(url) for url in urls}

    async def get(
        self, url: str, *, lane: str = "foreground"
    ) -> tuple[int, str, dict[str, str]]:
        self.singles.append((url, lane))
        return self.response(url)

    async def reload(self) -> None:
        return None


def client(browser: FakeBrowser) -> FomoClient:
    instance = FomoClient(
        transport="browser",
        session_file=Path(".test-fomo-session-do-not-create.json"),
    )
    instance._http = object()  # type: ignore[assignment]
    instance._browser = browser
    return instance


class FomoApiBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_panels_use_one_foreground_batch_and_cache_results(self) -> None:
        browser = FakeBrowser()
        fomo = client(browser)
        first = await fomo.profile_panels("user-1")
        second = await fomo.profile_panels("user-1")

        self.assertEqual(len(browser.batches), 1)
        urls, lane = browser.batches[0]
        self.assertEqual(lane, "foreground")
        self.assertEqual(len(urls), 4)
        self.assertTrue(all(url.startswith(API_BASE) for url in urls))
        self.assertEqual(first, second)

    async def test_tracking_calls_are_routed_to_background_lane(self) -> None:
        browser = FakeBrowser()
        fomo = client(browser)
        await fomo.swaps("user-1", limit=25, fresh=True, background=True)
        await fomo.trades("user-1", fresh=True, background=True)
        self.assertEqual([lane for _url, lane in browser.singles], [
            "background", "background",
        ])

    async def test_trade_details_share_one_background_batch(self) -> None:
        browser = FakeBrowser()
        fomo = client(browser)
        results = await fomo.trade_details(["trade-1", "trade-2", "trade-1"])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(browser.batches), 1)
        urls, lane = browser.batches[0]
        self.assertEqual(lane, "background")
        self.assertEqual(urls, [
            f"{API_BASE}/trades/trade-1",
            f"{API_BASE}/trades/trade-2",
        ])


class FomoApiHeaderTests(unittest.TestCase):
    def test_direct_transport_requests_every_supported_chain(self) -> None:
        self.assertEqual(
            BROWSERISH["x-supported-chains"], SUPPORTED_CHAINS_HEADER
        )
        self.assertEqual(BROWSERISH["Content-Type"], "application/json")


if __name__ == "__main__":
    unittest.main()
