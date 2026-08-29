#!/usr/bin/env python3
"""
Verify the multi-wallet buy watcher's plumbing from the machine that runs it.

    python3 tools/diag_multiwallet.py                 # endpoints + sockets + Telegram
    python3 tools/diag_multiwallet.py <wallet>        # replay that wallet's recent txs
    python3 tools/diag_multiwallet.py <wallet> 50     # …looking further back

It answers, for THIS machine and THESE keys:

  * which RPC and websocket URL each chain resolved to, and from which env var
    (host only — a key in a path is never printed)
  * does the Solana websocket accept a logsSubscribe, and does each EVM chain
    accept an eth_subscribe on the Transfer topic
  * can the bot reach the multi-wallet channel it is configured to post in
  * for a given wallet: what its recent transactions look like through the same
    parser the watcher uses — which were buys, and why the others were not

Nothing here posts to Telegram or writes a buy. It is safe to run on the VPS
while the bot is live; `--send` is the only exception and it is opt-in.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp                                    # noqa: E402
from src import multiwallet as MW                 # noqa: E402
from src import multiwallet_sources as S          # noqa: E402
from src import multiwallet_store as store        # noqa: E402


def line(label: str, value: str) -> None:
    print(f"  {label:<28} {value}")


def which_env(*names: str) -> str:
    for name in names:
        if os.getenv(name, "").strip():
            return f"[{name}]"
    return "[public default]"


async def probe_solana_socket(session: aiohttp.ClientSession, wallet: str) -> str:
    url = S.solana_wss()
    if not url:
        return "no SOLANA_WSS and no derivable RPC — sweep-only mode"
    try:
        async with session.ws_connect(url, timeout=aiohttp.ClientTimeout(total=15),
                                      heartbeat=30) as ws:
            await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                "params": [{"mentions": [wallet]},
                                           {"commitment": "confirmed"}]})
            raw = await asyncio.wait_for(ws.receive(), timeout=15)
            data = json.loads(raw.data) if raw.type is aiohttp.WSMsgType.TEXT else {}
            if isinstance(data.get("result"), int):
                return f"OK — subscription {data['result']}"
            return f"refused: {json.dumps(data)[:160]}"
    except Exception as e:
        return f"failed: {e!r}"


async def probe_evm_socket(session: aiohttp.ClientSession, chain: str, wallet: str) -> str:
    url = S.evm_wss(chain)
    if not url:
        return "no websocket URL — sweep-only mode"
    try:
        async with session.ws_connect(url, timeout=aiohttp.ClientTimeout(total=15),
                                      heartbeat=30) as ws:
            await ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                                "params": ["logs", {"topics": [
                                    S.TRANSFER_TOPIC, None, [S.pad_address(wallet)]]}]})
            raw = await asyncio.wait_for(ws.receive(), timeout=15)
            data = json.loads(raw.data) if raw.type is aiohttp.WSMsgType.TEXT else {}
            if data.get("result"):
                return f"OK — subscription {str(data['result'])[:14]}"
            return f"refused: {json.dumps(data)[:160]}"
    except Exception as e:
        return f"failed: {e!r}"


async def probe_telegram(session: aiohttp.ClientSession) -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = MW.channel_id()
    if not token:
        return "no TELEGRAM_BOT_TOKEN"
    if not chat:
        return "no MULTIWALLET_CHANNEL_ID and no YOUR_TELEGRAM_USER_ID"
    try:
        async with session.post(MW.TELEGRAM_API.format(token=token, method="getChat"),
                                json={"chat_id": chat},
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
        if data.get("ok"):
            result = data["result"]
            title = result.get("title") or result.get("username") or result.get("first_name")
            return f"OK — {result.get('type')} “{title}” ({chat})"
        return f"cannot reach {chat}: {data.get('description')}"
    except Exception as e:
        return f"failed: {e!r}"


async def replay_wallet(session: aiohttp.ClientSession, wallet: str, limit: int) -> None:
    """Run a wallet's recent history through the real parser and show the
    verdict on each transaction — the fastest way to tell 'detection is broken'
    from 'this wallet has not bought anything'."""
    kind = store.wallet_kind(wallet)
    print(f"\n▶ Replaying {limit} recent transactions for {wallet} ({kind or 'unknown'})")
    if kind != "sol":
        print("  EVM replay is not implemented — use the sweep instead:")
        print("  the watcher's eth_getLogs sweep covers the last "
              f"{S.EVM_LOG_SPAN} blocks on every start.")
        return

    rpc = S.Rpc(session, S.solana_rpcs(), "solana")
    rows = await rpc.call("getSignaturesForAddress",
                          [wallet, {"limit": limit, "commitment": "confirmed"}])
    if not rows:
        print("  no signatures returned — wrong address, or the RPC refused")
        return
    buys = 0
    for row in rows:
        signature = row.get("signature") or ""
        if row.get("err"):
            print(f"  {signature[:18]}…  failed tx, skipped")
            continue
        tx = await rpc.call("getTransaction", [signature, {
            "encoding": "jsonParsed", "commitment": "confirmed",
            "maxSupportedTransactionVersion": 0}])
        if not tx:
            print(f"  {signature[:18]}…  could not fetch")
            continue
        found = S.parse_solana_buys(tx, wallet)
        if not found:
            print(f"  {signature[:18]}…  not a buy (no token in, or nothing paid out)")
            continue
        buys += 1
        buy = found[0]
        stamp = time.strftime("%H:%M:%S UTC", time.gmtime(buy["ts"]))
        print(f"  {signature[:18]}…  BUY {MW.fmt_amount(buy['amount'])} of "
              f"{buy['token'][:8]}… for {buy['quote_amt']:.4f} {buy['quote_sym']}  {stamp}")
    print(f"\n  {buys} buy(s) in the last {len(rows)} transactions")


async def main() -> int:
    wallet_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    print("\n🪙 Multi-wallet watcher diagnostics\n")
    print("Configuration")
    rule = store.get_rule()
    line("rule", f"≥{rule['min_wallets']} wallets in {rule['window_min']} min · "
                 f"milestones to {rule['max_wallets']} · {rule['cooldown_h']}h cooldown")
    line("database", f"{store.DB_FILE} "
                     f"({'exists' if os.path.exists(store.DB_FILE) else 'will be created'})")
    wallets = store.list_wallets()
    line("wallets monitored", f"{len(wallets)} "
                              f"({sum(1 for w in wallets if w['kind'] == 'sol')} sol / "
                              f"{sum(1 for w in wallets if w['kind'] == 'evm')} evm)")

    print("\nEndpoints")
    for row in S.endpoint_report():
        source = (which_env("SOLANA_RPC") if row["chain"] == "solana"
                  else which_env(f"MULTIWALLET_RPC_{row['chain'].upper()}",
                                 S._EVM_ENV.get(row["chain"], ("", ""))[0],
                                 f"EVM_RPC_{row['chain'].upper()}"))
        line(row["chain"], f"{row['rpc'] or '— none'} {source} · "
                           f"ws {row['wss'] or '— none'}")

    probe_wallet = (wallet_arg if store.wallet_kind(wallet_arg) == "sol"
                    else next((w["address"] for w in wallets if w["kind"] == "sol"),
                              "So11111111111111111111111111111111111111112"))
    probe_evm = next((w["address"] for w in wallets if w["kind"] == "evm"),
                     "0x0000000000000000000000000000000000000001")

    async with aiohttp.ClientSession() as session:
        print("\nWebsockets")
        line("solana", await probe_solana_socket(session, probe_wallet))
        for chain in S.evm_chains():
            line(chain, await probe_evm_socket(session, chain, probe_evm))

        print("\nTelegram")
        line("channel", await probe_telegram(session))

        if wallet_arg:
            await replay_wallet(session, wallet_arg, limit)

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
