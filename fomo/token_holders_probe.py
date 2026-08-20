"""
token_holders_probe.py -- does FOMO publish the holders list its own UI shows?

    python token_holders_probe.py E3i7sTY5QYEBh3itepnomZQt7Eh5kzmHFk1vkm2pump

`/token` names a holder only when that wallet is already in `wallet_cache.json`,
because identity is a REVERSE lookup over handles `/fomo` has resolved before.
fomo.family's own token page has a `Holders (1,005)` tab listing traders by
name, so the server has a wallet -> handle mapping the bot never asked for.

This walks the routes that could serve that tab, GET only, and reports which
return holder-shaped data. Anything that works removes the cache dependency
from `/token` entirely.

Nothing here writes, trades or touches key material. Read-only probes only.

Stop `fomo_bot.py` first -- both use the same persistent Chrome profile.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from dotenv import load_dotenv

load_dotenv()

from fomo_api import FomoClient  # noqa: E402

OUT = Path("hunt_out")

# Seen in the JS bundle but never probed (FOMO_API.md section 4). The
# `userTokens` family is the strongest candidate: "aggregated snapshot" of user
# tokens is exactly the shape a per-token holders tab needs.
CANDIDATES = (
    "/v2/userTokens/aggregatedSnapshotById?{token}",
    "/v2/userTokens/aggregatedSnapshot?{token}",
    "/v2/userTokens/aggregatedSnapshot/interval?{token}",
    "/v2/tokens/{address}/holders",
    "/v2/tokens/{address}/traders",
    "/tokens/{address}/holders",
    "/v2/tokens/holders?{token}",
    "/v2/holders?{token}",
    "/holders?{token}",
)

# `tokenAddress` is the field name FOMO uses on trades and swaps; `tokenId` is
# the `{address}:{networkId}` form seen on balance rows.
QUERY_SHAPES = (
    ("tokenAddress", "{address}"),
    ("tokenId", "{address}:{network}"),
    ("address", "{address}"),
)


# Words that appear in every zod/express validation envelope and are never the
# name of the parameter we are missing.
_NOISE = {
    "errorCode", "ERR_VALIDATION_FAILED", "validation", "message", "code",
    "path", "expected", "received", "invalid_type", "required", "Required",
    "undefined", "string", "number", "array", "object", "issues", "body",
    "query", "params", "success", "responseObject", "statusCode", "name",
    "type", "keys", "union", "errors", "too_small", "minimum", "inclusive",
    "validationErrors", "field",
}


def missing_params(detail: str) -> list[str]:
    """Parameter names a validation failure is complaining about.

    A 400 ERR_VALIDATION_FAILED means the route EXISTS and rejected our query,
    which is far more useful than a 404 -- the envelope usually names the field
    it wanted under `path`.
    """
    # FOMO's envelope is {'validationErrors': [{'field': 'query.userId', ...}]}
    # so the field NAME is the value of 'field', not a bare quoted word. Reading
    # every quoted word instead returns the envelope's own keys.
    named = re.findall(r"['\"]field['\"]\s*:\s*['\"]([^'\"]+)['\"]", detail or "")
    fields = [name.split(".")[-1] for name in named]
    if not fields:
        fields = re.findall(r"['\"]([A-Za-z][A-Za-z0-9_]{2,30})['\"]", detail or "")
    ordered = [name for name in dict.fromkeys(fields) if name not in _NOISE]
    return ordered[:8]


def holder_shape(payload: Any) -> tuple[bool, str]:
    """Does this payload look like a list of traders holding the token?"""
    if not isinstance(payload, (dict, list)):
        return False, "not JSON object/array"
    rows: Any = payload
    if isinstance(payload, dict):
        for key in ("holders", "users", "userTokens", "traders", "data", "results"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            return False, f"dict keys: {sorted(payload)[:8]}"
    if not rows:
        return False, "empty list"
    first = rows[0]
    if not isinstance(first, dict):
        return False, "rows are not objects"
    keys = set(first)
    identity = keys & {"userHandle", "handle", "displayName", "userId", "user"}
    holding = keys & {"balance", "humanTokenAmount", "shiftedBalance",
                      "position", "totalCostBasis", "amount"}
    if identity and holding:
        return True, f"{len(rows)} row(s), identity+holding fields present"
    if identity:
        return True, f"{len(rows)} row(s), identity fields only: {sorted(identity)}"
    return False, f"row keys: {sorted(keys)[:10]}"


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("address", help="token contract address")
    parser.add_argument("--network", default="1399811149",
                        help="FOMO network id (default Solana)")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--path", default="",
                        help="call one route directly instead of probing")
    parser.add_argument("--param", action="append", default=[],
                        help="key=value query parameter, repeatable, with --path")
    args = parser.parse_args()

    if args.path:
        query = urlencode(dict(
            item.split("=", 1) for item in args.param if "=" in item
        ))
        manual = f"{args.path}?{query}" if query else args.path
        async with FomoClient() as fomo:
            try:
                payload = await fomo._get(manual, cache=False)
            except Exception as exc:
                print(f"{manual}\n  {exc}")
                return 1
        looks, why = holder_shape(payload)
        print(f"{manual}\n  {'HIT' if looks else 'miss'}: {why}")
        OUT.mkdir(exist_ok=True)
        (OUT / "holders_probe_manual.json").write_text(
            json.dumps(payload, indent=1)[:400_000], encoding="utf-8"
        )
        print(f"  payload -> {OUT / 'holders_probe_manual.json'}")
        return 0 if looks else 1

    paths: list[str] = []
    for template in CANDIDATES:
        if "{token}" not in template:
            paths.append(template.format(address=args.address))
            continue
        for field, value in QUERY_SHAPES:
            query = urlencode({
                field: value.format(address=args.address, network=args.network),
                "limit": args.limit,
            })
            paths.append(template.format(token=query, address=args.address))

    OUT.mkdir(exist_ok=True)
    hits: list[tuple[str, str]] = []
    # route -> parameter names its validator asked for. A 400 is a live route.
    live: dict[str, list[str]] = {}

    async def attempt(fomo: FomoClient, path: str) -> None:
        try:
            payload = await fomo._get(path, cache=False)
        except Exception as exc:
            detail = str(exc)
            print(f"  --   {path}\n         {detail}")
            if "ERR_VALIDATION_FAILED" in detail or "validation" in detail.lower():
                route = path.split("?", 1)[0]
                wanted = missing_params(detail)
                live[route] = sorted(set(live.get(route, []) + wanted))
            return
        looks, why = holder_shape(payload)
        print(f"  {'HIT ' if looks else 'miss'} {path}\n         {why}")
        if looks:
            hits.append((path, why))
        name = path.replace("/", "_").replace("?", "_")[:80]
        (OUT / f"holders_probe{name}.json").write_text(
            json.dumps(payload, indent=1)[:400_000], encoding="utf-8"
        )

    async with FomoClient() as fomo:
        for path in dict.fromkeys(paths):
            await attempt(fomo, path)

        # Second pass: a validator that named its fields told us how to call it.
        retries: list[str] = []
        for route, wanted in live.items():
            for field in wanted:
                for value in (args.address, f"{args.address}:{args.network}"):
                    retries.append(f"{route}?{urlencode({field: value})}")
                    retries.append(
                        f"{route}?{urlencode({f'{field}[]': args.address})}"
                    )
        retries = [path for path in dict.fromkeys(retries) if path not in paths]
        if retries:
            print(f"\nvalidators named {sum(len(v) for v in live.values())} "
                  f"field(s); retrying {len(retries)} shape(s)")
            for path in retries:
                await attempt(fomo, path)

    if live:
        print("\nroutes that exist but rejected the query "
              "(400, not 404) -- these are real endpoints:")
        for route, wanted in live.items():
            print(f"  {route}  wants: {', '.join(wanted) or 'unnamed field(s)'}")

    print(f"\n{len(hits)} candidate holder route(s)")
    for path, why in hits:
        print(f"  {path}  --  {why}")
    if hits:
        print(f"\nPayloads written to {OUT}/ -- check whether the rows carry a "
              "wallet as well as a handle. A handle alone still lets /token "
              "name a holder if the row's balance matches an on-chain owner.")
    elif live:
        print("\nNo holder-shaped payload yet, but the routes above are live. "
              "Their full 400 bodies are printed inline -- read the field names "
              "and rerun with --path/--param to call one directly.")
    else:
        print("\nNo route responded with holder-shaped data. The holders tab is "
              "then either a private/authenticated route or assembled client "
              "side, and /token stays on cache-based identity.")
    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
