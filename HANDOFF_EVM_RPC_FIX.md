# Handoff: Wallet Groups — why EVM wallets showed nothing

**Date:** 2026-08-28
**Symptom:** Wallet Groups shows Solana cards. EVM wallets produce zero tokens. No error, no traceback.
**Status:** Root cause found and fixed in `dashboard/wallets.py`. The network half is unverified from here — see *Verify on the VPS*.

---

## What the first attempt got wrong

The v1.39 commit (`094bcea`) diagnosed this as an env-var naming mismatch: `wallets.py`
looked for `EVM_RPC_ETHEREUM`, `fomo/.env` supplies `ETH_RPC`, so `evm_rpc()` fell through
to the free public RPC. That part is **true**, and the fallback it added works — verified
offline, the three chains now resolve to the Alchemy URLs.

But it does not explain the symptom, and it could not have fixed it. Swapping a public RPC
for Alchemy changes **how fast** balances are read. It does not change **which tokens are
looked at** — and that was the actual failure. A rate-limited RPC would have produced slow
scans and partial results; it would not produce a clean, silent zero on every round.

Two further problems with that handoff, for the record:

- Its verification step told you to run `dashboard/verify_evm_rpc_fix.py`. That file was
  never written. Nothing was actually verified.
- Its troubleshooting step told you to `grep ETHERSCAN_API_KEY /etc/systemd/system/memedash.service`.
  The key does not live there — `load_env_files()` reads it out of `../.env`. That grep
  finds nothing even when everything is configured correctly.

---

## The actual root cause

`evm_holdings()` had exactly two providers, and on this box **neither one can discover a token**:

1. **Etherscan V2 `addresstokenbalance`** — a Pro-plan action. On a free key it answers NOTOK,
   the chain is added to `_etherscan_retired`, and it is never asked again. Correct behaviour,
   and it means this provider contributes nothing here.

2. **The watchlist scan** — batched `eth_call balanceOf` over `_evm_watchlists()`, which is
   built from `tokens`, `wgroup_tokens` and `wallet_holdings`. Every one of those is a record
   of **tokens the bot has already posted or already seen**.

So an EVM wallet could only ever be found holding a token that had already come through the
Telegram/Discord feed. Wallet Groups then requires **two wallets in the same group** to hold
the **same** such token before a card exists. That intersection is empty in practice, so the
page renders zero EVM tokens — and, because an empty balance sheet is a perfectly valid
answer, it does so without logging anything. That is the silent zero you saw.

Solana never had this problem: `sol_holdings()` calls `getTokenAccountsByOwner`, which returns
**everything** the wallet owns. The EVM side simply had no equivalent.

---

## The fix

Added a real EVM discovery provider — `alchemy_getTokenBalances`, which Alchemy answers on the
**same URL** already used for `eth_call`, on the free plan, 100 tokens per request with a
`pageKey` for the rest. This is what makes the previous commit's work pay off: the Alchemy URLs
it wired in are now used for something the public RPCs could never do.

New provider order in `evm_holdings()`:

1. Etherscan `addresstokenbalance` (unchanged — still wins when the key is entitled)
2. **`alchemy_balances()` — new. Every ERC-20 the wallet holds.**
3. Watchlist scan (unchanged — now genuinely a last resort)

Degradation is explicit. A plain RPC answers `-32601 method not found`; the chain goes into
`_alchemy_retired` on the first refusal and is never re-probed, exactly like the Etherscan
path. A network blip returns `None` **without** retiring, so a transient failure does not cost
you discovery for the life of the process. An empty result is treated as a real answer
("this wallet holds no ERC-20 here"), not as a reason to fall through.

Also in this change:

- `evm_rpc()` rewritten on top of a new `evm_rpc_source()`, which returns the *env key name*
  that supplied the URL. Same resolution order as before, but the two can no longer drift, and
  diagnostics can now say where a value came from without printing an API key.
- `tools/diag_wallet_groups.py` prints the source key per chain and probes the discovery
  provider on its own, before the fallback chain.

### Files changed

- `dashboard/wallets.py` — `alchemy_balances()`, `_alchemy_retired`, the branch in
  `evm_holdings()`, `evm_rpc_source()`, `evm_rpc()` rewritten, module docstring
- `tools/diag_wallet_groups.py` — RPC source key, per-chain discovery probe

---

## What was verified, and what was not

**Verified offline** (stubbed RPC client, real `evm_holdings` code path, 8 groups of assertions,
all passing):

- the three chains resolve to the Alchemy URLs, and `evm_rpc_source()` agrees with `evm_rpc()`
- Alchemy results are parsed and scaled correctly for mixed decimals (18 and 6), with zero
  and null balances dropped
- `pageKey` pagination merges both pages
- `-32601` retires the chain once and is never re-probed
- a network error falls back **without** retiring
- an empty wallet returns `("alchemy", [])` rather than falling through
- an entitled Etherscan key still takes precedence and Alchemy is not called
- the original bug reproduces: empty watchlist + no discovery = zero positions, no error

**Not verified — nobody has called Alchemy from this session.** There is no crypto-API egress
from either shell here. Specifically unconfirmed: that the key in `fomo/.env` is valid, that
Alchemy serves `alchemy_getTokenBalances` on your plan for all three chains, and that the VPS's
own `fomo/.env` even contains `ETH_RPC` / `BASE_RPC` / `BSC_RPC` — `.env` is gitignored, so the
file this was written against is the one on your laptop, not the one the dashboard actually reads.

---

## Verify on the VPS

```bash
cd /root/memecoin-bot-new
python3 tools/diag_wallet_groups.py 0xYOUR_EVM_WALLET
```

Read two blocks:

- **`── keys ──`** — each `rpc:<chain>` line ends in the env key that supplied it. `[ETH_RPC]`
  means fomo/.env is being read. `[public default — no key configured]` means the VPS's
  `fomo/.env` does **not** have these keys, and that is your problem — copy them over.
- **`── evm holdings ──`** — the `discover:<chain>` lines are the verdict.
  `alchemy_getTokenBalances OK — N ERC-20 positions` is a working fix.
  `unavailable` means Alchemy refused; the message is in `journalctl -u memedash -n 100 | grep alchemy`.

Then:

```bash
git push origin main          # webhook restarts memedash
journalctl -u memedash -f | grep evm_holdings
```

Expect `evm_holdings:ethereum ... alchemy · N positions`. `watchlist · …` means discovery is
still not running. The page's own note ("EVM wallets are scanned against tokens this dashboard
already knows") disappears on its own once Alchemy answers — if that banner is still there,
the fix is not live.

Remember EVM tokens still need **two wallets in one group** holding the same token before a
card appears. Discovery is necessary, not sufficient.

---

## Loose end, not fixed

`evm_chains()` now includes **robinhood**, because `ROBINHOOD_RPC` exists in `fomo/.env` and
the new lookup finds it. It has no entry in `EVM_CHAIN_IDS`, so Etherscan skips it, and every
watchlist token whose chain is unknown gets scanned against it. It is harmless — failures are
swallowed — but it costs a couple of requests per EVM wallet per round and adds `robinhood` to
the UI's watchlist note. Set `WG_CHAIN_ID_ROBINHOOD` to make it a first-class chain, or set
`WG_EVM_CHAINS=ethereum,base,bsc` to drop it. Left alone deliberately: it is not related to
this bug and changing it would have been an unverified change riding along with a verified one.
