#!/usr/bin/env python3
"""
Verify the data providers behind the Wallet Groups dashboard page, from the
machine that will actually run it.

    python3 tools/diag_wallet_groups.py <wallet> [<token>]
    python3 tools/diag_wallet_groups.py <sol-wallet> <mint>      # also test cost basis

It answers, for this machine and these keys:

  * which keys are visible, and which .env they came from
  * does the Solana RPC return this wallet's positions
  * which EVM provider answers — Etherscan's Pro balance endpoint, or the
    free watchlist scan over tokens the dashboard already knows
  * does Dexscreener price what the wallet holds
  * can Solscan reconstruct an average entry for one position

Nothing here writes to the dashboard's database.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import httpx                       # noqa: E402
import wallets as W                # noqa: E402  (imports load the .env files)

MASK = lambda v: f"{v[:6]}…{v[-4:]} ({len(v)} chars)" if v else "— not set"


def line(label: str, value: str) -> None:
    print(f"  {label:<26} {value}")


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    wallet = W.normalize_wallet(sys.argv[1])
    token = sys.argv[2] if len(sys.argv) > 2 else ""
    kind = W.wallet_kind(wallet)
    if not kind:
        print(f"'{wallet}' is not a Solana or EVM address")
        return 2

    print("\n── keys ──")
    line("SOLANA_RPC", W.rpc_display_name(os.getenv("SOLANA_RPC", ""))
         if os.getenv("SOLANA_RPC") else "— not set (public endpoint will be used)")
    line("SOLSCAN_API_KEY", MASK(os.getenv("SOLSCAN_API_KEY", "").strip()))
    line("ETHERSCAN_API_KEY", MASK(os.getenv("ETHERSCAN_API_KEY", "").strip()))
    line("EVM chains configured", ", ".join(W.evm_chains()) or "none")
    for chain in W.evm_chains():
        line(f"  rpc:{chain}", W.rpc_display_name(W.evm_rpc(chain)) or "— none")

    async with httpx.AsyncClient(headers={"User-Agent": "memedash-diag/1.0"},
                                 follow_redirects=True) as client:
        holdings: list[dict] = []

        if kind == "sol":
            print("\n── solana holdings ──")
            try:
                holdings = await W.sol_holdings(client, wallet)
                line("positions", str(len(holdings)))
            except Exception as e:
                line("FAILED", str(e)[:150])
        else:
            print("\n── evm holdings ──")
            # the watchlist the dashboard would use; here, whatever it knows
            watch: list[str] = []
            try:
                import sqlite3
                dbf = Path(__file__).resolve().parent.parent / "dashboard" / "data" / "dash.db"
                if dbf.exists():
                    con = sqlite3.connect(dbf)
                    watch = [r[0].lower() for r in con.execute(
                        "SELECT address FROM tokens WHERE address LIKE '0x%' AND dead=0")]
                    con.close()
            except Exception as e:
                line("watchlist", f"unreadable ({e})")
            line("watchlist size", f"{len(watch)} known EVM tokens")
            decimals: dict[str, int] = {}
            for chain in W.evm_chains():
                try:
                    rows, provider = await W.evm_holdings(client, wallet, chain, watch, decimals)
                    holdings += rows
                    line(chain, f"{len(rows)} positions via {provider}")
                except Exception as e:
                    line(chain, f"FAILED — {str(e)[:120]}")

        interesting = [h for h in holdings if not W.is_boring(h["address"])][:12]
        if interesting:
            print("\n── dexscreener ──")
            markets = await W.dex_markets(client, [h["address"] for h in interesting])
            line("priced", f"{len(markets)}/{len(interesting)}")
            for h in interesting[:8]:
                m = markets.get(h["address"])
                if not m:
                    print(f"    {h['address'][:12]:<14} {h['amount']:>18,.2f}  (no pair)")
                    continue
                supply = m["supply"]
                print(f"    ${m['symbol']:<8} {h['amount']:>16,.2f}  "
                      f"${h['amount'] * m['price']:>12,.2f}  "
                      f"{(h['amount'] / supply * 100) if supply else 0:>6.3f}% of supply")

        if token:
            print("\n── cost basis ──")
            held = next((h["amount"] for h in holdings if h["address"].lower() == token.lower()), 0)
            line("current amount", f"{held:,.4f}" if held else "0 (not held — history only)")
            chain = "solana" if not token.startswith("0x") else "ethereum"
            basis = await W.cost_basis(client, wallet, token, chain, held)
            if basis:
                line("average entry", f"${basis['avg_entry']:.10f}")
                line("cost basis", f"${basis['cost_usd']:,.2f}")
                line("realized", f"${basis['realized_usd']:,.2f}")
                line("source", f"{basis['source']} ({basis['trades']} trades read)")
            else:
                line("result", "no usable history — the page will fall back to the cost "
                               "basis it observes from now on")

    print("\n── provider status ──")
    for name, status in sorted(W.PROVIDER_STATUS.items()):
        line(name, ("ok   " if status["ok"] else "FAIL ") + status["note"])
    if not W.PROVIDER_STATUS:
        line("(none)", "nothing was called")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
