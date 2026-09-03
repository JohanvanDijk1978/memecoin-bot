#!/usr/bin/env bash
# vps_relogin.sh -- install a freshly copied FOMO session on the VPS and bring
# fomobot back up. Idempotent; safe to re-run.
#
#   scp session.tgz vps_relogin.sh root@209.250.245.16:/root/
#   ssh root@209.250.245.16 'bash /root/vps_relogin.sh'
#
# ship_session.ps1 on borz does all of that for you.
#
# The Chrome profile IS the auth. Privy rotates its refresh token on every use,
# so whichever machine touched fomo.family last owns the session -- browsing
# fomo.family on borz silently revokes the box's copy. That is the failure this
# script repairs. See DEPLOY_VPS.md.

set -uo pipefail

FOMO=/root/memecoin-bot-new/fomo
PROFILE="$FOMO/.chrome-profile"
TGZ="${1:-/root/session.tgz}"
MEMBERS=("Local State" "Default/Local Storage" "Default/IndexedDB" "Default/Network" "Default/Preferences")

die() { echo "!! $*" >&2; exit 1; }

[ -f "$TGZ" ] || die "no session tarball at $TGZ -- run ship_session.ps1 on borz first"
[ -d "$FOMO" ] || die "$FOMO does not exist"

echo "== 1. stop the bot and anything holding the profile =="
systemctl stop fomobot 2>/dev/null
pkill -f "user-data-dir=$PROFILE" 2>/dev/null && echo "   killed an orphaned Chrome"
sleep 2
rm -f "$PROFILE"/Singleton*
pgrep -af "user-data-dir=$PROFILE" && die "something still holds the profile (see above)"
echo "   profile is free"

echo "== 2. is the deployed code the fixed one? =="
if grep -q "BrowserUnavailable" "$FOMO/fomo_api.py"; then
    echo "   OK -- fomo_api.py has the stale-session retry"
else
    echo "   !! WARNING: fomo_api.py on this box is the OLD version."
    echo "      Without the BrowserUnavailable retry the bot wedges permanently the"
    echo "      first time this session goes stale ('Failed to fetch' forever)."
    echo "      Push the fix from VS Code, let the webhook pull, then re-run this."
    echo "      Continuing anyway -- a fresh session still buys you hours."
fi

echo "== 3. back up the session that is there now =="
mkdir -p /root/fomo-session-backups
ts=$(date +%Y%m%d-%H%M%S)
if [ -d "$PROFILE" ]; then
    ( cd "$PROFILE" && tar -czf "/root/fomo-session-backups/session-$ts.tgz" "${MEMBERS[@]}" 2>/dev/null ) \
        && echo "   saved /root/fomo-session-backups/session-$ts.tgz" \
        || echo "   (nothing usable to back up -- fine)"
fi
ls -1t /root/fomo-session-backups/*.tgz 2>/dev/null | tail -n +6 | xargs -r rm -f

echo "== 4. install the new session =="
mkdir -p "$PROFILE"
# Remove the old copies first: extracting on top of a live leveldb merges two
# generations of files and Chrome then reads the older manifest.
rm -rf "$PROFILE/Default/Local Storage" "$PROFILE/Default/IndexedDB" "$PROFILE/Default/Network"
tar -xzf "$TGZ" -C "$PROFILE" || die "extract failed"
for m in "${MEMBERS[@]}"; do
    [ -e "$PROFILE/$m" ] || die "session incomplete: '$m' missing after extract"
done
echo "   session installed ($(du -sh "$PROFILE" | cut -f1) profile total)"

echo "== 5. gate test on a throwaway copy (does not disturb the profile) =="
rm -rf /tmp/diag-profile
cp -a "$PROFILE" /tmp/diag-profile
rm -f /tmp/diag-profile/Singleton*
cd "$FOMO" || die "cannot cd $FOMO"
FOMO_CHROME_CHANNEL= FOMO_CHROME_PROFILE=/tmp/diag-profile \
    xvfb-run -a "$FOMO/.venv/bin/python" vps_gate.py
gate=$?
rm -rf /tmp/diag-profile

if [ "$gate" -ne 0 ]; then
    echo
    echo "!! GATE RED -- not starting the bot."
    echo "   'no privy:token'  -> the session did not travel; re-run ship_session.ps1."
    echo "   'HTTP 401'        -> the profile is logged out; log in on borz first."
    echo "   'Cloudflare block'-> the WAF changed its mind about this IP; DEPLOY_VPS.md"
    echo "                        'If the gate is red' has the three fallbacks."
    exit 1
fi

echo "== 6. gate is green -- starting fomobot =="
rm -f "$PROFILE"/Singleton*
systemctl start fomobot || die "systemctl start fomobot failed"
sleep 12
systemctl --no-pager --lines=0 status fomobot
echo
echo "== last 25 log lines =="
tail -n 25 "$FOMO/fomo_bot.log"
echo
echo "Done. Expect 'browser transport ready' then the discord.py gateway login."
echo "The only proof that counts: run /fomo <handle> in Discord."
echo "From here on, do NOT open fomo.family in the borz .chrome-profile --"
echo "Privy rotates the refresh token and the box loses the session again."
