# Running `fomo_bot` 24/7 on the VPS

Written 2026-08-31. Companion script: `vps_gate.py`.

## Short answer

Yes — all four mechanics work exactly as described. Chromium installs fine, a
persistent profile is portable, `xvfb`/headless both run, and the systemd unit is
the same shape as `dashboard/memedash.service`. None of that is the risk.

**The risk is the IP, and Chrome does not change the IP.** Cloudflare's rule
refuses the Vultr address; a browser changes the fingerprint half of the bot
score, not the reputation half. Two corrections to the plan, then one 30-second
test that settles it, then the build.

### Correction 1 — do NOT run `--login` on the VPS

`fomo_browser.py --login` wants a window and a wallet/email flow. Copy the
already-authenticated session off borz instead. The profile is 691 MB but the
session is ~4 MB of it — `Cache/` and `Code Cache/` are 552 MB of that and are
worthless here.

### Correction 2 — `FOMO_CHROME_HEADLESS=1` is the fallback, not the default

The code defaults to headed on purpose (`fomo_browser.py:116`): headless is a
signal Cloudflare scores against you, and on a datacenter IP you have no budget
to spend on extra signals. `xvfb-run` gives a real headed Chrome on a virtual
display for ~40 MB of packages. Keep headless in your pocket.

### ✅ The gate is GREEN (2026-09-01)

**Cloudflare serves the Vultr IP through a real browser.** `vps_diag.py` on the
box got `200 {"success":true,"message":"User found"}` from
`/v2/users/userHandle/MrSwole`, `cf-ray=a34f544b98a69fe8-AMS`,
`access-control-allow-origin: https://fomo.family`. The 2026-08-18 finding —
that the VPS 403s on every path — was about **raw HTTP**, and it does not
generalise to an in-page fetch from a logged-in Chrome.

So the standing "the bot runs on borz, never the VPS" constraint is **retired**.
`FOMO_API.md` §1 and `README.md` line 19 still say otherwise; they are stale.

Keep `vps_gate.py` around anyway — Cloudflare rules change, and the gate is how
you find out it was the WAF rather than your code.

---

## When it stops working: re-ship the session (one command)

**The usual cause is not the code and not Cloudflare — it is Privy.** The refresh
token rotates on every use, so whichever machine touched fomo.family last owns
the session. Open fomo.family in the borz `.chrome-profile` — even just to look
at a token page — and the box is logged out from that moment. It cannot say so:
the API omits `access-control-allow-origin` on error responses, so the 401 never
reaches JS and `fetch()` throws `Failed to fetch` instead.

On borz, with the local bot and any Chrome on that profile closed:

```powershell
cd C:\Users\mzshu\Downloads\memebot\fomo
powershell -ExecutionPolicy Bypass -File .\ship_session.ps1
```

That refuses to run while anything holds the profile, packages the five session
paths, scp's them plus `vps_relogin.sh` to the box, and runs the remote half:
stop fomobot → kill orphaned Chrome → clear `Singleton*` → back up the old
session under `/root/fomo-session-backups/` → install the new one → **gate test
on the real profile** → start fomobot only if the gate is green. Add
`-SkipRemote` to stop after the copy; run `bash /root/vps_relogin.sh` yourself
afterwards.

### The session has exactly one owner — copies steal it

Loading fomo.family makes the SPA refresh its Privy session, and Privy **rotates
the refresh token on use**. So any second Chrome that opens the app with a
*copy* of the profile consumes the rotation and leaves the original holding a
token the server has already retired. The copy works; the original is logged out
and cannot recover.

This is not only the borz-vs-VPS problem. It burned us on 2026-09-03 in a much
sneakier form: `vps_relogin.sh` gate-tested on `/tmp/diag-profile`, the gate went
**green**, and the bot it then started was already logged out — every command
hung on "Generating the … profile for @x…". The gate now runs on the real
profile, which is safe because the bot is stopped at that point.

Same warning applies to the `/tmp/diag-profile` trick in Troubleshooting below:
it does not disturb the *files*, but it does take the *session*. Use it only
when you are willing to re-ship afterwards.

`vps_relogin.sh` also checks that the box's `fomo_api.py` contains the
`BrowserUnavailable` retry. If it warns that it does not, the deployed code is
the version that wedges permanently on a stale token — push the fix first, let
the webhook pull, then re-run.

The manual steps below are the same thing by hand, and the reference for a first
install.

---

## 1. Stop the local copy (borz)

The port-47821 single-instance lock is per-machine. Two machines on one bot
token both hold a gateway session and one of them fails every interaction ack
with 10062. Close the local `fomo_bot.py` and leave it closed.

## 2. Ship the session (borz → VPS)

```powershell
cd C:\Users\mzshu\Downloads\memebot\fomo

tar -czf session.tgz -C .chrome-profile `
    "Local State" "Default/Local Storage" "Default/IndexedDB" `
    "Default/Network" "Default/Preferences"

scp session.tgz root@209.250.245.16:/root/
```

Optional, for continuity — tracked traders and warm caches. Skip all four for a
clean start; the bot recreates them:

```powershell
scp fomo_tracks.json connected_cache.json pump_evm_cache.json pump_profile_cache.json `
    root@209.250.245.16:/root/memecoin-bot-new/fomo/
scp .fomo_session.json root@209.250.245.16:/root/memecoin-bot-new/fomo/
```

`fomo/.env` is **already on the box** — you scp'd it 2026-08-30 for the
multiwallet watcher, and it already carries `DISCORD_BOT_TOKEN`,
`FOMO_TRANSPORT=browser` and `FOMO_CHROME_PROFILE=.chrome-profile`. Only re-copy
it if the local one has changed since; compare first rather than clobbering,
since the dashboard and the multiwallet watcher read the same file.

On the VPS:

```bash
mkdir -p /root/memecoin-bot-new/fomo/.chrome-profile
tar -xzf /root/session.tgz -C /root/memecoin-bot-new/fomo/.chrome-profile
```

## 3. Dependencies on the VPS

```bash
apt update && apt install -y xvfb

cd /root/memecoin-bot-new/fomo
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium
```

**The venv is not optional.** This bot needs real `discord.py`; memebot needs
`discord.py-self`. Both install a package called `discord`. A `pip install` into
the system Python breaks `main.py`.

Check headroom first — Chrome with a persistent profile is ~400-500 MB RSS on
top of memebot, memedash and the coding agent:

```bash
free -m
```

If the box is 1 GB, add swap before going further.

## 4. The gate test — before writing any unit file

`vps_gate.py` is tracked in git, so it lands with the next push; or scp it now.

```bash
cd /root/memecoin-bot-new/fomo
FOMO_CHROME_CHANNEL= xvfb-run -a .venv/bin/python vps_gate.py
```

| Result | Meaning | Next |
|---|---|---|
| `PASS` | The Vultr IP is served through a real browser | Step 5 |
| `FAIL … Cloudflare block` | The IP is refused even with Chrome | "If the gate is red" |
| `FAIL … 401 from the app` | WAF passed, profile logged out | Redo step 2 |
| `FAIL  no privy:token` | Session did not travel | Redo step 2 |

It writes `vps_gate.png`; scp it back if the verdict is surprising.

## 5. The systemd unit

`/etc/systemd/system/fomobot.service`:

```ini
[Unit]
Description=fomo_bot - FOMO/Pump research Discord bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/root/memecoin-bot-new/fomo
Environment=PYTHONUNBUFFERED=1
Environment=FOMO_CHROME_CHANNEL=
ExecStartPre=/bin/rm -f /root/memecoin-bot-new/fomo/.chrome-profile/SingletonLock \
                        /root/memecoin-bot-new/fomo/.chrome-profile/SingletonCookie \
                        /root/memecoin-bot-new/fomo/.chrome-profile/SingletonSocket
ExecStart=/usr/bin/xvfb-run -a /root/memecoin-bot-new/fomo/.venv/bin/python fomo_bot.py
Restart=always
RestartSec=15
StandardOutput=append:/root/memecoin-bot-new/fomo/fomo_bot.log
StandardError=append:/root/memecoin-bot-new/fomo/fomo_bot.log

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now fomobot
```

`ExecStartPre` matters: `Restart=always` plus a Chrome killed by SIGKILL leaves
a stale `SingletonLock`, and the next start fails to open the profile. Clearing
it is safe because only this unit ever touches that profile.

`systemctl enable` covers reboot on its own — no `/etc/rc.local` entry, unlike
memebot and the agent.

## 6. Wire it into the deploy webhook

Add to `/root/deploy.sh`, beside the memebot and agent restarts:

```bash
systemctl restart fomobot
```

`fomo/` is tracked in git, so a push already pulls the code to the box; without
this line the new code sits there until you restart by hand.

## 7. Verify

```bash
systemctl status fomobot
tail -f /root/memecoin-bot-new/fomo/fomo_bot.log
```

Expect `browser transport ready (profile=…, headless=False)` and then the
discord.py gateway login. Then, in Discord, `/fomo <handle>` — that round trip
is the only proof that counts. Finish with `reboot` and confirm it comes back
on its own.

---

## If the gate is red

Three ways out, cheapest first.

### (a) Leave it on borz, but make it a real service

`nssm install fomobot` → Path `…\fomo\.venv\Scripts\python.exe`, Startup dir
`C:\Users\mzshu\Downloads\memebot\fomo`, Arguments `fomo_bot.py`. Auto-starts at
boot, restarts on crash, survives logout. Zero Cloudflare risk; it dies whenever
borz does. This is the honest default if the gate fails.

### (b) Residential proxy, egressing from the VPS

One change in `fomo_browser.py`, in the `launch` dict (~line 146):

```python
if os.getenv("FOMO_PROXY_SERVER"):
    launch["proxy"] = {
        "server": os.environ["FOMO_PROXY_SERVER"],
        "username": os.getenv("FOMO_PROXY_USER") or None,
        "password": os.getenv("FOMO_PROXY_PASS") or None,
    }
```

~$3-15/mo for the traffic this bot makes. Note this does **not** violate the
standing "no TLS-fingerprint evasion" decision — it changes where the packets
come out, not what they claim to be.

### (c) WireGuard split tunnel through borz

VPS routes only the fomo.family / prod-api.fomo.family addresses out through
your home connection. Free and keeps the true residential IP, but borz has to be
powered on regardless — in which case (a) is strictly simpler.

---

## True either way

- `discord.py` ≠ `discord.py-self`. Separate venv, always.
- One process per bot token. Whichever machine wins, the other must be off.
- The Chrome profile **is** the auth. If it logs out, re-copy the 4 MB session
  from borz; there is no re-login path on a headless box.
- Git carries the code. `.env`, `.chrome-profile/` and the four cache/track JSON
  files are gitignored and never deploy themselves.

---

## Troubleshooting

### `Opening in existing browser session` / `profile is already in use`

Not Cloudflare — the profile lock. A script that crashes never reaches
`await t.close()`, so its Chrome outlives it; the next launch hands off to that
orphan and exits, leaving Playwright with no browser.

```bash
pgrep -af "user-data-dir=/root/memecoin-bot-new/fomo/.chrome-profile"
systemctl is-active fomobot          # the service legitimately holds it too
pkill -f "user-data-dir=/root/memecoin-bot-new/fomo/.chrome-profile" ; sleep 2
rm -f /root/memecoin-bot-new/fomo/.chrome-profile/Singleton*
```

To diagnose *without* stopping a running bot, work against a copy — but see
"The session has exactly one owner" above: the copy will take the running bot's
Privy session with it, so plan to re-ship afterwards.

```bash
cp -a .chrome-profile /tmp/diag-profile && rm -f /tmp/diag-profile/Singleton*
FOMO_CHROME_PROFILE=/tmp/diag-profile xvfb-run -a python3 vps_diag.py
```

`vps_diag.py` checks for this before it launches anything and prints the exact
commands, so you should not meet the raw Playwright wall of text twice.

### `BrowserUnavailable: in-page fetch failed … Failed to fetch`

**This IS the expired session — it just cannot say so.** The API sends
`access-control-allow-origin` on success and **omits it on error responses**
(proved on the box: an unauthenticated call to the same URL came back
`net::ERR_FAILED` on the wire while the authenticated one returned 200 with the
header). So the browser refuses to hand JS the 401 and `fetch()` throws instead.
The status never exists, which means the 401 re-mint branch in
`_decode_response` could never fire.

Fixed 2026-09-01: `fomo_api._get()` now catches `BrowserUnavailable` from the
browser transport and treats it exactly like a 401 — reload the page so the SPA
re-mints, retry once. **This is what makes 24/7 viable**; without it the process
wedges the first time its token goes stale and never recovers, which is what a
long-lived orphaned Chrome was doing on the box.

If it still throws after the retry, run `vps_diag.py` — it watches Playwright's
`response`/`requestfailed` events, which see the real status even when CORS
hides it from JS.

### A command hangs on "Generating the … profile for @x…" forever

The interaction is waiting on a call that raised `BrowserUnavailable`, which is a
plain `RuntimeError` — so the handlers' `except (FomoError, asyncio.TimeoutError)`
never caught it and the placeholder was never edited. Fixed 2026-09-03:
`fomo_api._get()` converts a `BrowserUnavailable` that survives the reload-retry
into `FomoAuthError`, so the card now says the session is gone instead of
spinning. **The underlying cause is still a dead session** — re-ship it.

### Every traceback frame is `/usr/lib/python3.12/`

The venv is being bypassed. Use `.venv/bin/python`, never bare `python3` — the
system Python has memebot's `discord.py-self`, not this bot's `discord.py`.
