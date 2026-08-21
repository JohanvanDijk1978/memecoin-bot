# Signup / Activation Wallet-Capture Experiment — Checklist

**Status:** analysis & observation only. **No resolver code changes.** No new endpoints
written to cache. First-party test account and first-party wallets only.

**Question it answers:** does a Fomo account's *real* wallet (or a value deterministically
tied to it) become observable **before the first sponsored trade** — specifically at
signup or at the `"activated"` transition — or only once on-chain trading history exists?

**Why this test first:** it's the cheapest way to resolve the single biggest unknown from
the analysis. Everything downstream (probing `/transfers`, `/referrals`, counterfactual
derivation) is only worth doing if this capture shows an address surfacing pre-trade.

---

## 0. Guardrails (read before starting)

- [ ] Use a **throwaway Fomo account you create and control**, and **wallets you control**. Nothing here touches other users.
- [ ] **Observation only.** Record traffic the browser already makes during normal signup/onboarding. Do **not** probe write/key routes (`/v2/send`, `/v2/fast-fill`, `/v2/users/exportedKeys`, `/v2/users/edit`).
- [ ] **No Cloudflare/TLS evasion.** All requests originate from the normal logged-in browser on `borz` (residential IP), exactly as a real user. This is the same footing as the existing tooling.
- [ ] **No resolver changes and no cache writes.** Do not run adoption paths or anything that persists to `wallet_cache.json`. Capture to a separate scratch file only.
- [ ] **Reversibility:** only irreversible step is "create an account" and "make one tiny trade." Both are on a disposable account. Get explicit go-ahead before Phase 3 (the trade), since that's the point where you deliberately create on-chain history.

## 1. Prerequisites

- [ ] Logged-out clean browser profile (or the existing `.chrome-profile` cleared of any Fomo/Privy session) so the signup flow runs from zero.
- [ ] A response-recording harness. Reuse the **read-only pattern already in `find_wallet_source.py`** (the `page.on("response", …)` recorder that saves every non-asset URL + body). Point it at a scratch output dir — do **not** modify the resolver files. Manual DevTools "Preserve log" + "Save all as HAR" is an acceptable fallback/second copy.
- [ ] A scratch capture folder, e.g. `hunt_out/signup_YYYYMMDD/`, ignored by git.
- [ ] Note the funding wallet address(es) you control **in advance**, so Phase 4 can grep for them.
- [ ] Have `.env` session handling ready but **do not** reuse Johan's live Privy refresh token for the throwaway account — a separate account avoids the token-rotation collision noted in FOMO_API §2.

## 2. Phase A — capture the signup / onboarding flow

- [ ] Start the response recorder **before** navigating to Fomo.
- [ ] Complete signup through the normal UI (email/Twitter/whatever the flow offers), all the way to the first landed profile/home screen. Do not trade.
- [ ] Let each screen settle (~9s, matching `SETTLE_MS`) so lazy panels fire their XHRs.
- [ ] Save the full response set (URL, status, content-type, body) to the scratch folder.
- [ ] Immediately snapshot the base profile object: `GET /v2/users/userHandle/{yourhandle}` and `GET /v2/users/{id}`. Record whether `address`, `evmAddress`, `activated`, and `createdAt` are **present, null, or absent**.

**Record for Phase A:**

| Field | Present at signup? | Value (or null) |
|---|---|---|
| `user.address` (SOL) | | |
| `user.evmAddress` | | |
| `activated` | | (expect `false`) |
| any `0x…` / base58 in *any other* response | | list URLs |
| Privy embedded-wallet pubkey / signer key anywhere | | |

## 3. Phase B — capture the `activated` transition (no trade)

- [ ] Identify the minimal in-app action that flips `"activated": false → true` **without** placing a trade (e.g. a "activate wallet" / "enable trading" / deposit-prompt step, if one exists in the onboarding UI).
- [ ] Recorder on. Perform that step. Settle. Save responses.
- [ ] Re-pull the profile object and diff against the Phase A snapshot.

**Record for Phase B:**

- [ ] Did `activated` change? What UI action caused it?
- [ ] Did any address field **appear or change** at this step (vs signup)?
- [ ] Did any onboarding response return a **deposit / receive / funding address**?
- [ ] Were any of the unprobed read routes called by the app here: `/transfers`, `/withdrawals`, `/referrals`, `/referrerDetails`, `/v2/users/current`, `/v2/users/current/followingIds`? Capture their bodies.

## 4. Phase C — minimal controlled first trade (gated on approval)

> Only after Phases A/B are analyzed, and with explicit go-ahead. This deliberately
> creates the on-chain history, so it's the boundary the whole experiment is measuring.

- [ ] Recorder on. Fund the account per the normal Fomo flow and make **one** smallest-possible trade.
- [ ] Capture: the resulting swap object (`/v2/users/{id}/swaps`), and the on-chain tx it produces.
- [ ] From the tx, note the **real** signer (Solana: non-sponsor `signers[0]`) and, for EVM, the ERC-4337 smart-account sender inside the `handleOps` batch.
- [ ] Record the **timestamp of the earliest artifact** in which the real wallet is derivable.

## 5. Phase D — analysis / diff

- [ ] Grep the **entire** Phase A + B capture set for the real wallet found in Phase C (both SOL base58 and EVM `0x…`). Did it appear anywhere *before* the trade?
- [ ] Grep for your **funding wallet** address in Phase A/B — does Fomo echo a receive/deposit address tied to your account pre-trade?
- [ ] Compare the Phase A vs Phase C `user.address` / `user.evmAddress`: are the synthetic addresses **stable** across the account's life, and do they match (they should **not**) the real wallet from Phase C?
- [ ] If a signer/owner pubkey appeared in any pre-trade response, check whether the Phase C EVM smart-account address is **counterfactually derivable** from it (factory + salt) — i.e. was the address computable before deployment?
- [ ] Confirm `eth_getCode` on the real EVM address was **empty before** Phase C and **non-empty after** (proves deploy-on-first-activity).

## 6. Outcome → interpretation

| Observation | Conclusion |
|---|---|
| Real wallet (or a deterministic seed for it) appears in a **signup** response | Wallet is discoverable pre-activity via **condition 1** (platform publishes it). Follow up: which endpoint/field. |
| Nothing at signup, but it appears at the **`activated`** step | Discoverability is gated on activation, not trading — a distinct, earlier hook than the first trade. |
| A **signer/owner pubkey** appears pre-trade and the EVM address is derivable from it | **Condition 2** (public counterfactual input) holds — address exists and is computable before any tx. |
| Real wallet appears in **no** pre-trade artifact; only the Phase C trade exposes it | Confirms the current repo finding: earliest exposure = first sponsored trade. Pre-activity discovery would require an unfound endpoint or a non-public seed. |
| Synthetic addresses present from signup, never match the real wallet | Reconfirms they are decoys; safe to keep ignoring them. |

## 7. Stop conditions

- [ ] Stop if the flow demands anything beyond a normal user's signup (KYC edge, CAPTCHA loops that push toward evasion) — do not work around it.
- [ ] Stop before Phase C if Phases A/B already answer the question (address found pre-trade), and report.
- [ ] If the throwaway account's Privy session collides with any live session, abandon the account rather than sharing tokens.

## 8. Deliverable of the experiment

- A short findings note (append to `HANDOFF.md`) stating, for a zero-tx account: which fields are present at signup, what the `activated` transition exposes, whether any pre-trade address maps to the real wallet, and which of the two architectural conditions (if either) was observed. **No resolver changes result from this experiment** — it only decides whether a follow-up (endpoint probe or counterfactual derivation) is worth building.

---

*Scope reminder: first-party test account, first-party wallets, observation of normal app
traffic only, no evasion, no writes to the wallet cache, no changes to `fomo_wallet.py` /
`fomo_evm.py` or any resolver.*
