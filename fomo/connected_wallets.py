"""
connected_wallets.py -- the wallet that funded a trader, and the wallets they
actually move money to and from.

What counts as a connection
---------------------------
One thing only: a **direct transfer of real size** between two addresses.

* on Solana, 1+ SOL or 50+ USDC/USDT
* on EVM, $200+ of the chain's native coin, or 50+ of a stablecoin

Everything else is thrown away before it is ever scored, counted or shown. A
swap is not a connection -- when a wallet buys a token on Jupiter, Raydium or
Meteora the chain records value leaving that wallet and arriving at a pool, and
earlier versions of this module read those legs as a relationship with the
pool. They are not relationships with anybody. Neither are liquidity deposits,
NFT sales, staking, or the dust a wallet sprays at a hundred addresses.

So on Solana only Helius transactions typed `TRANSFER`, carrying no swap event
and no DEX source, are read at all. On EVM there is no such type, so the
defences are the ones that were always here -- known routers, contract code,
counterparty degree -- plus the $200 floor, which is what most swap legs of a
memecoin trade fall under.

The funding wallet
------------------
Separate from the list, and never value-gated: a wallet is usually opened with
a fraction of a SOL, and whoever sent it is the single most interesting address
in the whole report. Finding it means reading a wallet's *oldest* transaction,
which is the one thing Solana RPC will not sort for you:

* with a `SOLSCAN_API_KEY`, Solscan Pro answers it in one request --
  `/account/transfer?flow=in&sort_by=block_time&sort_order=asc`, which is what
  the "oldest first" toggle on solscan.io does
* without one, Helius history is paged backwards until it runs out. If it runs
  out, the oldest page holds the wallet's first transaction and the answer is
  exact; if the page budget runs out first, the report says the funder is
  unknown rather than naming the oldest thing it happened to see
* on EVM `alchemy_getAssetTransfers` takes `order: "asc"` directly, so the
  first incoming transfer is one request away

No scores
---------
There are no confidence bands here any more. The bar is the transfer rule
above: a wallet either moved real money with this one or it did not, and the
card shows how much, how often and when, so the reader can judge it. What is
still excluded structurally is infrastructure -- exchanges, bridges, routers,
programs, program-owned accounts, and any address dealing with more
counterparties in one page than a person's second wallet ever would.

Data sources are the ones the project already pays for: Helius parsed
transaction history on Solana, `alchemy_getAssetTransfers` on EVM, and
optionally Solscan Pro for the funding lookup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from solscan_api import solscan_get

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

# ------------------------------------------------------------ what counts --
# The whole bar. A transfer under these is noise: gas top-ups, dust, the tail
# of a swap route. A transfer over them is somebody moving money on purpose.
MIN_SOL = _env_float("CONNECTED_MIN_SOL", 1.0)
MIN_STABLE = _env_float("CONNECTED_MIN_STABLE", 50.0)
# EVM has no "1 SOL", so the native leg is judged in dollars. 200 is roughly
# what 1 SOL is worth, which keeps the bar reading the same on every chain.
MIN_EVM_USD = _env_float("CONNECTED_MIN_EVM_USD", 200.0)

# How far back the funding lookup will page Helius when Solscan is not
# configured. 20 pages is 2000 transactions; if that does not reach a wallet's
# first transaction, the report says so instead of guessing.
FUNDING_PAGES = _env_int("CONNECTED_FUNDING_PAGES", 20, low=1, high=100)
# A logical path, not a URL -- `solscan_api` decides whether this key reaches
# it under /v2.0 or only under /playground. A full URL here still works and
# skips that resolution.
SOLSCAN_TRANSFER_URL = os.getenv("SOLSCAN_TRANSFER_URL", "account/transfer").strip()

CACHE_TTL = _env_float("CONNECTED_CACHE_TTL", 6 * 3600)
CACHE_FILE = os.getenv("CONNECTED_CACHE_FILE", "connected_cache.json")
# Bumped whenever the shape of a cached report changes, so an old file is
# ignored rather than misread. v1 was the score/band era.
CACHE_SCHEMA = "v2"

# ------------------------------------------------------------- thresholds --

# Helius types a plain send as TRANSFER. A swap is SWAP, a liquidity move is
# ADD_LIQUIDITY / WITHDRAW_LIQUIDITY, an NFT sale is NFT_SALE -- none of them
# are a relationship with the counterparty they name, so only TRANSFER is
# read. `events.swap` and the DEX source list below are belt and braces for a
# router that manages to route through a transfer-typed instruction.
TRANSFER_TYPES = {"TRANSFER"}
SWAP_SOURCES = {
    "JUPITER", "RAYDIUM", "RAYDIUM_CLMM", "ORCA", "METEORA", "PUMP_FUN",
    "PUMP_AMM", "PUMPSWAP", "PHOENIX", "LIFINITY", "SABER", "SERUM",
    "OPENBOOK", "MERCURIAL", "ALDRIN", "CROPPER", "STABBLE", "FLUXBEAM",
    "INVARIANT", "MOONSHOT", "VIRTUALS", "OKX", "DRIFT", "MARINADE", "JITO",
    "MAGIC_EDEN", "TENSOR", "HADESWAP", "SOLANART", "STAKED", "SANCTUM",
    "KAMINO", "MARGINFI", "SOLEND", "BONKSWAP", "DEXLAB", "STEPN", "ONE_DEX",
    "GOOSEFX", "PERPS", "ZETA", "MANGO", "SYMMETRY", "BOOP", "DAOS_FUN",
}

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
class Connection:
    """One counterparty this wallet has moved real money with."""

    relationship: Relationship
    funder: bool = False   # this address is the analysed wallet's first funder

    @property
    def address(self) -> str:
        return self.relationship.address

    @property
    def chain(self) -> str:
        return self.relationship.chain


@dataclass(frozen=True)
class Funding:
    """The first money that ever arrived at an analysed wallet.

    `exact` is the honest half. It is True only when the lookup actually
    reached the wallet's first transaction -- Solscan sorted ascending, an
    ascending Alchemy scan, or Helius history that ran out inside the page
    budget. When it is False there is no funder to report, only a note saying
    how deep the search got.
    """

    wallet: str
    chain: str
    address: str = ""
    amount: float = 0.0
    symbol: str = ""
    usd: float | None = None
    timestamp: int = 0
    reference: str = ""
    identity: str | None = None
    label: str | None = None
    exact: bool = False
    note: str = ""

    @property
    def found(self) -> bool:
        return bool(self.address)


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


# ----------------------------------------------------------------- ranking --


def rank_connections(
    records: Iterable[Relationship], *, funders: Iterable[str] = (),
) -> list[Connection]:
    """Order the counterparties by how much money actually moved.

    There is no filtering left to do here: a relationship only exists at all
    if `solana_transfers_from_history` / `evm_transfers_from_rows` already
    judged its transfers big enough to count. What remains is the order, which
    is value first and transfer count as the tie-break -- one $80,000 transfer
    says more than nine $60 ones, and the card shows both numbers either way.

    A funding wallet is pinned to the top whatever it moved since, because it
    is the address the reader came for.
    """
    funding = {address for address in funders if address}
    lowered = {address.lower() for address in funding}
    items = [
        Connection(
            relationship=record,
            funder=record.address in funding or record.address.lower() in lowered,
        )
        for record in records
    ]
    items.sort(
        key=lambda item: (
            item.funder,
            item.relationship.total_usd,
            item.relationship.transfers,
        ),
        reverse=True,
    )
    return items


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
    funding: tuple[Funding, ...]              # one per analysed wallet, when found
    connections: tuple[Connection, ...]
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


def _funding_payload(item: Funding) -> dict[str, Any]:
    return {
        "wallet": item.wallet, "chain": item.chain, "address": item.address,
        "amount": item.amount, "symbol": item.symbol, "usd": item.usd,
        "timestamp": item.timestamp, "reference": item.reference,
        "identity": item.identity, "label": item.label,
        "exact": item.exact, "note": item.note,
    }


def _funding_from_payload(raw: dict[str, Any]) -> Funding:
    return Funding(
        wallet=str(raw.get("wallet") or ""),
        chain=str(raw.get("chain") or ""),
        address=str(raw.get("address") or ""),
        amount=float(raw.get("amount") or 0.0),
        symbol=str(raw.get("symbol") or ""),
        usd=(None if raw.get("usd") is None else float(raw.get("usd") or 0.0)),
        timestamp=int(raw.get("timestamp") or 0),
        reference=str(raw.get("reference") or ""),
        identity=raw.get("identity") or None,
        label=raw.get("label") or None,
        exact=bool(raw.get("exact")),
        note=str(raw.get("note") or ""),
    )


def report_payload(report: ConnectedReport) -> dict[str, Any]:
    def connection(item: Connection) -> dict[str, Any]:
        return {
            "funder": item.funder,
            "relationship": _relationship_payload(item.relationship),
        }

    return {
        "wallets": [list(pair) for pair in report.wallets],
        "funding": [_funding_payload(item) for item in report.funding],
        "connections": [connection(item) for item in report.connections],
        "transactions": report.transactions,
        "warnings": list(report.warnings),
        "generatedAt": report.generated_at,
    }


def report_from_payload(raw: dict[str, Any]) -> ConnectedReport:
    def connection(row: dict[str, Any]) -> Connection:
        return Connection(
            relationship=_relationship_from_payload(row.get("relationship") or {}),
            funder=bool(row.get("funder")),
        )

    return ConnectedReport(
        wallets=tuple(
            (str(pair[0]), str(pair[1]))
            for pair in raw.get("wallets") or [] if len(pair) == 2
        ),
        funding=tuple(
            _funding_from_payload(row) for row in raw.get("funding") or []
            if isinstance(row, dict)
        ),
        connections=tuple(
            connection(row) for row in raw.get("connections") or []
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
SOLANA_WRAPPED_SOL = "So11111111111111111111111111111111111111112"
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
        # Optional. With it, the funding wallet is one ascending request; without
        # it, Helius history is paged backwards and may not reach far enough.
        self.solscan_key = os.getenv("SOLSCAN_API_KEY", "").strip()
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
        fresh: bool = False,
    ) -> ConnectedReport:
        """Analyse every known wallet and return one report.

        A run is cached whole, keyed by the wallet set, because the expensive
        half is the history paging and that does not become cheaper for the
        second person to ask. `fresh` is what the card's Refresh spends.
        """
        pairs = tuple(
            (address.strip(), chain.strip().lower())
            for address, chain in wallets if address and chain
        )
        if not pairs:
            return ConnectedReport((), (), (), 0, ("No wallet to analyse.",))

        key = CACHE_SCHEMA + "|" + "|".join(
            sorted(f"{chain}:{address}" for address, chain in pairs)
        )
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
            report = await self._analyse(pairs)
        try:
            self.cache.put(key, report_payload(report), source="onchain")
        except Exception as exc:  # a cache that raises must not lose the answer
            log.debug("could not cache the connected report: %s", exc)
        return report

    # ----------------------------------------------------------- internal --

    async def _analyse(
        self, pairs: tuple[tuple[str, str], ...]
    ) -> ConnectedReport:
        warnings: list[str] = []
        records: list[Relationship] = []
        funding: list[Funding] = []
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
                "connected: %s %s -> %d qualifying transfer(s), %d counterparties",
                chain, address[:10], len(transfers), len(found),
            )
            records.extend(found)

            # The funder is looked for separately and is never value-gated: a
            # wallet is usually opened with a fraction of a coin.
            try:
                first = await self._funder(address, chain)
            except Exception as exc:
                log.warning("connected: %s funding lookup failed for %s: %s",
                            chain, address, exc)
                first = Funding(
                    wallet=address, chain=chain,
                    note=f"{chain}: the funding wallet could not be read "
                         f"({str(exc)[:60]}).",
                )
            if first.note:
                warnings.append(first.note)
            if first.found:
                funding.append(first)

        for record in records:
            record.identity = self._identity(record.address)

        # Order by value, verify the top of the field, then re-order with what
        # verification found. Verification is what costs requests, so it never
        # runs on a candidate the transfer rule already refused.
        shortlist = [
            item.relationship for item in rank_connections(records)
        ][:VERIFY_CANDIDATES]
        if shortlist:
            await self._verify(shortlist, warnings)

        survivors = [record for record in shortlist if not record.label]
        ranked = rank_connections(survivors)
        await self._drop_high_degree(ranked[:DEGREE_CANDIDATES], warnings)

        funding = [
            replace(item, identity=item.identity or self._identity(item.address))
            for item in funding
        ]
        funders = [item.address for item in funding]
        connections = tuple(rank_connections(
            [item.relationship for item in ranked if not item.relationship.label],
            funders=funders,
        ))
        return ConnectedReport(
            wallets=pairs,
            funding=tuple(funding),
            connections=connections,
            transactions=transactions,
            warnings=tuple(dict.fromkeys(warnings)),
            generated_at=int(time.time()),
        )

    # ------------------------------------------------------------ funding --

    async def _funder(self, wallet: str, chain: str) -> Funding:
        """The address whose money first reached this wallet.

        Three routes, in order of how exactly they answer the question:
        Solscan sorted ascending (one request, exact), an ascending Alchemy
        scan on EVM (one request, exact), or Helius paged backwards until it
        runs out (exact only if it does).
        """
        if chain == "solana":
            if self.solscan_key:
                found = await self._solscan_funder(wallet)
                if found is not None:
                    return found
            return await self._helius_funder(wallet)
        return await self._evm_funder(wallet, chain)

    async def _solscan_funder(self, wallet: str) -> Funding | None:
        """Solscan Pro, sorted oldest first -- the site's own 'oldest' toggle.

        Returns None rather than raising when the key is rejected or the shape
        is unfamiliar, so the Helius walk-back still gets its turn.
        """
        payload = await solscan_get(
            self.http,
            SOLSCAN_TRANSFER_URL,
            {
                "address": wallet, "flow": "in",
                "sort_by": "block_time", "sort_order": "asc",
                "page": 1, "page_size": 10,
                "exclude_amount_zero": "true",
            },
            timeout=30,
            key=self.solscan_key,
        )
        if payload is None:
            return None
        rows = (payload or {}).get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            sender = str(row.get("from_address") or "").strip()
            if not sender or sender == wallet:
                continue
            decimals = int(row.get("token_decimals") or 0)
            try:
                amount = float(row.get("amount") or 0) / (10 ** decimals)
            except (TypeError, ValueError, OverflowError):
                amount = 0.0
            mint = str(row.get("token_address") or "")
            symbol = "SOL" if mint in ("", SOLANA_WRAPPED_SOL) else \
                SOLANA_STABLE_MINTS.get(mint, mint[:6])
            return Funding(
                wallet=wallet, chain="solana", address=sender,
                amount=amount, symbol=symbol,
                timestamp=int(row.get("block_time") or 0),
                reference=str(row.get("trans_id") or ""),
                label=known_label(sender, self.extra_labels),
                exact=True,
            )
        return Funding(
            wallet=wallet, chain="solana", exact=True,
            note="Solana: no incoming transfer was found, so this wallet has "
                 "no funder on record.",
        )

    async def _helius_funder(self, wallet: str) -> Funding:
        """Page Helius backwards until the history runs out.

        Helius returns newest first and takes no ascending order, so reaching
        a wallet's first transaction means reading every transaction it has.
        `FUNDING_PAGES` is the ceiling on that; hitting it means the answer is
        unknown, and the report says so rather than naming whatever the oldest
        page happened to hold.
        """
        key = self._helius_key()
        if not key:
            return Funding(
                wallet=wallet, chain="solana",
                note="Solana: the funding wallet needs a Helius endpoint in "
                     "SOLANA_RPC, or a SOLSCAN_API_KEY.",
            )
        entries = await self._helius_history(wallet, key, FUNDING_PAGES)
        exhausted = len(entries) < FUNDING_PAGES * HELIUS_TX_LIMIT
        if not exhausted:
            return Funding(
                wallet=wallet, chain="solana",
                note=f"Solana: the funding wallet is deeper than "
                     f"{len(entries):,} transactions, so it is not reported. "
                     "Set SOLSCAN_API_KEY to read it in one request.",
            )
        # Oldest last, because Helius pages newest first.
        for entry in reversed(entries):
            if not isinstance(entry, dict):
                continue
            for transfer in solana_transfers_from_history(
                [entry], wallet, min_sol=0.0, min_stable=0.0, skip_swaps=False
            ):
                if transfer.outgoing or not transfer.counterparty:
                    continue
                return Funding(
                    wallet=wallet, chain="solana",
                    address=transfer.counterparty, amount=transfer.amount,
                    symbol=transfer.symbol, timestamp=transfer.timestamp,
                    reference=transfer.reference,
                    label=known_label(transfer.counterparty, self.extra_labels),
                    exact=True,
                )
        return Funding(
            wallet=wallet, chain="solana", exact=True,
            note="Solana: no incoming transfer was found, so this wallet has "
                 "no funder on record.",
        )

    async def _evm_funder(self, wallet: str, chain: str) -> Funding:
        """`alchemy_getAssetTransfers` takes `order: "asc"` -- one request."""
        if not self.evm_rpcs.get(chain):
            return Funding(wallet=wallet, chain=chain)
        rows = await self._asset_transfers(
            chain, page_size="0xa", pages=1, order="asc", toAddress=wallet
        )
        price = await self._native_price(chain)
        transfers = evm_transfers_from_rows(
            [], rows, wallet, chain, native_price=price,
            min_usd=0.0, min_stable=0.0,
        )
        for transfer in transfers:
            if transfer.outgoing or not transfer.counterparty:
                continue
            return Funding(
                wallet=wallet, chain=chain, address=transfer.counterparty,
                amount=transfer.amount, symbol=transfer.symbol,
                usd=transfer.usd, timestamp=transfer.timestamp,
                reference=transfer.reference,
                label=known_label(transfer.counterparty, self.extra_labels),
                exact=True,
            )
        return Funding(wallet=wallet, chain=chain, exact=True)

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
        log.debug(
            "connected: %s -> %d of %d transactions were plain transfers",
            wallet[:10], sum(1 for e in entries if is_plain_transfer(e)),
            len(entries),
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
        self, candidates: Sequence[Connection], warnings: list[str]
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
            # Everything, unfiltered: a service is a service because of the
            # swaps and dust it handles, not in spite of them.
            transfers = solana_transfers_from_history(
                entries, address, min_sol=0.0, min_stable=0.0, skip_swaps=False
            )
            return len({transfer.counterparty for transfer in transfers})
        outgoing, incoming = await asyncio.gather(
            self._asset_transfers(
                chain, page_size=hex(DEGREE_SAMPLE), pages=1, fromAddress=address
            ),
            self._asset_transfers(
                chain, page_size=hex(DEGREE_SAMPLE), pages=1, toAddress=address
            ),
        )
        transfers = evm_transfers_from_rows(
            outgoing, incoming, address, chain, min_usd=0.0, min_stable=0.0
        )
        return len({transfer.counterparty for transfer in transfers})


# ------------------------------------------------------------- parsing -----


def is_plain_transfer(entry: Any) -> bool:
    """True when a Helius transaction is somebody sending somebody money.

    This is the filter the whole command turns on. A swap moves value between
    a wallet and a liquidity pool, and reading those legs is how `/connected`
    used to report Meteora, Jupiter and Raydium as a trader's associates. They
    are venues, not associates.

    Helius types a plain send `TRANSFER`; a swap is `SWAP`, a liquidity move is
    `ADD_LIQUIDITY` / `WITHDRAW_LIQUIDITY`, an NFT trade is `NFT_SALE`. The
    swap-event and source checks are belt and braces for anything that manages
    to route through a transfer-typed instruction. A failed transaction moved
    nothing and is dropped too.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("transactionError"):
        return False
    if str(entry.get("type") or "").upper() not in TRANSFER_TYPES:
        return False
    if str(entry.get("source") or "").upper() in SWAP_SOURCES:
        return False
    events = entry.get("events")
    if isinstance(events, dict) and events.get("swap"):
        return False
    return True


def solana_transfers_from_history(
    entries: Iterable[Any], wallet: str, *, sol_price: float | None = None,
    min_sol: float | None = None, min_stable: float | None = None,
    skip_swaps: bool = True,
) -> list[Transfer]:
    """Qualifying transfers touching `wallet`, out of Helius parsed history.

    Both `nativeTransfers` and `tokenTransfers` name owner accounts, so the
    counterparty is read directly rather than resolved from a token account.

    Two filters run here rather than downstream, because a transfer that does
    not qualify should never reach a relationship, a reference list or a
    counterparty count: the transaction must be a plain transfer
    (`is_plain_transfer`), and the amount must clear `min_sol` / `min_stable`.
    Assets that are neither SOL nor a known stablecoin cannot be priced
    honestly, so they are not evidence of anything and are dropped.

    `min_sol=0`, `min_stable=0` and `skip_swaps=False` turn all of that off,
    which is what the funding lookup and the degree probe want -- the first
    money into a wallet is usually dust, and a service address should be
    counted on everything it touches.
    """
    floor_sol = MIN_SOL if min_sol is None else min_sol
    floor_stable = MIN_STABLE if min_stable is None else min_stable
    out: list[Transfer] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if skip_swaps and not is_plain_transfer(entry):
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
            if amount < floor_sol or amount <= 0:
                continue
            if wallet not in (sender, recipient):
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
            if symbol:
                if amount < floor_stable:
                    continue
                usd: float | None = amount
            elif mint == SOLANA_WRAPPED_SOL:
                if amount < floor_sol:
                    continue
                symbol = "SOL"
                usd = amount * sol_price if sol_price else None
            elif floor_stable or floor_sol:
                # An arbitrary token cannot be priced honestly, so it can
                # neither clear the bar nor be shown as value moved.
                continue
            else:
                symbol, usd = mint[:6], None
            outgoing = sender == wallet
            out.append(Transfer(
                counterparty=recipient if outgoing else sender,
                outgoing=outgoing, amount=amount,
                symbol=symbol, usd=usd,
                timestamp=timestamp, reference=reference,
            ))
    return out


def evm_transfers_from_rows(
    outgoing: Iterable[Any], incoming: Iterable[Any], wallet: str, chain: str,
    *, native_price: float | None = None,
    min_usd: float | None = None, min_stable: float | None = None,
) -> list[Transfer]:
    """Qualifying transfers from the two `alchemy_getAssetTransfers` directions.

    EVM carries no transaction type, so there is no swap filter to apply here;
    what keeps pools and routers out is the label list, the contract-code
    check, the degree probe -- and `min_usd`, which most legs of a memecoin
    swap fall under.

    Only the chain's native coin and known stablecoins can be priced honestly,
    so an arbitrary ERC-20 is dropped rather than counted unpriced. Passing
    zero for both floors keeps everything, which is what the funding lookup
    and the degree probe want.

    The two calls can return the same row when a wallet sends to itself, so
    rows are deduplicated on (hash, from, to, value) rather than trusted.
    """
    floor_usd = MIN_EVM_USD if min_usd is None else min_usd
    floor_stable = MIN_STABLE if min_stable is None else min_stable
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
            symbol = str(row.get("asset") or "").upper()
            usd: float | None = None
            if symbol in STABLE_SYMBOLS:
                usd = amount
                if amount < floor_stable:
                    continue
            elif native and symbol == native:
                usd = amount * native_price if native_price else None
                if floor_usd:
                    if usd is None or usd < floor_usd:
                        continue
            elif floor_usd or floor_stable:
                continue
            seen.add(fingerprint)
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            out.append(Transfer(
                counterparty=recipient if is_outgoing else sender,
                outgoing=sender == lowered,
                amount=amount, symbol=symbol or "?", usd=usd,
                timestamp=_iso_epoch(metadata.get("blockTimestamp")),
                reference=reference,
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
