# Solscan Integration Handoff

## Problem Statement
`/token` was missing holders that Solscan already ranks. Example:
`GccvEcZqMrcNdp3CarCasGdN5dUfdQ4wZ6iQsk5myRzX` (2.6% of $Morty) showed on
Solscan but not in the Discord bot output. Holders came only from Helius DAS,
which indexes behind Solscan.

## Why it was 401 — settled
**The key is a free key, and free keys only reach `/playground/...`.** The paid
Pro API lives at `/v2.0/...` on the same host and rejects a free key outright.
Confirmed working from the shell:

```
curl.exe -X GET "https://pro-api.solscan.io/playground/account/transactions/enhanced?address=11111111111111111111111111111111&limit=1" ^
  -H "accept: application/json" -H "token: <key>"
```

Two things made this hard to see. Solscan's gateway answers `401 {"message":
"Token is missing"}` *before* it routes, so an unauthenticated probe cannot
tell you which prefixes exist. And the old code logged only the status code —
never the body — so a wrong prefix, a wrong header and a dead key were
indistinguishable.

The header is `token: <key>`, not `Authorization: Bearer <key>`.

## What the code does now

### `solscan_api.py` (new)
One request helper both callers share. It resolves at runtime, per endpoint,
and caches the answer for the life of the process:

- **Prefix** — tries `/playground` then `/v2.0` (`SOLSCAN_PREFIXES` overrides,
  `SOLSCAN_PREFIX` pins one). Playground first costs a paid key nothing, since
  a paid key reaches it too.
- **Header** — `token:` → `Authorization: Bearer` → bare `Authorization:` →
  `x-api-key:` (`SOLSCAN_AUTH_STYLE` pins one).
- **Parameter spelling** — callers may pass several parameter dicts in
  preference order; a 400 falls through to the next. `token/holders` offers
  both `address`/`page`/`page_size` and `tokenAddress`/`offset`/`limit`.
- Reads `SOLSCAN_API_KEY` **at call time**, so a refreshed `.env` is not frozen
  into a module constant the way it used to be.
- Treats a 200 carrying `"success": false` as a failure, not as data.
- Logs the **response body** when nothing works. Never raises — both callers
  have a fallback and take it quietly.
- `SOLSCAN_HOST` overrides the host.

Once an endpoint resolves, later calls are a single request: no re-probing. A
path that answered nothing is not re-probed for `SOLSCAN_RETRY_SECONDS`
(default 300), so a dead endpoint costs one round of probes, not one per call.
`reset_resolution()` clears both caches --- `test_connected_wallets.setUp`
calls it, since the state is process-wide by design and would otherwise leak
between tests.

### `token_intelligence.py`
- `_solscan_holders(mint, supply, limit)` rewritten. Pages up to 3 × 40.
- Reads `data.items`, with `data` as a bare list, `data.result` and
  `data.holders` also accepted — the playground and v2.0 shapes differ.
- **Amounts are scaled by the item's `decimals`.** The old code used the raw
  integer, so balances would have come out 10^6 too large had auth worked.
- **Totals are summed per `owner`.** Solscan ranks token accounts, so a wallet
  holding through two accounts used to rank below its real weight.
- Percentage from token supply when known, else Solscan's `percentage` field,
  normalised (Solscan sends a fraction on some tokens, a percent on others).
- `_das_holders()` still runs Helius DAS and Solscan in parallel and keeps
  whichever returned more owners — so if `token/holders` turns out not to be a
  playground endpoint, `/token` degrades to today's Helius-only behaviour and
  logs why.

### `connected_wallets.py`
`_solscan_funder()` goes through the same helper. `SOLSCAN_TRANSFER_URL` now
defaults to the logical path `account/transfer` rather than a hardcoded
`/v2.0` URL; a full URL in the env still works and skips prefix resolution.
The Helius walk-back is still the fallback.

### `solscan_diag.py` (new)
Standalone. Prints the key's decoded JWT claims, settles which header style the
key speaks, then walks `token/holders`, `account/transfer` and
`account/transactions/enhanced` across both prefixes and both parameter
spellings — printing status **and body** for each — and ends with the
`SOLSCAN_PREFIXES` / `SOLSCAN_AUTH_STYLE` values to pin.

```
python solscan_diag.py
python solscan_diag.py <mint>
```

## Reading Solscan's 401s
Its gateway authenticates *before* it routes, and the message says which half
failed:

| body | meaning |
| --- | --- |
| `Token is missing` | no key reached Solscan at all |
| `Token is invalid` | a key reached it and was rejected |

Verified by probing every prefix with a deliberately bogus key: unknown routes
and real ones answer identically, so route existence cannot be probed without
a valid key. This is also why the first version of the warning misled --- it
printed only the *last* attempt, which is the `x-api-key` style, and that style
sends no token header, so it always said "Token is missing" no matter what the
real refusal had been. The warning now prints every attempt and names the
conclusion.

## Answered: Solscan cannot serve holders on a free key

The runtime probe settled it in two requests:

```
/playground token/holders -> 404 {"code":404,"message":"Not found"}
/v2.0       token/holders -> 401 {"code":401,"message":"Unauthorized: Please upgrade your api key level."}
```

A **404 means the key was accepted** --- routing happened, and playground has no
such endpoint. The `/v2.0` line then says the endpoint exists, the key is
valid, and the plan does not cover it. There is no free route to Solscan's
holder list; only a Pro plan opens it. The playground's own endpoints are
account-scoped (transfer, DeFi activities, enhanced transactions), which is
also why `/connected`'s funding lookup is fine on this key.

Confirmed at the same time: `token: <key>` is the *only* header Solscan reads.
`Authorization: Bearer`, bare `Authorization` and `x-api-key` all come back
"Token is missing" --- its way of saying no key arrived at all.

The client now stops wasting requests on this. A 404 ends the header probe for
that prefix (no header conjures a route), a non-"missing" refusal ends it too
(a different spelling of the same key will not help), and "upgrade your api key"
marks the path forbidden for the life of the process. Cost: two requests once
per start, none thereafter.

## The holder that was actually missing

Solscan was never the fix. `_query_helius_das` capped at `DAS_MAX_PAGES = 3`
x 1,000 = **3,000 token accounts**, and DAS returns accounts in index order,
*not* by balance. So the cap did not keep the largest holders --- it kept an
arbitrary slice, and any token with more than 3,000 accounts could drop a
holder of any size. That is almost certainly why a 2.6% holder of $Morty
showed on Solscan and not on the card.

`DAS_MAX_PAGES` now defaults to 40 (40,000 accounts, `DAS_MAX_PAGES` env
overrides) and the loop still stops early the moment a short page proves the
set exhausted --- so the extra ceiling costs nothing on small tokens. Hitting
the ceiling now logs a warning naming the truncation instead of silently
ranking a slice.

Reproduced both ways against a fake 4,200-account mint with a whale on page 4:
at 3 pages the whale is missing, at 40 it is found.

## Verify
1. Restart the bot so `.env` is re-read.
2. `python solscan_diag.py`
3. `/token GUmbtfjSZkybSFgPibBcvwExEBdXwewJHR5PkTjzpump`

Working log line:
```
INFO solscan.api: token/holders answered under /playground with the 'token' header
INFO token.intelligence: Using Solscan holders (N) over Helius DAS (M) for ...
```

Failing log line now carries the reason:
```
WARNING solscan.api: no prefix/header combination answered token/holders
(last HTTP 401: {"success":false,"errors":{...}}). Free keys reach /playground only ...
```

## Tests
`test_connected_wallets.py` — 45 passing against the rewritten funder.
Holder parsing covered ad hoc: free-key path (playground + `token` header +
`tokenAddress`/`offset`/`limit`), paid-key path (v2.0 + Bearer +
`page`/`page_size`), resolution caching (2 probes on the first call, 1 request
per page after), per-owner aggregation, decimal scaling, and the all-rejected
path returning `[]`. `test_token_intelligence.py` needs the venv (imports
`discord`).
