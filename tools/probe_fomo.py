"""
probe_fomo.py
─────────────
Finds the endpoint that maps a FOMO username -> its connected wallets.

Frontrun's published API has no such route: every documented endpoint is keyed
by Twitter handle, and the Fomo label the extension shows comes out of
wallets-batch-query (wallet -> Fomo, i.e. the wrong direction). So the reverse
lookup has to come from fomo.family itself, and this script finds out which of
its routes actually answers.

Run on the VPS (a sandbox gets Cloudflare-blocked):
    cd /root/memecoin-bot-new && python3 tools/probe_fomo.py koyla_sol

FREE for the fomo.family probes. The optional Frontrun cross-check at the end
costs ~400 credits and only runs with --frontrun.

Whatever route returns a profile with wallet addresses — put it first in
FOMO_USER_PATHS in src/frontrun.py.
"""

import json
import os
import re
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

FOMO_HEADERS = {
    "accept": "application/json",
    "user-agent": UA,
    "origin": "https://fomo.family",
    "referer": "https://fomo.family/",
}

# Ordered by how likely they are to be real. prod-api/v2/users/{u} is the one
# the Omo extension (github.com/anondevv69/Omo) reads.
CANDIDATES = [
    ("https://prod-api.fomo.family/v2/users/{u}", "GET"),
    ("https://prod-api.fomo.family/v2/users/by-username/{u}", "GET"),
    ("https://prod-api.fomo.family/v2/users/username/{u}", "GET"),
    ("https://prod-api.fomo.family/v2/profile/{u}", "GET"),
    ("https://prod-api.fomo.family/v2/profiles/{u}", "GET"),
    ("https://prod-api.fomo.family/v2/users/{u}/wallets", "GET"),
    ("https://prod-api.fomo.family/v2/search?q={u}", "GET"),
    ("https://prod-api.fomo.family/v1/users/{u}", "GET"),
    ("https://api.fomo.family/v2/users/{u}", "GET"),
]

SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def find_addresses(obj, path="", out=None):
    """Walk any JSON and collect every value that looks like a wallet address,
    remembering where it was found so we know which field to read."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            find_addresses(v, f"{path}.{k}" if path else k, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            find_addresses(v, f"{path}[{i}]", out)
    elif isinstance(obj, str):
        if SOL_RE.match(obj) or EVM_RE.match(obj):
            out.append((path, obj))
    return out


def probe(username):
    print(f"Probing fomo.family for username: {username}\n")
    winners = []

    for template, method in CANDIDATES:
        url = template.format(u=username)
        try:
            r = requests.request(method, url, headers=FOMO_HEADERS, timeout=15)
        except Exception as e:
            print(f"  [ERR ] {url}\n         {type(e).__name__}: {e}")
            continue

        marker = "OK  " if r.status_code == 200 else f"{r.status_code:<4}"
        print(f"  [{marker}] {url}  ({len(r.content)} bytes)")

        if r.status_code == 403 and "cloudflare" in r.text.lower():
            print("         ^ Cloudflare challenge — this IP is blocked.")
            continue
        if r.status_code != 200:
            continue

        try:
            body = r.json()
        except Exception:
            print("         ^ 200 but not JSON")
            continue

        found = find_addresses(body)
        if found:
            print(f"         ^^ {len(found)} address-shaped value(s):")
            for p, a in found[:10]:
                print(f"            {p:<40} {a}")
            winners.append((url, body, found))
        else:
            print("         ^ 200 JSON but no addresses; keys: "
                  f"{list(body)[:12] if isinstance(body, dict) else type(body).__name__}")

    if not winners:
        print("\n" + "=" * 66)
        print("NO ROUTE RETURNED WALLETS.")
        print("=" * 66)
        print("Most likely fomo.family requires an authenticated session — the")
        print("Omo extension reads its API using YOUR logged-in browser cookies.")
        print("Next step: open fomo.family in Chrome, log in, visit the profile,")
        print("open DevTools -> Network, filter 'prod-api', and paste me the")
        print("request URL + response. That gives us the exact route and shape.")
        return 1

    print("\n" + "=" * 66)
    print("WINNER — put this first in FOMO_USER_PATHS (src/frontrun.py)")
    print("=" * 66)
    url, body, found = winners[0]
    print(f"  {url}\n")
    print(json.dumps(body, indent=2)[:2500])
    return 0


def cross_check(username):
    """Optional: what does Frontrun think, and does any wallet name this
    username on Fomo? Costs ~400 credits."""
    key = os.getenv("FRONTRUN_API_KEY", "").strip()
    if not key:
        print("\n(skipping Frontrun cross-check — no FRONTRUN_API_KEY)")
        return
    print("\n" + "=" * 66)
    print("FRONTRUN CROSS-CHECK (~400 credits)")
    print("=" * 66)
    h = {"accept": "application/json", "Authorization": f"Bearer {key}",
         "X-Copilot-Client-Language": "en"}
    r = requests.get(
        f"https://api.frontrun.pro/api/v1/pro/twitter/{username}/associated-wallets",
        headers=h, timeout=60)
    print(f"  associated-wallets: HTTP {r.status_code}")
    if r.status_code != 200:
        return
    addrs = [a["address"] for a in
             (r.json().get("data") or {}).get("addresses", []) if a.get("address")]
    print(f"  {len(addrs)} address(es): {addrs}")
    if not addrs:
        return

    r2 = requests.post(
        "https://api.frontrun.pro/api/v1/pro/twitter/wallets-batch-query",
        headers={**h, "Content-Type": "application/json"},
        json={"wallets": [{"chain": "SOLANA", "address": a} for a in addrs]},
        timeout=60)
    print(f"  batch-query: HTTP {r2.status_code}")
    if r2.status_code != 200:
        return
    print(json.dumps(r2.json(), indent=2)[:2500])

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from src.frontrun import _extract_wallets, fomo_username
        print("\n  Fomo username parsed from each wallet:")
        for w in _extract_wallets(r2.json().get("data")):
            print(f"    {w.get('address')}  ->  {fomo_username(w)!r}")
        print("\n  If those are None but the extension shows a Fomo label,")
        print("  paste me the batch-query response and I'll fix the parser.")
    except Exception as e:
        print(f"  parser import failed: {e}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    username = args[0].lstrip("@").strip()
    rc = probe(username)
    if "--frontrun" in sys.argv:
        cross_check(username)
    return rc


if __name__ == "__main__":
    sys.exit(main())
