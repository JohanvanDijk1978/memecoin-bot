"""A coin that migrated to a new pool today is as old as its mint, not its pool.

Run: PYTHONIOENCODING=utf-8 fomo/.venv/Scripts/python tools/test_dex_watcher_age.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import dex_watcher as dw  # noqa: E402

NOW = int(time.time())
MINT_TS = NOW - 400 * 86400          # mint's first transaction
POOL_MS = (NOW - 1800) * 1000        # PumpSwap pool, 30 minutes old


class _Resp:
    status = 200

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """Dexscreener: a fresh pool plus a bonding-curve pair with no timestamp.
    RPC: two pages of signatures, the second one short."""

    def __init__(self):
        self.rpc_calls = []

    def get(self, url, **kw):
        return _Resp({"pairs": [
            {"baseToken": {"symbol": "OLD"}, "liquidity": {"usd": 17000},
             "pairCreatedAt": POOL_MS},
            {"baseToken": {"symbol": "OLD"}},
        ]})

    def post(self, url, json=None, **kw):
        opts = json["params"][1]
        self.rpc_calls.append(opts)
        if "before" not in opts:
            rows = [{"signature": f"s{i}", "blockTime": NOW - i} for i in range(1000)]
        else:
            assert opts["before"] == "s999"
            rows = [{"signature": "first", "blockTime": MINT_TS}]
        return _Resp({"result": rows})


async def main():
    async def no_wait():
        return None
    dw.dex_wait = no_wait

    s = _Session()
    market = await dw._fetch_pair_data(s, "MintOld")
    assert market["pair_created_ms"] == MINT_TS * 1000, market
    assert dw._age_hours(market["pair_created_ms"]) > dw.MIN_AGE_HOURS
    assert len(s.rpc_calls) == 2

    # cached: a second look costs no RPC
    await dw._fetch_pair_data(s, "MintOld")
    assert len(s.rpc_calls) == 2

    # pairs already clear the gate: the chain is never asked
    s2 = _Session()
    s2.get = lambda url, **kw: _Resp({"pairs": [
        {"baseToken": {"symbol": "X"}, "liquidity": {"usd": 1},
         "pairCreatedAt": (NOW - 72 * 3600) * 1000}]})
    await dw._fetch_pair_data(s2, "MintClear")
    assert s2.rpc_calls == []
    print("ok")


asyncio.run(main())
