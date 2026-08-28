"""
discover.py — suggest wallets worth adding to a group.

The question this answers is "who else is in the trades my group finds". For
every token the group has converged on, it reads who traded that token, then
surfaces the addresses that keep coming back across *different* convergences.
One co-holding is a coincidence; the same stranger in three of your group's
finds is a wallet worth tracking.

Two kinds of evidence, ranked together:

  co-holding   the address traded a token this group converged on. The base
               signal, and the reason a candidate exists at all.
  early buyer  the address was among the first into that token. A bonus, not a
               requirement — being early is what separates a wallet with an
               edge from one that bought your group's signal after you did.

The heavy lifting is `fomo/token_traders.py`, which already reconstructs a
per-address cost-basis ledger from Helius history and filters out pools,
routers and other infrastructure. It is imported rather than reimplemented, on
Johan's call. `fomo/` ships with this repo so it is present on the VPS too, and
the module is pure stdlib — but the import is still guarded, because a feature
that suggests wallets must never be able to take the page down with it.

Everything here is bounded: at most DISCOVER_TOKENS convergences per scan, one
history request each, and the whole thing runs on its own slow schedule or when
Johan presses the button.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import wallets as W

log = logging.getLogger("memedash.discover")

FOMO_DIR = Path(__file__).resolve().parent.parent / "fomo"
HELIUS_TX = "https://api.helius.xyz/v0/addresses/{address}/transactions"

DISCOVER_TOKENS = int(os.getenv("WG_DISCOVER_TOKENS", "12"))    # convergences per scan
DISCOVER_TRADERS = int(os.getenv("WG_DISCOVER_TRADERS", "40"))  # ranked traders kept per token
EARLY_N = int(os.getenv("WG_DISCOVER_EARLY_N", "10"))           # "early" means this many buyers in
MIN_CONVERGENCES = int(os.getenv("WG_DISCOVER_MIN", "2"))       # Johan's threshold: 2+
TX_LIMIT = int(os.getenv("WG_DISCOVER_TX_LIMIT", "100"))

STATUS: dict = {"ok": None, "note": "not run yet", "source": ""}

_tt = None          # the token_traders module, or False once we know it is absent


def traders_module():
    """Import fomo/token_traders lazily, once, and never raise.

    Flat imports inside fomo (`from token_traders import ...`) mean the
    directory itself has to be on sys.path, not just importable as a package.
    """
    global _tt
    if _tt is None:
        try:
            if FOMO_DIR.is_dir() and str(FOMO_DIR) not in sys.path:
                sys.path.insert(0, str(FOMO_DIR))
            import token_traders                      # noqa: PLC0415
            _tt = token_traders
            log.info("wallet discovery: using fomo/token_traders")
        except Exception as e:
            _tt = False
            log.warning("wallet discovery: fomo/token_traders unavailable (%s) — "
                        "falling back to holder lists only", e)
    return _tt or None


def _helius_key() -> str:
    """The api-key out of SOLANA_RPC, when that RPC is a Helius one."""
    for url in (os.getenv("SOLANA_RPC", ""), *os.getenv("SOLANA_RPC_FALLBACKS", "").split(",")):
        url = url.strip()
        if not url or "helius" not in url:
            continue
        key = (parse_qs(urlsplit(url).query).get("api-key") or [""])[0]
        if key:
            return key
    return ""


# ------------------------------------------------------------------ providers

async def token_history(client, mint: str) -> list | None:
    """Helius parsed transaction history for a mint. None when unavailable."""
    key = _helius_key()
    if not key:
        return None
    try:
        r = await client.get(HELIUS_TX.format(address=mint),
                             params={"api-key": key, "limit": TX_LIMIT}, timeout=30)
        if r.status_code != 200:
            log.info("helius history %s for %s", r.status_code, mint[:8])
            return None
        payload = r.json()
        return payload if isinstance(payload, list) else None
    except Exception as e:
        log.info("helius history failed for %s: %s", mint[:8], e)
        return None


async def top_holders(client, mint: str) -> list[str]:
    """Owner addresses of the largest token accounts.

    The fallback evidence path: it says who holds, which is enough to count a
    co-holding, but carries no PnL and no entry time. `getTokenLargestAccounts`
    returns token accounts, so owners need a second lookup.
    """
    rpc = os.getenv("SOLANA_RPC", "").strip()
    if not rpc:
        return []
    try:
        r = await client.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                                         "method": "getTokenLargestAccounts",
                                         "params": [mint]}, timeout=25)
        accounts = [a["address"] for a in
                    (((r.json() or {}).get("result") or {}).get("value") or []) if a.get("address")]
        if not accounts:
            return []
        r2 = await client.post(rpc, json={"jsonrpc": "2.0", "id": 1,
                                          "method": "getMultipleAccounts",
                                          "params": [accounts[:20], {"encoding": "jsonParsed"}]},
                               timeout=25)
        owners = []
        for acc in (((r2.json() or {}).get("result") or {}).get("value") or []):
            info = ((((acc or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
            if info.get("owner"):
                owners.append(info["owner"])
        return owners
    except Exception as e:
        log.info("top holders failed for %s: %s", mint[:8], e)
        return []


# -------------------------------------------------------------------- the scan

async def _token_flows(client, token: dict, sol_price: float):
    """Parsed flows for one token, or None when history is not available."""
    tt = traders_module()
    if not tt:
        return None
    payload = await token_history(client, token["address"])
    if payload is None:
        return None
    try:
        # Without a quote price the parser can read the token leg but not what
        # was paid for it, so every trade comes back unpriced and the PnL column
        # is empty. One SOL price per scan fixes that for the whole run, because
        # the money leg is already on the same page.
        prices = {W.WSOL: Decimal(str(sol_price))} if sol_price > 0 else None
        return tt.parse_helius_transactions(payload, token["address"], prices=prices)
    except Exception as e:                     # a parser surprise is not fatal
        log.warning("history parse failed for %s: %s", token["address"][:8], e)
        return None


def _rank_token(tt, token: dict, flows, exclude: set[str]) -> list[dict]:
    """Rank one token's traders, given the addresses already ruled out."""
    try:
        price = Decimal(str(token.get("price") or 0)) or None
        ranked = tt.aggregate_traders(flows, exclude=exclude, limit=DISCOVER_TRADERS,
                                      current_price=price)
    except Exception as e:
        log.warning("trader ledger failed for %s: %s", token["address"][:8], e)
        return []
    # "early" is by first appearance in the sampled window, which is the only
    # ordering the history actually supports.
    by_first = sorted([t for t in ranked if t.first_seen], key=lambda t: t.first_seen)
    early = {t.address for t in by_first[:EARLY_N]}
    return [{"wallet": t.address,
             "pnl_usd": float(t.realized_pnl_usd or 0) + float(t.unrealized_pnl_usd or 0),
             "early": t.address in early, "source": "history"}
            for t in ranked]


async def _holder_evidence(client, token: dict, exclude: set[str]) -> list[dict]:
    """The fallback: who holds, with no profit and no entry time."""
    owners = await top_holders(client, token["address"])
    if owners:
        STATUS.update(ok=True, note=("holder lists only — no Helius key" if not _helius_key()
                                     else "holder lists only — history unavailable"),
                      source="holders")
    return [{"wallet": w, "pnl_usd": None, "early": False, "source": "holders"}
            for w in owners if w not in exclude]


async def scan(client, group_id: int, tokens: list[dict], tracked: set[str]) -> list[dict]:
    """Rank untracked wallets that recur across this group's convergences.

    `tokens` is the group's convergence set, newest first; only the most recent
    DISCOVER_TOKENS are read, because one history request per token is the
    expensive part and old convergences say least about a wallet today.
    """
    if not tokens:
        STATUS.update(ok=None, note="no convergences to learn from yet", source="")
        return []
    try:
        sol_price = await W.native_price(client, "solana")
    except Exception:
        sol_price = 0.0                  # unpriced evidence still ranks by recurrence
    tt = traders_module()
    chosen = tokens[:DISCOVER_TOKENS]

    # Pass 1: read every token's history once, and keep the flows.
    flows_by_token: dict[str, list] = {}
    for token in chosen:
        flows = await _token_flows(client, token, sol_price)
        if flows:
            flows_by_token[token["address"]] = flows

    # Infrastructure is judged across the WHOLE scan, not per token. A pool sits
    # on one side of one token's swaps, but a router like Jupiter sits on one
    # side of everything — and recurrence across tokens is exactly the signal
    # this feature ranks by, so an unfiltered router would top every list. The
    # per-token check cannot see that; a scan-wide sample can.
    infra: set[str] = set()
    if tt and flows_by_token:
        try:
            infra = tt.infrastructure_addresses(
                [f for flows in flows_by_token.values() for f in flows])
            if infra:
                log.info("wallet discovery: ruled out %d infrastructure addresses", len(infra))
        except Exception as e:
            log.warning("infrastructure detection failed: %s", e)
    exclude = set(tracked) | infra
    if flows_by_token:
        STATUS.update(ok=True, note="Helius history", source="history")

    # Pass 2: rank each token against what is left.
    found: dict[str, dict] = {}
    for token in chosen:
        flows = flows_by_token.get(token["address"])
        rows = (_rank_token(tt, token, flows, exclude) if flows
                else await _holder_evidence(client, token, exclude))
        for row in rows:
            entry = found.setdefault(row["wallet"], {
                "wallet": row["wallet"], "tokens": [], "pnl_usd": 0.0,
                "priced_n": 0, "early_n": 0, "source": row["source"]})
            entry["tokens"].append({"address": token["address"],
                                    "symbol": token.get("symbol") or "?",
                                    "pnl_usd": row["pnl_usd"], "early": row["early"]})
            if row["pnl_usd"] is not None:
                entry["pnl_usd"] += row["pnl_usd"]
                entry["priced_n"] += 1
            if row["early"]:
                entry["early_n"] += 1

    out = []
    for entry in found.values():
        n = len({t["address"] for t in entry["tokens"]})
        if n < MIN_CONVERGENCES:
            continue                    # one co-holding is a coincidence
        entry["convergences"] = n
        # Recurrence dominates deliberately: a wallet in three of your finds
        # matters more than one that got lucky once. PnL and being early break
        # ties within a recurrence tier rather than jumping one.
        entry["score"] = round(n * 100 + entry["early_n"] * 25
                               + max(-50.0, min(50.0, entry["pnl_usd"] / 1000.0)), 2)
        out.append(entry)
    out.sort(key=lambda e: (e["convergences"], e["score"]), reverse=True)
    return out


def to_row(group_id: int, e: dict, now: float) -> tuple:
    return (group_id, e["wallet"], e["convergences"], json.dumps(e["tokens"]),
            e["pnl_usd"] if e["priced_n"] else None, e["early_n"], e["score"],
            e["source"], now)


def from_row(r: dict) -> dict:
    return {"wallet": r["wallet"], "convergences": r["convergences"],
            "tokens": json.loads(r["tokens_json"] or "[]"), "pnl_usd": r["pnl_usd"],
            "early_n": r["early_n"], "score": r["score"], "source": r["source"],
            "scanned_at": r["scanned_at"]}
