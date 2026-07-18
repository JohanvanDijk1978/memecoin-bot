# memedash 🪙

Analytics dashboard for memecoin-bot. Reads `../data/ca_history.json` (bot untouched),
keeps its own SQLite DB, polls Dexscreener for live peaks, serves a dark web UI.

Full design: see `../DASHBOARD_DESIGN.md`.

## One-time VPS setup

```bash
# after git push has synced this folder to the VPS:
pip3 install fastapi uvicorn httpx
cp /root/memecoin-bot-new/dashboard/memedash.service /etc/systemd/system/
nano /etc/systemd/system/memedash.service   # set DASH_PASSWORD
systemctl daemon-reload
systemctl enable --now memedash
```

Then open `http://209.250.245.16:8080` — username anything, password = DASH_PASSWORD.

Add to `/root/deploy.sh` so redeploys pick up changes:

```bash
systemctl restart memedash
```

## Local dev (Windows)

```powershell
cd dashboard
pip install fastapi uvicorn httpx
python -m uvicorn main:app --port 8080
# open http://localhost:8080  (no DASH_PASSWORD set = no login)
```

## Env vars

- `DASH_PASSWORD` — enables HTTP Basic auth (any username). Unset = open.
- `HISTORY_FILE` — path to ca_history.json (default `../data/ca_history.json`)
- `DASH_DB` — path to SQLite DB (default `dashboard/data/dash.db`)

## Notes

- `dashboard/data/` is gitignored (DB lives there). Deleting `dash.db` rebuilds
  from the JSON on next start — only dashboard-observed peaks are lost.
- Peak poller: tokens called in the last 48h every ~5 min (batches of 30,
  1 request per 2 s), older tokens once a day, dead tokens skipped.
- Multiplier = MAX(first_mc, bot peak, dashboard-observed peak) / first_mc.
  Historical calls made before the dashboard was deployed only have bot peaks,
  which understate performance (see design doc §2 for the GT backfill plan).
