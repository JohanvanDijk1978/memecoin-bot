"""
diag_wallet_cohort.py
─────────────────────
Sizes the winner / loser cohorts in the memedash read model, to decide whether
there is enough labelled data to attempt wallet discovery from early buyers.

Run on the VPS from the repo root:
    cd /root/memecoin-bot-new && python3 tools/diag_wallet_cohort.py

Or against a local copy:
    python3 tools/diag_wallet_cohort.py path/to/dash.db

100% READ-ONLY. Opens the DB with mode=ro and never writes.

Reports, in order:
  1. Schema check — is peak_mc_live present? (v1.22 per-call attribution)
  2. Overall call/token counts, per chain, with date range
  3. Multiplier distribution for SOL calls
  4. Winner cohort size at 2x / 3x / 5x / 10x thresholds
  5. Loser cohort size (the control group — needed to de-bias wallet ranking)
  6. Month-by-month split, to check a time-held-out validation is viable
"""

import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dashboard", "data", "dash.db",
)

WINNER_THRESHOLDS = [2.0, 3.0, 5.0, 10.0]
LOSER_MAX = 1.5  # a call that never crossed this is a control-group loser


def ts(value):
    if not value:
        return "-"
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")


def month(value):
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m")


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB

    if not os.path.exists(db_path):
        print(f"FAIL: no database at {db_path}")
        return 1

    age_days = (datetime.now().timestamp() - os.path.getmtime(db_path)) / 86400
    print(f"DB: {db_path}")
    print(f"    {os.path.getsize(db_path) / 1024:.0f} KB, "
          f"last modified {age_days:.1f} days ago\n")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # ── 1. schema check ────────────────────────────────────────────────
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(calls)")}
    has_live = "peak_mc_live" in cols

    print("=" * 62)
    print("1. SCHEMA")
    print("=" * 62)
    if has_live:
        print("  peak_mc_live present -> using per-CALL peaks (correct)")
        peak_col = "c.peak_mc_live"
    else:
        print("  !! peak_mc_live MISSING -- this DB predates memedash v1.22.")
        print("     Falling back to tokens.peak_mc_dash, which OVERSTATES late")
        print("     callers (they inherit pumps that happened before the call).")
        print("     Treat every number below as an upper bound only.")
        peak_col = "t.peak_mc_dash"

    # ── 2. overall counts ──────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("2. OVERALL")
    print("=" * 62)
    for row in conn.execute(
        "SELECT chain, COUNT(*) n FROM tokens GROUP BY chain ORDER BY n DESC"
    ):
        print(f"  tokens  {row['chain']:<5} {row['n']:>6}")
    for row in conn.execute(
        "SELECT chain, COUNT(*) n FROM calls GROUP BY chain ORDER BY n DESC"
    ):
        print(f"  calls   {row['chain']:<5} {row['n']:>6}")

    span = conn.execute(
        "SELECT MIN(called_at) a, MAX(called_at) b FROM calls"
    ).fetchone()
    print(f"\n  call range: {ts(span['a'])} -> {ts(span['b'])}")

    # ── 3+4+5. SOL cohort ──────────────────────────────────────────────
    rows = conn.execute(f"""
        SELECT c.address, c.sender_name, c.group_name, c.called_at,
               c.first_mc, {peak_col} AS peak
        FROM calls c
        JOIN tokens t ON t.address = c.address
        WHERE c.chain = 'SOL' AND c.first_mc > 0
    """).fetchall()

    print("\n" + "=" * 62)
    print("3. SOL MULTIPLIER DISTRIBUTION")
    print("=" * 62)
    print(f"  SOL calls with first_mc > 0: {len(rows)}")

    if not rows:
        print("  nothing to analyse.")
        return 0

    mults = []
    for r in rows:
        peak = r["peak"] or 0
        # a peak below entry means the poller never observed it; treat as 1x
        mults.append((r, max(peak / r["first_mc"], 1.0) if peak else 1.0))

    buckets = [(0, 1.5), (1.5, 2), (2, 3), (3, 5), (5, 10), (10, 1e9)]
    for lo, hi in buckets:
        n = sum(1 for _, m in mults if lo <= m < hi)
        label = f"{lo:g}x-{hi:g}x" if hi < 1e9 else f"{lo:g}x+"
        bar = "#" * int(50 * n / len(mults))
        print(f"  {label:>9} {n:>5}  {bar}")

    unobserved = sum(1 for r, _ in mults if not r["peak"])
    if unobserved:
        print(f"\n  NOTE: {unobserved} calls have no peak recorded at all "
              f"({100*unobserved/len(mults):.0f}%).")
        print("        These are indistinguishable from true losers here.")

    print("\n" + "=" * 62)
    print("4. WINNER COHORTS (unique SOL tokens)")
    print("=" * 62)
    print(f"  {'threshold':>10} {'calls':>7} {'tokens':>8}   verdict")
    for th in WINNER_THRESHOLDS:
        hits = [r for r, m in mults if m >= th]
        toks = len({r["address"] for r in hits})
        if toks >= 100:
            verdict = "plenty"
        elif toks >= 40:
            verdict = "workable"
        elif toks >= 15:
            verdict = "thin - wide error bars"
        else:
            verdict = "TOO FEW - do not use"
        print(f"  {th:>9.0f}x {len(hits):>7} {toks:>8}   {verdict}")

    print("\n" + "=" * 62)
    print("5. CONTROL GROUP (losers, needed to de-bias wallet ranking)")
    print("=" * 62)
    losers = {r["address"] for r, m in mults if m < LOSER_MAX}
    print(f"  tokens under {LOSER_MAX:g}x: {len(losers)}")
    print("  Without these, a sniper bot that buys everything scores as alpha.")

    # ── 6. time split ──────────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("6. TIME SPLIT (for held-out validation)")
    print("=" * 62)
    per_month = defaultdict(lambda: [0, 0])
    for r, m in mults:
        slot = per_month[month(r["called_at"])]
        slot[0] += 1
        if m >= 5.0:
            slot[1] += 1

    print(f"  {'month':>9} {'calls':>7} {'5x+':>6}")
    for mo in sorted(per_month):
        n, w = per_month[mo]
        print(f"  {mo:>9} {n:>7} {w:>6}")

    if len(per_month) < 2:
        print("\n  !! Only one month of data -- a time-held-out test is not")
        print("     possible yet. Any wallet cohort you find will be in-sample.")
    else:
        print("\n  Split at a month boundary: discover on the earlier months,")
        print("  validate the cohort's hit rate on the latest month only.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
