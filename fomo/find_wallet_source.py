"""
find_wallet_source.py — which request actually carries a trader's real wallet?

wallet_check.py proved the correct address is not in /v2/users/userHandle/*,
/leaderboard or /balances. So the profile page gets it somewhere else. Instead
of guessing endpoint names, open the real profile page in our logged-in Chrome,
record every response it makes, and grep them all for the address we know is
right.

    python find_wallet_source.py
    python find_wallet_source.py onmycheck=Ay77dk...CuD2

Prints the URL, the JSON path inside it, and the neighbouring keys — enough to
know both which endpoint to call and which field to read.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any

from fomo_browser import APP_ORIGIN, BrowserTransport, BrowserUnavailable

TARGETS = {
    "onmycheck": "Ay77dkJkbjPCLbhHmwNg5z4WVtP2bMUpjKNnWFo1CuD2",
    "FIippingProfits": "DdM1tyCdoEyoxYYmGMjdf5rRPcpmj3UzZTpE7ScuTf7d",
}

SETTLE_MS = 9000


def json_paths(node: Any, needle: str, path: str = "") -> list[str]:
    """Every path in a decoded JSON tree whose string value equals `needle`."""
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += json_paths(v, needle, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += json_paths(v, needle, f"{path}[{i}]")
    elif isinstance(node, str) and node == needle:
        out.append(path or "<root>")
    return out


def parent_of(node: Any, path: str) -> Any:
    """The object containing `path`, so we can show its sibling keys."""
    parts = re.findall(r"[^.\[\]]+", path)[:-1]
    cur = node
    for p in parts:
        try:
            cur = cur[int(p)] if p.isdigit() else cur[p]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return cur


async def hunt(handle: str, expected: str) -> None:
    print(f"\n{'='*74}\n  @{handle}  looking for {expected}\n{'='*74}")

    t = BrowserTransport(headless=False)
    await t.start()
    page = t._page
    seen: list[tuple[str, int, str]] = []
    bodies: dict[str, str] = {}

    async def on_response(resp: Any) -> None:
        url = resp.url
        if url.startswith("data:") or re.search(r"\.(png|jpe?g|gif|svg|webp|woff2?|css|ico)(\?|$)", url):
            return
        try:
            body = await resp.text()
        except Exception:
            return
        seen.append((url, resp.status, resp.headers.get("content-type", "")))
        bodies[url] = body

    page.on("response", lambda r: asyncio.create_task(on_response(r)))

    url = f"{APP_ORIGIN}/profile/{handle}"
    print(f"  navigating to {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(SETTLE_MS)
    # Profiles lazy-load panels; a scroll usually triggers the rest.
    try:
        await page.mouse.wheel(0, 2000)
        await page.wait_for_timeout(3000)
    except Exception:
        pass

    print(f"  captured {len(seen)} responses")

    hits = 0
    for u, status, ctype in seen:
        body = bodies.get(u, "")
        if expected not in body:
            continue
        hits += 1
        print(f"\n  HIT  {status}  {u}\n       content-type: {ctype.split(';')[0]}")
        try:
            data = json.loads(body)
        except ValueError:
            idx = body.find(expected)
            print(f"       (not JSON) …{body[max(0,idx-120):idx+120]}…")
            continue
        for p in json_paths(data, expected):
            print(f"       path: {p}")
            par = parent_of(data, p)
            if isinstance(par, dict):
                keys = {k: v for k, v in par.items()
                        if isinstance(v, (str, int, float, bool, type(None)))}
                print(f"       siblings: {json.dumps(keys, default=str)[:400]}")

    # Is it even rendered on the page, or only linked out to Solscan?
    in_dom = await page.evaluate(
        "(a) => ({ text: document.body.innerText.includes(a),"
        " href: [...document.querySelectorAll('a[href]')].some(x => x.href.includes(a)) })",
        expected,
    )
    print(f"\n  visible in page text: {in_dom['text']}   in a link href: {in_dom['href']}")

    if not hits:
        print("\n  NOT in any captured response. Next step: it may be derived client-side,")
        print("  or loaded only after an interaction (opening a wallet/portfolio panel).")
        print("  Non-image URLs seen, for manual scanning:")
        for u, status, ctype in seen:
            if "fomo" in u or "api" in u:
                print(f"    {status}  {u}")

    await t.close()


async def main() -> int:
    targets = dict(TARGETS)
    for arg in sys.argv[1:]:
        h, _, exp = arg.partition("=")
        if exp:
            targets = {h: exp}
    try:
        for handle, expected in targets.items():
            await hunt(handle, expected)
    except BrowserUnavailable as exc:
        print(f"browser unavailable: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
