#!/bin/bash
# Deploy hook for memecoin-bot. Copy to /root/deploy.sh (chmod +x) — the webhook runs it.
#
# The rule this script exists to enforce: NOTHING RESTARTS UNLESS THE PULL SUCCEEDED.
# The previous version ran `git pull` without checking its exit status, so a pull
# aborted by a locally-modified file still restarted every service and still logged
# "bot restarted". The box sat on ec43e73 for a day while three pushes "deployed"
# (2026-09-04). It also referenced $before/$after without ever setting them, so the
# fomo/ change check was comparing empty strings and always restarted fomobot.
set -uo pipefail

REPO=/root/memecoin-bot-new
LOG=/root/deploy.log
log() { echo "$(date) - $*" >> "$LOG"; }

log "deploy started"
cd "$REPO" || { log "DEPLOY FAILED: cannot cd to $REPO"; exit 1; }

before=$(git rev-parse HEAD)

if ! git pull --ff-only origin main >> "$LOG" 2>&1; then
    log "DEPLOY FAILED: pull aborted — nothing was restarted, box still at ${before:0:7}"
    log "DEPLOY FAILED: working tree state below (a modified tracked file blocks the pull)"
    git status --short >> "$LOG" 2>&1
    exit 1
fi

after=$(git rev-parse HEAD)

if [ "$before" = "$after" ]; then
    log "already up to date at ${after:0:7} — nothing restarted"
    exit 0
fi

log "pulled ${before:0:7} -> ${after:0:7}"

if systemctl restart memebot; then log "bot restarted (systemctl)"; else log "WARNING: memebot restart FAILED"; fi

pkill -f "python3 /root/coding-agent/agent.py" 2>/dev/null
sleep 1
nohup python3 /root/coding-agent/agent.py >> /root/coding-agent/agent.log 2>&1 &
log "agent restarted"

if systemctl restart memedash; then log "memedash restarted"; else log "WARNING: memedash restart FAILED"; fi

# Restart fomobot only when something under fomo/ actually changed.
if ! git diff --quiet "$before" "$after" -- fomo/; then
    if systemctl restart fomobot; then log "fomobot restarted (fomo/ changed)"; else log "WARNING: fomobot restart FAILED"; fi
else
    log "fomobot left running (no fomo/ changes)"
fi

log "deploy finished at ${after:0:7}"
