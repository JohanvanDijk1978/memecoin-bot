#!/usr/bin/env python3
"""
Offline test for the multi-wallet buy watcher.

    python3 tools/test_multiwallet.py

Runs the REAL parsers, the REAL rule engine and the REAL message formatter
against transactions built by hand, with the network replaced at three points
only: Dexscreener metadata, the native price quote, and the Telegram send. It
needs no RPC, no API key and no bot token, so it can run anywhere — including a
sandbox that cannot reach a crypto API.

It covers the things that would be embarrassing in production:
  * an airdrop, a transfer in, a sell and a failed transaction are NOT buys
  * a wallet buying five times is still one wallet toward the threshold
  * the third wallet posts once, the fourth posts once, and re-processing every
    transaction again (a restart, or a reconcile sweep) posts nothing
  * a buy older than the window does not count
  * the ceiling stops a hot token, and the cooldown lets it start again
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("MULTIWALLET_DB", str(Path(tempfile.mkdtemp()) / "multiwallet.db"))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("MULTIWALLET_CHANNEL_ID", "-1001234567890")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import multiwallet as MW                 # noqa: E402
from src import multiwallet_sources as S          # noqa: E402
from src import multiwallet_store as store        # noqa: E402

MINT = "B7q2X2uMrft6VaVJMcRy7Zoia9tpxHgWC3qgiak8pump"
WALLETS = {
    "rowdy":      "6g7NphUCPN8965YrbZDTQyaMoEnKe817Q8DmKE7516rX",
    "ProfitPUMP": "DxjmHXm1p7cs8Tezdf2EJm5xnwp9QC2E8CbfD1aXeH1j",
    "RowdyFOMO":  "EaVboaPxFCYanjoNWdkxTbPvt57nhXGu5i6m9m6ZS2kK",
    "FrankFOMO":  "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "hexiecs":    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
}
EVM_WALLET = "0x1111111111111111111111111111111111111111"
EVM_TOKEN = "0x2222222222222222222222222222222222222222"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

PASS, FAIL = "  ✅", "  ❌"
_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        _failures.append(label)


# ── fixtures ──────────────────────────────────────────────────────────────
def sol_tx(signature: str, wallet: str, mint: str, tokens_in: float,
           sol_out: float, ts: float, err=None, fee: int = 5000) -> dict:
    """A swap as Solana reports it: balances before and after, nothing else.

    `sol_out` negative means SOL came IN, which is how a sell looks.
    """
    pre_lamports = 2_000_000_000
    post_lamports = int(pre_lamports - sol_out * 1e9 - fee)
    pre_tokens = 1000.0 if tokens_in < 0 else 0.0
    post_tokens = max(pre_tokens + tokens_in, 0.0)
    return {
        "blockTime": int(ts),
        "meta": {
            "err": err, "fee": fee,
            "preBalances": [pre_lamports, 1_000_000],
            "postBalances": [post_lamports, 1_000_000],
            "preTokenBalances": [{"owner": wallet, "mint": mint,
                                  "uiTokenAmount": {"uiAmountString": str(pre_tokens)}}],
            "postTokenBalances": [{"owner": wallet, "mint": mint,
                                   "uiTokenAmount": {"uiAmountString": str(post_tokens)}}],
        },
        "transaction": {
            "message": {"accountKeys": [{"pubkey": wallet}, {"pubkey": "11111111111111111111111111111111"}]},
            "signatures": [signature],
        },
    }


def evm_receipt(tx_hash: str, transfers: list[tuple[str, str, str, int]],
                status: str = "0x1", block: int = 21_000_000) -> dict:
    """transfers: (token, from, to, raw amount)."""
    return {
        "transactionHash": tx_hash, "status": status, "blockNumber": hex(block),
        "logs": [{
            "address": token,
            "topics": [S.TRANSFER_TOPIC, S.pad_address(sender), S.pad_address(receiver)],
            "data": hex(raw),
        } for token, sender, receiver, raw in transfers],
    }


# ── parser tests ──────────────────────────────────────────────────────────
def test_solana_parser() -> None:
    print("\nSolana parser")
    now = time.time()
    wallet = WALLETS["rowdy"]

    buys = S.parse_solana_buys(sol_tx("sig-buy", wallet, MINT, 12_740_000, 0.5, now), wallet)
    check("a swap is a buy", len(buys) == 1 and buys[0]["amount"] == 12_740_000,
          f"{buys[0]['amount']:,.0f} tokens for {buys[0]['quote_amt']:.4f} SOL" if buys else "nothing")

    airdrop = sol_tx("sig-air", wallet, MINT, 5_000_000, 0.0, now, fee=0)
    check("an airdrop is not a buy", S.parse_solana_buys(airdrop, wallet) == [])

    sell = sol_tx("sig-sell", wallet, MINT, -500.0, -0.4, now)
    check("a sell is not a buy", S.parse_solana_buys(sell, wallet) == [])

    failed = sol_tx("sig-fail", wallet, MINT, 12_740_000, 0.5, now, err={"InstructionError": []})
    check("a failed swap is not a buy", S.parse_solana_buys(failed, wallet) == [])

    other = S.parse_solana_buys(sol_tx("sig-buy", wallet, MINT, 12_740_000, 0.5, now),
                                WALLETS["ProfitPUMP"])
    check("another wallet's swap is not attributed", other == [])


def test_evm_parser() -> None:
    print("\nEVM parser")
    wallets = {EVM_WALLET}
    decimals = {EVM_TOKEN: 18}

    swap = evm_receipt("0xaaa", [
        (WETH, EVM_WALLET, "0x9999999999999999999999999999999999999999", 10**17),
        (EVM_TOKEN, "0x9999999999999999999999999999999999999999", EVM_WALLET, 5 * 10**18),
    ])
    buys = S.parse_evm_buys(swap, {"from": EVM_WALLET, "value": "0x0"}, wallets,
                            "ethereum", decimals)
    check("WETH out + token in is a buy",
          len(buys) == 1 and abs(buys[0]["amount"] - 5) < 1e-9 and buys[0]["quote_sym"] == "WETH")

    native = evm_receipt("0xbbb", [
        (EVM_TOKEN, "0x9999999999999999999999999999999999999999", EVM_WALLET, 3 * 10**18),
    ])
    buys = S.parse_evm_buys(native, {"from": EVM_WALLET, "value": hex(2 * 10**17)},
                            wallets, "base", decimals)
    check("native ETH out + token in is a buy",
          len(buys) == 1 and buys[0]["quote_sym"] == "ETH" and abs(buys[0]["quote_amt"] - 0.2) < 1e-9)

    airdrop = evm_receipt("0xccc", [
        (EVM_TOKEN, "0x9999999999999999999999999999999999999999", EVM_WALLET, 10**21),
    ])
    check("an EVM airdrop is not a buy",
          S.parse_evm_buys(airdrop, {"from": "0x9999999999999999999999999999999999999999",
                                     "value": "0x0"}, wallets, "ethereum", decimals) == [])

    reverted = evm_receipt("0xddd", [
        (WETH, EVM_WALLET, "0x9999999999999999999999999999999999999999", 10**17),
        (EVM_TOKEN, "0x9999999999999999999999999999999999999999", EVM_WALLET, 5 * 10**18),
    ], status="0x0")
    check("a reverted tx is not a buy",
          S.parse_evm_buys(reverted, {"from": EVM_WALLET, "value": "0x0"}, wallets,
                           "ethereum", decimals) == [])

    unknown = S.parse_evm_buys(swap, {"from": EVM_WALLET, "value": "0x0"}, wallets,
                               "ethereum", {})
    check("unknown decimals still detects the buy, amount filled in later",
          len(unknown) == 1 and unknown[0]["amount"] == 0 and unknown[0]["raw_amount"] == 5 * 10**18)


# ── engine tests ──────────────────────────────────────────────────────────
SENT: list[dict] = []


async def fake_send(session, text: str, image_url: str = ""):
    SENT.append({"text": text, "image": image_url})
    return 1000 + len(SENT)


async def fake_token(session, chain: str, address: str, max_age: float = 0):
    return {"symbol": "feesh", "name": "feesh", "image": "https://example.invalid/banner.png",
            "price": 0.0001294, "mcap": 129_460.0, "supply": 1_000_000_000.0,
            "liq": 42_000.0, "links": {"website": "https://feesh.invalid",
                                       "twitter": "https://x.com/feesh"}}


async def fake_native(session, symbol: str) -> float:
    return 1.0 if symbol.upper().startswith("USD") else 200.0


async def feed(engine, name: str, signature: str, tokens: float, sol: float,
               ts: float | None = None) -> None:
    """Push one Solana buy through the real path: parse → store → evaluate."""
    wallet = WALLETS[name]
    tx = sol_tx(signature, wallet, MINT, tokens, sol, ts or time.time())
    for buy in S.parse_solana_buys(tx, wallet):
        await engine.on_buy(buy)


async def test_engine() -> None:
    print("\nRule engine")
    MW.send_alert, MW.fetch_token, MW.native_price = fake_send, fake_token, fake_native
    store.init_schema()
    for name, address in WALLETS.items():
        store.add_wallet(address, name)
    store.set_rule(min_wallets=3, window_min=120, max_wallets=6, cooldown_h=24)

    engine = MW.Watcher(session=None)
    now = time.time()

    await feed(engine, "rowdy", "sig-1", 12_740_000, 0.6, now - 1800)
    await feed(engine, "ProfitPUMP", "sig-2", 15_690_000, 0.8, now - 1200)
    check("two wallets are not enough", len(SENT) == 0)

    await feed(engine, "rowdy", "sig-1b", 3_000_000, 0.2, now - 900)
    check("the same wallet buying again is still one wallet", len(SENT) == 0)

    await feed(engine, "RowdyFOMO", "sig-3", 15_120_000, 0.7, now - 600)
    check("the third wallet fires the alert", len(SENT) == 1)
    check("the alert says 3 wallets", "3 wallets bought feesh" in SENT[0]["text"],
          SENT[0]["text"].splitlines()[0] if SENT else "")
    check("a repeat buyer is one line, marked ×2",
          SENT[0]["text"].count("\n• ") == 3 and "×2" in SENT[0]["text"])

    await feed(engine, "RowdyFOMO", "sig-3b", 1_000_000, 0.1, now - 300)
    check("a fourth buy from a counted wallet is silent", len(SENT) == 1)

    await feed(engine, "FrankFOMO", "sig-4", 39_940, 0.05, now - 120)
    check("the fourth wallet posts its own alert", len(SENT) == 2)
    check("that alert says 4 wallets", "4 wallets bought feesh" in SENT[1]["text"])

    # a restart: same transactions arrive again through the reconcile sweep
    replay = MW.Watcher(session=None)
    await feed(replay, "rowdy", "sig-1", 12_740_000, 0.6, now - 1800)
    await feed(replay, "RowdyFOMO", "sig-3", 15_120_000, 0.7, now - 600)
    await feed(replay, "FrankFOMO", "sig-4", 39_940, 0.05, now - 120)
    check("replaying every transaction after a restart sends nothing", len(SENT) == 2)

    # a buy older than the window must not count toward a new milestone
    await feed(engine, "hexiecs", "sig-old", 831_160, 0.3, now - 3 * 3600)
    check("a buy outside the window does not count", len(SENT) == 2)

    # ceiling and cooldown
    store.set_rule(max_wallets=4)
    await feed(engine, "hexiecs", "sig-5", 831_160, 0.3, now - 60)
    check("above the ceiling the token goes quiet", len(SENT) == 2)

    store.record_alert(store.DEFAULT_LIST, "solana", MINT, 4, 1)
    with store.db() as c:                       # pretend the cooldown has passed
        c.execute("UPDATE mw_alerts SET last_at=? WHERE token=?",
                  (time.time() - 25 * 3600, store.normalize(MINT)))
    await feed(engine, "hexiecs", "sig-6", 500_000, 0.2, now - 30)
    check("after the cooldown the token may alert again", len(SENT) == 3)

    print("\nMessage as the channel would receive it")
    print("─" * 62)
    print(SENT[0]["text"])
    print("─" * 62)


def main() -> int:
    test_solana_parser()
    test_evm_parser()
    asyncio.run(test_engine())
    print()
    if _failures:
        print(f"❌ {len(_failures)} check(s) failed: " + ", ".join(_failures))
        return 1
    print("✅ every check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
