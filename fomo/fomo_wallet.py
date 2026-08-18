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
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("fomo.wallet")

RPC_URL = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
CACHE = os.getenv("FOMO_WALLET_CACHE", "wallet_cache.json")

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


def rpc_display_name(url: str) -> str:
    """Show which RPC is in use without leaking the API key in its URL."""
    parsed = urlsplit(url)
    host = parsed.hostname or "configured RPC"
    if parsed.port:
        host += f":{parsed.port}"
    return f"{parsed.scheme}://{host}" if parsed.scheme else host


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


class Rpc:
    """Minimal JSON-RPC client with backoff -- public endpoints 429 constantly."""

    def __init__(self, http: Any, url: str = RPC_URL) -> None:
        self.http, self.url, self.calls = http, url, 0

    async def __call__(self, method: str, params: list[Any]) -> Any:
        for attempt in range(8):
            r = await self.http.post(
                self.url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            if r.status_code == 429:
                wait = min(2.0 * (attempt + 1), 12.0)
                if attempt == 0:
                    log.info("RPC rate limited, backing off "
                             "(a paid endpoint in SOLANA_RPC removes this)")
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            self.calls += 1
            payload = r.json()
            if "error" in payload:
                raise RuntimeError(f"{method}: {payload['error']}")
            return payload.get("result")
        raise RuntimeError(f"{method}: still rate limited after 8 tries")

    async def batch(self, method: str, param_sets: list[list[Any]]) -> list[Any]:
        """One HTTP request, many calls. Rate limits are counted per REQUEST,
        so this is the difference between 15 throttled calls and 2 clean ones.
        Falls back to sequential if the endpoint rejects batching."""
        if not param_sets:
            return []
        payload = [{"jsonrpc": "2.0", "id": i, "method": method, "params": p}
                   for i, p in enumerate(param_sets)]
        for attempt in range(8):
            r = await self.http.post(self.url, json=payload)
            if r.status_code == 429:
                await asyncio.sleep(min(2.0 * (attempt + 1), 12.0))
                continue
            if r.status_code >= 400:
                break  # batching unsupported -> sequential
            self.calls += 1
            try:
                out = r.json()
                if not isinstance(out, list):
                    break
            except ValueError:
                break
            by_id = {item.get("id"): item.get("result") for item in out}
            return [by_id.get(i) for i in range(len(param_sets))]

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
                swap: dict | None = None) -> tuple[str | None, dict | None]:
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
                if delta > 0 and close(delta, amount):
                    hit(i, c, "out-amount")
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
                              swap: dict | None = None
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
                       limit=SPONSOR_TX_FETCH, swap=swap)


async def find_tx(rpc: Rpc, mint: str, amount: float, when: int,
                  verbose: bool = True, swap: dict | None = None
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
    return await _scan(rpc, pool, mint, amount, when, say, "mint", swap=swap)


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
                             swap: dict | None = None
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
                    if delta > 0 and close(delta, amount):
                        return took(entry, tx, "out-amount")
    say("      blocks: no amount match in the window")
    return None, None


async def locate_swap(rpc: Rpc, swap: dict, index: SponsorIndex | None = None,
                      deep: bool = False, verbose: bool = True
                      ) -> tuple[str | None, dict | None, str]:
    """Find the transaction behind one swap. Returns (signature, tx, route)."""
    mint = swap.get("outTokenAddress")
    try:
        amount = float(swap.get("outHumanAmount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if not mint or not amount or not swap.get("createdAt"):
        return None, None, "unusable swap"
    when = iso_epoch(swap["createdAt"])

    if index is not None:
        sig, tx = await find_tx_via_sponsor(rpc, index, mint, amount, when,
                                            verbose, swap=swap)
        if tx:
            return sig, tx, "sponsor"
    sig, tx = await find_tx(rpc, mint, amount, when, verbose, swap=swap)
    if tx:
        return sig, tx, "mint"
    if deep:
        sig, tx = await find_tx_via_blocks(rpc, mint, amount, when,
                                           verbose=verbose, swap=swap)
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
    todo = [s for s in swaps if s.get("createdAt")][:targets]
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


def pick_swaps(rows: list[dict], want: int = 3) -> list[dict]:
    """Prefer recent BUYS -- the received amount is the distinctive number,
    and recent means a short walk back through the mint's signatures.

    Spread the picks over DISTINCT mints. A trader who spent the day on one
    viral token would otherwise hand every attempt to the same unpageable
    mint and fail wholesale; distinct mints fail independently.
    """
    buys = [r for r in rows
            if r.get("outTokenAddress") not in QUOTES
            and r.get("inTokenAddress") in QUOTES
            and r.get("outHumanAmount")]
    pool = buys or rows

    picked: list[dict] = []
    seen: set[str] = set()
    for r in pool:
        mint = r.get("outTokenAddress")
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

def _load_cache() -> dict[str, Any]:
    try:
        with open(CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        with open(CACHE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=1)
    except OSError as exc:
        log.warning("could not write %s: %s", CACHE, exc)


def cached_wallet(handle: str) -> str | None:
    entry = _load_cache().get(handle.lower())
    return entry.get("wallet") if isinstance(entry, dict) else None


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

    def __init__(self, http: Any, url: str = RPC_URL, verify_targets: int = 2,
                 deep: bool = False) -> None:
        self.rpc = Rpc(http, url)
        # The bot path stays snappy with a light verify; the CLI asks for more.
        self.verify_targets = verify_targets
        # The block scan is thorough but expensive -- off on the embed path.
        self.deep = deep
        self._locks: dict[str, asyncio.Lock] = {}

    async def resolve(self, fomo: Any, user: Any, limit: int = 25,
                      use_cache: bool = True) -> str | None:
        handle = (getattr(user, "handle", "") or "").lower()
        if use_cache and handle:
            if hit := cached_wallet(handle):
                return hit

        # Two /fomo calls for the same handle at once would each pay the full
        # scan. Serialise per handle so the second one gets the cache.
        lock = self._locks.setdefault(handle, asyncio.Lock())
        async with lock:
            if use_cache and handle:
                if hit := cached_wallet(handle):
                    return hit
            try:
                return await self._resolve(fomo, user, handle, limit)
            except Exception as exc:
                log.warning("wallet resolution failed for %s: %s", handle, exc)
                return None

    async def _resolve(self, fomo: Any, user: Any, handle: str, limit: int) -> str | None:
        data = await fomo._get(f"/v2/users/{user.id}/swaps?limit={limit}", cache=False)
        rows = (data.get("swaps") if isinstance(data, dict) else data) or []
        if not rows:
            return None

        # One index for all four attempts -- the sponsor history is paged once.
        index = SponsorIndex(self.rpc)
        wallet = hit_sig = None
        for sw in pick_swaps(rows, want=4):
            _sig, tx, _route = await locate_swap(self.rpc, sw, index,
                                                 deep=self.deep, verbose=False)
            if tx:
                wallet, _how = derive_trader(tx)
                hit_sig = _sig
                break
        if not wallet:
            return None

        confirmed = 0
        if self.verify_targets:
            confirmed, _checked = await verify_wallet(
                self.rpc, wallet, rows, skip_sig=hit_sig,
                targets=self.verify_targets, verbose=False)

        cache = _load_cache()
        cache[handle] = {"wallet": wallet, "confirmed": confirmed,
                         "resolvedAt": int(time.time())}
        _save_cache(cache)
        log.info("resolved %s -> %s (%d confirmation(s))", handle, wallet, confirmed)
        return wallet
