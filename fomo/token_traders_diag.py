"""
token_traders_diag.py -- what did `/token`'s Top Traders actually read, and why
does a wallet's PnL differ from Padre's?

    python token_traders_diag.py 7RY9w8brhM4DgQwiwn4D9cVnk4L7RJuZESS3mEKmpump
    python token_traders_diag.py <mint> --top 20
    python token_traders_diag.py <mint> --wallet <address>     # trade by trade
    python token_traders_diag.py <mint> --pages 60 --budget 180
    python token_traders_diag.py <mint> --csv hunt_out/px_traders.csv

It drives the SAME object `/token` drives (`TokenIntelligenceClient`), so it
cannot drift from the card, and it installs a handler on the `token.*` loggers
so the client's own explanations are printed rather than reimplemented.

Three questions, in the order they go wrong:

    coverage    how deep did paging get, and did it reach the token's first
                transaction? A ranking over the last few hundred transactions
                of a live memecoin is a ranking of its newest buyers -- the
                wallets that made the money bought at the beginning.
    pricing     how many trades carried a readable money leg? A sample that
                priced nothing produces a board of dashes; a sample that priced
                everything can still be wrong if it started too late.
    ledger      for one wallet, every trade in order with its running position,
                cost basis and realised PnL -- which is what to hold up against
                Padre or Solscan when a number looks wrong.

Exit code 0 when the sample reached the start of the token's history, 1 when it
was cut short (the ranking is then a window, not a verdict), 2 on a setup error.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from rpc_config import env_rpc_urls  # noqa: E402
from token_intelligence import (  # noqa: E402
    EVM_TRADER_PAGES,
    SOLANA_TRADER_PAGES,
    TRADER_BUDGET_SECONDS,
    TokenIntelligenceClient,
    TokenIntelligenceError,
)
from token_traders import RANK_KEYS, TokenTrader, rank_traders  # noqa: E402


def _usd(value) -> str:
    if value is None:
        return "—"
    number = float(value)
    sign = "-" if number < 0 else "+"
    return f"{sign}${abs(number):,.2f}"


def _plain_usd(value) -> str:
    return "—" if value is None else f"${float(value):,.2f}"


def _tokens(value) -> str:
    number = float(value)
    for cutoff, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= cutoff:
            return f"{number / cutoff:,.2f}{suffix}"
    return f"{number:,.2f}"


def _pct(value) -> str:
    return "—" if value is None else f"{float(value):+,.1f}%"


def _stamp(value) -> str:
    if not value:
        return "—"
    from datetime import datetime, timezone

    return f"{datetime.fromtimestamp(value, tz=timezone.utc):%d %b %H:%M}"


def _flags(trader: TokenTrader) -> str:
    marks = []
    if trader.open_tokens > 0 and not trader.realized_only:
        marks.append("open")
    if trader.free_tokens > 0:
        marks.append("free-in")
    if trader.untracked_sold > 0:
        marks.append("pre-sample-sells")
    if trader.unpriced_buy_tokens > 0 or trader.unpriced_sell_tokens > 0:
        marks.append("unpriced")
    return ",".join(marks) or "-"


async def run(args: argparse.Namespace) -> int:
    try:
        import httpx
    except ImportError:
        print("httpx is required: pip install httpx", file=sys.stderr)
        return 2

    solana = env_rpc_urls("SOLANA_RPC", "SOLANA_RPC_FALLBACKS")
    if args.chain == "Solana" and not solana:
        print("SOLANA_RPC is not configured; nothing to page.", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(timeout=60) as http:
        client = TokenIntelligenceClient(http, solana)
        started = time.monotonic()
        try:
            token = await client.lookup(args.mint, limit=1)
        except TokenIntelligenceError as exc:
            print(f"token lookup failed: {exc}", file=sys.stderr)
            return 2
        chain = args.chain or token.chain
        print(f"token     ${token.symbol} · {chain} · {args.mint}")
        print(f"market    cap {_plain_usd(token.market_cap)} · "
              f"price {token.price_usd}")
        print(f"budget    {SOLANA_TRADER_PAGES} Solana pages / "
              f"{EVM_TRADER_PAGES} EVM pages / {TRADER_BUDGET_SECONDS}s")
        print()

        meta = await client.top_traders(
            args.mint, chain, limit=args.top, price_usd=token.price_usd,
        )
        elapsed = time.monotonic() - started
        # The same flows the ranking was built from, so a wallet's individual
        # trades cost no second pass over the history.
        sample = list(client.last_sample)

    print()
    print(f"source    {meta.source}")
    print(f"coverage  {meta.transactions:,} transactions · "
          f"{_stamp(meta.earliest)} → {_stamp(meta.latest)} · "
          f"{'CUT SHORT' if meta.truncated else 'reached the start'}")
    print(f"pricing   {meta.priced} of {len(meta.traders)} ranked wallets "
          f"carry a PnL")
    print(f"elapsed   {elapsed:.1f}s")
    if meta.truncated:
        print()
        print("  ! The sample did not reach this token's first transaction, so")
        print("    wallets that bought before it are missing or under-counted.")
        print("    Raise TOKEN_TRADER_SOLANA_PAGES / TOKEN_TRADER_BUDGET_SECONDS")
        print("    and run again before comparing against a full-history tool.")

    ranked = rank_traders(meta.traders, key=args.rank, limit=args.top)
    print()
    print(f"top {len(ranked)} by {args.rank}")
    print(f"{'#':>3}  {'wallet':<46} {'entry':>12} {'PnL':>14} "
          f"{'ROI':>10}  {'txs':>4}  flags")
    for position, trader in enumerate(ranked, 1):
        print(
            f"{position:>3}  {trader.address:<46} "
            f"{_plain_usd(trader.avg_entry_price):>12} "
            f"{_usd(trader.total_pnl_usd):>14} {_pct(trader.roi_pct):>10}  "
            f"{trader.transactions:>4}  {_flags(trader)}"
        )

    if args.wallet:
        wanted = args.wallet.strip()
        match = next(
            (trader for trader in meta.traders
             if trader.address.casefold() == wanted.casefold()), None,
        )
        print()
        if not match:
            print(f"{wanted} is not in the sample at all. Either it never "
                  f"traded this token in the window, or the window is too "
                  f"short — check the coverage line above.")
        else:
            print(f"ledger for {match.address}")
            print(f"  bought        {_tokens(match.bought)} tokens "
                  f"over {match.buys} buy(s)")
            print(f"  sold          {_tokens(match.sold)} tokens "
                  f"over {match.sells} sell(s)")
            print(f"  invested      {_plain_usd(match.invested_usd)}")
            print(f"  proceeds      {_plain_usd(match.proceeds_usd)}")
            print(f"  avg entry     {_plain_usd(match.avg_entry_price)}")
            print(f"  avg exit      {_plain_usd(match.avg_exit_price)}")
            print(f"  realised      {_usd(match.realized_pnl_usd)}")
            print(f"  unrealised    {_usd(match.unrealized_pnl_usd)}")
            print(f"  total PnL     {_usd(match.total_pnl_usd)}")
            print(f"  ROI           {_pct(match.roi_pct)}")
            print(f"  still holding {_tokens(match.open_tokens)} tokens "
                  f"(cost {_plain_usd(match.open_cost_usd)})")
            print(f"  free in       {_tokens(match.free_tokens)} tokens")
            print(f"  unpriced      {_tokens(match.unpriced_buy_tokens)} in / "
                  f"{_tokens(match.unpriced_sell_tokens)} out")
            print(f"  sold from before the sample: "
                  f"{_tokens(match.untracked_sold)} tokens")
            if match.untracked_sold > 0:
                print("  ^ this is the usual reason a PnL reads lower than a "
                      "full-history tool's")
            trades = [
                flow for flow in sample
                if flow.address.casefold() == wanted.casefold()
            ]
            trades.sort(key=lambda flow: flow.timestamp or 0)
            if trades:
                print()
                print(f"  {'when':<14} {'side':<5} {'tokens':>14} "
                      f"{'value':>14}  transaction")
                for flow in trades:
                    side = "buy" if flow.delta > 0 else "sell"
                    value = (
                        "free" if flow.value_usd == 0
                        else _plain_usd(flow.value_usd)
                    )
                    print(f"  {_stamp(flow.timestamp):<14} {side:<5} "
                          f"{_tokens(abs(flow.delta)):>14} {value:>14}  "
                          f"{flow.reference or '—'}")

    if args.csv:
        path = Path(args.csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "rank", "address", "avg_entry_price", "invested_usd",
                "proceeds_usd", "realized_pnl_usd", "unrealized_pnl_usd",
                "total_pnl_usd", "roi_pct", "bought", "sold", "open_tokens",
                "free_tokens", "untracked_sold", "transactions", "flags",
            ])
            for position, trader in enumerate(ranked, 1):
                writer.writerow([
                    position, trader.address, trader.avg_entry_price,
                    trader.invested_usd, trader.proceeds_usd,
                    trader.realized_pnl_usd, trader.unrealized_pnl_usd,
                    trader.total_pnl_usd, trader.roi_pct, trader.bought,
                    trader.sold, trader.open_tokens, trader.free_tokens,
                    trader.untracked_sold, trader.transactions, _flags(trader),
                ])
        print()
        print(f"wrote {path}")

    return 1 if meta.truncated else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explain /token's Top Traders sample, pricing and ledger",
    )
    parser.add_argument("mint", help="token mint or contract address")
    parser.add_argument("--chain", default="", help="override the detected chain")
    parser.add_argument("--top", type=int, default=25, help="rows to print")
    parser.add_argument("--rank", default="pnl", choices=RANK_KEYS)
    parser.add_argument("--wallet", default="", help="dump one wallet's ledger")
    parser.add_argument("--csv", default="", help="write the ranking to a file")
    parser.add_argument("--pages", type=int, default=0,
                        help="override TOKEN_TRADER_SOLANA_PAGES for this run")
    parser.add_argument("--budget", type=int, default=0,
                        help="override TOKEN_TRADER_BUDGET_SECONDS (seconds)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # The budget lives in module constants read at import; overriding them here
    # keeps one source of truth for the defaults while letting a run go deeper.
    import token_intelligence

    if args.pages:
        token_intelligence.SOLANA_TRADER_PAGES = args.pages
    if args.budget:
        token_intelligence.TRADER_BUDGET_SECONDS = args.budget
    if not args.chain:
        args.chain = ""

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
