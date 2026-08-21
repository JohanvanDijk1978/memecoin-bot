"""
connected_wallets.py -- wallets with strong on-chain evidence of belonging to
the same cluster as a known one.

What this can and cannot say
----------------------------
It cannot say two wallets have the same owner. Nothing observable on a chain
says that. What it can say is how much *evidence* there is for a relationship
that is hard to produce any other way: the same two addresses moving value
between each other repeatedly, in both directions, over months, with one of
them having funded the other in the first place. Any one of those is ordinary.
Several of them together, between two addresses that interact with almost
nobody else, is not.

So the output is a score with a band -- Very High, High, Possible -- and the
score is the strength of the evidence, never a probability of ownership. A
band is additionally capped by how many *independent* signals fired, because
one very loud signal (a hundred transfers on a single day) is a worse case
than three quiet ones (regular transfers, both directions, across nine
months).

Precision over recall
---------------------
It is better to return nothing than to return a router. Three defences, in
order of how much they cost:

1. **Known addresses** -- exchanges, bridges, routers, programs, the FOMO gas
   sponsor. A static list, extendable at runtime through
   `CONNECTED_LABELS_FILE` so an operator can add one without a release.
2. **What kind of account it is** -- on Solana a real wallet is owned by the
   system program and is not executable, which rules out pools, token
   accounts, vaults and PDAs outright. On EVM the same test would be wrong:
   FOMO's own wallets are ERC-4337 contracts, so contract code caps the band
   instead of excluding, and only for an address the wallet cache does not
   already know.
3. **Degree** -- one bounded page of the candidate's own history. An address
   that deals with dozens of unrelated counterparties is a service, whatever
   it is called. This costs a request per candidate, so it runs last and only
   on the few that survived everything else.

Data sources are the ones the project already pays for: Helius parsed
transaction history on Solana, `alchemy_getAssetTransfers` on EVM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

log = logging.getLogger("fomo.connected")

# ------------------------------------------------------------------ budget --


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    try:
        return max(low, min(int(os.getenv(name, str(default))), high))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# 5 Helius pages is 500 parsed transactions per wallet; each EVM chain costs
# two `alchemy_getAssetTransfers` calls (one per direction) per page.
SOLANA_PAGES = _env_int("CONNECTED_SOLANA_PAGES", 5, low=1, high=25)
EVM_PAGES = _env_int("CONNECTED_EVM_PAGES", 1, low=1, high=10)
EVM_PAGE_SIZE = "0x1f4"  # 500 transfers per direction per page
HELIUS_TX_LIMIT = 100
# Chains an EVM wallet is checked on when the cache does not say which ones it
# has been seen on. FOMO trades Base and BSC; the others are opt-in because
# every extra chain is two more requests for a usually empty answer.
DEFAULT_EVM_CHAINS = tuple(
    chain.strip().lower()
    for chain in os.getenv("CONNECTED_EVM_CHAINS", "base,bsc").split(",")
    if chain.strip()
)
# How many candidates are worth an account-type check, and how many of those
# are worth a degree check. Both are ordered by score, so the cut falls on the
# weakest evidence.
VERIFY_CANDIDATES = _env_int("CONNECTED_VERIFY_CANDIDATES", 12, low=1, high=40)
DEGREE_CANDIDATES = _env_int("CONNECTED_DEGREE_CANDIDATES", 8, low=1, high=25)
# An address dealing with this many distinct counterparties in one sampled
# page is a service, not a person's second wallet.
HIGH_DEGREE_COUNTERPARTIES = _env_int(
    "CONNECTED_HIGH_DEGREE", 40, low=10, high=500
)
DEGREE_SAMPLE = 100

CACHE_TTL = _env_float("CONNECTED_CACHE_TTL", 6 * 3600)
CACHE_FILE = os.getenv("CONNECTED_CACHE_FILE", "connected_cache.json")

# ------------------------------------------------------------- thresholds --

# A single transfer is never evidence. Two is a coincidence with a receipt.
MIN_TRANSFERS = 3
SCORE_VERY_HIGH = 85
SCORE_HIGH = 70
SCORE_POSSIBLE = 55
# `/connected` shows only the strongest by default; the weaker band is behind
# a button rather than dropped, so a run that found something borderline can
# say so without putting it on the first page.
DEFAULT_MIN_SCORE = SCORE_HIGH
BANDS = (
    (SCORE_VERY_HIGH, "Very High"),
    (SCORE_HIGH, "High"),
    (SCORE_POSSIBLE, "Possible"),
)
# A band no number of points can reach without this many independent signals.
BAND_SIGNAL_FLOOR = {"Very High": 4, "High": 3, "Possible": 2}

STABLE_SYMBOLS = {
    "USDC", "USDT", "USDG", "USDS", "DAI", "USDC.E", "USDBC", "BUSD", "FDUSD",
}
NATIVE_SYMBOLS = {
    "solana": "SOL", "ethereum": "ETH", "base": "ETH", "bsc": "BNB",
    "robinhood": "ETH",
}

# ------------------------------------------------------- known addresses ----
# Structural filtering (account type, degree) is the primary defence; this
# list is the cheap first pass and is deliberately conservative. Wrong entries
# cost recall, never precision, which is the direction this feature wants to
# fail in.

FOMO_GAS_SPONSOR = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"

SOLANA_LABELS = {
    "11111111111111111111111111111111": "System program",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "Token program",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022 program",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "Associated token program",
    "ComputeBudget111111111111111111111111111111": "Compute budget program",
    "1nc1nerator11111111111111111111111111111111": "Incinerator",
    FOMO_GAS_SPONSOR: "FOMO gas sponsor",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter aggregator",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpools",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun program",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap AMM",
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "Binance",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": "Binance",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Binance",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "Coinbase",
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE": "Coinbase",
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "Kraken",
}

EVM_LABELS = {
    "0x0000000000000000000000000000000000000000": "Null address",
    "0x000000000000000000000000000000000000dead": "Burn address",
    "0x000000000022d473030f116ddee9f6b43ac78ba3": "Permit2",
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2 router",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap V3 router",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "Uniswap universal router",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch router",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x exchange proxy",
    "0x10ed43c718714eb63d5aa57b78b54704e256024e": "PancakeSwap router",
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance",
    "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be": "Binance",
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase",
    "0x503828976d22510aad0201ac7ec88293211d23da": "Coinbase",
    "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740": "Coinbase",
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX",
    "0x2910543af39aba0cd09dbb2d50200b3e800a63d2": "Kraken",
}


def _load_extra_labels() -> dict[str, str]:
    """Operator-supplied labels, merged over the built-in list.

    A JSON object of `{"address": "label"}`. A missing or malformed file is not
    an error -- the built-in list still applies.
    """
    path = os.getenv("CONNECTED_LABELS_FILE", "").strip()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("could not read CONNECTED_LABELS_FILE %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if key}


def known_label(address: str, extra: dict[str, str] | None = None) -> str | None:
    """The infrastructure label for an address, or None if it looks personal."""
    clean = (address or "").strip()
    if not clean:
        return "Empty address"
    for table in (extra or {}, SOLANA_LABELS):
        if clean in table:
            return table[clean]
    lowered = clean.lower()
    for table in (extra or {}, EVM_LABELS):
        if lowered in table:
            return table[lowered]
    return None


# ------------------------------------------------------------------ model --


@dataclass(frozen=True)
class Transfer:
    """One value movement between the known wallet and somebody else."""

    counterparty: str
    outgoing: bool          # True: known wallet -> counterparty
    amount: float           # token or native units
    symbol: str
    usd: float | None       # None when the asset cannot be priced honestly
    timestamp: int
    reference: str          # signature or transaction hash


@dataclass
class Relationship:
    """Everything one counterparty did with the known wallet."""

    address: str
    chain: str
    known_wallet: str
    sent_count: int = 0          # known wallet -> counterparty
    received_count: int = 0      # counterparty -> known wallet
    sent_usd: float = 0.0
    received_usd: float = 0.0
    unpriced: int = 0
    first_seen: int = 0
    last_seen: int = 0
    days: set[int] = field(default_factory=set)
    references: list[str] = field(default_factory=list)
    first_direction: str = ""    # which way the earliest transfer went
    label: str | None = None
    is_contract: bool = False
    identity: str | None = None  # FOMO/Pump handle, when the cache knows one

    @property
    def transfers(self) -> int:
        return self.sent_count + self.received_count

    @property
    def total_usd(self) -> float:
        return self.sent_usd + self.received_usd

    @property
    def reciprocal(self) -> bool:
        return self.sent_count > 0 and self.received_count > 0

    @property
    def span_days(self) -> float:
        if not self.first_seen or not self.last_seen:
            return 0.0
        return max(0.0, (self.last_seen - self.first_seen) / 86400)

    @property
    def active_days(self) -> int:
        return len(self.days)


@dataclass(frozen=True)
class Association:
    """A scored relationship, ready to render."""

    relationship: Relationship
    score: int
    band: str
    reasons: tuple[str, ...]
    signals: tuple[str, ...]

    @property
    def address(self) -> str:
        return self.relationship.address

    @property
    def chain(self) -> str:
        return self.relationship.chain


# ------------------------------------------------------------ aggregation --


def _day(timestamp: int) -> int:
    return int(timestamp) // 86400


def build_relationships(
    transfers: Iterable[Transfer], known_wallet: str, chain: str,
    *, extra_labels: dict[str, str] | None = None,
) -> list[Relationship]:
    """Group transfers by counterparty, dropping the obvious infrastructure.

    Labelled addresses are dropped here rather than scored and hidden later,
    so an exchange can never occupy a slot a real candidate wanted.
    """
    grouped: dict[str, Relationship] = {}
    self_addresses = {known_wallet, known_wallet.lower()}
    for transfer in transfers:
        address = (transfer.counterparty or "").strip()
        if not address or address in self_addresses:
            continue
        if known_label(address, extra_labels):
            continue
        record = grouped.get(address)
        if record is None:
            record = Relationship(
                address=address, chain=chain, known_wallet=known_wallet
            )
            grouped[address] = record
        if transfer.outgoing:
            record.sent_count += 1
            record.sent_usd += transfer.usd or 0.0
        else:
            record.received_count += 1
            record.received_usd += transfer.usd or 0.0
        if transfer.usd is None:
            record.unpriced += 1
        if transfer.timestamp:
            if not record.first_seen or transfer.timestamp < record.first_seen:
                record.first_seen = transfer.timestamp
                record.first_direction = "out" if transfer.outgoing else "in"
            record.last_seen = max(record.last_seen, transfer.timestamp)
            record.days.add(_day(transfer.timestamp))
        if transfer.reference and len(record.references) < 25:
            if transfer.reference not in record.references:
                record.references.append(transfer.reference)
    return list(grouped.values())


# ----------------------------------------------------------------- scoring --


def _points(value: float, ladder: Sequence[tuple[float, int]]) -> int:
    """The highest rung whose threshold `value` reaches."""
    awarded = 0
    for threshold, points in ladder:
        if value >= threshold:
            awarded = points
    return awarded


def score_relationship(record: Relationship) -> Association:
    """Turn one relationship into a score, a band and the reasons behind it.

    Every rung is deliberately hard to reach by accident. The band is then
    capped by the number of *independent* signals, so a single loud one never
    reads as strongly as several quiet ones agreeing.
    """
    signals: list[str] = []
    reasons: list[str] = []
    score = 0

    repeat = _points(record.transfers, ((3, 12), (5, 18), (10, 25), (20, 30)))
    if repeat:
        score += repeat
        signals.append("repetition")
        reasons.append(
            f"{record.transfers} direct transfers between the two wallets"
        )

    if record.reciprocal and min(record.sent_count, record.received_count) >= 2:
        score += 18
        signals.append("reciprocity")
        reasons.append(
            f"value moved both ways ({record.sent_count} out, "
            f"{record.received_count} in)"
        )

    longevity = _points(record.span_days, ((7, 6), (30, 12), (90, 18)))
    if longevity:
        score += longevity
        signals.append("longevity")
        reasons.append(
            f"the relationship spans {record.span_days:.0f} days"
        )

    spread = _points(record.active_days, ((3, 6), (8, 10), (20, 14)))
    if spread:
        score += spread
        signals.append("spread")
        reasons.append(f"transfers on {record.active_days} separate dates")

    value = _points(record.total_usd, ((1_000, 6), (10_000, 12), (100_000, 20)))
    if value:
        score += value
        signals.append("value")
        reasons.append(f"${record.total_usd:,.0f} moved in total")

    # A wallet whose first sight of the chain is money arriving from the known
    # wallet was, in all likelihood, opened by whoever controls it.
    if (record.first_direction == "out" and record.sent_count >= 2
            and record.received_count >= 1):
        score += 10
        signals.append("funding")
        reasons.append(
            "the known wallet funded it first, and funds later came back"
        )
    elif record.first_direction == "out" and record.sent_count >= 3:
        score += 6
        signals.append("funding")
        reasons.append("the known wallet has repeatedly funded it")

    if record.identity:
        score += 8
        signals.append("identity")
        reasons.append(f"the wallet cache already knows it as @{record.identity}")

    if record.is_contract and not record.identity:
        # FOMO's own wallets are contracts, so this is a caution rather than a
        # disqualification -- but an unrecognised contract is far more likely
        # to be a protocol than a person.
        score -= 15
        reasons.append("contract code, and no known identity — treat with care")

    if record.unpriced and not record.total_usd:
        reasons.append(
            f"{record.unpriced} transfers carried assets that could not be "
            "priced, so no USD total is claimed"
        )

    score = max(0, min(100, score))
    band = ""
    for floor, name in BANDS:
        if score >= floor and len(signals) >= BAND_SIGNAL_FLOOR[name]:
            band = name
            break
    if not band:
        # Enough points, not enough independent evidence: say so rather than
        # promoting it.
        for floor, name in BANDS:
            if score >= floor:
                reasons.append(
                    f"only {len(signals)} independent signal"
                    f"{'s' if len(signals) != 1 else ''} — held below {name}"
                )
                break
    return Association(
        relationship=record,
        score=score,
        band=band,
        reasons=tuple(reasons),
        signals=tuple(signals),
    )


def rank_associations(
    records: Iterable[Relationship], *, min_score: int = DEFAULT_MIN_SCORE
) -> list[Association]:
    """Score, filter and order. Nothing under `MIN_TRANSFERS` is ever scored."""
    scored = [
        score_relationship(record) for record in records
        if record.transfers >= MIN_TRANSFERS
    ]
    kept = [item for item in scored if item.band and item.score >= min_score]
    kept.sort(key=lambda item: (item.score, item.relationship.transfers), reverse=True)
    return kept


def link_cross_chain(associations: Sequence[Association]) -> list[Association]:
    """Promote a candidate whose identity is connected on more than one chain.

    This is the only cross-chain claim made anywhere in here, and it is not an
    inference from transaction patterns: it holds when the *same* verified
    identity turns up as a candidate on two chains, which the wallet cache
    states rather than this module guessing.
    """
    chains: dict[str, set[str]] = {}
    for item in associations:
        if item.relationship.identity:
            chains.setdefault(item.relationship.identity.lower(), set()).add(item.chain)

    out: list[Association] = []
    for item in associations:
        identity = (item.relationship.identity or "").lower()
        seen = chains.get(identity, set())
        if identity and len(seen) > 1:
            score = min(100, item.score + 8)
            signals = tuple(dict.fromkeys(item.signals + ("cross-chain",)))
            reasons = item.reasons + (
                f"the same identity is connected on {', '.join(sorted(seen))}",
            )
            band = item.band
            for floor, name in BANDS:
                if score >= floor and len(signals) >= BAND_SIGNAL_FLOOR[name]:
                    band = name
                    break
            out.append(Association(item.relationship, score, band, reasons, signals))
        else:
            out.append(item)
    out.sort(key=lambda item: (item.score, item.relationship.transfers), reverse=True)
    return out


def explorer_url(chain: str, reference: str) -> str | None:
    """Where to read the transaction that backs a relationship."""
    bases = {
        "solana": "https://solscan.io/tx/",
        "ethereum": "https://etherscan.io/tx/",
        "bsc": "https://bscscan.com/tx/",
        "base": "https://basescan.org/tx/",
        "robinhood": "https://robinhoodchain.blockscout.com/tx/",
    }
    base = bases.get(chain.lower())
    return f"{base}{reference}" if base and reference else None


def address_url(chain: str, address: str) -> str | None:
    bases = {
        "solana": "https://solscan.io/account/",
        "ethereum": "https://etherscan.io/address/",
        "bsc": "https://bscscan.com/address/",
        "base": "https://basescan.org/address/",
        "robinhood": "https://robinhoodchain.blockscout.com/address/",
    }
    base = bases.get(chain.lower())
    return f"{base}{address}" if base and address else None


def fmt_day(timestamp: int | None) -> str:
    if not timestamp:
        return "—"
    return f"{datetime.fromtimestamp(timestamp, tz=timezone.utc):%d %b %Y}"


# ----------------------------------------------------------------- report --


@dataclass(frozen=True)
class ConnectedReport:
    wallets: tuple[tuple[str, str], ...]      # (address, chain)
    associations: tuple[Association, ...]
    weaker: tuple[Association, ...]           # scored, below the surfacing bar
    transactions: int
    warnings: tuple[str, ...] = ()
    generated_at: int = 0
    cached: bool = False


def _relationship_payload(record: Relationship) -> dict[str, Any]:
    return {
        "address": record.address, "chain": record.chain,
        "knownWallet": record.known_wallet,
        "sentCount": record.sent_count, "receivedCount": record.received_count,
        "sentUsd": record.sent_usd, "receivedUsd": record.received_usd,
        "unpriced": record.unpriced, "firstSeen": record.first_seen,
        "lastSeen": record.last_seen, "days": sorted(record.days),
        "references": record.references[:25],
        "firstDirection": record.first_direction,
        "label": record.label, "isContract": record.is_contract,
        "identity": record.identity,
    }


def _relationship_from_payload(raw: dict[str, Any]) -> Relationship:
    record = Relationship(
        address=str(raw.get("address") or ""),
        chain=str(raw.get("chain") or ""),
        known_wallet=str(raw.get("knownWallet") or ""),
        sent_count=int(raw.get("sentCount") or 0),
        received_count=int(raw.get("receivedCount") or 0),
        sent_usd=float(raw.get("sentUsd") or 0.0),
        received_usd=float(raw.get("receivedUsd") or 0.0),
        unpriced=int(raw.get("unpriced") or 0),
        first_seen=int(raw.get("firstSeen") or 0),
        last_seen=int(raw.get("lastSeen") or 0),
        first_direction=str(raw.get("firstDirection") or ""),
        label=raw.get("label") or None,
        is_contract=bool(raw.get("isContract")),
        identity=raw.get("identity") or None,
    )
    record.days = {int(day) for day in raw.get("days") or []}
    record.references = [str(ref) for ref in raw.get("references") or []]
    return record


def report_payload(report: ConnectedReport) -> dict[str, Any]:
    def association(item: Association) -> dict[str, Any]:
        return {
            "score": item.score, "band": item.band,
            "reasons": list(item.reasons), "signals": list(item.signals),
            "relationship": _relationship_payload(item.relationship),
        }

    return {
        "wallets": [list(pair) for pair in report.wallets],
        "associations": [association(item) for item in report.associations],
        "weaker": [association(item) for item in report.weaker],
        "transactions": report.transactions,
        "warnings": list(report.warnings),
        "generatedAt": report.generated_at,
    }


def report_from_payload(raw: dict[str, Any]) -> ConnectedReport:
    def association(row: dict[str, Any]) -> Association:
        return Association(
            relationship=_relationship_from_payload(row.get("relationship") or {}),
            score=int(row.get("score") or 0),
            band=str(row.get("band") or ""),
            reasons=tuple(str(item) for item in row.get("reasons") or []),
            signals=tuple(str(item) for item in row.get("signals") or []),
        )

    return ConnectedReport(
        wallets=tuple(
            (str(pair[0]), str(pair[1]))
            for pair in raw.get("wallets") or [] if len(pair) == 2
        ),
        associations=tuple(
            association(row) for row in raw.get("associations") or []
            if isinstance(row, dict)
        ),
        weaker=tuple(
            association(row) for row in raw.get("weaker") or []
            if isinstance(row, dict)
        ),
        transactions=int(raw.get("transactions") or 0),
        warnings=tuple(str(item) for item in raw.get("warnings") or []),
        generated_at=int(raw.get("generatedAt") or 0),
        cached=True,
    )


# --------------------------------------------------------------- analyzer --

HELIUS_TX_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens"
LAMPORTS = 1_000_000_000
SOLANA_SYSTEM_PROGRAM = "11111111111111111111111111111111"
SOLANA_STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}
# Wrapped natives, used only to price the native leg of a transfer.
NATIVE_PRICE_TOKENS = {
    "solana": ("solana", "So11111111111111111111111111111111111111112"),
    "ethereum": ("ethereum", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
    "base": ("base", "0x4200000000000000000000000000000000000006"),
    "bsc": ("bsc", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
}
EVM_CHAIN_ENV = {
    "ethereum": ("ETH_RPC", "ETH_RPC_FALLBACKS"),
    "bsc": ("BSC_RPC", "BSC_RPC_FALLBACKS"),
    "base": ("BASE_RPC", "BASE_RPC_FALLBACKS"),
    "robinhood": ("ROBINHOOD_RPC", "ROBINHOOD_RPC_FALLBACKS"),
}


class ConnectedWalletAnalyzer:
    """Find wallets with strong on-chain evidence of a shared cluster.

    Everything expensive is bounded and ordered so the cheap disqualifications
    run first: labelled addresses never reach a request, weakly scored
    candidates never reach an account-type check, and only the survivors of
    that cost a degree probe.
    """

    def __init__(
        self,
        http: Any,
        solana_rpcs: Sequence[str] | None = None,
        evm_rpcs: dict[str, list[str]] | None = None,
        *,
        cache_path: str = CACHE_FILE,
        identify: Any = None,
    ) -> None:
        from rpc_config import env_rpc_urls, normalize_rpc_urls
        from wallet_profile_cache import ProfileCache

        self.http = http
        self.solana_rpcs = normalize_rpc_urls(solana_rpcs or [])
        self.evm_rpcs = evm_rpcs if evm_rpcs is not None else {
            chain: env_rpc_urls(primary, fallback)
            for chain, (primary, fallback) in EVM_CHAIN_ENV.items()
        }
        self.identify = identify
        self.extra_labels = _load_extra_labels()
        self.cache = ProfileCache(
            cache_path, ttl=CACHE_TTL, negative_ttl=CACHE_TTL,
            normalize=lambda value: (value or "").strip().lower(),
        )
        self._prices: dict[str, tuple[float, float | None]] = {}

    # ------------------------------------------------------------ public --

    async def analyse(
        self,
        wallets: Sequence[tuple[str, str]],
        *,
        min_score: int = DEFAULT_MIN_SCORE,
        fresh: bool = False,
    ) -> ConnectedReport:
        """Analyse every known wallet and return one ranked report.

        A run is cached whole, keyed by the wallet set and the bar it was run
        at, because the expensive half is the history paging and that does not
        become cheaper for the second person to ask.
        """
        pairs = tuple(
            (address.strip(), chain.strip().lower())
            for address, chain in wallets if address and chain
        )
        if not pairs:
            return ConnectedReport((), (), (), 0, ("No wallet to analyse.",))

        key = "|".join(sorted(f"{chain}:{address}" for address, chain in pairs))
        key += f"@{min_score}"
        if not fresh:
            entry = self.cache.get(key)
            if entry is not None and entry.found:
                log.info("connected: cache hit for %s", key)
                return report_from_payload(entry.payload)

        async with self.cache.locks(key):
            if not fresh:
                entry = self.cache.get(key)
                if entry is not None and entry.found:
                    return report_from_payload(entry.payload)
            report = await self._analyse(pairs, min_score)
        try:
            self.cache.put(key, report_payload(report), source="onchain")
        except Exception as exc:  # a cache that raises must not lose the answer
            log.debug("could not cache the connected report: %s", exc)
        return report

    # ----------------------------------------------------------- internal --

    async def _analyse(
        self, pairs: tuple[tuple[str, str], ...], min_score: int
    ) -> ConnectedReport:
        warnings: list[str] = []
        records: list[Relationship] = []
        transactions = 0

        for address, chain in pairs:
            try:
                if chain == "solana":
                    transfers, sampled, note = await self._solana_transfers(address)
                else:
                    transfers, sampled, note = await self._evm_transfers(
                        address, chain
                    )
            except Exception as exc:
                log.warning("connected: %s history failed for %s: %s",
                            chain, address, exc)
                warnings.append(f"{chain}: history unavailable ({str(exc)[:80]})")
                continue
            transactions += sampled
            if note:
                warnings.append(note)
            found = build_relationships(
                transfers, address, chain, extra_labels=self.extra_labels
            )
            log.info(
                "connected: %s %s -> %d transfer(s), %d counterparties worth scoring",
                chain, address[:10], len(transfers),
                sum(1 for item in found if item.transfers >= MIN_TRANSFERS),
            )
            records.extend(found)

        for record in records:
            record.identity = self._identity(record.address)

        # Score once to order the field, verify the top of it, then score again
        # with what verification found. Verification is what costs requests, so
        # it never runs on a candidate the first pass already refused.
        shortlist = [
            item.relationship for item in rank_associations(
                records, min_score=SCORE_POSSIBLE
            )
        ][:VERIFY_CANDIDATES]
        if shortlist:
            await self._verify(shortlist, warnings)

        survivors = [record for record in shortlist if not record.label]
        ranked = rank_associations(survivors, min_score=SCORE_POSSIBLE)
        await self._drop_high_degree(ranked[:DEGREE_CANDIDATES], warnings)

        ranked = link_cross_chain(
            rank_associations(
                [item.relationship for item in ranked
                 if not item.relationship.label],
                min_score=SCORE_POSSIBLE,
            )
        )
        strong = tuple(item for item in ranked if item.score >= min_score)
        weaker = tuple(item for item in ranked if item.score < min_score)
        return ConnectedReport(
            wallets=pairs,
            associations=strong,
            weaker=weaker,
            transactions=transactions,
            warnings=tuple(dict.fromkeys(warnings)),
            generated_at=int(time.time()),
        )

    def _identity(self, address: str) -> str | None:
        if not self.identify:
            return None
        try:
            return self.identify(address)
        except Exception as exc:
            log.debug("identity lookup failed for %s: %s", address, exc)
            return None

    # ------------------------------------------------------------- prices --

    async def _native_price(self, chain: str) -> float | None:
        """Current USD price of a chain's native asset, cached for 5 minutes.

        Only natives and stablecoins are priced. Every other asset is counted
        as a transfer and left out of the USD total, which is why the card can
        say "3 transfers carried assets that could not be priced" rather than
        inventing a number for them.
        """
        reference = NATIVE_PRICE_TOKENS.get(chain)
        if not reference:
            return None
        hit = self._prices.get(chain)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        price: float | None = None
        try:
            response = await self.http.get(
                f"{DEXSCREENER_TOKEN_URL}/{reference[1]}",
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if int(getattr(response, "status_code", 200)) < 400:
                pairs = (response.json() or {}).get("pairs") or []
                best = 0.0
                for pair in pairs:
                    if not isinstance(pair, dict):
                        continue
                    liquidity = pair.get("liquidity") or {}
                    weight = float(liquidity.get("usd") or 0) \
                        if isinstance(liquidity, dict) else 0.0
                    value = pair.get("priceUsd")
                    if value and weight >= best:
                        best, price = weight, float(value)
        except Exception as exc:
            log.debug("native price lookup failed for %s: %s", chain, exc)
        self._prices[chain] = (time.monotonic() + 300, price)
        return price

    # ------------------------------------------------------------- solana --

    def _helius_key(self) -> str | None:
        from urllib.parse import parse_qs, urlsplit

        for url in self.solana_rpcs:
            if "helius" not in url.lower():
                continue
            key = parse_qs(urlsplit(url).query).get("api-key", [""])[0].strip()
            if key:
                return key
        return None

    async def _helius_history(
        self, address: str, key: str, pages: int
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        before = ""
        for _ in range(pages):
            params: dict[str, Any] = {"api-key": key, "limit": HELIUS_TX_LIMIT}
            if before:
                params["before"] = before
            response = await self.http.get(
                HELIUS_TX_URL.format(address=address),
                params=params,
                headers={"Accept": "application/json"},
                timeout=30,
            )
            if int(getattr(response, "status_code", 200)) >= 400:
                raise RuntimeError(f"HTTP {getattr(response, 'status_code', '?')}")
            payload = response.json()
            if not isinstance(payload, list) or not payload:
                break
            entries.extend(row for row in payload if isinstance(row, dict))
            last = payload[-1] if isinstance(payload[-1], dict) else {}
            before = str(last.get("signature") or "")
            if len(payload) < HELIUS_TX_LIMIT or not before:
                break
        return entries

    async def _solana_transfers(
        self, wallet: str
    ) -> tuple[list[Transfer], int, str]:
        key = self._helius_key()
        if not key:
            return [], 0, (
                "Solana: connection analysis needs a Helius endpoint in "
                "SOLANA_RPC — no Solana history was read."
            )
        entries = await self._helius_history(wallet, key, SOLANA_PAGES)
        price = await self._native_price("solana")
        transfers = solana_transfers_from_history(entries, wallet, sol_price=price)
        note = ""
        if len(entries) >= SOLANA_PAGES * HELIUS_TX_LIMIT:
            note = (
                f"Solana: only the most recent {len(entries)} transactions were "
                "read, so older relationships may be missing."
            )
        return transfers, len(entries), note

    # ---------------------------------------------------------------- evm --

    async def _alchemy(self, chain: str, method: str, params: list[Any]) -> Any:
        last: Exception | None = None
        for url in self.evm_rpcs.get(chain, []):
            if method.startswith("alchemy_") and "alchemy.com" not in url.lower():
                continue
            try:
                response = await self.http.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params},
                    timeout=30,
                )
                if int(getattr(response, "status_code", 200)) >= 400:
                    raise RuntimeError(
                        f"HTTP {getattr(response, 'status_code', '?')}"
                    )
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("error"):
                    raise RuntimeError(str((payload or {}).get("error") or "bad reply"))
                return payload.get("result")
            except Exception as exc:
                last = exc
                log.debug("%s %s failed on %s: %s", chain, method, url[:32], exc)
        if last is not None:
            raise last
        return None

    async def _asset_transfers(
        self, chain: str, *, page_size: str = EVM_PAGE_SIZE,
        pages: int = EVM_PAGES, **direction: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page_key: str | None = None
        for _ in range(pages):
            params: dict[str, Any] = {
                "fromBlock": "0x0", "toBlock": "latest",
                "category": ["external", "erc20"],
                "withMetadata": True, "excludeZeroValue": True,
                "maxCount": page_size, "order": "desc", **direction,
            }
            if page_key:
                params["pageKey"] = page_key
            result = await self._alchemy(chain, "alchemy_getAssetTransfers", [params])
            if not isinstance(result, dict):
                break
            rows.extend(
                row for row in result.get("transfers") or [] if isinstance(row, dict)
            )
            page_key = str(result.get("pageKey") or "") or None
            if not page_key:
                break
        return rows

    async def _evm_transfers(
        self, wallet: str, chain: str
    ) -> tuple[list[Transfer], int, str]:
        if not self.evm_rpcs.get(chain):
            return [], 0, f"{chain}: no RPC endpoint configured."
        outgoing, incoming = await asyncio.gather(
            self._asset_transfers(chain, fromAddress=wallet),
            self._asset_transfers(chain, toAddress=wallet),
        )
        price = await self._native_price(chain)
        transfers = evm_transfers_from_rows(
            outgoing, incoming, wallet, chain, native_price=price
        )
        sampled = len({str(row.get("hash") or "") for row in outgoing + incoming})
        note = ""
        if len(outgoing) >= EVM_PAGES * 500 or len(incoming) >= EVM_PAGES * 500:
            note = (
                f"{chain}: the transfer history was truncated, so older "
                "relationships may be missing."
            )
        return transfers, sampled, note

    # ------------------------------------------------------ verification --

    async def _verify(
        self, candidates: Sequence[Relationship], warnings: list[str]
    ) -> None:
        """Label anything that is not a personal wallet.

        Solana is decisive: a real wallet is owned by the system program and is
        not executable, so a pool, a token account, a vault or a PDA is ruled
        out for what it *is* rather than for how it behaves. EVM cannot use the
        same test -- FOMO's own wallets are ERC-4337 contracts -- so code there
        is recorded and left to the score, which treats an unrecognised
        contract as a caution.
        """
        by_chain: dict[str, list[Relationship]] = {}
        for record in candidates:
            by_chain.setdefault(record.chain, []).append(record)

        for chain, records in by_chain.items():
            if chain == "solana":
                try:
                    await self._verify_solana(records)
                except Exception as exc:
                    log.warning("connected: Solana account check failed: %s", exc)
                    warnings.append(
                        "Solana: account types could not be checked, so "
                        "program accounts may not have been excluded."
                    )
                continue
            try:
                await self._verify_evm(chain, records)
            except Exception as exc:
                log.warning("connected: %s code check failed: %s", chain, exc)
                warnings.append(
                    f"{chain}: contract code could not be checked."
                )

    async def _verify_solana(self, records: Sequence[Relationship]) -> None:
        addresses = [record.address for record in records]
        if not addresses or not self.solana_rpcs:
            return
        result = None
        for url in self.solana_rpcs:
            try:
                response = await self.http.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                          "params": [addresses,
                                     {"encoding": "jsonParsed",
                                      "commitment": "confirmed"}]},
                    timeout=30,
                )
                if int(getattr(response, "status_code", 200)) >= 400:
                    continue
                payload = response.json()
                if isinstance(payload, dict) and not payload.get("error"):
                    result = (payload.get("result") or {}).get("value")
                    break
            except Exception as exc:
                log.debug("getMultipleAccounts failed: %s", exc)
        if not isinstance(result, list):
            return
        for record, account in zip(records, result):
            if account is None:
                continue  # never funded on its own: still a plain address
            if not isinstance(account, dict):
                continue
            owner = str(account.get("owner") or "")
            if account.get("executable"):
                record.label = "Program"
            elif owner and owner != SOLANA_SYSTEM_PROGRAM:
                record.label = f"Program-owned account ({owner[:8]}…)"

    async def _verify_evm(self, chain: str, records: Sequence[Relationship]) -> None:
        for record in records:
            code = await self._alchemy(chain, "eth_getCode", [record.address, "latest"])
            record.is_contract = bool(
                isinstance(code, str) and code not in ("", "0x", "0x0")
            )

    async def _drop_high_degree(
        self, candidates: Sequence[Association], warnings: list[str]
    ) -> None:
        """Label whatever deals with far too many people to be a second wallet.

        This is the test that catches the infrastructure no list knows about:
        an unlabelled deposit address, a new router, a market maker. It costs a
        request per candidate, which is why it runs last and on a handful.
        """
        for item in candidates:
            record = item.relationship
            if record.label:
                continue
            try:
                degree = await self._degree(record.address, record.chain)
            except Exception as exc:
                log.debug("degree probe failed for %s: %s", record.address, exc)
                warnings.append(
                    f"{record.chain}: one candidate's counterparty count could "
                    "not be checked."
                )
                continue
            if degree is None:
                continue
            if degree >= HIGH_DEGREE_COUNTERPARTIES:
                record.label = f"High-degree address ({degree}+ counterparties)"
                log.info(
                    "connected: excluded %s on %s — %d counterparties in one page",
                    record.address[:10], record.chain, degree,
                )

    async def _degree(self, address: str, chain: str) -> int | None:
        """Distinct counterparties in one bounded page of an address's history."""
        if chain == "solana":
            key = self._helius_key()
            if not key:
                return None
            entries = await self._helius_history(address, key, 1)
            transfers = solana_transfers_from_history(entries, address)
            return len({transfer.counterparty for transfer in transfers})
        outgoing, incoming = await asyncio.gather(
            self._asset_transfers(
                chain, page_size=hex(DEGREE_SAMPLE), pages=1, fromAddress=address
            ),
            self._asset_transfers(
                chain, page_size=hex(DEGREE_SAMPLE), pages=1, toAddress=address
            ),
        )
        transfers = evm_transfers_from_rows(outgoing, incoming, address, chain)
        return len({transfer.counterparty for transfer in transfers})


# ------------------------------------------------------------- parsing -----


def solana_transfers_from_history(
    entries: Iterable[Any], wallet: str, *, sol_price: float | None = None,
) -> list[Transfer]:
    """Transfers touching `wallet` out of Helius's parsed transaction history.

    Both `nativeTransfers` and `tokenTransfers` name owner accounts, so the
    counterparty is read directly rather than resolved from a token account.
    Only SOL and the two stablecoin mints carry a USD figure; everything else
    is counted and left unpriced.
    """
    out: list[Transfer] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        reference = str(entry.get("signature") or "")
        timestamp = int(entry.get("timestamp") or 0)
        for row in entry.get("nativeTransfers") or []:
            if not isinstance(row, dict):
                continue
            sender = str(row.get("fromUserAccount") or "")
            recipient = str(row.get("toUserAccount") or "")
            try:
                amount = float(row.get("amount") or 0) / LAMPORTS
            except (TypeError, ValueError):
                continue
            if amount <= 0 or wallet not in (sender, recipient):
                continue
            outgoing = sender == wallet
            counterparty = recipient if outgoing else sender
            out.append(Transfer(
                counterparty=counterparty, outgoing=outgoing, amount=amount,
                symbol="SOL",
                usd=amount * sol_price if sol_price else None,
                timestamp=timestamp, reference=reference,
            ))
        for row in entry.get("tokenTransfers") or []:
            if not isinstance(row, dict):
                continue
            sender = str(row.get("fromUserAccount") or "")
            recipient = str(row.get("toUserAccount") or "")
            try:
                amount = float(row.get("tokenAmount") or 0)
            except (TypeError, ValueError):
                continue
            if amount <= 0 or wallet not in (sender, recipient):
                continue
            mint = str(row.get("mint") or "")
            symbol = SOLANA_STABLE_MINTS.get(mint, "")
            outgoing = sender == wallet
            out.append(Transfer(
                counterparty=recipient if outgoing else sender,
                outgoing=outgoing, amount=amount,
                symbol=symbol or mint[:6],
                usd=amount if symbol else None,
                timestamp=timestamp, reference=reference,
            ))
    return out


def evm_transfers_from_rows(
    outgoing: Iterable[Any], incoming: Iterable[Any], wallet: str, chain: str,
    *, native_price: float | None = None,
) -> list[Transfer]:
    """Transfers from the two `alchemy_getAssetTransfers` directions.

    The two calls can return the same row when a wallet sends to itself, so
    rows are deduplicated on (hash, from, to, value) rather than trusted.
    """
    native = NATIVE_SYMBOLS.get(chain, "")
    lowered = wallet.lower()
    seen: set[tuple[str, str, str, str]] = set()
    out: list[Transfer] = []
    for rows, is_outgoing in ((outgoing, True), (incoming, False)):
        for row in rows:
            if not isinstance(row, dict):
                continue
            sender = str(row.get("from") or "").lower()
            recipient = str(row.get("to") or "").lower()
            reference = str(row.get("hash") or "")
            try:
                amount = float(row.get("value") or 0)
            except (TypeError, ValueError):
                continue
            if amount <= 0 or lowered not in (sender, recipient):
                continue
            fingerprint = (reference, sender, recipient, repr(row.get("value")))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            symbol = str(row.get("asset") or "").upper()
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            timestamp = _iso_epoch(metadata.get("blockTimestamp"))
            usd: float | None = None
            if symbol in STABLE_SYMBOLS:
                usd = amount
            elif native and symbol == native and native_price:
                usd = amount * native_price
            out.append(Transfer(
                counterparty=recipient if is_outgoing else sender,
                outgoing=sender == lowered,
                amount=amount, symbol=symbol or "?", usd=usd,
                timestamp=timestamp, reference=reference,
            ))
    return out


def _iso_epoch(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())
