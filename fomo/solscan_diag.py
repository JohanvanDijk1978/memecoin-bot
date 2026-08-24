"""Say exactly which Solscan endpoints this key can reach.

    python solscan_diag.py
    python solscan_diag.py <mint>

Solscan serves the same engine under `/v2.0` (paid) and `/playground` (free),
and its gateway answers 401 before it routes -- so an unreachable prefix, a
wrong header and a dead key are all the same status code. This walks every
combination and prints the body for each, which is the only thing that names
the reason.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover -- dotenv is optional
    pass

HOST = os.getenv("SOLSCAN_HOST", "https://pro-api.solscan.io").rstrip("/")
DEFAULT_MINT = "GUmbtfjSZkybSFgPibBcvwExEBdXwewJHR5PkTjzpump"
PREFIXES = ("playground", "v2.0")
STYLES = {
    "token": lambda key: {"token": key},
    "bearer": lambda key: {"Authorization": f"Bearer {key}"},
    "raw": lambda key: {"Authorization": key},
    "x-api-key": lambda key: {"x-api-key": key},
}


def describe_key(key: str) -> None:
    print(f"key length      : {len(key)}")
    print(f"key starts with : {key[:12]}...")
    print(f"key ends with   : ...{key[-8:]}")
    parts = key.split(".")
    if len(parts) == 3:
        try:
            pad = parts[1] + "=" * (-len(parts[1]) % 4)
            print(f"key claims      : {json.loads(base64.urlsafe_b64decode(pad))}")
        except Exception:
            print("key claims      : (could not decode)")
    else:
        print("key claims      : not a JWT -- Solscan keys are JWTs")
    print()


async def probe(http, url, params, headers):
    try:
        response = await http.get(url, params=params, headers=headers)
    except Exception as exc:
        return None, f"request failed: {exc}"
    body = " ".join((response.text or "").split())[:180]
    return response.status_code, body


async def main() -> int:
    mint = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MINT
    key = os.getenv("SOLSCAN_API_KEY", "").strip()
    if not key:
        print("SOLSCAN_API_KEY is not set. Put it in .env and re-run.")
        return 1
    describe_key(key)

    # (logical path, [parameter spellings to try])
    endpoints = [
        ("token/holders", [
            {"address": mint, "page": 1, "page_size": 10},
            {"tokenAddress": mint, "offset": 0, "limit": 10},
        ]),
        ("account/transfer", [
            {"address": "11111111111111111111111111111111", "flow": "in",
             "page": 1, "page_size": 10},
        ]),
        ("account/transactions/enhanced", [
            {"address": "11111111111111111111111111111111", "limit": 1},
        ]),
    ]

    working: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=30) as http:
        # Which header does this key speak? Settle it on one endpoint first.
        good_style = None
        for style, build in STYLES.items():
            for prefix in PREFIXES:
                url = f"{HOST}/{prefix}/account/transactions/enhanced"
                status, body = await probe(
                    http, url,
                    {"address": "11111111111111111111111111111111", "limit": 1},
                    build(key),
                )
                print(f"  header {style:<10} /{prefix:<10} -> HTTP {status}  {body}")
                if status and status < 400 and '"success":false' not in body:
                    good_style = style
                    break
            if good_style:
                break
        print()

        if not good_style:
            print(
                "No header style was accepted anywhere.\n"
                "  1. The key may not be activated -- open "
                "https://solscan.io/user/profile#api_management and click "
                "'Activate my API key'.\n"
                "  2. Or it was regenerated; the newest key invalidates the "
                "previous one, so copy the current value into .env.\n"
            )
            return 1

        print(f"Header style that works: {good_style}\n")
        for path, variants in endpoints:
            for prefix in PREFIXES:
                hit = False
                for variant in variants:
                    url = f"{HOST}/{prefix}/{path}"
                    status, body = await probe(
                        http, url, variant, STYLES[good_style](key)
                    )
                    keys = ",".join(variant)
                    print(f"  /{prefix:<10} {path:<30} [{keys}] -> HTTP {status}  {body}")
                    if status and status < 400 and '"success":false' not in body:
                        working.setdefault(path, f"/{prefix}  [{keys}]")
                        hit = True
                        break
                if hit:
                    break
            print()

    print("Reachable with this key:")
    for path, where in working.items():
        print(f"  {path:<32} {where}")
    for path, _ in endpoints:
        if path not in working:
            print(f"  {path:<32} NOT REACHABLE")

    if working:
        prefix = next(iter(working.values())).split()[0].strip("/")
        print(
            f"\nPin it if you want to skip negotiation at runtime:\n"
            f"  SOLSCAN_PREFIXES={prefix}\n"
            f"  SOLSCAN_AUTH_STYLE={good_style}\n"
        )
    return 0 if working else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
