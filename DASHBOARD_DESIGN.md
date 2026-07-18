# Memebot Dashboard — Design Document

*2026-07-18 · v1 · companion web app for memecoin-bot. The bot is untouched; the dashboard reads its data.*

## 0. Scope decisions (agreed)

- **Calls, not trades.** The bot records *calls* (CA mentions with mcap at call time). There are no entries/exits, so no real PnL, hold time, or strategy data. v1 ranks everything by **peak multiplier** (`peak_mc / first_mc`) and **hit rates** (% of calls reaching ≥2x / ≥5x / ≥10x). PnL simulation and real trade logging are future options (§9).
- **Dashboard-side peak tracking.** The bot's `peak_mc` only updates on same-group re-posts (the d6050f4 tracker was rolled back). The dashboard runs its **own** background job polling Dexscreener, so bot code stays frozen.
- **Same VPS**, deployed via the existing git-push → webhook flow, run as its own systemd service.
- Solo user → lightweight auth (HTTP Basic), no roles in v1.

## 1. Architecture

```
┌────────────────────────── Vultr VPS ──────────────────────────┐
│                                                               │
│  memecoin-bot (memebot.service)          UNCHANGED            │
│      └─ writes data/ca_history.json                           │
│                          │  read-only, every 60s              │
│                          ▼                                    │
│  dashboard (memedash.service)  /root/memecoin-bot-new/dashboard│
│      ├─ ingest loop: ca_history.json → SQLite (upsert)        │
│      ├─ peak loop:  Dexscreener poll → peak_mc/current_mc     │
│      ├─ FastAPI: /api/* (JSON) + serves static frontend       │
│      └─ dash.db (SQLite, WAL mode, lives in dashboard/data/)  │
│                          ▲                                    │
└──────────────────────────│────────────────────────────────────┘
                           │ HTTPS (nginx optional) + Basic auth
                     Browser (dark SPA, hash routing)
```

One process, three concerns: ingest (file → DB), enrich (Dexscreener → DB), serve (DB → JSON → UI). The JSON file stays the bot's source of truth; the SQLite DB is a rebuildable read model — deleting `dash.db` and restarting re-ingests everything (only dashboard-observed peaks are lost, hence the DB is backed up by simply not deleting it).

## 2. Database schema (SQLite)

```sql
-- one row per (address, group): mirrors ca_history.json entries
CREATE TABLE calls (
  id           INTEGER PRIMARY KEY,
  address      TEXT NOT NULL,
  chain        TEXT NOT NULL,             -- 'SOL' | 'ETH' (derived: 0x… = ETH)
  group_name   TEXT NOT NULL DEFAULT '',
  sender_name  TEXT NOT NULL DEFAULT '',
  source       TEXT NOT NULL DEFAULT '',  -- 'telegram' | 'discord' | scanner name
  ticker       TEXT DEFAULT '',
  first_mc     REAL DEFAULT 0,            -- mcap at first mention in this group
  peak_mc_bot  REAL DEFAULT 0,            -- bot's (stale) peak
  scan_count   INTEGER DEFAULT 1,
  called_at    REAL NOT NULL,             -- unix ts
  UNIQUE(address, group_name)
);
CREATE INDEX idx_calls_addr   ON calls(address);
CREATE INDEX idx_calls_time   ON calls(called_at);
CREATE INDEX idx_calls_sender ON calls(sender_name);
CREATE INDEX idx_calls_group  ON calls(group_name);

-- one row per token: dashboard-tracked live data
CREATE TABLE tokens (
  address      TEXT PRIMARY KEY,
  chain        TEXT NOT NULL,
  ticker       TEXT DEFAULT '',
  current_mc   REAL DEFAULT 0,
  peak_mc_dash REAL DEFAULT 0,            -- max mcap observed by OUR poller
  peak_at      REAL DEFAULT 0,            -- when that peak was observed
  first_seen   REAL NOT NULL,
  last_checked REAL DEFAULT 0,
  dead         INTEGER DEFAULT 0          -- no pairs on Dexscreener anymore
);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);  -- ingest cursor, etc.
```

**Effective peak** everywhere = `MAX(first_mc, peak_mc_bot, peak_mc_dash)`; multiplier = effective peak / first_mc (guarding first_mc > 0). Old JSON entries lack `first_mc`/`peak_mc`/`ticker` (schema evolved) — ingest falls back to `market_cap` and `''`.

**Known limitation:** the dashboard poller only sees peaks *from its deploy time forward*. Historical calls show bot-recorded peaks only. Backfill path: GeckoTerminal OHLCV post-call ATH (the d6050f4 design, preserved in memory) as a one-off batch script — §9.

**Why SQLite, not Postgres/Supabase:** one writer process, one reader (you), <100k rows for years at current volume. WAL mode handles concurrent read/write. Zero ops, zero accounts, backed up with `cp`. Postgres becomes right only if this goes multi-user (§9).

## 3. API

All under `/api`, JSON, protected by HTTP Basic when `DASH_PASSWORD` is set.

| Endpoint | Returns |
|---|---|
| `GET /api/overview?days=30` | KPIs: total calls, unique CAs, hit rates (2x/5x/10x/20x), avg & median multiplier, calls/day series, multiplier histogram, top movers 24h |
| `GET /api/callers?days=&chain=&min_calls=` | leaderboard rows: calls, unique CAs, hit rates, avg/median mult, best call, last active, consistency (see below) |
| `GET /api/groups?days=&chain=` | same shape per group + top sender per group |
| `GET /api/sources?days=` | telegram vs discord vs scanner: tokens found, hit rates, avg mult, avg time-to-peak (dash-observed) |
| `GET /api/calls?q=&caller=&group=&chain=&source=&min_mult=&days=&sort=&page=` | paginated call explorer |
| `GET /api/token/{address}` | drill-down: all group calls in order, who was earliest, peaks, Dexscreener/Axiom/GMGN links |
| `GET /api/caller/{name}` · `GET /api/group/{name}` | profile: monthly series, hit-rate trend, favorite chains/groups, best & worst calls |
| `GET /api/health` | ingest lag, last peak-poll, DB row counts |

**Consistency score** (v1, honest and simple): share of the caller's calls that beat 2x, weighted by `log(n_calls)` so a 2-for-2 caller doesn't outrank a 40-for-60 one. Displayed with n so you can judge sample size.

## 4. Frontend

**No-build stack:** single-page app in `dashboard/static/` — vanilla ES modules + ECharts (CDN) + hand-rolled dark CSS. No node, no bundler, no npm on the VPS or your laptop; Borz can edit it with small diffs. Hash routing:

```
#/            Overview      KPI cards · calls/day area chart · multiplier
                            histogram · hit-rate donuts · top movers table
#/callers     Callers       sortable/filterable leaderboard, click → profile
#/caller/X    Profile       monthly bars · hit-rate trend · best/worst calls
#/groups      Groups        leaderboard, click → profile
#/group/X     Profile       same shape as caller profile + top callers
#/calls       Explorer      global search · chain/source/caller/group/date/
                            min-mult filters · paginated table · click → token
#/token/ADDR  Token         call timeline across groups · earliest caller ·
                            peak history · external links
#/sources     Scan sources  telegram vs discord vs per-scanner comparison
```

Wireframe (Overview, the pattern all pages follow):

```
┌ sidebar ─┬──────────────────────────────────────────────┐
│ Overview │  [30d ▾] [All chains ▾]              ⌕ search │
│ Callers  │  ┌──────┐┌──────┐┌──────┐┌──────┐┌──────┐    │
│ Groups   │  │calls ││ ≥2x% ││ ≥5x% ││avg × ││med × │    │
│ Explorer │  └──────┘└──────┘└──────┘└──────┘└──────┘    │
│ Sources  │  ┌ calls per day ───────┐┌ mult histogram ─┐ │
│          │  └──────────────────────┘└─────────────────┘ │
│ ● live   │  ┌ top movers (24h) ──────────────────────┐  │
└──────────┴──└────────────────────────────────────────┘──┘
```

Component hierarchy: `App(router, state) → Sidebar · Topbar(filters, search) → Page → {KpiCards, Chart(ECharts wrapper), DataTable(sort/paginate), FilterBar}`. Global filter state (days/chain) persists in `localStorage` and survives navigation — that's the "saved view" for v1. Keyboard: `/` focuses search, `1–5` switch pages.

**Stack evaluation (what you asked about):** Next.js + TS + Tailwind + shadcn + TanStack + Prisma + tRPC + Clerk + Vercel is the right answer for a multi-user SaaS. For a solo dashboard on your VPS it buys polish you can hand-roll and costs a node toolchain on Windows + VPS, a build step in your webhook deploy, and a framework Borz handles worse than plain Python/JS. Recharts vs ECharts: ECharts wins for heatmaps/treemaps/sankey later and works from a CDN without React. If v2 goes multi-user (§9), migrate frontend to Next.js + shadcn and keep the FastAPI API as-is — the API boundary is the migration seam.

## 5. Background jobs

- **Ingest loop (60s):** mtime-check `../data/ca_history.json`; if changed, upsert all entries (474 CAs today, trivial). Idempotent: `UNIQUE(address, group_name)` + update `scan_count`, `peak_mc_bot`, backfill `ticker`.
- **Peak loop (5 min):** tokens where `dead=0` and called in the last **48h** (matches the rolled-back design), batched 30 addresses per Dexscreener call (`/tokens/v1/{chain}/{csv}`), ≤60 req/min shared budget. Update `current_mc`, raise `peak_mc_dash`, mark `dead` after repeated empty responses. Tokens older than 48h are checked once daily.
- **Caching:** aggregates computed in SQL per request with a 30s in-process TTL cache — plenty at this size. No Redis.

## 6. Auth & deployment

- `DASH_PASSWORD` env → HTTP Basic (constant-time compare). Unset = open (LAN/dev only). Nginx + certbot in front for HTTPS is recommended but optional; the app also runs fine bound to `0.0.0.0:8080` behind the VPS firewall with Basic auth.
- Runs as `memedash.service` (systemd, `Restart=always`), started with `uvicorn`. Deploys ride the existing webhook: push → auto-pull → `systemctl restart memedash` added to deploy.sh.

## 7. Roadmap

1. **v1 (this session):** schema, ingest, peak poller, API, Overview/Callers/Groups/Explorer/Sources/profiles/token pages, Basic auth, systemd unit.
2. **v1.1:** GeckoTerminal ATH backfill script (one-off, rate-limited) so historical multipliers stop understating; time-to-peak metrics become meaningful.
3. **v1.2:** alerts — a check loop evaluating rules (caller >80% 2x-rate over 30d, group hit-rate drop, unusual scanner find) and pinging your existing Telegram bot token. Rules as rows in an `alerts` table, managed from the UI.
4. **v1.3:** advanced analytics page — posting-time/weekday heatmaps, mcap-band performance, duplicate-call detection across groups (already derivable: same address, multiple groups, ordered by timestamp → "early vs late caller" comparison).
5. **v2 (if ever multi-user):** Postgres, Next.js + shadcn frontend against the same API, real auth (Clerk/Better Auth), Docker.

## 8. Bottlenecks & mitigations

- **Dexscreener rate limits** — batched requests, 48h active window, shared limiter (same discipline as the bot's `utils.py`). Worst case peaks update late; never blocks the UI.
- **JSON file growth** — cleanup loop in the bot already prunes; ingest is mtime-gated and upsert-based. If the file ever hits tens of MB, switch ingest to comparing per-CA hashes.
- **Peak fidelity** — 5-min polling misses wicks between polls; peaks are floors, not exact ATHs. GT OHLCV backfill (v1.1) is the fix where it matters.
- **SQLite write contention** — single writer (the app itself), WAL mode; not a real risk here.
- **Old-schema rows** — entries without `first_mc` fall back to `market_cap`; rows with `first_mc = 0` are excluded from multiplier math (counted, flagged, never divide-by-zero).

## 9. AI opportunities (later, cheap to add)

- **Caller scoring:** the consistency score is deliberately dumb; a better version = empirical-Bayes shrinkage of hit rate toward the global mean (no ML infra needed, big quality jump).
- **NL insights:** nightly job feeds the day's aggregates to Claude Haiku → 3-sentence digest posted to your Telegram ("skotadi hit 2 of 3 over 5x; The Village's 2x-rate is down 40% w/w").
- **Emerging-group detection:** rolling z-score of each group's weekly hit rate vs its own history; flag sustained positive drift.
- **Behavior clustering:** embed callers as vectors (mcap bands, chains, hours, hit rates) → k-means to find "early micro-cap snipers" vs "momentum re-callers".
- **Dupe/lag networks:** who consistently calls the same CA minutes after whom → directed graph, exposes copy-callers (sankey: origin caller → repeater groups → outcome).
