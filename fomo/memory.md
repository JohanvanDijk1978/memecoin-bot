# Full Memory — Zenza (Updated)

**Last updated:** 2026-08-21

---

## Profile

Entrepreneur and developer building automated crypto trading and monitoring tools.

- Based in the Netherlands (Nieuwegein area)
- Dutch speaker
- Comfortable with: Python, VPS deployment (Vultr), Telegram/Discord bot development, REST APIs, Anthropic API
- Works primarily in VS Code locally with deployment via GitHub
- Manages at least two real estate properties (Parkville and Ocean Pearl)
- Exploring an AI agents startup

---

## Primary Active Project: Memecoin Bot (VPS)

**Solana/ETH memecoin signal bot on VPS**

### Deployment
- Repo: `JohanvanDijk1978/memecoin-bot`
- VPS: `root@209.250.245.16`, project path `/root/memecoin-bot-new`
- Local path: `C:\Users\mzshu\Downloads\memebot`
- Deploy via git push only (webhook auto-pulls on port 9000)
- `.env` is never committed to git
- Bot stack: Python 3, python-telegram-bot, Telethon, Discord self-bot
- Operational preference: always use nohup, never pm2
- Restart: `pkill -f "python3 main.py"` then `nohup python3 main.py > data/bot.log 2>&1 &`
- `memory.md` in repo is kept updated after sessions

### Features
- `/pump` command with inline buttons (1h/6h/12h/24h) showing top pumping coins
- Peak MC tracking
- CA pings with Dexscreener data, Axiom/Padre/GMGN links, multipliers, scan counts
- Per-group cooldowns and multi-group alerts
- Discord scraper cleanup
- Peak MC tracking
- Reformatted wallet tracking into new JSON schema
- Cluster/bundle scanner module (early-buyer clustering + wallet hold-time profiling)

### Recent Work
- Fixed multiple bugs (undefined group_name, mirror.py syntax errors, wrong variable names)
- Added `/leaderboard` command ranking top groups and users by average multiplier
- Added `/status` command
- Added ticker symbol saving
- Wrote backfill script for existing CAs
- Added second Discord self-bot account and Discord mirror functionality

---

## Secondary Project: Fomo Bot (Discord, local on borz)

**Standalone Discord bot for FOMO (fomo.family) social crypto trading platform**

### Setup
- Lives in `fomo/` under memecoin-bot folder
- Runs locally on borz (not the VPS)
- Uses Helius as Solana RPC
- Deployment: local only

### Commands (Current Surface)
- `/fomo` — FOMO trader profile lookup
- `/pump` — Pump.fun data
- `/wallet` — wallet lookup (Solana/EVM)
- `/token` — token page with top 50 holders and top traders
- `/thesis` — top holders' written theses
- `/track` — platform choice for tracking (FOMO/Pump)
- `/tracked` — edit/remove tracked items
- `/fomotop` — top traders

### `/fomo` Features
- Shows profile embed with display name, Twitter, SOL + EVM wallets, followers/following, trades/volume
- Wallet resolution via Helius
- Open positions field (token, avg entry, position, PnL)
- Wide buys use green marker
- Padre-linked tickers matching sell rows

### `/token` Implementation
- **Top 50 Holders:** 10 per page, paging through all 50
- **Top Traders:** Best performers ranked by PnL in USD and ROI %
  - Entry shown as entry market cap (weighted-average)
  - Never by token volume
  - Compact scannable table
  - No invented data where PnL cannot be calculated

### `/token` Top Traders — Current Status (Session 36)

**Implementation Complete:**
- P/L calculation: ✅ Working correctly
- Ranking by profitability: ✅ Working correctly  
- Diagnostic tool (`token_traders_diag.py`): ✅ Confirms functionality

**Current Issue — Sample Size Limitation:**
- Sample currently covers: 110 transactions in 2 minutes
- Real top trader: `gasAx5Y917MYdmdnwiomwYDhmDKNGDJnN1MmEbxVdVw` with +$394k
- Showing instead: +$7.69 (different trader, due to limited window)
- Warning: "CUT SHORT" appears when transaction budget exhausted
- Root cause: TOKEN_TRADER_SOLANA_PAGES budget runs out before complete history is fetched

**Data Sources:**
- Solana: Helius (`/v0/addresses/{mint}/transactions`)
- EVM: CMC (`alchemy_getAssetTransfers`), Blockscout (Robinhood)

**Solutions Evaluated:**
- HelloMoon: Pre-calculated P/L (used by axiom.trade) — NOT FREE
- Solscan: Blockchain data only, no pre-calculated P/L
- DexScreener: Price/volume data, not trader rankings
- GeckoTerminal: DEX data, not trader P/L
- **Decision:** Build free local indexer (1-day effort, documented in fomo-indexer-architecture.md)

### `/connected` (New Command)
- Finds wallets with strong on-chain evidence of same cluster as FOMO user's wallets
- Confidence bands rather than ownership claims
- Infrastructure excluded (CEX/bridge/router/contract/high-degree)
- High precision preferred over recall
- Accepts FOMO handle or raw wallet address
- Analysis budget: ~500 transactions per wallet

### `/thesis` (New Command)
- Top holders' written theses ranked by position value
- Shows: handle, X account, position, PnL, hold time, thesis quote
- 5 entries per page

### Wallet Resolution
- Traced FOMO traders' real Solana wallets by hand on Solscan (ground truth for automation)
- Multiple resolution routes: holders → transactions → balances (cheapest first)
- User prefers targeted changes over refactors of unrelated code

### Technical Notes
- Routes, schemas, resolver design documented in repo: `fomo/HANDOFF.md`, `fomo/README.md`, `fomo/FOMO_API.md`
- User prefers being asked before permanent/irreversible data writes
- Approved bulk wallet-cache adoption explicitly

### Open Items from Handoff
1. `FOMO_ENRICH_TIMEOUT` cancels enrichment mid-flight (handles never converge on slow scans)
2. Duplicate 50-swap request in `WalletResolver._resolve()`
3. `fomo/` still untracked in git
4. Adoption unproven at scale (unit-tested but never spot-checked on hand-traced wallets)
5. `/hodlers/top` pagination unknown (returned 48 rows for 1006 holders)
6. Pump has no known batch profile route
7. `pump_map_top.py` and `pump_resolve_diag.py` never run live
8. `/feed/token/sortedThesis` never probed
9. `_das_holders` never made a real request
10. Batched `/hodlers/top` never sent with multiple tokens
11. Top Traders never made a real request (now tested, sample size issue identified)
12. `/connected` never made a real request
13. Cross-chain evidence is identity-based only (bridge tracing not implemented)
14. `/connected` prices only SOL, native EVM coins and stablecoins (memecoins unpriced)
15. Sponsor index reaches back under an hour (shrinking as FOMO grows)

---

## Local Indexer Architecture (Planned for Future Build)

**Goal:** Eliminate Top Traders sample size limitation without API costs

**What it is:** One-day build (~8 hours), free forever, complete transaction history, fast queries

**Components:**
- Sync Worker: Scan all historical transactions (run once, 2-5 min per token)
- Real-time Listener: Poll Helius every 10s for new transactions
- Parser: Extract wallet, amount, price, timestamp
- P/L Calculator: avg_entry, avg_exit, profit/loss, ROI
- SQLite Database: Persistent storage
- Rank Engine: Sort by P/L, ROI, volume, etc
- FastAPI endpoint: `/traders/{mint}?sort=pnl&limit=50`

**Advantages:**
- Free (no HelloMoon subscription needed)
- Complete data (no sampling limits)
- Fast queries (cached locally)
- Persistent (survives restarts)
- Scalable (1000s of tokens)
- Can run on cheap VPS ($5-10/mo)

**Effort:** 4-6 hrs indexer + 1-2 hrs API + 2 hrs testing = ~1 workday

**Status:** Architecture documented, not yet built (scheduled for later)

---

## FOMO Platform Research

- Researched programmatic/API access to Fomo (fomo.family)
- Focused on legitimate methods only — no auth bypass, no exploiting, no scraping private data
- Third-party bot (dlurfomobot) on Discord uses FOMO's routes to fetch trader profiles
- Goal: Know what can be legitimately automated vs what requires direct access

---

## Recent Work (Other Projects)

- Explored Compute Royale platform (Solana-based GPU compute competition)
- Built Pump.fun memecoin launcher bot: autonomous monitoring Twitter/X for viral trends, GPT-4o-mini for coin names/tickers, Replicate SDXL for artwork, deployed on Vultr VPS using screen sessions
- Built two crypto waitlist landing pages: "YieldX" (Solana green/purple dark theme) and "SOLDOWAY" (editorial cream/black style) for DeFi yield product with x402 gasless transactions and Solana wallet confirmation flow

---

## Technical Stack & Preferences

### Languages & Tools
- Python (primary)
- VS Code (local development)
- GitHub (version control & deployment)
- VPS: Vultr (209.250.245.16)
- nohup for process management (no pm2)
- Helius (Solana RPC)
- Telegram/Discord bot frameworks
- Anthropic API

### Crypto Tools & Platforms
- Solscan (manual wallet tracing)
- Dexscreener (signal data)
- Axiom.trade (reference for top traders ranking)
- Padre.gg (GMGN links)
- Pump.fun
- Fomo.family
- Compute Royale

---

## Preferences & Notes

- Deploy via git push only (webhook auto-pulls)
- `.env` never committed to git; `.env.example` as template
- Always ask before permanent/irreversible data writes
- Prefers targeted changes over refactors
- High precision over recall (especially for `/connected` command)
- Compact, scannable table formats
- No invented data where actual data unavailable
- Wants to understand "how it works" before implementation (researches platforms/APIs)
- Pragmatic approach: use free APIs/infrastructure when possible, but understand limitations clearly
