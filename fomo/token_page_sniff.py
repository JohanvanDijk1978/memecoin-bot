"""
token_page_sniff.py -- record every API call fomo.family's token page makes.

    python token_page_sniff.py E3i7sTY5QYEBh3itepnomZQt7Eh5kzmHFk1vkm2pump
    python token_page_sniff.py 0xe172e9b6... --chain base
    python token_page_sniff.py --url https://fomo.family/tokens/solana/<address>

Guessing route names is over: `/v2/userTokens/aggregatedSnapshot*` all require
`query.userId`, so that family is one user's portfolio, not a token's holders.
The Holders tab clearly has a source, so watch the page load and read it off
the wire.

Opens the same persistent Chrome profile the bot uses, navigates to the token
page, clicks the Holders tab, and prints every `prod-api.fomo.family` request
with its status and payload shape. Bodies land in `hunt_out/` so the winning
route can be replayed offline.

Read-only: it loads a public page and records traffic the browser was going to
send anyway. No writes, no trading routes.

Stop `fomo_bot.py` first -- both use the same persistent Chrome profile.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from fomo_browser import CHROME_CHANNEL, PROFILE_DIR  # noqa: E402

OUT = Path("hunt_out")
API_HOST = "prod-api.fomo.family"

# Confirmed 2026-08-20: the token page is chain-scoped --
# https://fomo.family/tokens/solana/<address>. The unscoped shapes are kept as
# fallbacks in case other chains route differently.
URL_SHAPES = (
    "https://fomo.family/tokens/{chain}/{address}",
    "https://fomo.family/token/{chain}/{address}",
    "https://fomo.family/tokens/{address}",
    "https://fomo.family/token/{address}",
)

# The slug fomo.family uses in that path, by FOMO network id.
CHAIN_SLUGS = {
    "1399811149": "solana", "8453": "base", "56": "bsc",
    "1": "ethereum", "4663": "robinhood",
}

IDENTITY_KEYS = {"userHandle", "handle", "displayName", "userId", "user",
                 "address", "owner", "wallet"}
HOLDING_KEYS = {"balance", "humanTokenAmount", "shiftedBalance", "position",
                "totalCostBasis", "amount", "avgEntryPrice", "equity",
                "percentage", "percent", "share", "holding"}


def _holderish(rows: Any) -> str:
    """Non-empty description when these rows name someone and a holding."""
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return ""
    keys = set(rows[0])
    identity = keys & IDENTITY_KEYS
    holding = keys & HOLDING_KEYS
    if identity and holding:
        return f"{len(rows)} row(s), identity {sorted(identity)} + {sorted(holding)}"
    return ""


def describe(body: str, depth: int = 3) -> tuple[bool, str]:
    """Summarise a payload and flag the ones shaped like a holders list.

    The search recurses. `/hodlers/top` returns ONE row per requested token
    whose own keys are `tokenAddress`/`totalHolders`, and the actual holders sit
    nested under `topHolders` -- a top-level-only check calls that a miss, which
    is exactly what happened the first time this ran.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return False, f"non-JSON ({len(body)} bytes)"
    inner = payload.get("responseObject", payload) if isinstance(payload, dict) else payload

    best = ""
    seen_lists: list[tuple[str, Any]] = []

    def walk(node: Any, trail: str, level: int) -> None:
        nonlocal best
        if level > depth or best:
            return
        if isinstance(node, list):
            seen_lists.append((trail, node))
            if found := _holderish(node):
                best = f"{trail or 'root'}: {found}"
                return
            for item in node[:3]:
                walk(item, f"{trail}[]", level + 1)
        elif isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, (list, dict)):
                    walk(value, f"{trail}.{key}" if trail else key, level + 1)

    walk(inner, "", 0)
    if best:
        return True, f"HOLDERS? {best}"
    if seen_lists:
        trail, rows = seen_lists[0]
        if rows and isinstance(rows[0], dict):
            return False, (f"{len(rows)} row(s) {trail or 'root'}, "
                           f"keys {sorted(rows[0])[:8]}")
        return False, f"{len(rows)} scalar row(s) {trail or 'root'}"
    if isinstance(inner, dict):
        return False, f"object keys: {sorted(inner)[:8]}"
    return False, "empty"


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("address", nargs="?", default="", help="token contract address")
    parser.add_argument("--url", default="", help="exact token page URL to open")
    parser.add_argument("--chain", default="",
                        help="chain slug in the page URL (default solana, or "
                             "base for a 0x address); accepts a FOMO network id")
    parser.add_argument("--tab", default="Holders",
                        help="tab label to click after load (default Holders)")
    parser.add_argument("--wait", type=float, default=6.0,
                        help="seconds to keep recording after the click")
    args = parser.parse_args()
    if not args.address and not args.url:
        parser.error("give a token address or --url")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("playwright is required: pip install playwright")
        return 2

    chain = CHAIN_SLUGS.get(args.chain.strip(), args.chain.strip().lower())
    if not chain:
        chain = "base" if args.address.lower().startswith("0x") else "solana"
    urls = [args.url] if args.url else [
        shape.format(address=args.address, chain=chain) for shape in URL_SHAPES
    ]

    OUT.mkdir(exist_ok=True)
    seen: dict[str, tuple[int, str]] = {}
    pending: list[asyncio.Task[None]] = []

    async def record(response: Any) -> None:
        url = response.url
        if API_HOST not in url:
            return
        try:
            body = await response.text()
        except Exception as exc:  # a body can be gone after navigation
            seen[url] = (response.status, f"body unavailable: {exc}")
            return
        looks, why = describe(body)
        seen[url] = (response.status, ("*** " if looks else "") + why)
        # Every body, not just the flagged ones: the first run classified
        # /hodlers/top as a miss and then had nothing on disk to re-read.
        name = url.split(API_HOST)[-1].replace("/", "_").replace("?", "_")[:90]
        (OUT / f"sniff{name or '_root'}.json").write_text(
            body[:400_000], encoding="utf-8"
        )

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False,
            channel=CHROME_CHANNEL or None,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        page.on("response", lambda response: pending.append(
            asyncio.create_task(record(response))
        ))

        opened = ""
        for url in urls:
            print(f"opening {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except Exception as exc:
                print(f"  navigation failed: {str(exc)[:120]}")
                continue
            await page.wait_for_timeout(3_000)
            if await page.get_by_text(args.tab, exact=False).count():
                opened = url
                break
            print(f"  no '{args.tab}' tab on this URL shape")

        if not opened:
            print("\nCould not find the token page. Open it in your browser, "
                  "copy the address bar, and rerun with --url <that URL>.")
        else:
            try:
                await page.get_by_text(args.tab, exact=False).first.click()
                print(f"clicked '{args.tab}'")
            except Exception as exc:
                print(f"could not click '{args.tab}': {str(exc)[:120]}")
            await page.wait_for_timeout(int(args.wait * 1000))
            # Holder lists are usually paginated on scroll; one nudge often
            # triggers the follow-up request that reveals the cursor param.
            await page.mouse.wheel(0, 2_000)
            await page.wait_for_timeout(2_500)

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await context.close()

    print(f"\n{len(seen)} API call(s) from the page\n")
    interesting = []
    for url, (status, why) in sorted(seen.items()):
        path = url.split(API_HOST)[-1]
        marker = "***" if why.startswith("*** ") else "   "
        print(f"  {marker} {status} {path}\n         {why.removeprefix('*** ')}")
        if marker == "***":
            interesting.append(path)

    if interesting:
        print("\nholder-shaped payload(s) -- this is the route /token should call:")
        for path in interesting:
            print(f"  {path}")
        print(f"\nBodies in {OUT}/. Check whether the rows carry a wallet as well "
              "as a handle; a handle plus an exact balance is enough to match "
              "an on-chain owner.")
    elif seen:
        print("\nNo holders-shaped payload. Widen --wait, or the tab may render "
              "from a payload already fetched with the page.")
    return 0 if interesting else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
