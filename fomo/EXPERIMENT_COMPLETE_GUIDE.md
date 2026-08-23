# FOMO Signup Wallet-Capture Experiment — Complete Setup Guide

**Goal:** Determine when/where a FOMO account's real wallet becomes observable (signup vs activation vs first trade)

**Status:** From zero to completion

---

## BEFORE YOU START — READ THESE GUARDRAILS

✅ **You will be:**
- Using a **throwaway test Fomo account** you create (not an existing account)
- Using **wallets you control** for funding
- Only **observing traffic** the app already makes
- Recording to a **separate scratch folder** (not modifying resolver files)

❌ **You will NOT:**
- Touch write endpoints (`/v2/send`, `/v2/fast-fill`, `/v2/users/exportedKeys`, `/v2/users/edit`)
- Use evasion techniques (all requests from your normal browser on borz)
- Modify `wallet_cache.json` or any resolver code
- Proceed with Phase C (actual trade) until Phases A/B are analyzed

---

## PART 0: ENVIRONMENT SETUP (Do This First)

### 0.1 Navigate to Your Working Folder

```bash
# On your machine, navigate to your fomo project folder
cd C:\Users\mzshu\Downloads\memebot\fomo

# The experiment files are already here:
ls -la
# You should see:
# - EXPERIMENT_README.md
# - EXPERIMENT_COMPLETE_GUIDE.md
# - EXPERIMENT_QUICK_START.txt
# - fomo_experiment_recorder.py
```

**When you run Phase A, this folder will be created:**
```
hunt_out/signup_YYYYMMDD/
├── phase_a_responses.jsonl
├── PHASE_A_FINDINGS.md
└── (more files as you progress)
```

### 0.2 Prepare Your Test Wallets

**Create or identify wallets you control:**

```bash
# Create a text file with addresses you'll use for funding
cat > wallet_notes.txt << 'EOF'
# FOMO Experiment Wallet Notes
# Created: 2026-08-21

## Wallets I Control (for funding):
- Solana funding wallet: [YOUR_SOLANA_ADDRESS_HERE]
- EVM funding wallet: [YOUR_EVM_ADDRESS_HERE]

## Test Account (to create):
- Fomo email: [TEMP_EMAIL_FOR_TEST_ACCOUNT]
- Twitter/X handle: [OR_TWITTER_IF_USING_OAUTH]
- Account created: [TIMESTAMP_WHEN_CREATED]

## Important Addresses Found:
- Synthetic SOL address (from signup response): 
- Synthetic EVM address (from signup response):
- Real SOL wallet (after Phase C):
- Real EVM wallet (after Phase C):

EOF
cat C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/wallet_notes.txt
```

### 0.3 Prepare Browser Environment

**Option A: Start with Clean Chrome Profile**
```bash
# Kill any existing Chrome instances
pkill -9 chrome || true

# Start a fresh Chrome profile (no Fomo cookies)
google-chrome --profile-directory=Default --no-default-browser-check &
```

**Option B: Clear Existing Fomo/Privy Session** (if you have .chrome-profile)
```bash
# If you have a .chrome-profile folder:
rm -rf ~/.chrome-profile/Default/Cache
rm -rf ~/.chrome-profile/Default/Code\ Cache
# Leave cookies alone unless you specifically need to clear Fomo
```

### 0.4 Set Up Response Recorder (Reuse from `find_wallet_source.py`)

**Download existing recorder pattern from the repo:**

```bash
# Copy the response recording logic from fomo_wallet.py or find_wallet_source.py
# into a standalone script
cat > C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/response_recorder.py << 'EOF'
"""
Response recorder using the same pattern as find_wallet_source.py
Records every XHR from the browser during signup flow
"""

import json
import os
from datetime import datetime
from pathlib import Path

# Configuration
CAPTURE_DIR = Path.home() / "Downloads" / "fomo-experiment" / "hunt_out" / f"signup_{datetime.now().strftime('%Y%m%d')}"
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

# This will be integrated with browser automation (Playwright)
# For now, prepare the folder structure

def init_capture_folder():
    """Initialize capture folder"""
    # Create subdirectories for each phase
    for phase in ['phase_a_signup', 'phase_b_activated', 'phase_c_trade']:
        (CAPTURE_DIR / phase).mkdir(exist_ok=True)
    
    # Create a manifest file
    manifest = {
        'experiment': 'fomo_signup_wallet_capture',
        'start_date': datetime.now().isoformat(),
        'phases': {
            'a': 'Signup through onboarding (no trade)',
            'b': 'Activated transition (if exists)',
            'c': 'Minimal controlled first trade',
            'd': 'Analysis - grep for wallet discovery'
        }
    }
    
    with open(CAPTURE_DIR / 'manifest.json', 'w') as f:
        json.dump(manifest, f, indent=2)
    
    print(f"✅ Capture folder initialized: {CAPTURE_DIR}")
    return CAPTURE_DIR

if __name__ == '__main__':
    init_capture_folder()
EOF

python C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/response_recorder.py
```

---

## PART 1: PHASE A — CAPTURE SIGNUP/ONBOARDING FLOW

### 1.1 Start Response Recorder (DevTools Method)

**Option A: Use Chrome DevTools (Manual but Reliable)**

1. **Open Chrome DevTools** (F12 or Ctrl+Shift+I)
2. **Go to Network tab**
3. **Check:** "Preserve log"
4. **Check:** "Disable cache" (Ctrl+Shift+Delete while DevTools open)
5. **Start fresh tab** → navigate to `https://fomo.family`

**Option B: Use Playwright (Automated)**

```bash
# Install Playwright if not already installed
pip install playwright --break-system-packages
playwright install chromium

# Create automation script
cat > C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/phase_a_recorder.py << 'EOF'
from playwright.async_api import async_playwright
import json
from pathlib import Path
from datetime import datetime

async def record_signup_flow():
    """Record all XHRs during signup"""
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        
        # Setup response interception
        responses = []
        
        async def handle_response(response):
            try:
                if 'fomo.family' in response.url or 'privy' in response.url.lower():
                    body = await response.text()
                    responses.append({
                        'url': response.url,
                        'status': response.status,
                        'headers': dict(response.headers),
                        'body': body[:5000],  # First 5KB
                        'timestamp': datetime.now().isoformat()
                    })
            except:
                pass
        
        page.on("response", handle_response)
        
        # Navigate and wait for user actions
        print("Navigate to https://fomo.family and complete signup")
        print("I'll record all XHRs. Press Ctrl+C when done with signup (after landing on profile/home)")
        
        await page.goto("https://fomo.family")
        await page.wait_for_timeout(300000)  # Wait 5 minutes for user
        
        # Save all responses
        output_dir = Path.home() / "Downloads" / "fomo-experiment" / "hunt_out" / f"signup_{datetime.now().strftime('%Y%m%d')}"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        with open(output_dir / 'phase_a_responses.jsonl', 'w') as f:
            for resp in responses:
                f.write(json.dumps(resp) + '\n')
        
        print(f"\n✅ Captured {len(responses)} responses to {output_dir / 'phase_a_responses.jsonl'}")
        
        await browser.close()

if __name__ == '__main__':
    import asyncio
    asyncio.run(record_signup_flow())
EOF

# Run the recorder
python C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/phase_a_recorder.py
```

### 1.2 Complete Signup Flow (Manually)

**While recorder is running:**

1. ✅ Navigate to https://fomo.family
2. ✅ Click "Sign Up"
3. ✅ Choose auth method (email/Twitter/Privy)
4. ✅ Complete all signup steps
5. ✅ Verify email or complete OAuth
6. ✅ Land on your first profile page
7. ✅ Wait ~9 seconds on the profile page (let lazy-loaded panels load)
8. ✅ **Do NOT trade yet**

### 1.3 Snapshot Phase A Profile State

**Stop the recorder and immediately run this:**

```bash
# In DevTools Console or via curl with your session cookie:
# Get your user handle from the URL or profile page first

cat > C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/phase_a_snapshot.sh << 'EOF'
#!/bin/bash

HANDLE="[YOUR_HANDLE_HERE]"  # Fill this in after signup
OUTPUT_DIR="$HOME/Downloads/fomo-experiment/hunt_out/signup_$(date +%Y%m%d)"

echo "=== PHASE A: Signup State Snapshot ==="
echo "Fetching profile data for handle: $HANDLE"

# Using curl + your session cookie (get from DevTools > Network > Copy as cURL)
# Replace COOKIE_VALUE with your actual session cookie

# Method 1: Via DevTools Console (paste directly)
# Copy this to DevTools Console:
# fetch('/v2/users/userHandle/YOUR_HANDLE').then(r=>r.json()).then(d=>console.log(JSON.stringify(d, null, 2)))

# Method 2: Manual HAR export (simpler)
# 1. In DevTools Network tab, right-click
# 2. "Save all as HAR with content"
# 3. Save to: $OUTPUT_DIR/phase_a_har.json

echo "Steps to capture profile state:"
echo "1. Open DevTools Console"
echo "2. Paste and run: fetch('/v2/users/userHandle/$HANDLE').then(r=>r.json()).then(d=>document.body.innerText = JSON.stringify(d, null, 2))"
echo "3. Copy the JSON output"
echo "4. Save to: $OUTPUT_DIR/phase_a_profile.json"

EOF

chmod +x C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/phase_a_snapshot.sh
bash C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/phase_a_snapshot.sh
```

### 1.4 Record Phase A Findings

**Create your analysis file:**

```bash
cat > C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/hunt_out/$(date +%Y%m%d)/PHASE_A_FINDINGS.md << 'EOF'
# Phase A Findings — Signup State

**Date:** $(date)
**Account Handle:** [FILL IN]
**Account Email:** [FILL IN]

## Profile Response at Signup

| Field | Present? | Value | Notes |
|---|---|---|---|
| `user.address` | [ ] | | Solana synthetic address |
| `user.evmAddress` | [ ] | | EVM synthetic address |
| `user.activated` | [ ] | | Should be `false` |
| `user.createdAt` | [ ] | | Account creation timestamp |
| Any `0x…` address in responses? | [ ] | | List URLs if found |
| Any Solana base58 address? | [ ] | | List URLs if found |
| Privy embedded wallet key? | [ ] | | Any signer pubkey? |

## Responses to check for addresses

URLs that returned responses:
- [ ] `/v2/users/userHandle/{handle}`
- [ ] `/v2/users/{id}`
- [ ] `/v2/users/current` (if called)
- [ ] `/v2/referrerDetails` (if called)
- Any others?

## Summary

At signup, before any activation or trading:
- Synthetic addresses present? YES / NO
- Real wallet observable? YES / NO
- Any pre-trade wallet hint? YES / NO

**Next step:** Proceed to Phase B to check the `activated` transition.

EOF

cat C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/hunt_out/$(date +%Y%m%d)/PHASE_A_FINDINGS.md
```

---

## PART 2: PHASE B — CAPTURE ACTIVATED TRANSITION

### 2.1 Find the Activation Step

**In the Fomo UI, look for:**
- A button like "Activate Wallet" or "Enable Trading"
- An "Activate Wallet" step in onboarding
- A toggle for "trading enabled"
- Any modal asking to confirm wallet activation

**If you don't see one:** It might happen automatically or be hidden. Record your observations.

### 2.2 Capture Phase B Responses

**Turn recorder back on (same Playwright script or DevTools):**

```bash
cat > C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/phase_b_recorder.py << 'EOF'
from playwright.async_api import async_playwright
import json
from pathlib import Path
from datetime import datetime

async def record_activated_transition():
    """Record all XHRs during activated transition"""
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        
        responses = []
        
        async def handle_response(response):
            try:
                if 'fomo.family' in response.url:
                    body = await response.text()
                    responses.append({
                        'url': response.url,
                        'status': response.status,
                        'body': body[:5000],
                        'timestamp': datetime.now().isoformat()
                    })
            except:
                pass
        
        page.on("response", handle_response)
        
        print("Navigate to your Fomo profile page (already logged in)")
        print("Find and click the 'Activate Wallet' or similar button")
        print("Wait 9 seconds, then press Ctrl+C")
        
        await page.goto("https://fomo.family")
        await page.wait_for_timeout(300000)
        
        output_dir = Path.home() / "Downloads" / "fomo-experiment" / "hunt_out" / f"signup_{datetime.now().strftime('%Y%m%d')}"
        
        with open(output_dir / 'phase_b_responses.jsonl', 'w') as f:
            for resp in responses:
                f.write(json.dumps(resp) + '\n')
        
        print(f"✅ Captured {len(responses)} Phase B responses")
        await browser.close()

if __name__ == '__main__':
    import asyncio
    asyncio.run(record_activated_transition())
EOF

python C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/phase_b_recorder.py
```

### 2.3 Snapshot Phase B Profile State

**After activation, immediately capture:**

```bash
cat > C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/hunt_out/$(date +%Y%m%d)/PHASE_B_FINDINGS.md << 'EOF'
# Phase B Findings — Activated Transition

**Date:** $(date)

## What Triggered Activation?

- [ ] Button click: [describe]
- [ ] Automatic step: [describe]
- [ ] Not found: [explain where you looked]

## Profile Response After Activation

| Field | Changed from Phase A? | New Value |
|---|---|---|
| `user.activated` | [ ] YES [ ] NO | |
| `user.address` | [ ] YES [ ] NO | |
| `user.evmAddress` | [ ] YES [ ] NO | |
| New fields present? | [ ] YES [ ] NO | |

## New Responses in Phase B

- [ ] `/v2/users/current` called?
- [ ] `/v2/users/current/followingIds` called?
- [ ] `/transfers` called?
- [ ] `/withdrawals` called?
- [ ] `/referrals` called?
- [ ] `/referrerDetails` called?
- Other new endpoints?

## Summary of Phase B

Address discovered at activation? YES / NO

If YES: Which address and which endpoint?

**Next step:** If no address found, proceed to Phase C (trade). If found, STOP and analyze.

EOF
```

---

## PART 3: PHASE C — MINIMAL CONTROLLED FIRST TRADE

⚠️ **ONLY PROCEED AFTER:**
- [ ] Phases A/B are complete and analyzed
- [ ] You've reviewed the findings
- [ ] No addresses were found pre-trade
- [ ] You've decided this is worth doing

### 3.1 Fund the Account (Smallest Amount)

1. Send a tiny amount to Fomo's receiving address (they provide this in the UI)
2. Wait for confirmation (~2 min)
3. Verify balance shows in Fomo UI

### 3.2 Capture Phase C Trade Responses

```bash
python C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/phase_c_recorder.py  # Same pattern as A/B
```

### 3.3 Make One Minimal Trade

**In Fomo UI:**
1. Find any token (doesn't matter which)
2. Set amount to minimum (e.g., 0.01 SOL)
3. Hit "Swap"
4. Wait for tx confirmation

### 3.4 Snapshot Phase C On-Chain Data

```bash
cat > C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/phase_c_analysis.sh << 'EOF'
#!/bin/bash

echo "=== PHASE C: On-Chain Analysis ==="

# You need the transaction signature from the trade
TX_SIG="[PASTE_TX_SIGNATURE_HERE]"

echo "Transaction: $TX_SIG"
echo ""
echo "On Solscan, check:"
echo "1. Non-sponsor signer (signers[0] or first non-fee-payer)"
echo "2. This is your REAL wallet"
echo "3. Save it to wallet_notes.txt"
echo ""
echo "For EVM:"
echo "1. Check who signed the ERC-4337 handleOps"
echo "2. The smart account address is the real wallet"

# Solana: Visit https://solscan.io/tx/TX_SIG and find the signer
# EVM: Visit block explorer and check transaction details

EOF

bash C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/phase_c_analysis.sh
```

---

## PART 4: ANALYSIS & FINDINGS

### 4.1 Grep for Wallet Addresses

```bash
cat > C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/analyze_captures.py << 'EOF'
import json
import re
from pathlib import Path

CAPTURE_DIR = Path.home() / "Downloads" / "fomo-experiment" / "hunt_out"

def grep_captures(real_wallet: str, funding_wallet: str):
    """Search all captures for wallet addresses"""
    
    print(f"🔍 Searching captures for:")
    print(f"  Real wallet (Phase C): {real_wallet}")
    print(f"  Funding wallet: {funding_wallet}")
    print("")
    
    # Pattern matching
    patterns = {
        'real_wallet': real_wallet,
        'funding_wallet': funding_wallet,
    }
    
    for phase_dir in sorted(CAPTURE_DIR.glob("*/phase_*")):
        print(f"\n📁 {phase_dir.name}:")
        
        for response_file in phase_dir.glob("*.jsonl"):
            with open(response_file) as f:
                for line in f:
                    data = json.loads(line)
                    for name, pattern in patterns.items():
                        if pattern.lower() in data.get('body', '').lower():
                            print(f"  ✅ FOUND {name} in {response_file.name}")
                            print(f"     URL: {data['url']}")
                            print(f"     Timestamp: {data['timestamp']}")

if __name__ == '__main__':
    # Fill in these values from your experiment
    REAL_WALLET = "[YOUR_REAL_WALLET_FROM_PHASE_C]"
    FUNDING_WALLET = "[YOUR_FUNDING_WALLET]"
    
    grep_captures(REAL_WALLET, FUNDING_WALLET)
EOF

python C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/analyze_captures.py
```

### 4.2 Create Final Findings Report

```bash
cat > C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/FINDINGS_REPORT.md << 'EOF'
# FOMO Signup Wallet-Capture Experiment — Final Findings

**Date Run:** $(date)
**Experiment Status:** ✅ Complete

---

## Summary

| Question | Answer | Evidence |
|---|---|---|
| Real wallet observable at signup? | YES / NO | Specify endpoint or "No observable endpoint" |
| Real wallet observable at activation? | YES / NO | Specify event or "N/A" |
| Real wallet observable before trade? | YES / NO | Specify first location |
| Condition 1 (platform publishes)? | YES / NO | Which endpoint? |
| Condition 2 (counterfactual derivable)? | YES / NO | From which public input? |

---

## Phase A — Signup State

### Responses Received
- [ ] `/v2/users/userHandle/{handle}` — Synthetic addresses present: YES / NO
- [ ] `/v2/users/{id}` — New fields? YES / NO
- [ ] Other endpoints: [list]

### Key Observations
- Synthetic address (SOL): [value from profile]
- Synthetic address (EVM): [value from profile]
- Real wallet hints? [YES/NO — if yes, where?]

---

## Phase B — Activated Transition

### Activation Event
- Found in UI? [describe or "not found"]
- Auto-triggered? YES / NO
- Endpoint called: [specify]

### Address Changes
- `activated` field changed? YES / NO
- Any address appeared? YES / NO

---

## Phase C — First Trade

### Real Wallet Discovered
- Solana signer: [address]
- EVM account: [address]
- First observed in: [tx signature or endpoint]

### Pre-Trade Search Results
- Real wallet found in Phase A responses? YES / NO
- Real wallet found in Phase B responses? YES / NO
- First appearance: [Phase A / Phase B / Phase C / On-chain only]

---

## Conclusion

**Does a FOMO account's real wallet become observable before the first trade?**

**Answer:** [YES / NO]

**If YES:**
- Which condition holds? (1 = published by platform, 2 = counterfactually derivable)
- Which endpoint reveals it?
- At what event (signup/activation/other)?

**If NO:**
- Confirms: earliest exposure = first sponsored trade
- Next investigation: Does the sponsor account hold deterministic information?

---

## Recommendations for Next Phase

Based on these findings:
- [ ] Follow-up: Probe `/transfers` endpoint (if address not found)
- [ ] Follow-up: Check counterfactual derivation (if signer pubkey found)
- [ ] Follow-up: Trace sponsor account structure (if nothing found)
- [ ] No further action needed (wallet discovery is post-trade only)

EOF

cat C:\Users\mzshu\Downloads\memebot\fomo (your working directory)/FINDINGS_REPORT.md
```

---

## PART 5: CLEANUP & UPLOAD TO REPO

### 5.1 Verify Everything is Captured

```bash
cd C:\Users\mzshu\Downloads\memebot\fomo (your working directory)
find hunt_out -type f | head -20

# Check for sensitive data
echo "⚠️ Checking for sensitive data in captures..."
grep -r "private\|secret\|key" hunt_out/ | head -5 || echo "✅ None found"
```

### 5.2 Add to `.gitignore` (if not already there)

```bash
cd ~/Downloads/memebot/fomo

# Make sure hunt_out/ is in gitignore
echo "hunt_out/" >> .gitignore

git status  # Verify hunt_out is untracked
```

### 5.3 Append Findings to HANDOFF.md

```bash
cat >> ~/Downloads/memebot/fomo/HANDOFF.md << 'EOF'

---

## Session 39 — Signup Wallet-Capture Experiment

**Date:** 2026-08-21

**Question:** Does a FOMO account's real wallet (or a value deterministically tied to it) become observable **before the first sponsored trade** — specifically at signup or at activation?

**Method:** First-party test account, observation-only, normal app traffic, no evasion.

**Result:** [TO BE FILLED AFTER EXPERIMENT RUNS]

### Phases Run

- [ ] Phase A: Signup/Onboarding state captured
- [ ] Phase B: Activated transition (if exists) captured
- [ ] Phase C: Minimal first trade executed and on-chain wallet identified
- [ ] Phase D: All captures grepped for real wallet address

### Key Finding

Real wallet first appears in: [Signup response / Activated response / First trade / On-chain only]

Condition confirmed: [1 = platform publishes / 2 = counterfactually derivable / 3 = post-activity only]

### Next Steps

[Based on findings, recommend...]

EOF

cat ~/Downloads/memebot/fomo/HANDOFF.md | tail -30
```

---

## FINAL CHECKLIST — COMPLETION CRITERIA

- [ ] **Prerequisites done:**
  - [ ] Clean browser profile or cleared Fomo session
  - [ ] Scratch capture folder created
  - [ ] Wallet addresses noted in `wallet_notes.txt`
  - [ ] Test account email prepared

- [ ] **Phase A complete:**
  - [ ] Signup flow recorded
  - [ ] Profile state captured
  - [ ] `PHASE_A_FINDINGS.md` filled out
  - [ ] Synthetic addresses identified

- [ ] **Phase B complete:**
  - [ ] Activation action found (or "not found" documented)
  - [ ] Activation responses recorded
  - [ ] `PHASE_B_FINDINGS.md` filled out
  - [ ] No real wallet found pre-trade (or noted if found)

- [ ] **Phase C complete:** (only if Phases A/B show no wallet)
  - [ ] Account funded
  - [ ] One minimal trade executed
  - [ ] Real wallet identified from on-chain tx
  - [ ] Wallet address saved

- [ ] **Phase D complete:**
  - [ ] All captures grepped for real wallet
  - [ ] All captures grepped for funding wallet
  - [ ] `FINDINGS_REPORT.md` completed
  - [ ] Conclusion stated clearly

- [ ] **Documentation done:**
  - [ ] `hunt_out/` added to `.gitignore`
  - [ ] Findings appended to `HANDOFF.md`
  - [ ] All temporary files in scratch folder (not tracked)

- [ ] **Ready to proceed:** Findings available for follow-up (endpoint probe or counterfactual derivation)

---

## Questions During Execution?

If anything is unclear or blocks you:
1. Check `SIGNUP_CAPTURE_EXPERIMENT.md` for the authoritative guardrails
2. Stop before Phase C if you find the answer in Phases A/B
3. Document what you find — even "not found" is a finding
4. Keep the account throwaway (don't reuse it)

**You're ready to start. Begin with Part 0.**
