from __future__ import annotations

import unittest

from fomo_browser import APP_ORIGIN, BrowserTransport
from fomo_chains import SUPPORTED_CHAINS_HEADER


class Page:
    def __init__(self) -> None:
        self.url = APP_ORIGIN
        self.calls: list[list[str]] = []
        self.scripts: list[str] = []

    def is_closed(self) -> bool:
        return False

    def set_default_timeout(self, _timeout: int) -> None:
        return None

    async def evaluate(self, script: str, args: dict) -> list[dict]:
        urls = list(args["urls"])
        self.calls.append(urls)
        self.scripts.append(script)
        return [
            {
                "url": url,
                "ok": True,
                "status": 200,
                "body": "{}",
                "headers": {},
                "hadToken": True,
            }
            for url in urls
        ]


class Context:
    def __init__(self, background: Page) -> None:
        self.background = background
        self.created = 0

    async def new_page(self) -> Page:
        self.created += 1
        return self.background


class BrowserBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_fetch_uses_one_evaluation_and_separate_background_page(self) -> None:
        foreground = Page()
        background = Page()
        transport = BrowserTransport()
        transport._ctx = Context(background)
        transport._page = foreground

        urls = [f"{APP_ORIGIN}/api/{index}" for index in range(4)]
        results = await transport.get_many(urls)
        await transport.get(urls[0], lane="background")

        self.assertEqual(foreground.calls, [urls])
        self.assertEqual(background.calls, [[urls[0]]])
        self.assertEqual(set(results), set(urls))
        self.assertIs(transport._background_page, background)

    async def test_fetch_requests_every_chain_supported_by_fomo(self) -> None:
        page = Page()
        transport = BrowserTransport()
        transport._ctx = Context(Page())
        transport._page = page

        await transport.get(f"{APP_ORIGIN}/api/profile")

        self.assertEqual(len(page.scripts), 1)
        self.assertIn("'x-supported-chains'", page.scripts[0])
        self.assertIn(SUPPORTED_CHAINS_HEADER, page.scripts[0])
        self.assertIn("'Content-Type': 'application/json'", page.scripts[0])


if __name__ == "__main__":
    unittest.main()
