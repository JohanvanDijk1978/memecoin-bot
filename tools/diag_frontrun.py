"""
diag_frontrun.py
────────────────
Dumps the RAW Frontrun API response for a handle, bypassing the cache and the
parser entirely. Use this when /wallet says "no wallets found" for someone you
know has wallets — it shows whether the problem is auth, the endpoint, or our
extractor guessing the wrong key.

Run on the VPS from the repo root:
    cd /root/memecoin-bot-new && python3 tools/diag_frontrun.py collectible

Add --mentioned to hit mentioned-wallets instead (500 credits vs 400).

COSTS CREDITS. associated-wallets is 400 credits per run, and the Gold plan
only includes 100,000/month — don't loop this.

Reads FRONTRUN_API_KEY from .env. Prints the key's length only, never the key.
"""

import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("FRONTRUN_API_KEY", "").strip()
BASE = "https://api.frontrun.pro/api/v1"

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {API_KEY}",
    "X-Copilot-Client-Language": "en",
    "X-Copilot-Client-Platform": "CHROME_EXTENSION",
    "X-Copilot-Client-Version": "1.0.0",
}


def show(title, resp):
    print("=" * 66)
    print(title)
    print("=" * 66)
    print(f"  HTTP {resp.status_code}  ({len(resp.content)} bytes)")
    body = resp.text
    try:
        parsed = json.loads(body)
        print(json.dumps(parsed, indent=2)[:4000])
        return parsed
    except Exception:
        print("  (not JSON)")
        print(body[:1500])
        return None


def walk_shape(obj, prefix="data", depth=0):
    """Print the key structure so we can see where the wallet list actually is."""
    pad = "  " * (depth + 1)
    if isinstance(obj, dict):
        for k, v in obj.items():
            kind = type(v).__name__
            extra = f" [{len(v)}]" if isinstance(v, (list, dict)) else ""
            print(f"{pad}{prefix}.{k}  <{kind}>{extra}")
            if depth < 2:
                walk_shape(v, f"{prefix}.{k}", depth + 1)
    elif isinstance(obj, list) and obj:
        print(f"{pad}{prefix}[0]  <{type(obj[0]).__name__}>")
        if depth < 2:
            walk_shape(obj[0], f"{prefix}[0]", depth + 1)


def main():
    if not API_KEY:
        print("FAIL: FRONTRUN_API_KEY is not set in .env")
        return 1

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    handle = args[0].lstrip("@").strip()
    endpoint = "mentioned-wallets" if "--mentioned" in sys.argv else "associated-wallets"

    print(f"key length: {len(API_KEY)} chars (not printed)")
    print(f"handle:     {handle}")
    print(f"endpoint:   {endpoint}\n")

    # 1. Cheapest possible call first — proves auth works before spending 400.
    r = requests.get(
        f"{BASE}/pro/twitter/{handle}/smart-followers/count",
        headers=HEADERS, timeout=20,
    )
    show("1. SMART-FOLLOWER COUNT (3 credits — auth smoke test)", r)
    if r.status_code in (401, 403):
        print("\n>> Auth is the problem. The key is rejected; nothing else will work.")
        return 1
    if r.status_code == 402:
        print("\n>> Out of credits.")
        return 1

    # 2. The call that actually matters.
    print()
    r2 = requests.get(
        f"{BASE}/pro/twitter/{handle}/{endpoint}",
        headers=HEADERS, timeout=60,
    )
    parsed = show(f"2. {endpoint.upper()} ({'500' if 'mentioned' in endpoint else '400'} credits)", r2)

    if isinstance(parsed, dict):
        print("\n" + "=" * 66)
        print("3. RESPONSE SHAPE  (where does the wallet list live?)")
        print("=" * 66)
        walk_shape(parsed.get("data"), "data")

        print("\n" + "=" * 66)
        print("4. WHAT OUR PARSER MAKES OF IT")
        print("=" * 66)
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            from src.frontrun import _extract_wallets, has_fomo_tag
            wallets = _extract_wallets(parsed.get("data"))
            print(f"  extracted: {len(wallets)} wallet(s)")
            for w in wallets:
                tags = [t.get("name") for t in (w.get("tags") or []) if isinstance(t, dict)]
                flag = "FOMO" if has_fomo_tag(w) else "    "
                print(f"  [{flag}] {w.get('address')}  tags={tags}")
            if not wallets and parsed.get("data"):
                print("\n  >> Parser found nothing but the API returned data.")
                print("     Send section 3 to Claude — the extractor needs the real key.")
        except Exception as e:
            print(f"  could not import parser: {e}")

    # 5. Credit balance, so you know what this cost.
    print()
    r3 = requests.get(f"{BASE}/user/paid-api/points/{API_KEY}", headers=HEADERS, timeout=20)
    show("5. CREDITS REMAINING", r3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
