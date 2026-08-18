"""
fomo_browser.py — Playwright transport for the FOMO API.

Why this exists: Cloudflare 403s a bare aiohttp client even from borz's
residential IP. Confirmed 2026-08-18 — the block is a bot-score rule
(HTML error page, cf-ray=…-AMS, no cf-mitigated), so it is the CLIENT that
is rejected, not the address. Chrome's TLS fingerprint, sec-ch-ua headers
and cf_clearance cookie are what the WAF wants, and the cheapest honest way
to have all three is to let Chrome make the request.

So: we drive a real Chrome with a persistent profile, keep a fomo.family tab
open, and run the API calls inside that page via fetch(). The page supplies
the right Origin, the cookie jar supplies cf_clearance, and the app's own
Privy token (read from localStorage at call time) supplies auth.

That last part removes the refresh-token dance entirely. The live app keeps
its own token fresh; we just read whatever is current. No Privy refresh, no
rotation race between browser and bot.

One-time setup:

    pip install playwright
    playwright install chromium      # only if you don't have Chrome
    python fomo_browser.py --login   # log into fomo.family, then close it

The profile lives in .chrome-profile/ and is gitignored. It cannot be your
everyday Chrome profile — Chrome locks a profile directory while running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("fomo.browser")

APP_ORIGIN = "https://fomo.family"
PROFILE_DIR = Path(os.getenv("FOMO_CHROME_PROFILE", ".chrome-profile")).resolve()
CHROME_CHANNEL = os.getenv("FOMO_CHROME_CHANNEL", "chrome")  # "" -> bundled chromium
NAV_TIMEOUT_MS = 45_000


class BrowserUnavailable(RuntimeError):
    """Playwright missing, Chrome won't start, or the profile isn't logged in."""


# The whole request happens inside the page. Returning a plain object keeps
# everything JSON-serialisable across the CDP boundary.
_FETCH_JS = """
async ({ url, timeoutMs }) => {
  const readToken = () => {
    for (const key of ['privy:token', 'privy:id_token']) {
      const raw = localStorage.getItem(key);
      if (!raw) continue;
      // Privy stores it JSON-encoded, i.e. with surrounding quotes.
      try { const v = JSON.parse(raw); if (typeof v === 'string' && v) return v; }
      catch (_) { if (raw) return raw; }
    }
    return null;
  };

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const token = readToken();
    const headers = { 'Accept': 'application/json, text/plain, */*' };
    if (token) headers['Authorization'] = 'Bearer ' + token;

    // cache:'no-store' matters — a 304 comes back with an empty body.
    const r = await fetch(url, {
      method: 'GET',
      credentials: 'include',
      cache: 'no-store',
      headers,
      signal: ctrl.signal,
    });
    const body = await r.text();
    const h = {};
    r.headers.forEach((v, k) => { h[k] = v; });
    return { ok: true, status: r.status, body, headers: h, hadToken: !!token };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  } finally {
    clearTimeout(timer);
  }
}
"""


class BrowserTransport:
    """
    Async GET transport backed by a real Chrome page.

    Mirrors what FomoClient._get needs: give it a full URL, get back
    (status, body, headers) exactly as if aiohttp had made the call.
    """

    def __init__(
        self,
        profile_dir: Path = PROFILE_DIR,
        headless: bool | None = None,
        channel: str = CHROME_CHANNEL,
        timeout: float = 25.0,
    ) -> None:
        self._profile_dir = Path(profile_dir)
        # Headed is meaningfully less bot-like than headless, and this runs on
        # a desktop anyway. FOMO_CHROME_HEADLESS=1 to override.
        if headless is None:
            headless = os.getenv("FOMO_CHROME_HEADLESS", "").strip() in ("1", "true", "yes")
        self._headless = headless
        self._channel = channel
        self._timeout = timeout

        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()

    # ---------------- lifecycle ----------------

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "playwright is not installed. `pip install playwright` "
                "(and `playwright install chromium` if you have no Chrome)."
            ) from exc

        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()

        launch: dict[str, Any] = {
            "user_data_dir": str(self._profile_dir),
            "headless": self._headless,
            "viewport": {"width": 1280, "height": 900},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self._channel:
            launch["channel"] = self._channel

        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(**launch)
        except Exception as exc:
            # Most common cause by far: Chrome already has this profile locked.
            if self._channel:
                log.warning("Chrome channel %r failed (%s), falling back to bundled chromium",
                            self._channel, exc)
                launch.pop("channel", None)
                try:
                    self._ctx = await self._pw.chromium.launch_persistent_context(**launch)
                except Exception as exc2:
                    await self._hard_stop()
                    raise BrowserUnavailable(f"could not start Chrome: {exc2}") from exc2
            else:
                await self._hard_stop()
                raise BrowserUnavailable(f"could not start Chrome: {exc}") from exc

        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._page.set_default_timeout(NAV_TIMEOUT_MS)
        await self._goto_app()
        log.info("browser transport ready (profile=%s, headless=%s)",
                 self._profile_dir, self._headless)

    async def _goto_app(self) -> None:
        assert self._page is not None
        if not self._page.url.startswith(APP_ORIGIN):
            await self._page.goto(APP_ORIGIN, wait_until="domcontentloaded",
                                  timeout=NAV_TIMEOUT_MS)
            # Give the SPA a moment to mint/restore the Privy token.
            await self._page.wait_for_timeout(2500)

    async def close(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
        finally:
            await self._hard_stop()

    async def _hard_stop(self) -> None:
        self._ctx = None
        self._page = None
        if self._pw:
            try:
                await self._pw.stop()
            finally:
                self._pw = None

    # ---------------- the actual request ----------------

    async def get(self, url: str) -> tuple[int, str, dict[str, str]]:
        if self._page is None:
            raise BrowserUnavailable("transport not started — call start() first")

        # Serialise: one page, one fetch at a time. These calls are ~200ms and
        # the bot's traffic is human-paced, so a lock is cheaper than tab churn.
        async with self._lock:
            await self._ensure_alive()
            result = await self._page.evaluate(
                _FETCH_JS, {"url": url, "timeoutMs": int(self._timeout * 1000)}
            )

        if not result.get("ok"):
            # A CORS rejection or an abort surfaces here, not as an HTTP status.
            raise BrowserUnavailable(f"in-page fetch failed for {url}: {result.get('error')}")

        if not result.get("hadToken"):
            log.warning("no Privy token in localStorage — profile may be logged out "
                        "(run: python fomo_browser.py --login)")

        headers = {str(k).lower(): str(v) for k, v in (result.get("headers") or {}).items()}
        return int(result["status"]), str(result.get("body") or ""), headers

    async def _ensure_alive(self) -> None:
        """Recover from a closed tab or a navigation that wandered off-origin."""
        try:
            if self._page is None or self._page.is_closed():
                self._page = await self._ctx.new_page()
                self._page.set_default_timeout(NAV_TIMEOUT_MS)
            await self._goto_app()
        except Exception as exc:
            raise BrowserUnavailable(f"browser page is not usable: {exc}") from exc

    async def reload(self) -> None:
        """Re-navigate the app so the SPA re-mints its Privy token after a 401."""
        async with self._lock:
            if self._page is None:
                raise BrowserUnavailable("transport not started")
            await self._page.goto(APP_ORIGIN, wait_until="domcontentloaded",
                                  timeout=NAV_TIMEOUT_MS)
            await self._page.wait_for_timeout(3000)

    async def logged_in(self) -> bool:
        """True if the profile currently holds a Privy access token."""
        await self._ensure_alive()
        token = await self._page.evaluate(
            "() => localStorage.getItem('privy:token')"
        )
        return bool(token)


# ---------------- one-time login helper ----------------

async def _login_flow() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    t = BrowserTransport(headless=False)
    await t.start()
    print(f"\nChrome is open on {APP_ORIGIN} using profile:\n  {t._profile_dir}\n")
    if await t.logged_in():
        print("Already logged in — nothing to do. Close the window when you're done.")
    else:
        print("Log into fomo.family in that window, wait for your profile to load,")
        print("then come back here and press Enter.")
    try:
        await asyncio.get_running_loop().run_in_executor(None, input, "Press Enter when done... ")
    except (EOFError, KeyboardInterrupt):
        pass
    ok = await t.logged_in()
    print("Privy token found — the bot can use this profile." if ok
          else "Still no Privy token. Did the login finish?")
    await t.close()
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--login" in sys.argv:
        raise SystemExit(asyncio.run(_login_flow()))
    print(__doc__)
