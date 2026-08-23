# FOMO Signup Wallet-Capture Experiment — Complete Setup

**Status:** Ready to run  
**Date prepared:** 2026-08-21  
**Goal:** Determine when a FOMO account's real wallet becomes observable

---

## 📋 What You Have

This folder contains everything needed to run the signup wallet-capture experiment from zero to completion:

```
fomo-experiment/
├── README.md                           ← You are here
├── QUICK_START.txt                     ← One-pager checklist
├── SETUP_COMPLETE_GUIDE.md             ← Detailed step-by-step instructions
├── fomo_experiment_recorder.py         ← Automated response recorder (Playwright)
├── response_recorder.py                ← Utility for folder setup
├── analyze_captures.py                 ← Post-experiment analysis tool
└── hunt_out/
    └── signup_YYYYMMDD/
        ├── phase_a_responses.jsonl     ← (Created during Phase A)
        ├── phase_b_responses.jsonl     ← (Created during Phase B)
        ├── phase_c_responses.jsonl     ← (Created during Phase C)
        ├── PHASE_A_FINDINGS.md         ← (You fill this in)
        ├── PHASE_B_FINDINGS.md         ← (You fill this in)
        └── FINDINGS_REPORT.md          ← (Final summary)
```

---

## 🚀 Quick Start (Pick Your Path)

### Path 1: Fully Automated (Recommended)

```bash
# 1. Install dependencies
pip install playwright --break-system-packages
playwright install chromium

# 2. Run Phase A (signup recording)
python fomo_experiment_recorder.py --phase a --handle YOUR_HANDLE

# 3. Fill in findings
# Edit: hunt_out/signup_YYYYMMDD/PHASE_A_FINDINGS.md

# 4. Run Phase B (if wallet not found)
python fomo_experiment_recorder.py --phase b --handle YOUR_HANDLE

# 5. Analyze (if still no wallet found)
python analyze_captures.py

# 6. Run Phase C (only if Phases A/B show no wallet)
python fomo_experiment_recorder.py --phase c

# 7. Review findings
cat hunt_out/signup_YYYYMMDD/FINDINGS_REPORT.md
```

### Path 2: Manual (using Chrome DevTools)

1. Read: `QUICK_START.txt` (5 min overview)
2. Read: `SETUP_COMPLETE_GUIDE.md` (detailed instructions)
3. Open Chrome DevTools Network tab
4. Follow the checklist in `QUICK_START.txt`
5. Save responses manually (DevTools → Save all as HAR)

---

## 📖 Documentation

| File | Purpose | Read When |
|---|---|---|
| `QUICK_START.txt` | One-pager checklist | You want a quick overview |
| `SETUP_COMPLETE_GUIDE.md` | Full step-by-step guide | You need detailed instructions |
| `fomo_experiment_recorder.py` | Automated response recorder | You want to automate Phases A-C |
| `analyze_captures.py` | Post-experiment analysis | You need to grep/analyze captures |

---

## 🎯 The Experiment in 30 Seconds

**Question:** When does a FOMO account's real wallet (the actual Solana/EVM address) become observable?

**Phases:**
1. **Phase A** — Signup flow: Record all API responses
2. **Phase B** — Activation: Check if wallet appears when account activates
3. **Phase C** — First trade: Make one tiny trade, identify real wallet on-chain
4. **Phase D** — Analysis: Grep all captures for the real wallet address

**Outcome:** One of three answers:
- ✅ Wallet observable at **signup** → Follow-up: which endpoint?
- ✅ Wallet observable at **activation** → Follow-up: which endpoint?
- ✅ Wallet only observable after **first trade** → Follow-up: any deterministic seed?

---

## ⚙️ Installation & Setup

### Prerequisites

```bash
# Python 3.8+
python --version

# Install Playwright (for automated recording)
pip install playwright --break-system-packages
playwright install chromium

# Verify installation
python fomo_experiment_recorder.py --help
```

### Create Capture Folder

```bash
# The script does this automatically, but you can pre-create:
mkdir -p C:\Users\mzshu\Downloads\memebot\fomo/hunt_out/signup_$(date +%Y%m%d)
cd C:\Users\mzshu\Downloads\memebot\fomo
```

### Create Wallet Notes File

```bash
cat > wallet_notes.txt << 'EOF'
# FOMO Experiment Wallet Notes
# Created: $(date)

## Funding Wallets (you control these):
- Solana: [YOUR_ADDRESS_HERE]
- EVM: [YOUR_ADDRESS_HERE]

## Test Account Details (to create):
- Email: [TEMP_EMAIL]
- Handle: [AFTER_SIGNUP]
- Created: [TIMESTAMP]

## Discovered Addresses (to fill in):
- Synthetic SOL (from signup):
- Synthetic EVM (from signup):
- Real SOL (from Phase C trade):
- Real EVM (from Phase C trade):
EOF
```

---

## 🏃 Running the Experiment

### Automated (Easiest)

```bash
# Phase A: Signup recording
python fomo_experiment_recorder.py --phase a

# The browser opens, you complete signup
# Responses are auto-recorded
# Close browser when done

# Phase B: Activation recording
python fomo_experiment_recorder.py --phase b

# Phase C: Trade recording (only if needed)
python fomo_experiment_recorder.py --phase c
```

### Manual (Using DevTools)

1. Open Chrome
2. Press F12 (DevTools)
3. Go to Network tab
4. Check "Preserve log"
5. Navigate to https://fomo.family
6. Complete signup
7. Right-click in Network tab → "Save all as HAR with content"
8. Save to: `hunt_out/signup_YYYYMMDD/phase_a_responses.har`

---

## 📊 After Each Phase

### After Phase A (Signup)

```bash
# 1. Fill in findings
nano hunt_out/signup_YYYYMMDD/PHASE_A_FINDINGS.md

# 2. Record:
#    - Is user.address present?
#    - Is user.evmAddress present?
#    - Any real wallet hints?

# 3. Decision:
#    IF wallet found → STOP, proceed to Part 4 (Analysis)
#    IF no wallet → Continue to Phase B
```

### After Phase B (Activation)

```bash
# 1. Fill in findings
nano hunt_out/signup_YYYYMMDD/PHASE_B_FINDINGS.md

# 2. Record:
#    - Did activation trigger?
#    - Did any address appear?

# 3. Decision:
#    IF wallet found → STOP, proceed to Part 4
#    IF no wallet → Continue to Phase C (need go-ahead)
```

### After Phase C (Trade)

```bash
# 1. Note the real wallet from on-chain transaction
#    Solscan: https://solscan.io/tx/[SIGNATURE]
#    Check first signer = your REAL wallet

# 2. Fill in wallet_notes.txt with discovered wallets

# 3. Proceed to Analysis
```

---

## 🔍 Analysis

### Automated Analysis

```bash
python analyze_captures.py
```

**Manually (if script fails):**

```bash
# Search Phase A for real wallet address
grep -r "YOUR_REAL_WALLET" hunt_out/signup_*/phase_a*.jsonl

# Search Phase B for real wallet
grep -r "YOUR_REAL_WALLET" hunt_out/signup_*/phase_b*.jsonl

# If found in either, note the endpoint and timestamp
```

### Generate Final Report

```bash
# Fill in the template
nano hunt_out/signup_YYYYMMDD/FINDINGS_REPORT.md

# Answer these questions:
# 1. Real wallet observable at signup? YES/NO
# 2. Real wallet observable at activation? YES/NO
# 3. Real wallet observable before trade? YES/NO
# 4. Which condition holds? (1/2/3)
```

---

## 📝 Deliverable

Your final report should answer:

**"Does a FOMO account's real wallet become observable before the first sponsored trade?"**

### If YES — Which condition holds?

**Condition 1:** Platform publishes wallet
- Endpoint: `/v2/users/userHandle/{handle}` or other
- Timing: At signup or at activation
- Next step: Use endpoint directly in resolver

**Condition 2:** Wallet derivable from public data
- Signer pubkey appears pre-trade
- Address computable from pubkey
- Next step: Implement counterfactual derivation

**Condition 3:** Post-trade only
- Wallet only discoverable after first tx
- Confirms current finding
- Next step: Investigate sponsor account

---

## ⚠️ Important Guardrails

Read the original experiment definition: `fomo/SIGNUP_CAPTURE_EXPERIMENT.md`

- ✅ Use throwaway test account (not your main)
- ✅ Use wallets you control
- ✅ Only observe traffic (no special techniques)
- ✅ Record to scratch folder (not tracking in git)
- ⚠️ Phase C needs approval before running (makes actual trade)
- ❌ DO NOT modify resolver code
- ❌ DO NOT write to wallet_cache.json
- ❌ DO NOT probe write endpoints

---

## 🐛 Troubleshooting

| Problem | Solution |
|---|---|
| Playwright install fails | `pip install --upgrade pip` then retry |
| Chrome not found | `playwright install chromium` |
| No responses recorded | Check DevTools → Network tab → Disable cache |
| Can't complete signup | Use clean profile (`--profile-directory=Fresh`) |
| Stuck on Phase B | Activation might be auto, proceed to Phase C decision |
| Can't make trade (no balance) | Fund account first via normal Fomo flow |

---

## 🗂️ File Locations

**Local (your machine):**
```
C:\Users\mzshu\Downloads\fomo-experiment\
└── hunt_out\signup_20260821\
    ├── phase_a_responses.jsonl
    ├── phase_b_responses.jsonl
    ├── PHASE_A_FINDINGS.md
    ├── PHASE_B_FINDINGS.md
    └── FINDINGS_REPORT.md
```

**Repo (after analysis):**
```
~/Downloads/memebot/fomo/
├── HANDOFF.md                 ← Append findings to Session 39
└── hunt_out/                  ← (in .gitignore, not tracked)
```

---

## ✅ Completion Checklist

- [ ] Prerequisites installed (Playwright, etc.)
- [ ] Wallet addresses noted in `wallet_notes.txt`
- [ ] Phase A completed and findings recorded
- [ ] Phase B completed and findings recorded
- [ ] Phase C completed (only if needed)
- [ ] Analysis done, real wallet locations documented
- [ ] FINDINGS_REPORT.md filled in
- [ ] Conclusion stated: Which condition holds?
- [ ] Ready to append findings to HANDOFF.md

---

## 🎯 Success Criteria

✅ You're done when:
1. You can answer: **When does the real wallet appear?** (signup/activation/trade/never)
2. You have evidence from captures (response timestamps or on-chain data)
3. You've documented findings in FINDINGS_REPORT.md
4. You're ready to discuss follow-up actions

---

## 📞 Questions?

Refer back to:
- **Original guardrails:** `fomo/SIGNUP_CAPTURE_EXPERIMENT.md`
- **Detailed steps:** `SETUP_COMPLETE_GUIDE.md`
- **Quick ref:** `QUICK_START.txt`

---

**Ready to start? Run:**

```bash
python fomo_experiment_recorder.py --phase a
```

Good luck! 🚀
