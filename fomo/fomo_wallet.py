"""
fomo_wallet.py -- resolve a FOMO handle to its real on-chain trading wallet.

fomo.family publishes four addresses per trader (user.address, swap.address,
trade.userAddress, evmAddress) and not one of them is the trading wallet --
all four are synthetic and have no on-chain history. It publishes no
transaction signature either. But it publishes enough to FIND the transaction:

    swap.outTokenAddress   the mint
    swap.outHumanAmount    the exact amount received
    swap.createdAt         the time, accurate to ~1s of block time

Match those on chain, then read the trader off the transaction with the rule:

    THE TRADER IS THE SIGNER THAT IS NOT THE FEE PAYER

FOMO sponsors gas, so signers[0] is always the platform account. Reading only
signers[0] is what made every earlier probe report a stranger.

Finding that transaction is the hard part, because FOMO publishes no
signature. There are three routes, tried cheapest first:

    sponsor  the gas sponsor's signature history. Every FOMO trade is in it
             and nothing else is, so its length tracks FOMO's throughput.
             This is the default route.
    mint     the traded token's own history. Cheap for a quiet token, and
             useless for a viral one -- a hot mint can stack >12000
             signatures in front of a two-hour-old swap.
    blocks   the chain at that timestamp, via getBlock. Depends on no
             account's history at all, so nothing can outrun it. Costs the
             most, so it is opt-in (--deep).

Confirmed 2026-08-18 on Konito (93fjdwW7...) and onmycheck (Ay77dkJk...),
5/5 corroborating swaps each, ~11 RPC calls per handle.

This module is the library. `wallet_resolve.py` is the CLI over it, and
`fomo_bot.py` uses `WalletResolver` for the /fomo embed. A trader's wallet does
not change, so results cache to disk and a handle is resolved once, ever.

Needs a Solana RPC in SOLANA_RPC -- the public endpoint throttles hard and
prunes history, so use Helius or QuickNode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from fomo_hodlers import confident_matches, parse_holder_groups
from rpc_config import env_rpc_urls, normalize_rpc_urls, rpc_display_name

log = logging.getLogger("fomo.wallet")

RPC_URL = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
RPC_URLS = env_rpc_urls(
    "SOLANA_RPC",
    "SOLANA_RPC_FALLBACKS",
    "https://api.mainnet-beta.solana.com",
)
CACHE = os.getenv("FOMO_WALLET_CACHE", "wallet_cache.json")
SOLANA_NETWORK_ID = 1399811149

# FOMO's gas sponsor -- the fee payer on every sponsored trade.
FOMO_SPONSOR = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"

# FOMO could add or rotate sponsors; a comma-separated FOMO_SPONSORS overrides
# the default without a code change. All of them get indexed.
SPONSORS = [a.strip() for a in
            os.getenv("FOMO_SPONSORS", FOMO_SPONSOR).split(",") if a.strip()]

# Quote tokens. A swap OUT of one of these is a buy, and the buy's out-amount
# is the distinctive number to match on.
QUOTES = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
}

# Observed drift between FOMO's createdAt and blockTime is ~1s; the window only
# has to be generous enough not to exclude the true tx, because candidates are
# always checked nearest-first.
TIME_WINDOW = 120
AMOUNT_TOL = 1e-6
MAX_SIG_PAGES = 12
# A busy mint can have hundreds of txs inside the window (67COIN had 255).
MAX_TX_FETCH = 60
# The sponsor's window holds every FOMO trade in those two minutes, so it can
# be denser than a single mint's -- allow a deeper look before giving up.
SPONSOR_TX_FETCH = 150
# getTransaction calls per HTTP request -- rate limits count requests.
BATCH = 10
# Solana's target slot time, for turning a timestamp into a slot.
SLOT_SECONDS = 0.4
# Slots either side of the estimate that the block scan opens.
BLOCK_SPAN = 10
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# The sponsor and mint routes are both capped at MAX_SIG_PAGES * 1000
# signatures, and that cap is a moving target: as FOMO's throughput grows, the
# same 12000 sponsored signatures cover less and less wall-clock time, so a
# day-old swap falls behind the index. The block route depends on no account's
# signature history at all, so it is the only route that survives that growth.
# It is expensive, so the embed path runs it ONLY after the cheap routes miss,
# and only on the newest few swaps.
DEEP_DEFAULT = os.getenv("FOMO_WALLET_DEEP", "1").strip().lower() not in (
    "0", "false", "no",
)
try:
    DEEP_ATTEMPTS = max(1, int(os.getenv("FOMO_WALLET_DEEP_ATTEMPTS", "2")))
except ValueError:
    DEEP_ATTEMPTS = 2

# How many of the trader's own positions the holder route asks FOMO about.
# They go in one request, so this bounds the on-chain half: only the tokens
# whose published holder list names this trader cost a query.
HODLER_TOKENS = 8


def iso_epoch(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00"))
               .replace(tzinfo=timezone.utc).timestamp())


def _sayer(verbose: bool):
    """Progress goes to stdout for the CLI, to the log for the bot."""
    return (lambda msg: print(msg)) if verbose else (lambda msg: log.debug(msg.strip()))


def close(a: float, b: float) -> bool:
    if a == b:
        return True
    scale = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / scale < AMOUNT_TOL


class RpcInvalidParams(RuntimeError):
    """The caller sent parameters that every healthy Solana RPC will reject."""


class Rpc:
    """Minimal JSON-RPC client with backoff -- public endpoints 429 constantly."""

    def __init__(self, http: Any, url: str | list[str] = RPC_URLS) -> None:
        self.http = http
        self.urls = normalize_rpc_urls(url)
        if not self.urls:
            raise ValueError("at least one Solana RPC URL is required")
        self.url, self.calls = self.urls[0], 0
        self._cooldown_until = 0.0
        self._probe_lock = asyncio.Lock()

    async def __call__(self, method: str, params: list[Any]) -> Any:
        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            raise RuntimeError(f"{method}: Solana RPC cooling down for {remaining:.1f}s")
        async with self._probe_lock:
            remaining = self._cooldown_until - time.monotonic()
            if remaining > 0:
                raise RuntimeError(
                    f"{method}: Solana RPC cooling down for {remaining:.1f}s"
                )
            last_error: Exception | None = None
            for url in self.urls:
                try:
                    r = await self.http.post(
                        url,
                        json={"jsonrpc": "2.0", "id": 1,
                              "method": method, "params": params},
                    )
                    if r.status_code == 429:
                        raise RuntimeError("HTTP 429")
                    r.raise_for_status()
                    payload = r.json()
                    if not isinstance(payload, dict):
                        raise RuntimeError(f"{method}: invalid JSON-RPC response")
                    detail = payload.get("error")
                    if isinstance(detail, dict) and detail.get("code") == -32602:
                        raise RpcInvalidParams(f"{method}: {detail}")
                    if detail:
                        raise RuntimeError(f"{method}: {detail}")
                    self.calls += 1
                    self._cooldown_until = 0.0
                    return payload.get("result")
                except RpcInvalidParams:
                    # Invalid parameters are deterministic caller errors. Trying
                    # every provider and cooling down healthy RPCs only hides the
                    # actual bug and pauses unrelated wallet lookups.
                    raise
                except Exception as exc:
                    last_error = exc
                    log.debug("Solana RPC %s failed: %s", rpc_display_name(url), exc)
            self._cooldown_until = time.monotonic() + 15.0
            log.info("All configured Solana RPCs failed; wallet discovery paused for 15s")
            raise RuntimeError(f"{method}: all Solana RPCs failed: {last_error}")

    async def batch(self, method: str, param_sets: list[list[Any]]) -> list[Any]:
        """One HTTP request, many calls. Rate limits are counted per REQUEST,
        so this is the difference between 15 throttled calls and 2 clean ones.
        Falls back to sequential if the endpoint rejects batching."""
        if not param_sets:
            return []
        payload = [{"jsonrpc": "2.0", "id": i, "method": method, "params": p}
                   for i, p in enumerate(param_sets)]
        for attempt in range(8):
            for url in self.urls:
                try:
                    r = await self.http.post(url, json=payload)
                    if r.status_code >= 400:
                        continue
                    out = r.json()
                    if not isinstance(out, list):
                        continue
                    self.calls += 1
                    by_id = {item.get("id"): item.get("result") for item in out}
                    return [by_id.get(i) for i in range(len(param_sets))]
                except Exception as exc:
                    log.debug("Solana batch RPC %s failed: %s",
                              rpc_display_name(url), exc)
            await asyncio.sleep(min(2.0 * (attempt + 1), 12.0))

        return [await self(method, p) for p in param_sets]

# ------------------------------------------------------- the confirmed rule

def derive_trader(tx: dict) -> tuple[str | None, str]:
    """The trader is the signer that is not the fee payer. See module docstring."""
    msg = tx["transaction"]["message"]
    meta = tx.get("meta") or {}
    keys = list(msg.get("accountKeys") or [])
    signers = [k["pubkey"] for k in keys if k.get("signer")]
    if not signers:
        return None, "no signers"

    payer = signers[0]
    others = [s for s in signers if s != payer]
    if len(others) == 1:
        return others[0], "sole non-fee-payer signer"
    if not others:
        return payer, "self-paid tx, fee payer is the trader"

    owners = {b.get("owner") for label in ("preTokenBalances", "postTokenBalances")
              for b in (meta.get(label) or []) if b.get("owner")}
    owning = [s for s in others if s in owners]
    if len(owning) == 1:
        return owning[0], "co-signer that owns a token account"
    return (owning or others)[0], f"ambiguous -- {len(others)} co-signers"

def mint_delta(tx: dict, mint: str) -> list[tuple[str, float]]:
    """[(owner, change)] for one mint, from the pre/post token balances."""
    meta = tx.get("meta") or {}
    bal: dict[str, list[float]] = {}
    for label, idx in (("preTokenBalances", 0), ("postTokenBalances", 1)):
        for b in meta.get(label) or []:
            if b.get("mint") != mint or not b.get("owner"):
                continue
            ui = b.get("uiTokenAmount") or {}
            # uiAmountString keeps full precision on big supplies; uiAmount is
            # a float the RPC already rounded.
            amt = ui.get("uiAmountString")
            if amt in (None, ""):
                amt = ui.get("uiAmount")
            bal.setdefault(b["owner"], [0.0, 0.0])[idx] = float(amt or 0.0)
    return [(o, p1 - p0) for o, (p0, p1) in bal.items() if p1 != p0]

# ------------------------------------------------- find the tx behind a swap
#
# Three routes to the transaction behind a swap, tried cheapest first. They
# differ only in WHERE they look for candidate signatures:
#
#   sponsor  the gas sponsor's history -- every FOMO trade, and only FOMO
#            trades, so its length is bounded by the platform's throughput
#   mint     the traded token's history -- short for a quiet token, hopeless
#            for a viral one
#   blocks   the chain itself at that timestamp -- depends on no account's
#            history at all, so it cannot be outrun, but costs the most
#
# All three then do the same thing: open candidates nearest-first and stop at
# the amount match.

async def _scan(rpc: Rpc, cands: list[dict], mint: str, amount: float,
                when: int, say, label: str, limit: int = MAX_TX_FETCH,
                swap: dict | None = None, direction: int = 1
                ) -> tuple[str | None, dict | None]:
    """Open candidate transactions nearest-first, stop at the match.

    Nearest-first is what makes any of this viable: createdAt tracks blockTime
    to about a second, so the true tx is almost always among the first opened.
    getTransaction goes out BATCH per HTTP request, because rate limits count
    requests rather than calls.

    Two tests, strict before loose, within each chunk. Strict asks that the
    transaction's derived trader account for BOTH legs of this exact swap;
    loose only asks that somebody received the out-amount. Loose is what
    session 2 confirmed and it stays as the fallback, but the sponsor's window
    holds every FOMO trade in those two minutes rather than one trader's, so
    it is the one place two users could collide on the same mint and amount --
    strict settles that without giving up the early exit.
    """
    if not cands:
        return None, None
    cands = sorted(cands, key=lambda s: abs(s["blockTime"] - when))
    say(f"      {label}: {len(cands)} candidate(s) in window, nearest-first "
        f"(closest {abs(cands[0]['blockTime'] - when)}s off)")

    opts = {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}
    shortlist = cands[:limit]
    for start in range(0, len(shortlist), BATCH):
        chunk = shortlist[start:start + BATCH]
        txs = await rpc.batch("getTransaction", [[c["signature"], opts] for c in chunk])
        got = [(i, c, tx) for i, (c, tx) in enumerate(zip(chunk, txs)) if tx]

        def hit(i: int, c: dict, how: str) -> None:
            say(f"      matched candidate {start + i + 1}/{len(shortlist)} "
                f"({abs(c['blockTime'] - when)}s off, {how})")

        if swap is not None:
            for i, c, tx in got:
                trader, _how = derive_trader(tx)
                if trader and match_swap(tx, swap, trader):
                    hit(i, c, "exact swap")
                    return c["signature"], tx
        for i, c, tx in got:
            for _owner, delta in mint_delta(tx, mint):
                if delta * direction > 0 and close(abs(delta), amount):
                    hit(i, c, "token-amount")
                    return c["signature"], tx
    say(f"      {label}: no match in the {len(shortlist)} nearest")
    return None, None


class SponsorIndex:
    """The sponsor's signature history, paged once and reused.

    FOMO pays the fee on every sponsored trade, so the sponsor account carries
    a complete, chronologically dense index of the platform's trading -- and
    its length is set by FOMO's own throughput, not by how viral a token is.
    That is the entire difference. A hot memecoin can stack >12000 signatures
    in front of a two-hour-old swap, which no amount of paging gets behind;
    the sponsor puts only FOMO's own trades there.

    One instance per resolution, so four swaps of the same trader share a
    single scan instead of paying for one each.
    """

    def __init__(self, rpc: Rpc, addresses: list[str] | None = None) -> None:
        self.rpc = rpc
        self.addresses = [a for a in (addresses or SPONSORS) if a]
        self._sigs: dict[str, list[dict]] = {a: [] for a in self.addresses}
        self._floor: dict[str, int | None] = {a: None for a in self.addresses}
        self._end: dict[str, bool] = {a: False for a in self.addresses}

    async def _extend(self, addr: str, target: int) -> None:
        """Page backwards until this account's history reaches `target`."""
        sigs = self._sigs[addr]
        while not self._end[addr]:
            floor = self._floor[addr]
            if floor is not None and floor <= target:
                return
            if len(sigs) >= MAX_SIG_PAGES * 1000:
                return
            params: list[Any] = [addr, {"limit": 1000}]
            if sigs:
                params[1]["before"] = sigs[-1]["signature"]
            page = await self.rpc("getSignaturesForAddress", params)
            if not page:
                self._end[addr] = True
                return
            sigs += page
            if tail := page[-1].get("blockTime"):
                self._floor[addr] = int(tail)

    async def candidates(self, when: int) -> tuple[list[dict], bool]:
        """(signatures inside the window, whether the index reached back that far)."""
        target = when - TIME_WINDOW
        out: list[dict] = []
        covered = True
        for addr in self.addresses:
            await self._extend(addr, target)
            floor = self._floor[addr]
            if not self._end[addr] and (floor is None or floor > target):
                covered = False
            out += [s for s in self._sigs[addr]
                    if s.get("blockTime") and not s.get("err")
                    and abs(s["blockTime"] - when) <= TIME_WINDOW]
        return out, covered

    @property
    def scanned(self) -> int:
        return sum(len(v) for v in self._sigs.values())


async def find_tx_via_sponsor(rpc: Rpc, index: SponsorIndex, mint: str,
                              amount: float, when: int, verbose: bool = True,
                              swap: dict | None = None, direction: int = 1
                              ) -> tuple[str | None, dict | None]:
    """Route 1 -- look the swap up in the gas sponsor's history."""
    say = _sayer(verbose)
    cands, covered = await index.candidates(when)
    if not cands:
        say("      sponsor: nothing in the window" if covered else
            f"      sponsor: index stopped {index.scanned} signature(s) back, "
            f"short of this swap")
        return None, None
    return await _scan(rpc, cands, mint, amount, when, say, "sponsor",
                       limit=SPONSOR_TX_FETCH, swap=swap, direction=direction)


async def find_tx(rpc: Rpc, mint: str, amount: float, when: int,
                  verbose: bool = True, swap: dict | None = None,
                  direction: int = 1
                  ) -> tuple[str | None, dict | None]:
    """Route 2 -- scan the MINT's signatures for the tx that moved `amount`.

    Fine for a quiet token or a swap from the last few minutes. It cannot work
    when the mint has more signatures newer than the swap than MAX_SIG_PAGES
    can page past, which is why the sponsor index is tried first now.
    """
    say = _sayer(verbose)
    before: str | None = None
    pool: list[dict] = []
    scanned = 0
    reached = False   # paged back past the start of the window
    ran_out = False   # ...or the mint's history simply ended first

    for _page in range(MAX_SIG_PAGES):
        params: list[Any] = [mint, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        sigs = await rpc("getSignaturesForAddress", params)
        if not sigs:
            ran_out = True
            break
        scanned += len(sigs)
        pool += [s for s in sigs
                 if s.get("blockTime") and not s.get("err")
                 and abs(s["blockTime"] - when) <= TIME_WINDOW]

        # Signatures come newest-first: once a page ends older than the window,
        # everything further back is older still.
        oldest = sigs[-1].get("blockTime")
        if oldest and oldest < when - TIME_WINDOW:
            reached = True
            break
        before = sigs[-1]["signature"]

    if not pool:
        if reached or ran_out:
            say(f"      mint: no candidate in the window "
                f"({scanned} signature(s) scanned)")
        else:
            say(f"      mint too busy: {scanned} signature(s) scanned and still "
                f"newer than this swap")
        return None, None
    return await _scan(
        rpc, pool, mint, amount, when, say, "mint",
        swap=swap, direction=direction,
    )


def normalise_block_tx(entry: dict) -> dict:
    """getBlock(transactionDetails="accounts") nests accountKeys one level
    above where getTransaction puts them. Reshape so derive_trader and
    mint_delta see the same structure from either source."""
    inner = entry.get("transaction") or {}
    return {
        "transaction": {"message": {"accountKeys": inner.get("accountKeys") or []},
                        "signatures": inner.get("signatures") or []},
        "meta": entry.get("meta") or {},
    }


async def _block_time(rpc: Rpc, slot: int) -> int | None:
    """getBlockTime, stepping outward past skipped slots (which error out)."""
    for step in (0, 1, -1, 2, -2, 3, -3, 5, -5, 8, -8):
        try:
            t = await rpc("getBlockTime", [max(1, slot + step)])
        except RuntimeError:
            continue
        if t:
            return int(t)
    return None


async def slot_for_time(rpc: Rpc, when: int) -> int | None:
    """Walk the slot clock back to `when`.

    Slots are ~400ms, so each probe measures its own drift and corrects by it;
    a dozen probes lands within a second from anywhere in recent history.
    """
    cur = await rpc("getSlot", [{"commitment": "confirmed"}])
    cur_t = await _block_time(rpc, int(cur))
    if not cur_t:
        return None
    slot = max(1, int(cur) - int((cur_t - when) / SLOT_SECONDS))
    for _ in range(12):
        t = await _block_time(rpc, slot)
        if t is None:
            return None
        drift = t - when
        if abs(drift) <= 1:
            return slot
        step = int(drift / SLOT_SECONDS) or (1 if drift > 0 else -1)
        slot = max(1, slot - step)
    return slot


async def find_tx_via_blocks(rpc: Rpc, mint: str, amount: float, when: int,
                             span: int = BLOCK_SPAN, verbose: bool = True,
                             swap: dict | None = None, direction: int = 1
                             ) -> tuple[str | None, dict | None]:
    """Route 3 -- read the blocks around the timestamp directly.

    This one depends on no account's signature history, so nothing about the
    mint or the platform can outrun it: createdAt pins the block, and the
    block contains the transaction. transactionDetails="accounts" keeps the
    payload to account keys plus token balances -- exactly what the match and
    the signer rule need, without the instruction data.

    It costs a slot search plus a getBlock per slot, so it is opt-in (--deep).
    """
    say = _sayer(verbose)
    slot = await slot_for_time(rpc, when)
    if not slot:
        say("      blocks: could not locate the slot for this timestamp")
        return None, None

    slots = await rpc("getBlocks", [max(1, slot - span), slot + span]) or []
    say(f"      blocks: slot ~{slot}, {len(slots)} block(s) within +/-{span}")
    opts = {"encoding": "jsonParsed", "transactionDetails": "accounts",
            "maxSupportedTransactionVersion": 0, "rewards": False}

    order = sorted(slots, key=lambda s: abs(s - slot))
    for start in range(0, len(order), 4):
        chunk = order[start:start + 4]
        blocks = await rpc.batch("getBlock", [[s, opts] for s in chunk])
        for at, blk in zip(chunk, blocks):
            live = [e for e in ((blk or {}).get("transactions") or [])
                    if not (e.get("meta") or {}).get("err")]

            def took(entry: dict, tx: dict, how: str):
                sigs = (entry.get("transaction") or {}).get("signatures") or []
                say(f"      blocks: matched in slot {at} ({how})")
                return (sigs[0] if sigs else None), tx

            if swap is not None:
                for entry in live:
                    tx = normalise_block_tx(entry)
                    trader, _how = derive_trader(tx)
                    if trader and match_swap(tx, swap, trader):
                        return took(entry, tx, "exact swap")
            for entry in live:
                tx = normalise_block_tx(entry)
                for _owner, delta in mint_delta(tx, mint):
                    if delta * direction > 0 and close(abs(delta), amount):
                        return took(entry, tx, "token-amount")
    say("      blocks: no amount match in the window")
    return None, None


def swap_search_leg(swap: dict) -> tuple[str | None, float, int]:
    """Return the non-quote token, absolute amount, and expected delta sign."""
    legs = (
        (swap.get("outTokenAddress"), swap.get("outHumanAmount"), 1),
        (swap.get("inTokenAddress"), swap.get("inHumanAmount"), -1),
    )
    for mint, raw_amount, direction in legs:
        if mint and mint not in QUOTES and raw_amount not in (None, ""):
            try:
                amount = abs(float(raw_amount))
            except (TypeError, ValueError):
                continue
            if amount:
                return str(mint), amount, direction
    # Token-to-token/unknown-quote fallback: retain the historical preference
    # for the output leg, then try the input leg.
    for mint, raw_amount, direction in legs:
        try:
            amount = abs(float(raw_amount or 0))
        except (TypeError, ValueError):
            continue
        if mint and amount:
            return str(mint), amount, direction
    return None, 0.0, 1


def is_solana_swap(swap: dict) -> bool:
    """True only when the selected token leg belongs to Solana."""
    mint, _amount, direction = swap_search_leg(swap)
    if not mint or not SOLANA_ADDRESS_RE.fullmatch(mint):
        return False

    side_network = swap.get("outNetworkId" if direction > 0 else "inNetworkId")
    network = side_network if side_network is not None else swap.get("networkId")
    if network is None:
        # Older FOMO payloads predate networkId; a valid base58 mint is the
        # strongest safe compatibility signal available for those rows.
        return True
    try:
        return int(network) == SOLANA_NETWORK_ID
    except (TypeError, ValueError):
        return False


async def locate_swap(rpc: Rpc, swap: dict, index: SponsorIndex | None = None,
                      deep: bool = False, verbose: bool = True
                      ) -> tuple[str | None, dict | None, str]:
    """Find the transaction behind one swap. Returns (signature, tx, route)."""
    mint, amount, direction = swap_search_leg(swap)
    if not mint or not amount or not swap.get("createdAt"):
        return None, None, "unusable swap"
    when = iso_epoch(swap["createdAt"])

    if index is not None:
        sig, tx = await find_tx_via_sponsor(rpc, index, mint, amount, when,
                                            verbose, swap=swap,
                                            direction=direction)
        if tx:
            return sig, tx, "sponsor"
    sig, tx = await find_tx(
        rpc, mint, amount, when, verbose, swap=swap, direction=direction
    )
    if tx:
        return sig, tx, "mint"
    if deep:
        sig, tx = await find_tx_via_blocks(rpc, mint, amount, when,
                                           verbose=verbose, swap=swap,
                                           direction=direction)
        if tx:
            return sig, tx, "blocks"
    return None, None, "not found"


def match_swap(tx: dict, swap: dict, wallet: str) -> bool:
    """Does this tx show `wallet` doing exactly this swap? Buys and sells both:
    a buy credits outHumanAmount of outTokenAddress, a sell debits
    inHumanAmount of inTokenAddress."""
    for mint, amount, sign in (
        (swap.get("outTokenAddress"), swap.get("outHumanAmount"), 1),
        (swap.get("inTokenAddress"), swap.get("inHumanAmount"), -1),
    ):
        if not mint or amount in (None, ""):
            continue
        want = float(amount)
        for owner, delta in mint_delta(tx, mint):
            if owner == wallet and delta * sign > 0 and close(abs(delta), want):
                return True
    return False


async def verify_wallet(rpc: Rpc, wallet: str, swaps: list[dict],
                        skip_sig: str | None = None, targets: int = 6,
                        verbose: bool = True) -> tuple[int, int]:
    """Confirm a candidate by scanning ITS OWN history, not the mints'.

    This is the direction that scales. A hot memecoin mint can have >12000
    signatures newer than a two-day-old swap, so paging a mint backwards is
    hopeless. A single trader's account runs to hundreds over the same period,
    and every swap FOMO reports must appear in it.

    Returns (confirmed, checked).
    """
    say = _sayer(verbose)
    todo = [s for s in swaps if s.get("createdAt") and is_solana_swap(s)][:targets]
    if not todo:
        return 0, 0
    oldest = min(iso_epoch(s["createdAt"]) for s in todo)

    say(f"\n  verifying against {wallet[:12]}...'s own history "
        f"(back to {min(s['createdAt'] for s in todo)[:19]})")

    sigs: list[dict] = []
    before: str | None = None
    for _page in range(MAX_SIG_PAGES):
        params: list[Any] = [wallet, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        page = await rpc("getSignaturesForAddress", params)
        if not page:
            break
        sigs += [x for x in page if x.get("blockTime") and not x.get("err")]
        tail = page[-1].get("blockTime")
        if tail and tail < oldest - TIME_WINDOW:
            break
        before = page[-1]["signature"]

    say(f"      {len(sigs)} signature(s) on the wallet")
    confirmed = checked = 0
    opts = {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}

    for sw in todo:
        when = iso_epoch(sw["createdAt"])
        near = sorted((x for x in sigs if abs(x["blockTime"] - when) <= TIME_WINDOW
                       and x["signature"] != skip_sig),
                      key=lambda x: abs(x["blockTime"] - when))[:BATCH]
        if not near:
            continue
        checked += 1
        txs = await rpc.batch("getTransaction", [[x["signature"], opts] for x in near])
        hit = next((x for x, tx in zip(near, txs) if tx and match_swap(tx, sw, wallet)), None)
        if hit:
            confirmed += 1
            say(f"      OK   {sw['createdAt'][:19]}  {hit['signature'][:20]}...")
        else:
            say(f"      --   {sw['createdAt'][:19]}  no matching tx on this wallet")

    return confirmed, checked


def swap_rows(payload: Any) -> list[dict]:
    """Swap rows out of either shape FOMO's swaps panel arrives in.

    `/v2/users/{id}/swaps` answers `{"swaps": [...]}`, but callers pass around
    both that envelope and the bare list. Iterating the envelope by mistake
    yields its keys, which look like "no usable swaps" instead of like an
    error -- so the unwrapping lives in one place.
    """
    rows = payload.get("swaps") if isinstance(payload, dict) else payload
    return [row for row in (rows or []) if isinstance(row, dict)]


def pick_swaps(rows: list[dict], want: int = 3) -> list[dict]:
    """Prefer recent BUYS -- the received amount is the distinctive number,
    and recent means a short walk back through the mint's signatures.

    Spread the picks over DISTINCT mints. A trader who spent the day on one
    viral token would otherwise hand every attempt to the same unpageable
    mint and fail wholesale; distinct mints fail independently.
    """
    rows = [row for row in rows if is_solana_swap(row)]
    buys = [r for r in rows
            if r.get("outTokenAddress") not in QUOTES
            and r.get("inTokenAddress") in QUOTES
            and r.get("outHumanAmount")]
    # Buys remain cheapest to match, but do not discard sells. Some profiles'
    # recent API window contains one unhelpfully busy buy followed by several
    # clean sells on distinct token mints.
    buy_ids = {id(row) for row in buys}
    pool = buys + [row for row in rows if id(row) not in buy_ids]

    picked: list[dict] = []
    seen: set[str] = set()
    for r in pool:
        mint, _amount, _direction = swap_search_leg(r)
        if mint in seen:
            continue
        seen.add(mint)
        picked.append(r)
        if len(picked) >= want:
            return picked
    # Not enough distinct mints -- top up with repeats rather than give back
    # less. A repeat is worthless to the mint route (an older swap on the same
    # mint only sits behind more signatures) but not to the sponsor route,
    # where a different timestamp is a genuinely different lookup.
    # Identity, not equality: two swaps of the same size on the same mint are
    # equal dicts, and `in` would silently drop them.
    for r in pool:
        if len(picked) >= want:
            break
        if not any(r is p for p in picked):
            picked.append(r)
    return picked

# ----------------------------------------------------------------- cache

def _load_cache(cache_path: str | Path = CACHE) -> dict[str, Any]:
    try:
        with open(cache_path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict[str, Any], cache_path: str | Path = CACHE) -> None:
    try:
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=1)
    except OSError as exc:
        log.warning("could not write %s: %s", cache_path, exc)


def cached_wallet(handle: str, cache_path: str | Path = CACHE) -> str | None:
    entry = _load_cache(cache_path).get(handle.lower())
    return entry.get("wallet") if isinstance(entry, dict) else None


@dataclass(frozen=True)
class SolanaBalancePosition:
    """One exact SPL-token ownership fingerprint from FOMO's portfolio."""

    mint: str
    raw_amounts: tuple[int, ...]
    value_usd: float


def solana_balance_positions(payload: Any) -> list[SolanaBalancePosition]:
    """Extract exact, unrounded Solana token balances from FOMO balance rows."""
    rows = payload.get("balances") if isinstance(payload, dict) else None
    positions: list[SolanaBalancePosition] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        balance = row.get("balance") if isinstance(row.get("balance"), dict) else {}
        token_filter = (row.get("tokenFilterResult")
                        if isinstance(row.get("tokenFilterResult"), dict) else {})
        token = token_filter.get("token") if isinstance(token_filter.get("token"), dict) else {}
        user_token = row.get("userToken") if isinstance(row.get("userToken"), dict) else {}
        network = (user_token.get("networkId") or token.get("networkId")
                   or balance.get("networkId"))
        token_id = str(balance.get("tokenId") or "")
        try:
            is_solana = int(network) == SOLANA_NETWORK_ID
        except (TypeError, ValueError):
            is_solana = token_id.endswith(f":{SOLANA_NETWORK_ID}")
        if not is_solana:
            continue
        mint = str(
            balance.get("tokenAddress") or token.get("address")
            or user_token.get("tokenAddress") or ""
        ).strip()
        if mint in QUOTES or not (32 <= len(mint) <= 44) or mint.startswith("0x"):
            continue
        amounts: list[int] = []
        for value in (balance.get("balance"), user_token.get("amountRemaining")):
            try:
                amount = int(str(value))
            except (TypeError, ValueError):
                continue
            if amount > 0 and amount not in amounts:
                amounts.append(amount)
        if not amounts:
            continue
        try:
            shifted = float(balance.get("shiftedBalance") or 0)
            price = float(token_filter.get("priceUSD") or 0)
            value_usd = shifted * price
        except (TypeError, ValueError, OverflowError):
            value_usd = 0.0
        positions.append(SolanaBalancePosition(mint, tuple(amounts), value_usd))
    positions.sort(key=lambda position: position.value_usd, reverse=True)
    return positions


@dataclass(frozen=True)
class CachedWalletMatch:
    handle: str
    address: str
    network: str
    confirmations: int | None = None
    source: str | None = None
    chains: tuple[str, ...] = ()


def find_cached_wallets(address: str, cache_path: str | Path = CACHE) -> list[CachedWalletMatch]:
    """Reverse-search verified Solana and EVM wallet mappings.

    Solana addresses are case-sensitive. EVM addresses are normalized to lower
    case, as required by their hexadecimal representation.
    """
    query = (address or "").strip().strip("`").strip()
    if not query:
        return []
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(cache, dict):
        return []

    matches: list[CachedWalletMatch] = []
    for handle, entry in cache.items():
        if not isinstance(entry, dict):
            continue

        solana = entry.get("wallet")
        if isinstance(solana, str) and solana == query:
            confirmed = entry.get("confirmed")
            matches.append(CachedWalletMatch(
                handle=str(handle),
                address=solana,
                network="Solana",
                confirmations=confirmed if isinstance(confirmed, int) else None,
                source="on-chain",
            ))

        evm = entry.get("evmWallet")
        if (query.lower().startswith("0x") and isinstance(evm, str)
                and evm.lower() == query.lower()
                and str(entry.get("evmStatus") or "").lower() == "verified"):
            chains = entry.get("evmChains")
            matches.append(CachedWalletMatch(
                handle=str(handle),
                address=evm,
                network="EVM",
                source=str(entry.get("evmSource") or "verified"),
                chains=tuple(str(chain) for chain in chains)
                if isinstance(chains, list) else (),
            ))

    return sorted(matches, key=lambda match: match.handle.casefold())


# --------------------------------------------------- unverified candidates

@dataclass(frozen=True)
class WalletCandidate:
    """A wallet a route pinned down on chain but could not verify.

    The routes below only ever *return* a wallet they have corroborated, and
    that stays true. But a route that reaches "exactly one on-chain owner
    holds the position FOMO publishes for this trader" and then fails to
    corroborate it used to drop that owner on the floor, which is how a
    handle with a perfectly good single candidate ended up showing nothing at
    all.

    A candidate is never cached and never returned as a wallet. It travels
    beside the (absent) answer so the caller can offer it *as unverified*,
    clearly marked, instead of offering nothing.

    `sources` names the routes that produced it and `evidence` the mints
    behind it -- both are unioned when two routes land on the same address,
    which is exactly what makes that address stronger than a lone hit.
    """

    address: str
    sources: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    @property
    def strength(self) -> tuple[int, int]:
        """Rank key: independent routes first, then independent tokens."""
        return len(self.sources), len(self.evidence)

    def merged_with(self, other: "WalletCandidate") -> "WalletCandidate":
        return WalletCandidate(
            address=self.address,
            sources=tuple(dict.fromkeys(self.sources + other.sources)),
            evidence=tuple(dict.fromkeys(self.evidence + other.evidence)),
        )


def choose_unverified_wallets(
    candidates: Sequence[WalletCandidate],
) -> list[WalletCandidate]:
    """The strongest unverified candidate(s), or all of them when tied.

    Candidates arrive from independent routes, so the same address seen twice
    is stronger than either sighting alone: merge first, rank after. If one
    address then stands above the rest it is the fallback and the only thing
    worth showing. If several tie at the top there is no honest way to prefer
    one, so every candidate is returned and the caller says so.
    """
    merged: dict[str, WalletCandidate] = {}
    for candidate in candidates:
        address = (candidate.address or "").strip()
        if not address:
            continue
        seen = merged.get(address)
        merged[address] = candidate if seen is None else seen.merged_with(candidate)
    if not merged:
        return []
    ranked = sorted(merged.values(), key=lambda item: item.strength, reverse=True)
    best = ranked[0].strength
    leaders = [item for item in ranked if item.strength == best]
    return leaders if len(leaders) == 1 else ranked


# -------------------------------------------------------------- resolver

class WalletResolver:
    """
    Handle -> real wallet, with an on-disk cache.

        async with httpx.AsyncClient(timeout=60) as http:
            wallets = WalletResolver(http)
            addr = await wallets.resolve(fomo_client, user)

    Never raises: a failure returns None, because the wallet is a nice-to-have
    on an embed that is useful without it.
    """

    def __init__(self, http: Any, url: str | list[str] = RPC_URLS,
                 verify_targets: int = 2,
                 deep: bool | None = None,
                 deep_attempts: int = DEEP_ATTEMPTS,
                 cache_path: str | Path = CACHE) -> None:
        self.rpc = Rpc(http, url)
        self.cache_path = Path(cache_path)
        # The bot path stays snappy with a light verify; the CLI asks for more.
        self.verify_targets = verify_targets
        # The block scan is expensive but it is the only route FOMO's growth
        # cannot outrun, so it runs as a bounded second pass rather than not at
        # all. `deep=None` takes the FOMO_WALLET_DEEP default; pass False to
        # force the cheap routes only.
        self.deep = DEEP_DEFAULT if deep is None else deep
        self.deep_attempts = max(1, deep_attempts)
        self._locks: dict[str, asyncio.Lock] = {}

    async def resolve(self, fomo: Any, user: Any, limit: int = 50,
                      use_cache: bool = True) -> str | None:
        handle = (getattr(user, "handle", "") or "").lower()
        if use_cache and handle:
            if hit := cached_wallet(handle, self.cache_path):
                return hit

        # Two /fomo calls for the same handle at once would each pay the full
        # scan. Serialise per handle so the second one gets the cache.
        lock = self._locks.setdefault(handle, asyncio.Lock())
        async with lock:
            if use_cache and handle:
                if hit := cached_wallet(handle, self.cache_path):
                    return hit
            try:
                return await self._resolve(fomo, user, handle, limit)
            except Exception as exc:
                if "cooling down" in str(exc):
                    log.debug("wallet resolution deferred for %s: %s", handle, exc)
                else:
                    log.warning("wallet resolution failed for %s: %s", handle, exc)
                return None

    async def _resolve(self, fomo: Any, user: Any, handle: str, limit: int) -> str | None:
        data = await fomo._get(
            f"/v2/users/{user.id}/swaps?limit={limit}",
            cache=False,
            lane="background",
        )
        rows = [row for row in swap_rows(data) if is_solana_swap(row)]
        if not rows:
            log.info("no Solana wallet match for %s: FOMO returned no swaps", handle)
            return None

        # One index for all four attempts -- the sponsor history is paged once.
        index = SponsorIndex(self.rpc)
        picks = pick_swaps(rows, want=4)
        wallet = hit_sig = None
        route = "sponsor"

        # All three routes per swap, cheapest first, before moving to the next
        # swap. The alternative -- every cheap route across every swap, then a
        # block pass -- pays four full mint scans (12 pages of 1000 signatures
        # each) before trying the one route that can still reach, which is the
        # slow way to answer a handle whose history is already known to be
        # behind the cap.
        #
        # The block route is bounded to the newest few swaps because it costs a
        # slot search plus a getBlock per slot. The newest swap is both the
        # likeliest to resolve and the cheapest slot to find, so the bound
        # spends that budget where it pays. Swaps past the bound still get the
        # cheap routes: a quiet mint is cheap and might still hit.
        for position, sw in enumerate(picks):
            use_blocks = self.deep and position < self.deep_attempts
            _sig, tx, found = await locate_swap(self.rpc, sw, index,
                                                deep=use_blocks, verbose=False)
            if tx:
                wallet, _how = derive_trader(tx)
                hit_sig, route = _sig, found
                break

        if not wallet:
            log.info(
                "no transaction-backed Solana wallet match for %s across %d "
                "usable swap(s)%s",
                handle, len(picks),
                "" if self.deep else " (block route off: set FOMO_WALLET_DEEP=1)",
            )
            return None

        confirmed = 0
        if self.verify_targets:
            confirmed, _checked = await verify_wallet(
                self.rpc, wallet, rows, skip_sig=hit_sig,
                targets=self.verify_targets, verbose=False)

        cache = _load_cache(self.cache_path)
        entry = cache.get(handle)
        if not isinstance(entry, dict):
            entry = {}
        entry.update({
            "wallet": wallet,
            "confirmed": confirmed,
            "walletSource": f"fomo-{route}",
            "resolvedAt": int(time.time()),
        })
        cache[handle] = entry
        _save_cache(cache, self.cache_path)
        log.info("resolved %s -> %s (%d confirmation(s))", handle, wallet, confirmed)
        return wallet

    async def _corroborate(
        self, handle: str, wallet: str, swaps: Sequence[dict] = ()
    ) -> tuple[bool, str, int]:
        """Is this candidate really this trader's wallet?

        Two gates, strongest first.

        `verify_wallet` scans the CANDIDATE's own signature history for the
        swaps FOMO reports for this trader. That ties the wallet to *these
        trades*, not merely to the platform, and it is the direction that
        scales -- a trader's account runs to hundreds of signatures where a
        viral mint runs to tens of thousands. It needs the trader's swap rows.

        `_has_fomo_sponsored_transaction` is the older gate and much weaker: it
        only asks whether the wallet ever co-signed a FOMO-sponsored
        transaction, and it looks at the newest 40 alone, so a wallet with
        recent non-FOMO activity or airdrop spam fails it while a whale that
        happens to hold the matching amount passes anything short of it. It is
        the fallback for callers that have no swaps -- `/token`'s adoption
        path, which would otherwise pay a FOMO request per holder.

        The distinction that matters is refuted vs inconclusive. `verify_wallet`
        returns (confirmed, checked); `checked == 0` means it found no
        signatures near any swap time and therefore never got to look, so the
        weaker gate still gets its turn. `checked > 0 and confirmed == 0` means
        it looked at this wallet's own history and this trader's swaps are not
        in it -- that is a refusal, and falling through to a weaker check after
        it would be throwing away the better answer.

        Returns (corroborated, how, confirmations); `how` becomes part of
        `walletSource`, so the cache records which gate let a wallet in.
        """
        usable = [row for row in swap_rows(swaps) if row.get("createdAt")]
        if usable:
            confirmed, checked = await verify_wallet(
                self.rpc, wallet, usable,
                targets=max(1, self.verify_targets), verbose=False,
            )
            if confirmed:
                return True, f"verify{confirmed}", confirmed
            if checked:
                log.info(
                    "candidate %s for %s explains none of %d checked swap(s); "
                    "rejected", wallet, handle, checked,
                )
                return False, "verify0", 0
            log.debug(
                "verify found no signatures near %s's swaps on %s; falling back "
                "to the sponsor check", handle, wallet,
            )
        return await self._has_fomo_sponsored_transaction(wallet), "fomo-sponsor", 1

    async def resolve_from_holders(
        self, fomo: Any, user: Any, balances: Any,
        swaps: Sequence[dict] = (), use_cache: bool = True,
        candidates: list[WalletCandidate] | None = None,
    ) -> str | None:
        """FOMO's own holder list, asked about this trader's own positions.

        The cheapest identity source in the project, and until now it only ran
        as a side effect of somebody typing `/token`. FOMO publishes each
        holder's exact position; the chain says who holds that amount; one
        unambiguous match is a wallet with no sponsor index, no mint scan and
        no block scan.

        Two things make it cheaper than the balance fallback it sits in front
        of. One request covers every position, and its answer says which tokens
        publish a row naming this trader -- so the on-chain query is paid only
        where it can possibly pay off. And the match itself is the one from
        `fomo_hodlers`, which tolerates the rounding FOMO applies to
        `humanAmount` and demands uniqueness in both directions: the amount
        must identify exactly one wallet AND that wallet must match exactly one
        trader.

        `candidates` is an optional sink. Pass a list and any unambiguous
        owner this route finds but cannot corroborate is appended to it as a
        `WalletCandidate`. The return value is unchanged -- an uncorroborated
        owner is still not a wallet -- but the caller keeps the option of
        showing it, clearly marked as unverified, rather than nothing.
        """
        handle = (getattr(user, "handle", "") or "").lower()
        if not handle:
            return None
        if use_cache and (hit := cached_wallet(handle, self.cache_path)):
            return hit
        lock = self._locks.setdefault(handle, asyncio.Lock())
        async with lock:
            if use_cache and (hit := cached_wallet(handle, self.cache_path)):
                return hit
            try:
                return await self._resolve_from_holders(
                    handle, fomo, balances, swaps, candidates
                )
            except Exception as exc:
                log.warning("Solana holder discovery failed for %s: %s", handle, exc)
                return None

    async def _holder_groups(self, fomo: Any, mints: list[str]) -> dict[str, Any]:
        """`/hodlers/top` for several tokens, by token address.

        The batch shape is what FOMO's own token page uses and the `tokens`
        parameter is an array, but it has only ever been observed with one
        entry. Any token the batch does not answer for is asked about on its
        own rather than silently dropped, so a cap on the server side costs
        requests instead of costing the route.
        """
        groups: dict[str, Any] = {}
        try:
            batch = await fomo.token_holders_many(mints, SOLANA_NETWORK_ID)
        except Exception as exc:
            log.info("batched holder lookup failed: %s", exc)
            batch = []
        for group in parse_holder_groups(batch):
            if group.address:
                groups[group.address] = group

        missing = [mint for mint in mints if mint not in groups]
        if missing and len(missing) < len(mints):
            log.info("holder batch answered %d of %d token(s); asking for the rest "
                     "one at a time", len(groups), len(mints))
        for mint in missing:
            try:
                single = await fomo.token_holders(mint, SOLANA_NETWORK_ID,
                                                  background=True)
            except Exception as exc:
                log.debug("holder lookup failed for %s: %s", mint, exc)
                continue
            for group in parse_holder_groups(single):
                groups[group.address or mint] = group
        return groups

    async def _solana_owner_amounts(self, mint: str) -> list[tuple[str, float]]:
        """(owner, human amount) for one mint, for the holder matcher.

        `_helius_token_balances` returns raw integers because the balance
        route fingerprints them exactly; `/hodlers/top` publishes a rounded
        human amount, so this scales by the mint's decimals to meet it.
        """
        totals = await self._helius_token_balances(mint)
        if not totals:
            return []
        supply = await self.rpc("getTokenSupply", [mint])
        decimals = ((supply or {}).get("value") or {}).get("decimals")
        if decimals is None:
            log.debug("no decimals for %s; cannot scale holder amounts", mint)
            return []
        scale = 10 ** int(decimals)
        return [(owner, raw / scale) for owner, raw in totals.items()]

    async def _resolve_from_holders(
        self, handle: str, fomo: Any, balances: Any, swaps: Sequence[dict],
        candidates: list[WalletCandidate] | None = None,
    ) -> str | None:
        positions = solana_balance_positions(balances)[:HODLER_TOKENS]
        if not positions:
            log.info("no Solana wallet match for %s: no usable Solana balances", handle)
            return None

        groups = await self._holder_groups(fomo, [p.mint for p in positions])
        if not groups:
            log.info(
                "no Solana wallet match for %s: FOMO published no holder list "
                "for any of its %d position(s)", handle, len(positions),
            )
            return None

        # A token whose published holder list does not name this trader can
        # never name their wallet, and finding that out costs no on-chain call.
        named = [group for position in positions
                 if (group := groups.get(position.mint))
                 and any(row.handle.lower() == handle for row in group.holders)]
        if not named:
            log.info(
                "no Solana wallet match for %s: not in the published top holders "
                "of any of its %d position(s)", handle, len(positions),
            )
            return None
        log.info(
            "holder route: %s is a published top holder of %d of its %d position(s)",
            handle, len(named), len(positions),
        )

        evidence: dict[str, list[str]] = {}
        for group in named:
            onchain = await self._solana_owner_amounts(group.address)
            if not onchain:
                continue
            # The cache-grade matcher: tolerant of FOMO's display rounding,
            # and unique in both directions or it refuses to pair at all.
            matches = confident_matches(group.holders, onchain)
            owners = [wallet for wallet, holder in matches.items()
                      if holder.handle.lower() == handle]
            if len(owners) != 1:
                log.debug(
                    "holder route: %s matched %d wallet(s) on %s",
                    handle, len(owners), group.address,
                )
                continue
            owner = owners[0]
            evidence.setdefault(owner, []).append(group.address)
            if len(evidence[owner]) >= 2:
                return self._save_derived_match(
                    handle, owner,
                    source="hodlers+amount+2tokens",
                    confirmed=len(evidence[owner]),
                    detail=f"{len(evidence[owner])} published holder position(s)",
                    extra={"hodlerToken": evidence[owner][0]},
                )

        singles = [(owner, tokens) for owner, tokens in evidence.items()
                   if len(tokens) == 1]
        refuted: set[str] = set()
        if len(singles) == 1:
            owner, tokens = singles[0]
            ok, how, confirmed = await self._corroborate(handle, owner, swaps)
            if ok:
                return self._save_derived_match(
                    handle, owner,
                    source=f"hodlers+amount+{how}",
                    confirmed=confirmed,
                    detail=f"a published holder position on {tokens[0][:12]}",
                    extra={"hodlerToken": tokens[0]},
                )
            if how == "verify0":
                refuted.add(owner)

        log.info(
            "no Solana wallet match for %s: %d published position(s), "
            "%d unambiguous owner(s), no verified owner",
            handle, len(named), len(evidence),
        )
        self._offer_candidates(handle, candidates, evidence, refuted,
                               source="hodlers+amount")
        return None

    async def resolve_from_balances(
        self, user: Any, balances: Any, swaps: Sequence[dict] = (),
        use_cache: bool = True,
        candidates: list[WalletCandidate] | None = None,
    ) -> str | None:
        """Fallback: map exact FOMO SPL balances to their on-chain owner.

        `swaps` is optional and only reaches the corroboration gate, where it
        upgrades the check from "this wallet has touched FOMO" to "this wallet
        made these trades".

        `candidates` is the same optional sink `resolve_from_holders` takes:
        an unambiguous owner that fails the gate is recorded there instead of
        being discarded, and is still not returned as a wallet.
        """
        handle = (getattr(user, "handle", "") or "").lower()
        if not handle:
            return None
        if use_cache and (hit := cached_wallet(handle, self.cache_path)):
            return hit
        lock = self._locks.setdefault(handle, asyncio.Lock())
        async with lock:
            if use_cache and (hit := cached_wallet(handle, self.cache_path)):
                return hit
            try:
                return await self._resolve_from_balances(
                    handle, balances, swaps, candidates
                )
            except Exception as exc:
                log.warning("Solana balance discovery failed for %s: %s", handle, exc)
                return None

    async def _resolve_from_balances(
        self, handle: str, balances: Any, swaps: Sequence[dict] = (),
        candidates: list[WalletCandidate] | None = None,
    ) -> str | None:
        positions = solana_balance_positions(balances)[:6]
        if not positions:
            log.info("no Solana wallet match for %s: no usable Solana balances", handle)
            return None

        evidence: dict[str, list[str]] = {}
        for position in positions:
            holders = await self._helius_token_balances(position.mint)
            matches = {
                owner for owner, amount in holders.items()
                if amount in position.raw_amounts
            }
            if len(matches) != 1:
                log.debug(
                    "balance fingerprint for %s on %s matched %d owner(s)",
                    handle, position.mint, len(matches),
                )
                continue
            owner = next(iter(matches))
            evidence.setdefault(owner, []).append(position.mint)
            if len(evidence[owner]) >= 2:
                return self._save_balance_match(handle, owner, evidence[owner])

        # One exact balance is high-entropy evidence, but confirm the owner
        # really is this trader before caching it -- against their own swaps
        # where the caller supplied them, against the sponsor otherwise.
        singles = [(owner, mints) for owner, mints in evidence.items() if len(mints) == 1]
        refuted: set[str] = set()
        if len(singles) == 1:
            owner, mints = singles[0]
            ok, how, confirmed = await self._corroborate(handle, owner, swaps)
            if ok:
                return self._save_balance_match(handle, owner, mints, how, confirmed)
            if how == "verify0":
                refuted.add(owner)

        log.info(
            "no Solana wallet match for %s: %d balance fingerprint(s), "
            "%d unambiguous owner(s), no verified owner",
            handle, len(positions), len(evidence),
        )
        self._offer_candidates(handle, candidates, evidence, refuted,
                               source="balance+helius")
        return None

    def _offer_candidates(
        self, handle: str, sink: list[WalletCandidate] | None,
        evidence: dict[str, list[str]], refuted: set[str], *, source: str,
    ) -> None:
        """Hand the route's unambiguous-but-unverified owners to the caller.

        The route still returns None and still caches nothing: none of this is
        a verified wallet. It only stops an owner the route already pinned
        down from being thrown away, so a caller that would otherwise print
        "no wallet" can offer it under a warning instead.

        An owner `_corroborate` *refuted* is not offered. `verify_wallet`
        looked at that wallet's own history and this trader's swaps were not
        in it -- that is an answer, not a gap, and re-offering it as "likely"
        would be worse than saying nothing.
        """
        if sink is None:
            return
        offered: list[str] = []
        for owner, tokens in evidence.items():
            if owner in refuted:
                log.info(
                    "not offering %s as an unverified wallet for %s: its own "
                    "history refutes this trader's swaps", owner, handle,
                )
                continue
            sink.append(WalletCandidate(
                address=owner, sources=(source,), evidence=tuple(tokens),
            ))
            offered.append(owner)
        if offered:
            log.info(
                "%s route: keeping %d unverified candidate(s) for %s: %s",
                source, len(offered), handle, ", ".join(offered),
            )

    async def _helius_token_balances(self, mint: str) -> dict[str, int]:
        """Return raw token totals by owner using Helius DAS getTokenAccounts."""
        helius_urls = [url for url in self.rpc.urls if "helius" in url.lower()]
        if not helius_urls:
            log.debug("Solana balance fallback skipped: no Helius RPC configured")
            return {}
        last_error: Exception | None = None
        for url in helius_urls:
            totals: dict[str, int] = {}
            try:
                for page in range(1, 4):
                    response = await self.rpc.http.post(url, json={
                        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccounts",
                        "params": {"mint": mint, "page": page, "limit": 1000},
                    })
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or payload.get("error"):
                        detail = payload.get("error") if isinstance(payload, dict) else payload
                        raise RuntimeError(f"getTokenAccounts: {detail}")
                    result = payload.get("result") or {}
                    accounts = result.get("token_accounts") or []
                    for account in accounts:
                        if not isinstance(account, dict) or not account.get("owner"):
                            continue
                        try:
                            amount = int(str(account.get("amount") or 0))
                        except (TypeError, ValueError):
                            continue
                        owner = str(account["owner"])
                        totals[owner] = totals.get(owner, 0) + amount
                    if len(accounts) < 1000:
                        break
                return totals
            except Exception as exc:
                last_error = exc
                log.debug("Helius balance query failed via %s: %s",
                          rpc_display_name(url), exc)
        if last_error:
            raise RuntimeError(f"all Helius balance queries failed: {last_error}")
        return {}

    async def _has_fomo_sponsored_transaction(self, owner: str) -> bool:
        signatures = await self.rpc(
            "getSignaturesForAddress", [owner, {"limit": 80}]
        ) or []
        live = [row for row in signatures if not row.get("err")][:40]
        opts = {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}
        for start in range(0, len(live), BATCH):
            chunk = live[start:start + BATCH]
            txs = await self.rpc.batch(
                "getTransaction", [[row["signature"], opts] for row in chunk]
            )
            for tx in txs:
                if not tx:
                    continue
                keys = ((tx.get("transaction") or {}).get("message") or {}).get(
                    "accountKeys"
                ) or []
                parsed = [
                    (str(key.get("pubkey")), bool(key.get("signer")))
                    if isinstance(key, dict) else (str(key), False)
                    for key in keys
                ]
                if (parsed and parsed[0][0] in SPONSORS
                        and any(pubkey == owner and signer for pubkey, signer in parsed)):
                    return True
        return False

    async def adopt_holder_matches(
        self, matches: dict[str, str], token: str = "",
        swaps_by_handle: dict[str, Sequence[dict]] | None = None,
    ) -> dict[str, str]:
        """Persist wallet -> handle pairs found in FOMO's own holder list.

        `/hodlers/top` states a trader's exact position and `/token` already
        knows every on-chain owner, so an unambiguous amount match is an
        identity for free -- no sponsor index, no mint scan, no block route.

        A cached wallet is permanent and is trusted by `/fomo` and `/wallet`,
        so this applies the same bar `_resolve_from_balances` uses for a single
        fingerprint, through the same `_corroborate` gate. `/token` renders a
        card for a token, not for a trader, so it holds no swap rows for the
        handles it names and cannot get them without a FOMO request each --
        which means this path takes the weaker sponsor check. `swaps_by_handle`
        is there for callers that already have the rows; supplying them
        upgrades the gate to the candidate's own trade history.

        Existing mappings are never overwritten; a disagreement is logged and
        skipped. Returns the pairs actually written.
        """
        written: dict[str, str] = {}
        for wallet, handle in matches.items():
            handle = (handle or "").lstrip("@").lower()
            if not wallet or not handle:
                continue
            existing = cached_wallet(handle, self.cache_path)
            if existing == wallet:
                continue
            if existing:
                log.info(
                    "holder match for %s (%s) disagrees with the cached wallet "
                    "%s; keeping the cached one", handle, wallet, existing,
                )
                continue
            claimed = [match.handle for match in
                       find_cached_wallets(wallet, self.cache_path)
                       if match.handle.lower() != handle]
            if claimed:
                log.info("wallet %s is already cached as @%s; not adopting @%s",
                         wallet, claimed[0], handle)
                continue
            try:
                corroborated, how, confirmed = await self._corroborate(
                    handle, wallet, (swaps_by_handle or {}).get(handle, ()),
                )
            except Exception as exc:
                log.debug("corroboration failed for %s: %s", wallet, exc)
                continue
            if not corroborated:
                log.info(
                    "holder match %s -> @%s not adopted: %s did not corroborate "
                    "that wallet", wallet, handle, how,
                )
                continue

            cache = _load_cache(self.cache_path)
            entry = cache.get(handle)
            if not isinstance(entry, dict):
                entry = {}
            entry.update({
                "wallet": wallet,
                "confirmed": confirmed,
                "walletSource": f"hodlers+amount+{how}",
                "resolvedAt": int(time.time()),
            })
            if token:
                entry["hodlerToken"] = token
            cache[handle] = entry
            _save_cache(cache, self.cache_path)
            written[wallet] = handle
            log.info("adopted %s -> @%s from FOMO's holder list", wallet, handle)
        return written

    def _save_balance_match(
        self, handle: str, owner: str, mints: list[str],
        how: str = "", confirmed: int | None = None,
    ) -> str:
        """Record a derived match and, honestly, what let it in.

        Two independent tokens agreeing needs no third-party gate, and used to
        be filed as `fomo-sponsor` anyway -- a check that path never ran.
        """
        gate = how or f"{len(mints)}mints"
        return self._save_derived_match(
            handle, owner,
            source=f"balance+helius+{gate}",
            confirmed=len(mints) if confirmed is None else confirmed,
            detail=f"{len(mints)} exact balance fingerprint(s)",
        )

    def _save_derived_match(
        self, handle: str, owner: str, *, source: str, confirmed: int,
        detail: str, extra: dict[str, Any] | None = None,
    ) -> str:
        cache = _load_cache(self.cache_path)
        entry = cache.get(handle)
        if not isinstance(entry, dict):
            entry = {}
        entry.update({
            "wallet": owner,
            "confirmed": confirmed,
            "walletSource": source,
            "resolvedAt": int(time.time()),
            **(extra or {}),
        })
        cache[handle] = entry
        _save_cache(cache, self.cache_path)
        log.info("resolved Solana %s -> %s from %s", handle, owner, detail)
        return owner
