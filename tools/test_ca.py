#!/usr/bin/env python3
"""
Offline test for EVM address classification and the CA re-check loop.

    python3 tools/test_ca.py

Runs the REAL classifier, the REAL message builder and the REAL re-check
scheduler, with the network replaced at three points only: the JSON-RPC
transport, the Dexscreener lookup and the Telegram send/edit. No RPC, no API
key, no bot token — it runs anywhere.

It covers the things that produced the bare pings in the first place:
  * an EOA on every chain is reported as a wallet, not as a contract
  * an EIP-7702 delegated account is a wallet even though it carries code
  * a contract that answers symbol()/decimals()/totalSupply() is a token, and
    its ticker reaches the alert without any indexer
  * a contract that answers none of them is neither
  * every RPC failing is "unknown", is not cached, and is re-asked
  * a token on BSC wins over the same address being an EOA on Ethereum
  * the enriched alert is byte-identical to the one the scrapers used to build
  * a bare ping carries the on-chain verdict instead of "Ξ EVM Contract"
  * a wallet is never queued for re-check; a pool-less token always is
  * the same CA bare in two groups is one lookup and two message edits
  * a re-check that succeeds edits the original message and backfills the
    market cap the history row was stored without
  * a re-check that never succeeds gives up instead of queuing forever
  * the scraper's own entry point wires all of that together
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("CA_PROBE_CHAINS", "ethereum,base,bsc")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import ca_enrich as E                    # noqa: E402
from src import ca_probe as P                     # noqa: E402
from src import mention_store                     # noqa: E402

# Never touch the real history file.
mention_store.HISTORY_FILE = str(Path(tempfile.mkdtemp()) / "ca_history.json")
store = mention_store.store
store._ca_history = {}

WALLET = "0xbee56417ac7a217fb720ff9a16b464949ec3174a"
TOKEN = "0x1111111111111111111111111111111111111111"
POOL = "0x2222222222222222222222222222222222222222"
MINT = "9XaVY3ugtSG3HLKGHh3uWZWJ8EF9CoAyTWdSxB8kUFB3"

PASS, FAIL = "  ✅", "  ❌"
_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        _failures.append(label)


# ── fake JSON-RPC transport ───────────────────────────────────────────────
def abi_string(text: str) -> str:
    """A string as `symbol()` really returns it: offset, length, padded data."""
    raw = text.encode()
    padded = raw + b"\x00" * ((32 - len(raw) % 32) % 32)
    return ("0x" + (32).to_bytes(32, "big").hex() + len(raw).to_bytes(32, "big").hex()
            + padded.hex())


def word(n: int) -> str:
    return "0x" + n.to_bytes(32, "big").hex()


ERC20_TOKEN_CALLS = {
    P.ERC20_SYMBOL: abi_string("WOJAK"),
    P.ERC20_NAME: abi_string("Wojak Coin"),
    P.ERC20_DECIMALS: word(18),
    P.ERC20_TOTAL_SUPPLY: word(1_000_000_000 * 10 ** 18),
}

# chain -> eth_getCode result (None = the RPC never answered)
CODE: dict[str, object] = {}
# chain -> {selector: eth_call result}
CALLS: dict[str, dict] = {}


class FakeRpc:
    """Stands in for multiwallet_sources.Rpc — same two methods ca_probe uses."""

    def __init__(self, session, urls, label: str = ""):
        self.chain = label.split(":")[-1]

    async def call(self, method: str, params: list, timeout: float = 0):
        if method == "eth_getCode":
            return CODE.get(self.chain)
        if method == "eth_call":
            return CALLS.get(self.chain, {}).get(params[0]["data"])
        return None


P.Rpc = FakeRpc


def scenario(code: dict, calls: dict = None) -> None:
    """Set what each chain answers, and drop any cached verdict."""
    CODE.clear(); CODE.update(code)
    CALLS.clear(); CALLS.update(calls or {})
    P._cache.clear()


# ── classification ────────────────────────────────────────────────────────
def test_classification() -> None:
    print("\nClassification")

    scenario({"ethereum": "0x", "base": "0x", "bsc": "0x"})
    verdict = asyncio.run(P.classify_evm(WALLET))
    check("an EOA on every chain is a wallet", verdict["kind"] == "wallet", verdict["kind"])
    check("the wallet is not reported as delegated", verdict["delegated"] is False)
    check("...and is not pinned to a chain it has never touched",
          verdict["chain"] == "", verdict["chain"] or "(none)")

    # The real answer BSC gave for the address in the screenshot.
    scenario({"ethereum": "0x", "base": "0x",
              "bsc": "0xef010063c0c19a282a1b52b07dd5a65b58948a07dae32b"})
    verdict = asyncio.run(P.classify_evm(WALLET))
    check("an EIP-7702 account is still a wallet", verdict["kind"] == "wallet", verdict["kind"])
    check("...flagged as delegated, on the right chain",
          verdict["delegated"] and verdict["chain"] == "bsc", verdict["chain"])

    scenario({"ethereum": "0x60806040", "base": "0x", "bsc": "0x"},
             {"ethereum": ERC20_TOKEN_CALLS})
    verdict = asyncio.run(P.classify_evm(TOKEN))
    check("an ERC-20 is a token", verdict["kind"] == "token", verdict["kind"])
    check("...with its ticker and name", verdict["symbol"] == "WOJAK"
          and verdict["name"] == "Wojak Coin", f"{verdict['symbol']} / {verdict['name']}")
    check("...and its supply in whole units", verdict["supply"] == 1_000_000_000,
          str(verdict["supply"]))

    scenario({"ethereum": "0x60806040", "base": "0x", "bsc": "0x"}, {"ethereum": {}})
    verdict = asyncio.run(P.classify_evm(POOL))
    check("code that answers nothing is a contract, not a token",
          verdict["kind"] == "contract", verdict["kind"])

    # Only symbol() answers — one signal is not enough to call it a token.
    scenario({"ethereum": "0x60806040", "base": "0x", "bsc": "0x"},
             {"ethereum": {P.ERC20_SYMBOL: abi_string("NOTATOKEN")}})
    verdict = asyncio.run(P.classify_evm(POOL))
    check("one ERC-20 signal alone is not a token", verdict["kind"] == "contract",
          verdict["kind"])

    # An EOA on Ethereum, a token on BSC: the token wins wherever it is.
    scenario({"ethereum": "0x", "base": "0x", "bsc": "0x60806040"},
             {"bsc": ERC20_TOKEN_CALLS})
    verdict = asyncio.run(P.classify_evm(TOKEN))
    check("a token on BSC beats an EOA on Ethereum",
          verdict["kind"] == "token" and verdict["chain"] == "bsc",
          f"{verdict['kind']} on {verdict['chain']}")

    scenario({})     # every RPC silent
    verdict = asyncio.run(P.classify_evm(TOKEN))
    check("no chain answering is 'unknown', never 'wallet'", verdict["kind"] == "unknown",
          verdict["kind"])
    check("...and an unknown verdict is not cached as fact",
          P._cache[TOKEN.lower()][0] - time.time() <= P._CACHE_TTL_UNKNOWN + 1)

    check("a malformed address is rejected without any RPC",
          asyncio.run(P.classify_evm("0xdeadbeef"))["kind"] == "unknown")

    scenario({"ethereum": "0x60806040", "base": "0x", "bsc": "0x"},
             {"ethereum": ERC20_TOKEN_CALLS})
    asyncio.run(P.classify_evm(TOKEN))
    CODE.clear()                                   # cache must answer now
    check("a settled verdict is cached", asyncio.run(P.classify_evm(TOKEN))["kind"] == "token")


# ── message building ──────────────────────────────────────────────────────
DEX_TOKEN = {
    "name": "Jake for Mayor", "symbol": "MAYOR", "price": 0.00001353,
    "volume_24h": 47047.59, "change_24h": -67.23, "market_cap": 11535.0,
    "url": "", "image_url": "https://example/img.png", "age": "30 minutes",
    "ath_mc": 43300.0, "ath_time": time.time() - 44 * 60,
    "chain_id": "solana", "dex_id": "pumpswap",
}


def test_messages() -> None:
    print("\nAlert body")
    store._ca_history = {}
    header = "👤 *kite0* in [Prosperity DAO](https://t.me/c/1/2)"

    msg, image = E.build_ca_message(header=header, address=MINT, chain="SOL",
                                    token=DEX_TOKEN, trading_links="[DexScreener](x)")
    expected = (
        f"{header}\n"
        "━━━━━━━━━━━━━━━\n"
        "🪙 *Jake for Mayor*  | *11.5K* | *$MAYOR*\n"
        "💊 Solana @ Pumpswap\n"
        "🕐 Age: 30 minutes\n"
        "💵 USD: `0.00001353`\n"
        "💎 FDV: *11.5K ⇨ 43.3K ATH[44m]*\n"
        "👥 *First scan!*\n"
        f"\n`{MINT}`\n"
        "\n🔗 [DexScreener](x)"
    )
    check("the enriched alert is unchanged", msg == expected,
          "" if msg == expected else repr(msg))
    check("...and still carries the token image", image == "https://example/img.png")

    bare, _ = E.build_ca_message(header=header, address=WALLET, chain="ETH", token={},
                                 trading_links="[DexScreener](x)",
                                 probe={"kind": "wallet", "chain": "bsc", "delegated": True,
                                        "symbol": "", "name": "", "supply": 0})
    check("a wallet is named as one", "Not a token" in bare and "delegated wallet" in bare)
    check("...on the chain it was found on", "BNB Chain" in bare, bare.splitlines()[2])
    check("...and never called a contract", "EVM Contract" not in bare)

    bare, _ = E.build_ca_message(header=header, address=TOKEN, chain="ETH", token={},
                                 trading_links="[DexScreener](x)",
                                 probe={"kind": "token", "chain": "base", "delegated": False,
                                        "symbol": "WOJAK", "name": "Wojak Coin",
                                        "supply": 1_000_000_000},
                                 pending_note=E.PENDING_NOTE)
    check("a pool-less token still shows its ticker", "*$WOJAK*" in bare and "Wojak Coin" in bare)
    check("...its supply, in units a person reads", "Supply: *1.0B*" in bare,
          bare.splitlines()[3])
    check("...and says the data is still coming", "re-checking" in bare)

    bare, _ = E.build_ca_message(header=header, address=MINT, chain="SOL", token={},
                                 trading_links="[DexScreener](x)")
    check("an unclassified Solana CA keeps the old wording", "◎ SOL Contract" in bare)

    # The deployer picks the name, so it is hostile input to a Markdown parse.
    bare, _ = E.build_ca_message(header=header, address=TOKEN, chain="ETH", token={},
                                 trading_links="[DexScreener](x)",
                                 probe={"kind": "token", "chain": "base", "delegated": False,
                                        "symbol": "A*B", "name": "[rug](evil)", "supply": 0})
    check("a hostile token name cannot break the Markdown",
          "\[rug\]" in bare and "A\*B" in bare, bare.splitlines()[2])


# ── re-check ──────────────────────────────────────────────────────────────
EDITS: list[dict] = []
LOOKUPS: list[str] = []
ANSWER: dict = {}


async def fake_fetch(address: str, chain: str) -> dict:
    LOOKUPS.append(address)
    return dict(ANSWER)


async def fake_edit(chat_id, message_id, text) -> bool:
    EDITS.append({"chat_id": chat_id, "message_id": message_id, "text": text})
    return True


def reset_recheck() -> None:
    EDITS.clear(); LOOKUPS.clear(); ANSWER.clear()
    E._pending.clear()
    store._ca_history = {}


async def test_recheck() -> None:
    print("\nRe-check")
    E.fetch_token_quick = fake_fetch
    E.edit_ping = fake_edit
    E.RECHECK_DELAYS = [0.0, 0.0]
    reset_recheck()

    header = "👤 *wailer__* in *Prosperity DAO*"
    check("a ping Telegram never confirmed is not queued",
          E.schedule_recheck(TOKEN, "ETH", header, None) is False)
    check("...nor one with no message id",
          E.schedule_recheck(TOKEN, "ETH", header, {"chat_id": -100}) is False)

    E.schedule_recheck(TOKEN, "ETH", header, {"chat_id": -100, "message_id": 11},
                       source="telegram", group_name="Prosperity DAO")
    E.schedule_recheck(TOKEN, "ETH", "👤 *Shinichi Kudo* in *xy*",
                       {"chat_id": -100, "message_id": 12}, source="telegram", group_name="xy")
    check("the same CA from two groups is one job", len(E._pending) == 1)
    check("...with a message waiting on each", len(E._pending[TOKEN]["targets"]) == 2)

    # Both groups recorded the call with no price, the way a bare ping does.
    for group in ("Prosperity DAO", "xy"):
        store.add_message(f"CA:{TOKEN}", source="telegram", group_name=group,
                          sender_name="wailer__", market_cap=0)
    check("the history rows start unpriced",
          all(e["market_cap"] == 0 for e in store._ca_history[TOKEN]))

    # First attempt: Dexscreener still has nothing.
    await E._attempt(TOKEN, E._pending[TOKEN])
    check("a failed re-check reschedules instead of dropping", TOKEN in E._pending)
    check("...and edits nothing", not EDITS)

    # Second attempt: it lists.
    ANSWER.update({**DEX_TOKEN, "name": "Wojak Coin", "symbol": "WOJAK",
                   "market_cap": 48000.0, "chain_id": "base", "dex_id": "uniswap",
                   "ath_mc": 0, "ath_time": 0})
    await E._attempt(TOKEN, E._pending[TOKEN])
    check("a successful re-check edits every waiting message", len(EDITS) == 2,
          f"{len(EDITS)} edit(s)")
    check("...with the market cap in it", all("48.0K" in e["text"] for e in EDITS))
    check("...keeping each message's own header",
          "wailer__" in EDITS[0]["text"] and "Shinichi Kudo" in EDITS[1]["text"])
    check("...and saying the data arrived late", "Market data arrived" in EDITS[0]["text"])
    check("...and dropping the pending footer", "re-checking" not in EDITS[0]["text"])
    check("the job is done and off the queue", TOKEN not in E._pending)
    check("the unpriced history rows were backfilled",
          all(e["market_cap"] == 48000.0 and e["first_mc"] == 48000.0
              for e in store._ca_history[TOKEN]), str(store._ca_history[TOKEN]))
    check("...without inflating the scan count",
          all(e["scan_count"] == 1 for e in store._ca_history[TOKEN]))

    # A real later scan must not be overwritten by a backfill.
    store.add_message(f"CA:{TOKEN}", source="telegram", group_name="third",
                      sender_name="someone", market_cap=90000.0)
    store.backfill_market_cap(TOKEN, "third", 1000.0)
    third = [e for e in store._ca_history[TOKEN] if e["group_name"] == "third"][0]
    check("a priced row is never overwritten", third["market_cap"] == 90000.0)

    # Never lists at all.
    reset_recheck()
    E.schedule_recheck(TOKEN, "ETH", header, {"chat_id": -100, "message_id": 13})
    for _ in range(len(E.RECHECK_DELAYS)):
        await E._attempt(TOKEN, E._pending[TOKEN])
    check("a CA that never lists is given up on", TOKEN not in E._pending)
    check("...after exactly one lookup per delay", len(LOOKUPS) == len(E.RECHECK_DELAYS),
          f"{len(LOOKUPS)} lookup(s)")

    # The loop itself, not just _attempt.
    reset_recheck()
    ANSWER.update({**DEX_TOKEN, "market_cap": 12000.0})
    E.RECHECK_TICK = 0.01
    E.schedule_recheck(TOKEN, "ETH", header, {"chat_id": -100, "message_id": 14})
    task = asyncio.create_task(E.run_recheck_loop())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not E._pending:
            break
    task.cancel()
    check("the loop picks the job up on its own", len(EDITS) == 1, f"{len(EDITS)} edit(s)")

    reset_recheck()
    E._pending.update({f"0x{i:040x}": {} for i in range(E.RECHECK_MAX_PENDING)})
    check("the queue refuses to grow without bound",
          E.schedule_recheck(TOKEN, "ETH", header, {"chat_id": -100, "message_id": 15}) is False)
    E._pending.clear()

    print("\nBare EVM ping as the channel would receive it")
    print("─" * 62)
    msg, _ = E.build_ca_message(
        header="👤 *lovka5354* in *Prosperity DAO*", address=WALLET, chain="ETH", token={},
        trading_links="[BasedBot](x) | [Padre](x) | [GMGN](x) | [DexScreener](x)",
        probe={"kind": "wallet", "chain": "bsc", "delegated": True,
               "symbol": "", "name": "", "supply": 0})
    print(msg)
    print("─" * 62)


# ── the scraper end to end ────────────────────────────────────────────────
async def test_scraper_path() -> None:
    """Drive the real handle_ca_ping, with only the network faked."""
    print("\nScraper path")
    from src import discord_scraper as DS

    reset_recheck()
    sent: list[dict] = []

    async def fake_send(text, image_url="", chat_id=""):
        sent.append({"text": text, "image": image_url})
        return {"chat_id": -100, "message_id": 100 + len(sent)}

    async def noop(*a, **kw):
        return None

    DS.fetch_token_quick = fake_fetch
    DS.send_ping = fake_send
    DS.maybe_forward = noop
    DS.notify_high_wr_scan = noop
    DS._recent_pings.clear()

    # A wallet: pinged, classified, never queued.
    async def wallet_probe(address, session=None):
        return {**P.blank(), "kind": "wallet", "chain": "bsc", "delegated": True}
    DS.classify_evm = wallet_probe
    await DS.handle_ca_ping(f"ape this {WALLET}", "lovka5354", "Prosperity DAO")
    check("a wallet still gets a ping", len(sent) == 1)
    check("...that says what it is", "Not a token" in sent[0]["text"])
    check("...with links for the chain it was found on",
          "/bsc/" in sent[0]["text"] or "bsc" in sent[0]["text"])
    check("...and is never queued for a re-check", not E._pending)

    # A token with no pool yet: pinged bare, queued, then enriched in place.
    async def token_probe(address, session=None):
        return {**P.blank(), "kind": "token", "chain": "base",
                "symbol": "WOJAK", "name": "Wojak Coin", "supply": 1_000_000_000}
    DS.classify_evm = token_probe
    await DS.handle_ca_ping(f"new one {TOKEN}", "wailer__", "xy")
    check("a pool-less token is queued", TOKEN in E._pending)
    check("...and its ping says so", "re-checking" in sent[1]["text"])

    ANSWER.update({**DEX_TOKEN, "name": "Wojak Coin", "symbol": "WOJAK",
                   "market_cap": 61000.0, "chain_id": "base", "dex_id": "uniswap",
                   "ath_mc": 0, "ath_time": 0})
    await E._attempt(TOKEN, E._pending[TOKEN])
    check("the queued ping is edited into a full alert", len(EDITS) == 1)
    check("...keeping the caller and group from the original",
          "wailer__" in EDITS[0]["text"] and "xy" in EDITS[0]["text"])
    check("...and now carrying the market cap", "61.0K" in EDITS[0]["text"])

    print("\nSame message, before and after the re-check")
    print("─" * 62)
    print(sent[1]["text"])
    print("─" * 62)
    print(EDITS[0]["text"])
    print("─" * 62)


def main() -> int:
    test_classification()
    test_messages()
    asyncio.run(test_recheck())
    asyncio.run(test_scraper_path())
    print()
    if _failures:
        print(f"❌ {len(_failures)} check(s) failed: " + ", ".join(_failures))
        return 1
    print("✅ every check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
