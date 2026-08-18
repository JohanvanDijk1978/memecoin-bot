"""Offline check of the new routes against synthetic RPC responses.
No network: a fake Rpc replays canned pages so the paging, ordering and
matching logic can be exercised without an endpoint."""
import asyncio, sys
import fomo_wallet as fw

SPONSOR = fw.FOMO_SPONSOR
TRADER  = "93fjdwW7S3Aw4TkrnMzy51sZ5pP4ArpvtmFYujNyDVgH"
MINT    = "DLYQBXkRo4111111111111111111111111111111111"
WHEN    = 1787065559
AMOUNT  = 5732942.956183

def tx(owner, amt, payer=SPONSOR, signer=TRADER):
    return {"transaction": {"message": {"accountKeys": [
                {"pubkey": payer, "signer": True, "writable": True},
                {"pubkey": signer, "signer": True, "writable": True},
                {"pubkey": "Pool11111111111111111111111111111111111111", "signer": False}]}},
            "meta": {"preTokenBalances": [{"mint": MINT, "owner": owner,
                        "uiTokenAmount": {"uiAmountString": "0"}}],
                     "postTokenBalances": [{"mint": MINT, "owner": owner,
                        "uiTokenAmount": {"uiAmountString": str(amt)}}]}}

class FakeRpc:
    """Sponsor history = 2 pages; the mint is 'too busy' (all pages newer)."""
    def __init__(self):
        self.calls = 0
        self.log = []
        self.sponsor_pages = [
            [{"signature": f"S{i}", "blockTime": WHEN + 300 - i, "err": None} for i in range(1000)],
            [{"signature": f"T{i}", "blockTime": WHEN - 700 - i, "err": None} for i in range(1000)],
        ]
    async def __call__(self, method, params):
        self.calls += 1; self.log.append((method, params[0]))
        if method == "getSignaturesForAddress":
            addr, opts = params[0], params[1]
            if addr == SPONSOR:
                page = 1 if "before" in opts else 0
                return self.sponsor_pages[page] if page < len(self.sponsor_pages) else []
            # a viral mint: every page still newer than the swap
            return [{"signature": f"M{i}", "blockTime": WHEN + 5000, "err": None} for i in range(1000)]
        raise RuntimeError("unexpected " + method)
    async def batch(self, method, param_sets):
        self.calls += 1
        out = []
        for p in param_sets:
            sig = p[0]
            # only one signature in the sponsor window is the real trade
            out.append(tx(TRADER, AMOUNT) if sig == "S301" else tx("Someone", 1.0))
        return out

async def main():
    global WHEN
    ok = True
    def check(name, cond):
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + name); ok = ok and cond

    print("derive_trader")
    w, how = fw.derive_trader(tx(TRADER, AMOUNT))
    check(f"trader is the non-fee-payer signer ({how})", w == TRADER)

    print("normalise_block_tx")
    entry = {"transaction": {"accountKeys": [{"pubkey": SPONSOR, "signer": True},
                                             {"pubkey": TRADER, "signer": True}],
                             "signatures": ["SIGX"]},
             "meta": {"preTokenBalances": [], "postTokenBalances": [
                 {"mint": MINT, "owner": TRADER, "uiTokenAmount": {"uiAmountString": str(AMOUNT)}}]}}
    n = fw.normalise_block_tx(entry)
    check("block tx reshapes for derive_trader", fw.derive_trader(n)[0] == TRADER)
    check("block tx reshapes for mint_delta",
          fw.mint_delta(n, MINT) == [(TRADER, AMOUNT)])

    print("pick_swaps")
    rows = [{"outTokenAddress": "AAA", "inTokenAddress": list(fw.QUOTES)[0],
             "outHumanAmount": "1", "createdAt": "x"} for _ in range(3)] + \
           [{"outTokenAddress": "BBB", "inTokenAddress": list(fw.QUOTES)[0],
             "outHumanAmount": "2", "createdAt": "x"},
            {"outTokenAddress": "CCC", "inTokenAddress": list(fw.QUOTES)[0],
             "outHumanAmount": "3", "createdAt": "x"}]
    picked = [r["outTokenAddress"] for r in fw.pick_swaps(rows, want=4)]
    check(f"spreads over distinct mints first: {picked}",
          picked[:3] == ["AAA", "BBB", "CCC"] and len(picked) == 4)

    print("SponsorIndex")
    rpc = FakeRpc()
    idx = fw.SponsorIndex(rpc, [SPONSOR])
    cands, covered = await idx.candidates(WHEN)
    check(f"pages back past the window and stops (covered={covered}, "
          f"scanned={idx.scanned})", covered and idx.scanned == 1000)
    check(f"{len(cands)} candidate(s) inside +/-{fw.TIME_WINDOW}s",
          all(abs(c["blockTime"] - WHEN) <= fw.TIME_WINDOW for c in cands) and cands)
    before = rpc.calls
    await idx.candidates(WHEN - 10)
    check("second lookup reuses the paged history (0 extra RPC)", rpc.calls == before)

    print("locate_swap -- the Rowdy case")
    rpc = FakeRpc()
    idx = fw.SponsorIndex(rpc, [SPONSOR])
    swap = {"outTokenAddress": MINT, "outHumanAmount": str(AMOUNT),
            "inTokenAddress": list(fw.QUOTES)[0],
            "createdAt": "2026-08-18T13:05:59.531Z"}
    # line the fixture up with the swap's real timestamp
    WHEN = fw.iso_epoch(swap["createdAt"])
    rpc.sponsor_pages = [
        [{"signature": f"S{i}", "blockTime": WHEN + 300 - i, "err": None} for i in range(1000)],
        [{"signature": f"T{i}", "blockTime": WHEN - 700 - i, "err": None} for i in range(1000)],
    ]
    sig, t, route = await fw.locate_swap(rpc, swap, idx, verbose=True)
    check(f"resolved via {route} in {rpc.calls} RPC call(s)", route == "sponsor" and sig == "S301")
    check("and the trader falls out of it", t and fw.derive_trader(t)[0] == TRADER)

    print("strict beats a loose decoy")
    class DecoyRpc(FakeRpc):
        """S300 is 1s nearer and credits the same amount to a POOL, not to any
        signer -- the loose out-amount test would take it. S301 is the real
        trade. Strict-before-loose has to prefer S301."""
        async def batch(self, method, param_sets):
            self.calls += 1
            out = []
            for p in param_sets:
                sig = p[0]
                if sig == "S300":
                    out.append(tx("Pool11111111111111111111111111111111111111", AMOUNT))
                elif sig == "S301":
                    out.append(tx(TRADER, AMOUNT))
                else:
                    out.append(tx("Someone", 1.0))
            return out
    rpc = DecoyRpc()
    rpc.sponsor_pages = [
        [{"signature": f"S{i}", "blockTime": WHEN + 300 - i, "err": None} for i in range(1000)],
    ]
    idx = fw.SponsorIndex(rpc, [SPONSOR])
    sig, t2, route = await fw.locate_swap(rpc, swap, idx, verbose=False)
    check(f"picked the signer's tx, not the pool's (got {sig})", sig == "S301")
    check("and it still names the trader", t2 and fw.derive_trader(t2)[0] == TRADER)

    print("\n" + ("ALL PASS" if ok else "FAILURES"))
    return 0 if ok else 1

sys.exit(asyncio.run(main()))
