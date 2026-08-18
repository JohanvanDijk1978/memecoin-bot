"""
wallet_hunt.py -- find the rule that maps a FOMO handle to its real wallet.

Ground truth (traced by hand on Solscan):

    Konito  ->  93fjdwW7S3Aw4TkrnMzy51sZ5pP4ArpvtmFYujNyDVgH
    via tx  63o3ZL1hpSCtf3wwtsdFbpEQPfA5RwP33fhYtKa5Naoyh3z8D6z6gsfkV5Ary4znrDvm28zib46MGoCxpMWofyjp

What the previous round got wrong: verify_wallet_onchain.py only ever looked at
the FEE PAYER, and when it found no signature in the swap payload it quietly
fell through to counting recent txs -- which is where "zero transactions" came
from. It never asked which ACCOUNT INSIDE the trade belongs to the trader. On a
platform that sponsors gas, the fee payer is the platform, not the user.

Verified: the old {80,90} regex WOULD have matched an 88-char signature, so
"no signature in /swaps" was a real finding, not a regex bug. The app still
renders a Solscan link per trade, so the signature has to come from somewhere
-- /trades and the trade-detail routes are the prime suspects, and stage B
probes them by id.

Two stages, runnable separately:

    python wallet_hunt.py --tx <sig> [--expect <wallet>]
        Stage A. Pure RPC, no FOMO call. Prints the full anatomy of one
        transaction and reports every position the expected wallet occupies.
        Run this FIRST -- it defines the rule.

    python wallet_hunt.py --handle Konito [--expect <wallet>]
        Stage B. Pulls raw payloads from every user-scoped FOMO route, deep-
        searches them for the wallet and for signature-shaped strings, and
        reports the exact JSON path of each hit. Answers "does FOMO publish a
        signature anywhere, and under what key".

    python wallet_hunt.py --validate
        Stage C. Runs the derived rule against all three known handles.

Needs the fomo/ venv (fomo_api + httpx + python-dotenv) and network access,
so run it in your own terminal -- the sandbox has no route to a Solana RPC.
Output is deliberately ASCII-only; the Windows console chokes on box-drawing
characters under cp1252.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from typing import Any

from dotenv import load_dotenv

load_dotenv()

RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

# Base58, no 0/O/I/l. A signature is 64 bytes -> 86-88 chars; an address is
# 32 bytes -> 32-44. Keeping these separate is the whole point: the old
# find_sigs() used {80,90} and matched nothing, which is why the previous run
# concluded there were no signatures.
B58 = r"[1-9A-HJ-NP-Za-km-z]"
SIG_RE = re.compile(rf"^{B58}{{86,88}}$")   # 64 bytes
ADDR_RE = re.compile(rf"^{B58}{{32,44}}$")  # 32 bytes

KNOWN = {
    "Konito": "93fjdwW7S3Aw4TkrnMzy51sZ5pP4ArpvtmFYujNyDVgH",
    "onmycheck": "Ay77dkJkbjPCLbhHmwNg5z4WVtP2bMUpjKNnWFo1CuD2",
    "FIippingProfits": "DdM1tyCdoEyoxYYmGMjdf5rRPcpmj3UzZTpE7ScuTf7d",
}
KNOWN_TX = "63o3ZL1hpSCtf3wwtsdFbpEQPfA5RwP33fhYtKa5Naoyh3z8D6z6gsfkV5Ary4znrDvm28zib46MGoCxpMWofyjp"

# CONFIRMED 2026-08-18 against the Konito tx. FOMO sponsors gas: the fee payer
# is a platform account (the only one with a SOL delta, exactly the fee), and
# the trader signs alongside it because moving their own tokens needs their
# signature. So the trader is THE SIGNER THAT IS NOT THE FEE PAYER.
#
# Token ownership is the weaker signal and must not be the primary rule --
# that tx had 5 distinct token owners, one of which (Ay77dkJk..., previously
# believed to be onmycheck's wallet) is not a signer at all.
FOMO_SPONSOR = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"


def derive_trader(roles: dict[str, list[str]]) -> tuple[str | None, str]:
    """Apply the confirmed rule. Returns (wallet, how_it_was_decided)."""
    signers = roles.get("signers") or []
    payer = (roles.get("fee_payer") or [None])[0]
    others = [s for s in signers if s != payer]

    if len(others) == 1:
        return others[0], "sole non-fee-payer signer"
    if not others:
        # Unsponsored trade: the trader paid their own gas.
        return (payer, "self-paid tx, fee payer is the trader") if payer else (None, "no signers")

    # More than one co-signer. Break the tie on token ownership, which the
    # trader always has and a co-signing program generally does not.
    owners = set(roles.get("token_owner") or [])
    owning = [s for s in others if s in owners]
    if len(owning) == 1:
        return owning[0], "co-signer that owns a token account"

    movers = [s for s in (owning or others) if s in (roles.get("token_mover") or [])]
    if len(movers) == 1:
        return movers[0], "co-signer whose token balance changed"
    return (owning or others)[0], f"ambiguous -- {len(others)} co-signers, took the first"

# Accounts that show up in every swap and are never the trader. Used only to
# make the output readable -- the rule itself must not depend on this list.
NOISE = {
    "11111111111111111111111111111111": "system",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "token program",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "token-2022",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "assoc. token",
    "ComputeBudget111111111111111111111111111111": "compute budget",
    "So11111111111111111111111111111111111111112": "wSOL mint",
    "SysvarRent111111111111111111111111111111111": "rent sysvar",
}

LAMPORTS = 1_000_000_000


def head(text: str) -> None:
    print(f"\n{'=' * 78}\n  {text}\n{'=' * 78}")


def tag(addr: str | None, expect: str | None) -> str:
    if not addr:
        return ""
    if expect and addr == expect:
        return "   <== THE REAL WALLET"
    if addr in NOISE:
        return f"   ({NOISE[addr]})"
    return ""


# ---------------------------------------------------------------- RPC

async def rpc(http: Any, method: str, params: list[Any]) -> Any:
    r = await http.post(
        RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    r.raise_for_status()
    payload = r.json()
    if "error" in payload:
        raise RuntimeError(f"RPC {method}: {payload['error']}")
    return payload.get("result")


# ------------------------------------------------- stage A: tx anatomy

async def anatomy(http: Any, sig: str, expect: str | None) -> dict[str, list[str]]:
    """Print every account in a tx by role. Returns role -> [addresses]."""
    head(f"TX {sig[:24]}...")
    tx = await rpc(
        http, "getTransaction",
        [sig, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}],
    )
    if not tx:
        print("  not found on chain (or the RPC has pruned it -- try a paid RPC)")
        return {}

    msg = tx["transaction"]["message"]
    meta = tx.get("meta") or {}
    keys: list[dict] = list(msg.get("accountKeys") or [])

    # Address-lookup tables append accounts that are NOT in accountKeys but DO
    # appear in token balances. Without these the trader is often just missing.
    loaded = meta.get("loadedAddresses") or {}
    for k in loaded.get("writable") or []:
        keys.append({"pubkey": k, "signer": False, "writable": True, "source": "lookupTable"})
    for k in loaded.get("readonly") or []:
        keys.append({"pubkey": k, "signer": False, "writable": False, "source": "lookupTable"})

    pubkeys = [k["pubkey"] for k in keys]
    roles: dict[str, list[str]] = {}

    signers = [k["pubkey"] for k in keys if k.get("signer")]
    roles["fee_payer"] = signers[:1]
    roles["signers"] = signers
    print(f"\n  slot {tx.get('slot')}   blockTime {tx.get('blockTime')}   "
          f"fee {meta.get('fee')} lamports")
    print(f"\n  SIGNERS ({len(signers)})")
    for i, s in enumerate(signers):
        print(f"    {'fee payer' if i == 0 else 'co-signer'}  {s}{tag(s, expect)}")

    # SOL deltas. The trader usually nets negative on a buy, positive on a sell.
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    deltas: list[tuple[str, int]] = []
    for i, pk in enumerate(pubkeys):
        if i < len(pre) and i < len(post):
            d = post[i] - pre[i]
            if d:
                deltas.append((pk, d))
    deltas.sort(key=lambda t: abs(t[1]), reverse=True)
    roles["sol_delta"] = [pk for pk, _ in deltas]
    print(f"\n  SOL BALANCE CHANGES ({len(deltas)})")
    for pk, d in deltas[:12]:
        print(f"    {d / LAMPORTS:+14.6f} SOL  {pk}{tag(pk, expect)}")

    # Token-account OWNERS. This is the field the last round never looked at,
    # and the most likely home of the answer: the trader owns the ATA that
    # receives the token, even when a relayer signs and pays.
    owners: list[str] = []
    for label in ("preTokenBalances", "postTokenBalances"):
        for b in meta.get(label) or []:
            o = b.get("owner")
            if o:
                owners.append(o)
    counts = Counter(owners)
    roles["token_owner"] = [o for o, _ in counts.most_common()]
    print(f"\n  TOKEN ACCOUNT OWNERS ({len(counts)} distinct)")
    for o, n in counts.most_common(12):
        print(f"    {n:>3}x  {o}{tag(o, expect)}")

    # Per-mint owner deltas -- who actually ended up holding the token.
    print("\n  TOKEN BALANCE DELTAS (owner / mint)")
    bal: dict[tuple[str, str], list[float]] = {}
    for label, idx in (("preTokenBalances", 0), ("postTokenBalances", 1)):
        for b in meta.get(label) or []:
            o, m = b.get("owner"), b.get("mint")
            if not o or not m:
                continue
            amt = (b.get("uiTokenAmount") or {}).get("uiAmount") or 0.0
            bal.setdefault((o, m), [0.0, 0.0])[idx] = float(amt)
    movers = [(o, m, p1 - p0) for (o, m), (p0, p1) in bal.items() if p1 != p0]
    movers.sort(key=lambda t: abs(t[2]), reverse=True)
    for o, m, d in movers[:12]:
        print(f"    {d:+18.6f}  mint {m[:8]}...  owner {o}{tag(o, expect)}")
    roles["token_mover"] = [o for o, _, _ in movers]

    # Transfer authorities from the parsed instructions.
    auths: list[str] = []

    def walk(instrs: list[dict]) -> None:
        for ins in instrs or []:
            info = ((ins.get("parsed") or {}).get("info")) or {}
            for k in ("authority", "owner", "source", "destination", "wallet",
                      "multisigAuthority", "newAccount", "account"):
                v = info.get(k)
                if isinstance(v, str) and ADDR_RE.match(v):
                    auths.append(v)

    walk(msg.get("instructions") or [])
    for inner in meta.get("innerInstructions") or []:
        walk(inner.get("instructions") or [])
    ac = Counter(auths)
    roles["instruction_party"] = [a for a, _ in ac.most_common()]
    print(f"\n  INSTRUCTION PARTIES ({len(ac)} distinct)")
    for a, n in ac.most_common(12):
        print(f"    {n:>3}x  {a}{tag(a, expect)}")

    roles["any_account"] = pubkeys

    if expect:
        head("WHERE THE REAL WALLET SHOWS UP")
        hits = [r for r, vals in roles.items() if expect in vals]
        if not hits:
            print(f"  {expect}\n  does NOT appear anywhere in this transaction.")
            print("  -> either the tx is not this trader's, or the wallet is behind")
            print("     a lookup table the RPC did not resolve. Retry with a paid RPC.")
        else:
            for r in hits:
                pos = roles[r].index(expect)
                print(f"  {r:<20} position {pos}"
                      + ("   <-- rank 0, usable as a rule" if pos == 0 else ""))
            print("\n  The narrowest role where it sits at position 0 is the rule.")

    got, how = derive_trader(roles)
    head("RULE OUTPUT")
    print(f"  trader   {got}")
    print(f"  via      {how}")
    if (roles.get("fee_payer") or [None])[0] == FOMO_SPONSOR:
        print(f"  fee payer is FOMO's known gas sponsor -- gas was sponsored")
    if expect:
        print("  MATCH" if got == expect else f"  MISMATCH -- expected {expect}")
    return roles


# ------------------------------------------- stage B: what FOMO publishes

def deep_find(node: Any, needles: dict[str, str], path: str = "$") -> list[tuple[str, str]]:
    """Every (needle_label, json_path) where a needle appears as a string value."""
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += deep_find(v, needles, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += deep_find(v, needles, f"{path}[{i}]")
    elif isinstance(node, str):
        for label, needle in needles.items():
            if needle and needle in node:
                out.append((label, path))
    return out


def collect(node: Any, pattern: re.Pattern, path: str = "$") -> list[tuple[str, str]]:
    """Every (value, json_path) whose string value matches pattern."""
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += collect(v, pattern, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += collect(v, pattern, f"{path}[{i}]")
    elif isinstance(node, str) and pattern.match(node):
        out.append((node, path))
    return out


async def raw_get(fomo: Any, path: str) -> Any:
    """Fetch a route WITHOUT the envelope check -- the root-level routes
    (/trades, /watchlist) may not use {success, responseObject} at all."""
    from fomo_api import API_BASE

    if getattr(fomo, "_browser", None) is not None:
        status, body, _ = await fomo._browser.get(API_BASE + path)
    else:
        token = await fomo._ensure_token()
        async with fomo._http.get(
            API_BASE + path, headers={"Authorization": f"Bearer {token}"}
        ) as resp:
            status, body = resp.status, await resp.text()
    if status != 200:
        raise RuntimeError(f"{status}: {body[:160]}")
    data = json.loads(body)
    # Unwrap the envelope when there is one, otherwise hand back the raw doc.
    if isinstance(data, dict) and "responseObject" in data:
        return data["responseObject"]
    return data


async def hunt(handle: str, expect: str | None, outdir: str) -> None:
    from fomo_api import FomoClient

    os.makedirs(outdir, exist_ok=True)
    async with FomoClient() as fomo:
        head(f"@{handle} -- what FOMO publishes")
        user = await fomo.user_by_handle(handle, with_ranks=False)
        uid = user.id
        print(f"  id            {uid}")
        print(f"  user.address  {user.sol_address}   (the dead one)")
        if expect:
            print(f"  real wallet   {expect}")

        routes = {
            "user": f"/v2/users/{uid}",
            "swaps": f"/v2/users/{uid}/swaps?limit=50",
            "trades": f"/trades?userId={uid}&orderBy=realizedPnlUsd",
            "spotlight": f"/v2/users/{uid}/spotlight",
            "balances": f"/v2/users/{uid}/balances",
            "transfers": f"/v2/transfers/with/{uid}",
        }

        needles = {"REAL WALLET": expect or "", "KNOWN TX": KNOWN_TX}
        needles = {k: v for k, v in needles.items() if v}

        for name, path in routes.items():
            print(f"\n  --- {name}  {path}")
            try:
                data = await raw_get(fomo, path)
            except Exception as exc:
                print(f"      failed: {str(exc)[:150]}")
                continue

            dest = os.path.join(outdir, f"{handle}_{name}.json")
            with open(dest, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            print(f"      saved {dest}")

            for label, jpath in deep_find(data, needles):
                print(f"      HIT  {label} at {jpath}")

            sigs = collect(data, SIG_RE)
            if sigs:
                seen = {}
                for val, jpath in sigs:
                    seen.setdefault(re.sub(r"\[\d+\]", "[]", jpath), val)
                print(f"      {len(sigs)} signature-shaped value(s):")
                for jpath, val in list(seen.items())[:6]:
                    print(f"        {jpath}  =  {val[:28]}...")
            else:
                print("      no signature-shaped values")

        # The swap objects carry inTradeId/outTradeId. If a trade-detail route
        # exists, that is the most likely home of the Solscan link the app
        # renders -- and therefore of the signature.
        print("\n  --- trade-detail routes (by inTradeId/outTradeId)")
        try:
            swaps = await raw_get(fomo, f"/v2/users/{uid}/swaps?limit=5")
            rows = swaps.get("swaps") if isinstance(swaps, dict) else swaps
        except Exception as exc:
            rows = None
            print(f"      could not re-fetch swaps: {str(exc)[:120]}")
        trade_ids: list[str] = []
        for r in (rows or [])[:3]:
            for k in ("inTradeId", "outTradeId", "id"):
                v = r.get(k)
                if isinstance(v, str) and v and v not in trade_ids:
                    trade_ids.append(v)
        if not trade_ids:
            print("      no trade ids on the swap objects")
        for tid in trade_ids[:4]:
            for tmpl in ("/trades/{}", "/v2/trades/{}", "/trades?tradeId={}",
                         "/v2/trades?tradeId={}", "/trades?id={}"):
                path = tmpl.format(tid)
                try:
                    data = await raw_get(fomo, path)
                except Exception as exc:
                    print(f"      {path[:52]:<52} {str(exc)[:40]}")
                    continue
                sigs = collect(data, SIG_RE)
                hits = deep_find(data, needles)
                note = f"{len(sigs)} sig(s)" if sigs else "no sigs"
                print(f"      {path[:52]:<52} OK  {note}")
                for val, jpath in sigs[:3]:
                    print(f"          {jpath}  =  {val[:28]}...")
                for label, jpath in hits:
                    print(f"          HIT {label} at {jpath}")
                dest = os.path.join(outdir, f"{handle}_tradedetail.json")
                with open(dest, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=1)
                break

        print("\n  Next: take any signature found above and run")
        print(f"    python wallet_hunt.py --tx <sig> --expect {expect or '<wallet>'}")


# ---------------------------------------------------- stage C: validate

async def validate(http: Any) -> None:
    head("VALIDATE -- does the rule reproduce all three known wallets?")
    print("  Fill RULE_ROLE below once stage A tells you which role wins,")
    print("  then this compares rule output against KNOWN for each handle.")
    from fomo_api import FomoClient

    async with FomoClient() as fomo:
        for handle, expected in KNOWN.items():
            print(f"\n  @{handle}  want {expected}")
            try:
                user = await fomo.user_by_handle(handle, with_ranks=False)
                data = await raw_get(fomo, f"/v2/users/{user.id}/swaps?limit=25")
            except Exception as exc:
                print(f"    fomo lookup failed: {str(exc)[:150]}")
                continue
            rows = data.get("swaps") if isinstance(data, dict) else data
            sigs = [v for v, _ in collect(rows, SIG_RE)]
            if not sigs:
                print("    no signatures in the swap payload -- rule cannot run here")
                continue
            got = Counter()
            for sig in sigs[:5]:
                try:
                    roles = await anatomy(http, sig, expected)
                except Exception as exc:
                    print(f"    {sig[:16]}... rpc failed: {str(exc)[:100]}")
                    continue
                cand, _how = derive_trader(roles)
                if cand:
                    got[cand] += 1
            if got:
                best, n = got.most_common(1)[0]
                print(f"    rule -> {best}  (agreed on {n}/{sum(got.values())} txs)")
                print("    MATCH" if best == expected else "    MISMATCH")
                if len(got) > 1:
                    print(f"    NOTE: txs disagreed -- {dict(got)}")


# ------------------------------------------------------------------ main

async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tx", nargs="?", const=KNOWN_TX,
                    help="dissect one transaction (defaults to the known Konito tx)")
    ap.add_argument("--handle", help="search FOMO's payloads for a handle")
    ap.add_argument("--expect", help="the known-correct wallet, for flagging")
    ap.add_argument("--validate", action="store_true", help="run the rule on all known handles")
    ap.add_argument("--outdir", default="hunt_out", help="where to dump raw JSON")
    args = ap.parse_args()

    if not (args.tx or args.handle or args.validate):
        ap.print_help()
        print("\nStart here:\n    python wallet_hunt.py --tx\n")
        return 0

    try:
        import httpx
    except ImportError:
        print("pip install httpx")
        return 1

    expect = args.expect or (KNOWN.get(args.handle or "") if args.handle else
                             KNOWN["Konito"] if args.tx else None)

    if args.tx and not SIG_RE.match(args.tx):
        print(f"Not a transaction signature: {args.tx}")
        print("  A signature is 86-88 base58 chars. That looks like a UUID --")
        print("  FOMO's trade ids are UUIDs and are NOT on-chain signatures.")
        print("  Use wallet_resolve.py, which finds the tx by mint+amount+time.")
        return 1

    async with httpx.AsyncClient(timeout=45) as http:
        if args.tx:
            await anatomy(http, args.tx, expect)
        if args.handle:
            await hunt(args.handle, expect, args.outdir)
        if args.validate:
            await validate(http)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
