"""
token_traders.py -- a token's *best* traders, from the same chain data the
holder list already comes from.

`/token`'s Top Holders answer one question the chain can answer directly: who
holds this token *now*. Top Traders asks the other half -- who is actually
winning on it -- and the chain answers that one too, just less cheaply: a
holder list is a single ranked query, while performance has to be reconstructed
out of transfer history.

FOMO has no top-traders route (`token_page_sniff.py` recorded the whole token
page; the tabs are Holders, Thesis and Activity), so this reads the same
providers `token_intelligence.py` already uses for holders:

    Solana     Helius -- parsed transaction history for the mint
    EVM        Alchemy `alchemy_getAssetTransfers`, Blockscout as the fallback

Every provider returns a different shape, so each parser reduces its rows to
one common unit -- a `TokenFlow`: one address, one signed token delta, one
transaction reference, and the USD value of the *other* side of that trade when
the source carried it. Buying is a positive delta and selling a negative one,
which is true of a transfer pair and of a balance delta alike, so the two kinds
of source aggregate together without either having to pretend to be the other.

**Ranking is by money made, not by tokens moved.** A wallet that moved ten
million tokens for a 3% gain is not a better trader than one that turned $400
into $19,000, and until session 37 this module said it was: it ranked on
`bought + sold`, which is activity. It now runs a real cost-basis ledger per
address -- weighted-average entry, realised PnL on every disposal, unrealised
PnL on what is still held -- and ranks on the result. Volume survives as a
secondary figure and as an explicitly selectable ranking, never as the default.

**What the USD figures can and cannot be.** The counter-leg of a swap is in the
same transaction as the token leg, so a trade's USD value is available wherever
the provider hands back the whole transaction (both Solana routes) or wherever
the venue's own quote-asset movement can be joined to it by transaction hash
(the EVM route). Where it is not available the trade is carried as *unpriced*
rather than valued at some invented price, and an unpriced acquisition never
becomes free profit later: the disposal that consumes it is excluded from
realised PnL instead. Every row says which of these happened to it.

**This is recent history, not lifetime history.** Every route pages backwards
from the chain head under a bounded budget, so the ledger covers the window
those pages reach. A wallet that sells more than the window saw it buy is
selling inventory whose cost is unknown; that excess is excluded from PnL and
the row is flagged, rather than being booked as pure profit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

log = logging.getLogger("token.traders")

# Addresses that are never a trader. Mints burn to the incinerator and to the
# zero address, and a token contract holding its own supply is not a person.
SOLANA_SYSTEM_PROGRAM = "11111111111111111111111111111111"
SOLANA_INCINERATOR = "1nc1nerator11111111111111111111111111111111"
EVM_ZERO = "0x0000000000000000000000000000000000000000"
EVM_DEAD = "0x000000000000000000000000000000000000dead"
ALWAYS_EXCLUDED = {
    SOLANA_SYSTEM_PROGRAM,
    SOLANA_INCINERATOR,
    EVM_ZERO,
    EVM_DEAD,
    "",
}

# An AMM pool, a router or a market-maker vault is on one side of nearly every
# swap, which is exactly what makes it detectable without a label service: an
# address touching this share of the sampled transactions is infrastructure,
# not a trader. 20% is far above what any real trader reaches in a window with
# more than a handful of participants, and far below where a pool sits.
POOL_TRANSACTION_SHARE = 0.20
# Below this many sampled transactions the share test has no signal -- three
# transactions make every participant look like a pool -- so it is skipped and
# only the known-address list applies.
POOL_SHARE_MIN_TRANSACTIONS = 12

# ------------------------------------------------------------ quote assets --
# What a trade is *paid in*. A stablecoin is its own USD price; everything else
# needs one, which the client supplies from the same DEX Screener lookup the
# card's market cap already comes from.
WSOL_MINT = "So11111111111111111111111111111111111111112"
SOLANA_STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA": "USDS",
}
# Native SOL and wrapped SOL are the same asset for accounting, so they share
# one price key: a swap that unwraps mid-route must not be counted twice.
SOLANA_QUOTE_MINTS = {WSOL_MINT, *SOLANA_STABLE_MINTS}
# Creating an associated token account costs 0.00203928 SOL and a signature
# costs 0.000005; neither is a trade. Anything below this is noise, not price
# information.
NATIVE_SOL_DUST = Decimal("0.005")
LAMPORTS = Decimal(10) ** 9

# The assets EVM swaps are actually quoted in, per chain. Only these are read
# back from the venue, so one extra bounded query prices a whole sample.
EVM_QUOTE_ASSETS: dict[str, dict[str, str]] = {
    "Ethereum": {
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": "WETH",
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": "USDC",
        "0xdac17f958d2ee523a2206206994597c13d831ec7": "USDT",
        "0x6b175474e89094c44da98b954eedeac495271d0f": "DAI",
    },
    "Base": {
        "0x4200000000000000000000000000000000000006": "WETH",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": "USDbC",
    },
    "BSC": {
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": "WBNB",
        "0x55d398326f99059ff775485246999027b3197955": "USDT",
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC",
        "0xe9e7cea3dedca5984780bafc599bd69add087d56": "BUSD",
    },
}
# Everything a chain quotes in that is already a dollar. Their price is 1 and
# needs no lookup.
STABLE_SYMBOLS = {"USDC", "USDT", "USDS", "DAI", "USDbC", "BUSD"}

# ROI on a $3 position is not a trading result, it is a rounding artefact, so a
# ROI ranking needs a floor under the denominator.
MIN_ROI_INVESTED_USD = Decimal("50")

RANK_KEYS = ("pnl", "roi", "volume")


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _epoch(value: Any) -> int | None:
    """Seconds since the epoch from a unix number or an ISO-8601 string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        return number if number > 0 else None
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


@dataclass(frozen=True)
class TokenFlow:
    """One address's signed movement of the token in one transaction.

    `value_usd` is the value of the *counter-leg* -- what was paid for a
    purchase or received for a sale -- and is `None` when the source did not
    carry it. It is never a valuation of the tokens at some other price.
    """

    address: str
    delta: Decimal
    timestamp: int | None = None
    reference: str = ""
    value_usd: Decimal | None = None


@dataclass(frozen=True)
class QuoteFlow:
    """One address's signed USD movement of a quote asset in one transaction.

    Positive means the address received value. These are joined onto token
    flows by transaction reference, which is what lets a venue's own USDC/WETH
    leg price a swap whose provider only returned the token side.
    """

    address: str
    usd: Decimal
    reference: str = ""


@dataclass(frozen=True)
class TokenTrader:
    """One address's ledger for this token over the sampled window.

    The quantities (`bought`, `sold`) are what the chain shows. The money
    figures are what the ledger could actually account for, and the `unpriced_*`
    and `untracked_sold` fields say how much it could not -- so a row can be
    read as "this is the whole story" or "this is the part that is knowable"
    rather than being silently one or the other.
    """

    address: str
    bought: Decimal
    sold: Decimal
    transactions: int
    first_seen: int | None = None
    last_seen: int | None = None

    # --- money -------------------------------------------------------------
    invested_usd: Decimal = Decimal(0)      # cost of every priced acquisition
    proceeds_usd: Decimal = Decimal(0)      # value of every priced disposal
    realized_pnl_usd: Decimal = Decimal(0)  # closed against weighted-avg cost
    unrealized_pnl_usd: Decimal | None = None   # open position at current price
    open_tokens: Decimal = Decimal(0)       # still held, out of this window
    open_cost_usd: Decimal = Decimal(0)     # cost basis of the priced part
    avg_entry_price: Decimal | None = None  # weighted average acquisition price
    avg_exit_price: Decimal | None = None   # weighted average disposal price
    buys: int = 0
    sells: int = 0

    # --- what could not be accounted for -----------------------------------
    unpriced_buy_tokens: Decimal = Decimal(0)
    unpriced_sell_tokens: Decimal = Decimal(0)
    untracked_sold: Decimal = Decimal(0)    # sold beyond what the window saw
    free_tokens: Decimal = Decimal(0)       # arrived with no money moving

    @property
    def volume(self) -> Decimal:
        """Tokens moved: bought + sold. Activity, not performance."""
        return self.bought + self.sold

    @property
    def net(self) -> Decimal:
        return self.bought - self.sold

    @property
    def total_pnl_usd(self) -> Decimal | None:
        """Realised plus unrealised, or `None` when neither is knowable."""
        if not self.has_pnl:
            return None
        return self.realized_pnl_usd + (self.unrealized_pnl_usd or Decimal(0))

    @property
    def has_pnl(self) -> bool:
        """True when at least one leg of this wallet's trading was priced."""
        return self.invested_usd > 0 or self.proceeds_usd > 0

    @property
    def roi_pct(self) -> Decimal | None:
        """PnL over invested capital. Undefined without a cost basis."""
        pnl = self.total_pnl_usd
        if pnl is None or self.invested_usd <= 0:
            return None
        return pnl / self.invested_usd * 100

    @property
    def realized_only(self) -> bool:
        """The position is closed, so total PnL is realised PnL."""
        return self.open_tokens <= 0

    @property
    def partial(self) -> bool:
        """This row's cost basis is incomplete, one way or another.

        Either something could not be priced, or the wallet sold more than the
        sample saw it buy, or part of the position arrived for free -- in which
        case the PnL is real but the ROI has no honest denominator.
        """
        return bool(
            self.untracked_sold > 0
            or self.unpriced_buy_tokens > 0
            or self.unpriced_sell_tokens > 0
            or self.free_tokens > 0
            or (self.open_tokens > 0 and self.unrealized_pnl_usd is None)
        )


def _transfer_flows(
    sender: str, recipient: str, amount: Decimal,
    timestamp: int | None, reference: str,
) -> list[TokenFlow]:
    """A transfer is two flows: the sender sold it and the recipient bought it."""
    flows: list[TokenFlow] = []
    if sender:
        flows.append(TokenFlow(sender, -amount, timestamp, reference))
    if recipient:
        flows.append(TokenFlow(recipient, amount, timestamp, reference))
    return flows


@dataclass
class _TxLedger:
    """Per-owner token and quote movement inside one transaction.

    `other_value` records that something of value moved which could not be
    priced -- an exotic quote asset, a swap into another memecoin. It is the
    difference between "this was a gift" and "this had a price we could not
    read", and the ledger treats those two very differently.
    """

    tokens: dict[str, Decimal] = field(default_factory=dict)
    quotes: dict[str, Decimal] = field(default_factory=dict)
    other_value: bool = False

    def token(self, address: str, delta: Decimal) -> None:
        if address:
            self.tokens[address] = self.tokens.get(address, Decimal(0)) + delta

    def quote(self, address: str, usd: Decimal) -> None:
        if address:
            self.quotes[address] = self.quotes.get(address, Decimal(0)) + usd

    def transfer(self, sender: str, recipient: str, amount: Decimal) -> None:
        self.token(sender, -amount)
        self.token(recipient, amount)

    def quote_transfer(self, sender: str, recipient: str, usd: Decimal) -> None:
        self.quote(sender, -usd)
        self.quote(recipient, usd)

    def flows(
        self, timestamp: int | None, reference: str, *,
        gifts_detectable: bool = False,
    ) -> list[TokenFlow]:
        """One flow per owner: a transaction is one trade, not one per hop.

        Netting here is what makes a multi-hop route (two pools, two transfers
        of the same mint, one intent) a single trade with a single price
        instead of two trades at half the size.

        A transaction in which *nothing else moved at all* is a gift, not an
        unreadable trade: the tokens changed hands and no money did. Those
        flows are priced at zero -- a real cost basis, not a missing one -- so
        that the sale of a dev allocation or an airdrop shows the profit it
        actually made instead of being dropped from the board.

        `gifts_detectable` is the safety catch: without a price table there is
        no such thing as a transaction with no money in it, only a transaction
        whose money we cannot see, and calling those free would hand every
        wallet an infinite return.
        """
        gift = gifts_detectable and not self.quotes and not self.other_value
        flows: list[TokenFlow] = []
        for address, delta in self.tokens.items():
            if delta == 0:
                continue
            value = _counter_value(delta, self.quotes.get(address))
            if value is None and gift:
                value = Decimal(0)
            flows.append(TokenFlow(
                address=address,
                delta=delta,
                timestamp=timestamp,
                reference=reference,
                value_usd=value,
            ))
        return flows


def _counter_value(delta: Decimal, quote_usd: Decimal | None) -> Decimal | None:
    """The USD value of a trade, from the quote asset that moved against it.

    The signs have to disagree: value only means anything when tokens came in
    and money went out, or the reverse. A wallet that received tokens *and*
    received USDC in the same transaction was not buying, so that transaction
    prices nothing and is carried as unpriced.
    """
    if quote_usd is None or quote_usd == 0:
        return None
    if delta > 0 and quote_usd < 0:
        return -quote_usd
    if delta < 0 and quote_usd > 0:
        return quote_usd
    return None


def parse_helius_transactions(
    payload: Any, mint: str, *, prices: Mapping[str, Decimal] | None = None,
) -> list[TokenFlow]:
    """Flows from Helius's parsed transaction history for a mint.

    `tokenTransfers` names the *owner* accounts (`fromUserAccount` /
    `toUserAccount`) rather than the token accounts, which is what makes this
    route worth its request: `getTokenLargestAccounts` and raw transaction
    parsing both hand back token accounts that then have to be resolved to
    owners one by one.

    `prices` maps a quote mint to its USD price (native SOL is keyed by the
    wrapped-SOL mint, because for accounting they are one asset). Given it,
    the same page that carries the token leg also carries the money leg, so
    pricing a trade costs no extra request at all.
    """
    if not isinstance(payload, list):
        return []
    wanted = str(mint or "")
    quotes = {key: value for key, value in (prices or {}).items() if key != wanted}
    sol_price = quotes.get(WSOL_MINT)
    flows: list[TokenFlow] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        # A reverted transaction moved nothing; only its fee was ever real.
        if entry.get("transactionError") or entry.get("err"):
            continue
        reference = str(entry.get("signature") or "")
        timestamp = _epoch(entry.get("timestamp") or entry.get("blockTime"))
        ledger = _TxLedger()
        for row in entry.get("tokenTransfers") or []:
            if not isinstance(row, dict):
                continue
            row_mint = str(row.get("mint") or "")
            amount = _decimal(row.get("tokenAmount"))
            if amount is None or amount <= 0:
                continue
            sender = str(row.get("fromUserAccount") or "")
            recipient = str(row.get("toUserAccount") or "")
            if not wanted or row_mint == wanted:
                ledger.transfer(sender, recipient, amount)
            elif row_mint in quotes:
                ledger.quote_transfer(sender, recipient, amount * quotes[row_mint])
            else:
                # Another asset moved and we cannot price it: this transaction
                # had a price, it is just not readable. Not a gift.
                ledger.other_value = True
        for row in entry.get("nativeTransfers") or []:
            if not isinstance(row, dict):
                continue
            lamports = _decimal(row.get("amount"))
            if lamports is None or lamports <= 0:
                continue
            sol = lamports / LAMPORTS
            if sol < NATIVE_SOL_DUST:
                continue  # rent and fees are not a price
            if not sol_price:
                ledger.other_value = True
                continue
            ledger.quote_transfer(
                str(row.get("fromUserAccount") or ""),
                str(row.get("toUserAccount") or ""),
                sol * sol_price,
            )
        flows.extend(ledger.flows(
            timestamp, reference, gifts_detectable=bool(quotes)
        ))
    return flows


def parse_rpc_transactions(
    results: Iterable[Any], mint: str, *,
    prices: Mapping[str, Decimal] | None = None,
) -> list[TokenFlow]:
    """Flows from raw `getTransaction` results, used when Helius is absent.

    Token balance deltas are read instead of instructions: `preTokenBalances`
    and `postTokenBalances` already carry the owner and the UI amount, so one
    subtraction per account gives the same answer as decoding every transfer
    instruction, and gives it for swap routes this module has never seen. The
    same subtraction over the *other* mints -- and over `preBalances` /
    `postBalances` for native SOL -- is what prices the trade.
    """
    wanted = str(mint or "")
    quotes = {key: value for key, value in (prices or {}).items() if key != wanted}
    sol_price = quotes.get(WSOL_MINT)
    flows: list[TokenFlow] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
        if meta.get("err"):
            continue
        timestamp = _epoch(entry.get("blockTime"))
        reference = ""
        transaction = entry.get("transaction")
        if isinstance(transaction, dict):
            signatures = transaction.get("signatures")
            if isinstance(signatures, list) and signatures:
                reference = str(signatures[0] or "")

        ledger = _TxLedger()
        for key, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
            for row in meta.get(key) or []:
                if not isinstance(row, dict):
                    continue
                row_mint = str(row.get("mint") or "")
                owner = str(row.get("owner") or "")
                amount = _decimal(
                    (row.get("uiTokenAmount") or {}).get("uiAmountString")
                    if isinstance(row.get("uiTokenAmount"), dict) else None
                )
                if not owner or amount is None:
                    continue
                if not wanted or row_mint == wanted:
                    ledger.token(owner, amount * sign)
                elif row_mint in quotes:
                    ledger.quote(owner, amount * sign * quotes[row_mint])
                elif amount:
                    ledger.other_value = True
        for address, lamports in _native_deltas(entry, meta).items():
            sol = lamports / LAMPORTS
            if abs(sol) < NATIVE_SOL_DUST:
                continue
            if not sol_price:
                ledger.other_value = True
                continue
            ledger.quote(address, sol * sol_price)
        flows.extend(ledger.flows(
            timestamp, reference, gifts_detectable=bool(quotes)
        ))
    return flows


def _native_deltas(entry: dict[str, Any], meta: dict[str, Any]) -> dict[str, Decimal]:
    """Lamport deltas per account key, with the fee payer's fee added back.

    The fee is not part of a trade's price, and on FOMO it is not even paid by
    the trader -- the sponsor pays it. Adding it back keeps a sponsored swap's
    quote leg honest either way.
    """
    transaction = entry.get("transaction")
    message = transaction.get("message") if isinstance(transaction, dict) else None
    keys = message.get("accountKeys") if isinstance(message, dict) else None
    pre = meta.get("preBalances")
    post = meta.get("postBalances")
    if not isinstance(keys, list) or not isinstance(pre, list) or not isinstance(post, list):
        return {}
    fee = _decimal(meta.get("fee")) or Decimal(0)
    deltas: dict[str, Decimal] = {}
    for index, key in enumerate(keys):
        if index >= len(pre) or index >= len(post):
            break
        address = key.get("pubkey") if isinstance(key, dict) else key
        address = str(address or "")
        before = _decimal(pre[index])
        after = _decimal(post[index])
        if not address or before is None or after is None:
            continue
        delta = after - before
        if index == 0:
            delta += fee
        if delta:
            deltas[address] = deltas.get(address, Decimal(0)) + delta
    return deltas


def parse_alchemy_transfers(payload: Any, token: str) -> list[TokenFlow]:
    """Flows from one `alchemy_getAssetTransfers` page."""
    rows = payload
    if isinstance(rows, dict):
        rows = (rows.get("result") or {}).get("transfers", rows.get("transfers"))
    if not isinstance(rows, list):
        return []
    wanted = str(token or "").lower()
    flows: list[TokenFlow] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get("rawContract") if isinstance(row.get("rawContract"), dict) else {}
        contract = str(raw.get("address") or "").lower()
        if wanted and contract and contract != wanted:
            continue
        amount = _decimal(row.get("value"))
        if amount is None or amount <= 0:
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        flows.extend(_transfer_flows(
            str(row.get("from") or "").lower(),
            str(row.get("to") or "").lower(),
            amount,
            _epoch(metadata.get("blockTimestamp")),
            str(row.get("hash") or ""),
        ))
    return flows


def parse_alchemy_quote_flows(
    payload: Any, prices: Mapping[str, Decimal],
    *, seen: set[str] | None = None,
) -> list[QuoteFlow]:
    """USD movements of the quote assets in one `getAssetTransfers` page.

    The token page for an EVM token carries only that token, so a swap's money
    leg has to be fetched separately -- from the venue, whose own USDC/WETH
    transfers sit in the same transactions. Joining them by hash is exact; no
    price of the traded token is ever assumed.
    """
    rows = payload
    if isinstance(rows, dict):
        rows = (rows.get("result") or {}).get("transfers", rows.get("transfers"))
    if not isinstance(rows, list):
        return []
    lookup = {str(key).lower(): value for key, value in prices.items()}
    native = lookup.get("native")
    flows: list[QuoteFlow] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get("rawContract") if isinstance(row.get("rawContract"), dict) else {}
        contract = str(raw.get("address") or "").lower()
        category = str(row.get("category") or "").lower()
        price = lookup.get(contract) if contract else (native if category == "external" else None)
        if price is None:
            continue
        amount = _decimal(row.get("value"))
        if amount is None or amount <= 0:
            continue
        reference = str(row.get("hash") or "")
        sender = str(row.get("from") or "").lower()
        recipient = str(row.get("to") or "").lower()
        if seen is not None:
            identity = str(row.get("uniqueId") or "") or (
                f"{reference}:{sender}:{recipient}:{contract}:{amount}"
            )
            if identity in seen:
                continue
            seen.add(identity)
        usd = amount * price
        if sender:
            flows.append(QuoteFlow(sender, -usd, reference))
        if recipient:
            flows.append(QuoteFlow(recipient, usd, reference))
    return flows


def _blockscout_address(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("hash")
    return str(value or "").lower()


def parse_blockscout_transfers(payload: Any, token: str) -> list[TokenFlow]:
    """Flows from Blockscout's `/tokens/{address}/transfers` page."""
    rows = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    wanted = str(token or "").lower()
    flows: list[TokenFlow] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        contract = _blockscout_address(
            (row.get("token") or {}).get("address")
            if isinstance(row.get("token"), dict) else None
        )
        if wanted and contract and contract != wanted:
            continue
        total = row.get("total") if isinstance(row.get("total"), dict) else {}
        amount = _decimal(total.get("value"))
        if amount is None:
            continue
        try:
            decimals = int(total.get("decimals") or 18)
        except (TypeError, ValueError):
            decimals = 18
        amount = amount / (Decimal(10) ** decimals)
        if amount <= 0:
            continue
        flows.extend(_transfer_flows(
            _blockscout_address(row.get("from")),
            _blockscout_address(row.get("to")),
            amount,
            _epoch(row.get("timestamp") or row.get("block_timestamp")),
            str(row.get("transaction_hash") or row.get("tx_hash") or ""),
        ))
    return flows


def attach_quote_values(
    flows: Sequence[TokenFlow],
    quotes: Sequence[QuoteFlow],
    *,
    venues: Iterable[str] = (),
) -> list[TokenFlow]:
    """Price token flows from quote movements in the same transactions.

    Two joins, in order of how much they assume:

    1. **The trader's own quote leg.** If the address that moved the token also
       moved USDC or WETH in that transaction, that is the price, full stop.
    2. **The venue's quote leg.** On EVM the money usually reaches the trader
       through a router, so their own address shows no quote movement at all;
       the pool's does. The pool's USD movement in that transaction is the size
       of the swap, and it is split across the traders in it in proportion to
       how much of the token each moved -- which is exact for the ordinary
       one-trader swap and a stated approximation for a batched one.
    """
    venue_set = {str(address).lower() for address in venues}
    own: dict[tuple[str, str], Decimal] = {}
    venue_usd: dict[str, Decimal] = {}
    for quote in quotes:
        key = (quote.reference, quote.address)
        own[key] = own.get(key, Decimal(0)) + quote.usd
        if quote.address.lower() in venue_set:
            current = venue_usd.get(quote.reference, Decimal(0))
            venue_usd[quote.reference] = current + quote.usd

    unpriced: dict[str, list[int]] = {}
    priced = list(flows)
    for index, flow in enumerate(priced):
        if flow.value_usd is not None:
            continue
        value = _counter_value(flow.delta, own.get((flow.reference, flow.address)))
        if value is not None:
            priced[index] = TokenFlow(
                flow.address, flow.delta, flow.timestamp, flow.reference, value
            )
        elif flow.reference:
            unpriced.setdefault(flow.reference, []).append(index)

    for reference, indexes in unpriced.items():
        total_usd = venue_usd.get(reference)
        if not total_usd:
            continue
        # The venue received value when the traders spent it, so the trader's
        # side is the venue's sign flipped.
        share_total = sum(abs(priced[index].delta) for index in indexes)
        if share_total <= 0:
            continue
        for index in indexes:
            flow = priced[index]
            portion = abs(total_usd) * (abs(flow.delta) / share_total)
            value = _counter_value(flow.delta, -total_usd / abs(total_usd) * portion)
            if value is not None and value > 0:
                priced[index] = TokenFlow(
                    flow.address, flow.delta, flow.timestamp, flow.reference, value
                )
    return priced


def infrastructure_addresses(
    flows: Sequence[TokenFlow], *, share: float = POOL_TRANSACTION_SHARE,
) -> set[str]:
    """Addresses that behave like a pool, a router or a program, not a trader.

    No label service is needed for this one: liquidity sits on one side of
    almost every swap, so an address that appears in a large share of the
    sampled transactions is the venue rather than a participant. The test is
    skipped on a sample too small to distinguish the two.
    """
    references: dict[str, set[str]] = {}
    seen: set[str] = set()
    for flow in flows:
        reference = flow.reference or f"anon:{id(flow)}"
        seen.add(reference)
        references.setdefault(flow.address, set()).add(reference)
    if len(seen) < POOL_SHARE_MIN_TRANSACTIONS:
        return set()
    cutoff = max(2.0, len(seen) * share)
    return {
        address for address, refs in references.items() if len(refs) >= cutoff
    }


def _trades_by_address(
    flows: Sequence[TokenFlow], skipped: set[str],
) -> dict[str, list[TokenFlow]]:
    """One trade per address per transaction, oldest first.

    Providers page backwards from the chain head, so the input is newest-first;
    a cost-basis ledger has to run the other way. Timestamps decide the order
    where they exist, and the reversed arrival order decides it where they do
    not -- which is exactly the paging order.
    """
    merged: dict[tuple[str, str], list[Any]] = {}
    order: list[tuple[str, str]] = []
    for index, flow in enumerate(flows):
        address = flow.address
        if not address or address in skipped:
            continue
        key = (address, flow.reference or f"anon:{index}")
        slot = merged.get(key)
        if slot is None:
            merged[key] = [flow.delta, flow.value_usd, flow.timestamp, index]
            order.append(key)
            continue
        slot[0] += flow.delta
        if flow.value_usd is not None:
            slot[1] = (slot[1] or Decimal(0)) + flow.value_usd
        if slot[2] is None:
            slot[2] = flow.timestamp

    by_address: dict[str, list[tuple[int, TokenFlow]]] = {}
    for key in order:
        delta, value, timestamp, index = merged[key]
        if delta == 0:
            continue
        address, reference = key
        by_address.setdefault(address, []).append((index, TokenFlow(
            address=address,
            delta=delta,
            timestamp=timestamp,
            reference="" if reference.startswith("anon:") else reference,
            # A netted multi-hop can leave a value whose sign no longer agrees
            # with the netted delta; `_counter_value` already ruled on that per
            # leg, so only a positive total survives here.
            # A zero here is a fact -- the transaction moved no money -- so it
            # survives the netting; only "never priced at all" becomes None.
            value_usd=value if value is not None and value >= 0 else None,
        )))

    return {
        address: [
            trade for _, trade in sorted(
                rows, key=lambda row: (row[1].timestamp or 0, -row[0])
            )
        ]
        for address, rows in by_address.items()
    }


def evaluate_trader(
    address: str,
    trades: Sequence[TokenFlow],
    *,
    current_price: Decimal | None = None,
) -> TokenTrader:
    """Run one address's trades through a weighted-average cost-basis ledger.

    Weighted average rather than FIFO, because that is what an entry price
    means to somebody reading the card ("what did they get in at?") and because
    FIFO would need a per-lot ordering the window cannot always supply.

    Inventory is kept in three buckets, because "we do not know what this cost"
    and "this cost nothing" are different facts:

    * **paid** -- acquired in a transaction with a readable money leg.
    * **free** -- acquired in a transaction where nothing of value moved at
      all: an airdrop, a dev allocation, a transfer from another wallet. Its
      cost basis is zero, and that is a fact rather than a gap, so selling it
      realises the full proceeds as profit.
    * **unknown** -- acquired in a transaction that did move value this module
      could not read. Selling it realises nothing: the proceeds and the basis
      are both dropped, because crediting the whole sale would invent a profit.

    A sale consumes all three in proportion. Unrealised PnL counts only the
    paid bucket, so a wallet sitting on a free allocation is not credited with
    a "profit" it never traded for -- that is what Top Holders is for.
    """
    paid_position = Decimal(0)     # tokens whose cost we know
    paid_cost = Decimal(0)         # ...and what they cost
    free_position = Decimal(0)     # tokens that verifiably cost nothing
    unknown_position = Decimal(0)  # tokens whose cost is unreadable

    bought = sold = Decimal(0)
    invested = proceeds = realized = Decimal(0)
    buy_paid_tokens = sell_priced_tokens = Decimal(0)
    unpriced_buys = unpriced_sells = untracked_sold = Decimal(0)
    free_in = Decimal(0)
    buys = sells = 0
    references: set[str] = set()
    first_seen: int | None = None
    last_seen: int | None = None

    for trade in trades:
        quantity = abs(trade.delta)
        if trade.reference:
            references.add(trade.reference)
        if trade.timestamp:
            first_seen = min(first_seen or trade.timestamp, trade.timestamp)
            last_seen = max(last_seen or trade.timestamp, trade.timestamp)

        if trade.delta > 0:
            bought += quantity
            buys += 1
            if trade.value_usd is None:
                unknown_position += quantity
                unpriced_buys += quantity
            elif trade.value_usd > 0:
                invested += trade.value_usd
                paid_cost += trade.value_usd
                paid_position += quantity
                buy_paid_tokens += quantity
            else:
                free_position += quantity
                free_in += quantity
            continue

        sold += quantity
        sells += 1
        inventory = paid_position + free_position + unknown_position
        matched = min(quantity, inventory)
        untracked_sold += quantity - matched
        share = (matched / inventory) if inventory > 0 else Decimal(0)
        from_paid = paid_position * share
        from_free = free_position * share
        from_unknown = matched - from_paid - from_free
        basis = (
            paid_cost * (from_paid / paid_position)
            if paid_position > 0 else Decimal(0)
        )
        if trade.value_usd is not None and quantity > 0:
            proceeds += trade.value_usd
            sell_priced_tokens += quantity
            attributable = (from_paid + from_free) / quantity
            realized += trade.value_usd * attributable - basis
        else:
            unpriced_sells += quantity
        paid_position -= from_paid
        paid_cost -= basis
        free_position -= from_free
        unknown_position -= from_unknown

    open_tokens = paid_position + free_position + unknown_position
    unrealized: Decimal | None = None
    if current_price is not None and paid_position > 0:
        unrealized = paid_position * current_price - paid_cost
    elif paid_position <= 0 and unknown_position <= 0:
        unrealized = Decimal(0)

    return TokenTrader(
        address=address,
        bought=bought,
        sold=sold,
        transactions=len(references) or max(1, buys + sells),
        first_seen=first_seen,
        last_seen=last_seen,
        invested_usd=invested,
        proceeds_usd=proceeds,
        realized_pnl_usd=realized,
        unrealized_pnl_usd=unrealized,
        open_tokens=open_tokens,
        open_cost_usd=paid_cost,
        free_tokens=free_in,
        avg_entry_price=(
            invested / buy_paid_tokens if buy_paid_tokens > 0 else None
        ),
        avg_exit_price=(
            proceeds / sell_priced_tokens if sell_priced_tokens > 0 else None
        ),
        buys=buys,
        sells=sells,
        unpriced_buy_tokens=unpriced_buys,
        unpriced_sell_tokens=unpriced_sells,
        untracked_sold=untracked_sold,
    )


def _rank_key(key: str, min_invested_usd: Decimal):
    """Sort keys, all descending, all with a defined tail for missing data."""
    if key == "volume":
        return lambda trader: (
            1, trader.volume, trader.transactions,
        )
    if key == "roi":
        def by_roi(trader: TokenTrader):
            roi = trader.roi_pct
            qualified = roi is not None and trader.invested_usd >= min_invested_usd
            return (
                (2, roi, trader.total_pnl_usd or Decimal(0)) if qualified
                else (1, trader.total_pnl_usd, Decimal(0)) if trader.has_pnl
                else (0, Decimal(0), Decimal(0))
            )
        return by_roi

    def by_pnl(trader: TokenTrader):
        pnl = trader.total_pnl_usd
        if pnl is None:
            # No priced leg at all: this wallet is unranked on money, so it
            # sits below every wallet that has a number, ordered by activity.
            return (0, Decimal(0), trader.volume)
        return (1, pnl, trader.roi_pct or Decimal(0))
    return by_pnl


def rank_traders(
    traders: Sequence[TokenTrader],
    *,
    key: str = "pnl",
    limit: int | None = 50,
    min_invested_usd: Decimal = MIN_ROI_INVESTED_USD,
) -> list[TokenTrader]:
    """Order traders by performance. `key` is 'pnl', 'roi' or 'volume'.

    PnL is the default because it is the question the card is asked -- who made
    money here -- and because ROI alone rewards a $20 position that happened to
    10x over a wallet that made five figures. ROI is one press away rather than
    absent, and volume stays available as an explicit choice rather than as the
    thing the list silently means.
    """
    chosen = key if key in RANK_KEYS else "pnl"
    ordered = sorted(
        traders, key=_rank_key(chosen, min_invested_usd), reverse=True
    )
    return ordered if limit is None else ordered[: max(0, int(limit))]


def candidate_pool(
    traders: Sequence[TokenTrader], *, limit: int = 50,
) -> list[TokenTrader]:
    """The union of the top `limit` under every ranking.

    The card re-ranks locally when the sort is switched, so the rows it holds
    have to be the right rows for any of the three -- otherwise 'sort by ROI'
    would mean 'sort the top PnL rows by ROI', which is a different and much
    less interesting question.
    """
    picked: dict[str, TokenTrader] = {}
    for key in RANK_KEYS:
        for trader in rank_traders(traders, key=key, limit=limit):
            picked.setdefault(trader.address, trader)
    return list(picked.values())


def aggregate_traders(
    flows: Sequence[TokenFlow],
    *,
    exclude: Iterable[str] = (),
    limit: int | None = 50,
    detect_infrastructure: bool = True,
    current_price: Decimal | None = None,
    rank_by: str = "pnl",
) -> list[TokenTrader]:
    """Build every address's ledger from the sampled flows and rank it.

    The ranking is performance -- realised plus unrealised PnL against the cost
    the wallet actually paid. It used to be `bought + sold`, which ranked a
    whale who bought once and never sold above a wallet that turned $300 into
    $9,000, because it was measuring activity and calling it quality.
    """
    skipped = {str(address) for address in exclude} | ALWAYS_EXCLUDED
    if detect_infrastructure:
        skipped |= infrastructure_addresses(flows)

    traders = [
        evaluate_trader(address, trades, current_price=current_price)
        for address, trades in _trades_by_address(flows, skipped).items()
    ]
    return rank_traders(traders, key=rank_by, limit=limit)


def sampled_window(flows: Sequence[TokenFlow]) -> tuple[int | None, int | None]:
    """Earliest and latest timestamp the sample actually covers."""
    stamps = [flow.timestamp for flow in flows if flow.timestamp]
    return (min(stamps), max(stamps)) if stamps else (None, None)
