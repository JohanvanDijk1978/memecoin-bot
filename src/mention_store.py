"""
mention_store.py
────────────────
In-memory store for coin mentions with persistent storage for CA history.
CA history survives bot restarts via a JSON file.
"""

import re
import time
import json
import os
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List

logger = logging.getLogger(__name__)

# ── Regex patterns ────────────────────────────────────────────────────────────
SOL_ADDRESS_RE = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')
ETH_ADDRESS_RE = re.compile(r'\b0x[a-fA-F0-9]{40}\b')

HISTORY_FILE = "data/ca_history.json"


@dataclass
class Mention:
    address: str
    chain: str
    source: str
    timestamp: float
    raw_text: str
    group_name: str = ""
    sender_name: str = ""
    market_cap: float = 0.0


class MentionStore:
    def __init__(self):
        self._mentions: Dict[str, List[Mention]] = defaultdict(list)
        self._ca_history: Dict[str, List[dict]] = {}  # persistent CA history
        self._load_history()

    def _load_history(self):
        """Load CA history from disk."""
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, "r") as f:
                    self._ca_history = json.load(f)
                logger.info(f"✅ Loaded CA history: {len(self._ca_history)} CAs")
        except Exception as e:
            logger.warning(f"Could not load CA history: {e}")
            self._ca_history = {}

    def _save_history(self):
        """Save CA history to disk."""
        try:
            os.makedirs("data", exist_ok=True)
            with open(HISTORY_FILE, "w") as f:
                json.dump(self._ca_history, f)
        except Exception as e:
            logger.warning(f"Could not save CA history: {e}")

    def add_message(self, text: str, source: str, group_name: str = "", sender_name: str = "", market_cap: float = 0.0, ticker: str = "", sender_id: str = "", chain_id: str = ""):
        """Parse a raw message and store any contract address mentions."""
        now = time.time()

        # Handle CA: prefixed entries (detailed history entries)
        if text.startswith("CA:"):
            address = text[3:]
            chain = "ETH" if address.startswith("0x") else "SOL"
            mention = Mention(
                address=address,
                chain=chain,
                source=source,
                timestamp=now,
                raw_text="",
                group_name=group_name,
                sender_name=sender_name,
                market_cap=market_cap,
            )
            self._mentions[f"CA:{address}"].append(mention)

            # Persist to history file
            if address not in self._ca_history:
                self._ca_history[address] = []

            # If group already recorded, increment scan_count; else add new entry
            existing_groups = {e["group_name"]: i for i, e in enumerate(self._ca_history[address])}
            if group_name in existing_groups:
                idx = existing_groups[group_name]
                self._ca_history[address][idx]["scan_count"] = self._ca_history[address][idx].get("scan_count", 1) + 1
                # Update peak MC if new scan is higher
                if market_cap > 0:
                    current_peak = self._ca_history[address][idx].get("peak_mc", 0)
                    self._ca_history[address][idx]["peak_mc"] = max(current_peak, market_cap)
                if ticker and not self._ca_history[address][idx].get("ticker"):
                    self._ca_history[address][idx]["ticker"] = ticker
            
            else:
                self._ca_history[address].append({
                    "group_name":  group_name,
                    "sender_name": sender_name,
                    "sender_id":   sender_id,   # "dc:<id>" / "tg:<id>"; "" for legacy entries
                    "chain_id":    chain_id,    # dexscreener chainId when the source knows it
                    "market_cap":  market_cap,
                    "first_mc":    market_cap,
                    "peak_mc":     market_cap,
                    "timestamp":   now,
                    "source":      source,
                    "scan_count":  1,
                    "ticker":      ticker,   # will be updated by telegram_scraper
                    "address":     address,
                })
            self._save_history()
            return

        addresses = []
        for m in SOL_ADDRESS_RE.finditer(text):
            addresses.append((m.group(), "SOL"))
        for m in ETH_ADDRESS_RE.finditer(text):
            addresses.append((m.group(), "ETH"))

        for addr, chain in addresses:
            mention = Mention(
                address=addr,
                chain=chain,
                source=source,
                timestamp=now,
                raw_text=text[:300],
                group_name=group_name,
                sender_name=sender_name,
                market_cap=market_cap,
            )
            self._mentions[addr].append(mention)

    def backfill_market_cap(self, address: str, group_name: str = "",
                            market_cap: float = 0.0, ticker: str = "",
                            chain_id: str = "") -> int:
        """Fill in the price a call was recorded without. Returns rows touched.

        A CA pinged before Dexscreener indexed it is stored with market_cap 0,
        which reads as "N/A" in every later alert and makes the call
        un-scoreable on the leaderboard (`first_mc <= 0` is skipped there).
        When the re-check loop finally gets a quote, this writes it back.

        Deliberately not `add_message`: that would also bump `scan_count` and
        inflate "scanned Nx". Only rows still sitting at zero are touched, so
        a real later scan is never overwritten. `first_mc` becomes the price at
        re-check time, which is minutes after the call rather than at it — an
        approximation, but a far better one than zero.
        """
        if market_cap <= 0:
            return 0
        entries = self._ca_history.get(address, [])
        if not entries:
            return 0

        targets = [e for e in entries if e.get("group_name") == group_name] if group_name else []
        if not targets:
            targets = entries

        touched = 0
        for entry in targets:
            changed = False
            if not entry.get("market_cap"):
                entry["market_cap"] = market_cap
                changed = True
            if not entry.get("first_mc"):
                entry["first_mc"] = market_cap
                changed = True
            if market_cap > entry.get("peak_mc", 0):
                entry["peak_mc"] = market_cap
                changed = True
            if ticker and not entry.get("ticker"):
                entry["ticker"] = ticker
                changed = True
            if chain_id and not entry.get("chain_id"):
                entry["chain_id"] = chain_id
                changed = True
            touched += 1 if changed else 0

        if touched:
            self._save_history()
            logger.info(f"📈 Backfilled {touched} history row(s) for {address} at {market_cap:.0f}")
        return touched

    def get_scan_stats(self, address: str) -> tuple:
        """Return (total_scans, unique_groups) for a CA."""
        entries = self._ca_history.get(address, [])
        # total = sum of all scan_count fields, or number of entries
        total = sum(e.get("scan_count", 1) for e in entries)
        groups = len({e.get("group_name", "") for e in entries})
        return total, groups

    def get_ca_history(self, address: str, limit: int = 3) -> List[Mention]:
        """Return the first N mentions of a CA — from persistent storage."""
        entries = self._ca_history.get(address, [])
        entries_sorted = sorted(entries, key=lambda e: e["timestamp"])[:limit]

        result = []
        for e in entries_sorted:
            result.append(Mention(
                address=address,
                chain="ETH" if address.startswith("0x") else "SOL",
                source=e.get("source", ""),
                timestamp=e["timestamp"],
                raw_text="",
                group_name=e.get("group_name", ""),
                sender_name=e.get("sender_name", ""),
                market_cap=e.get("market_cap", 0.0),
            ))
        return result

    def clear_old(self, keep_hours: float = 24):
        """Prune in-memory mentions older than keep_hours."""
        cutoff = time.time() - keep_hours * 3600
        for addr in list(self._mentions.keys()):
            self._mentions[addr] = [m for m in self._mentions[addr] if m.timestamp >= cutoff]
            if not self._mentions[addr]:
                del self._mentions[addr]

        # Also prune persistent history older than 7 days
        week_ago = time.time() - 7 * 24 * 3600
        changed = False
        for addr in list(self._ca_history.keys()):
            self._ca_history[addr] = [e for e in self._ca_history[addr] if e["timestamp"] > week_ago]
            if not self._ca_history[addr]:
                del self._ca_history[addr]
                changed = True
        if changed:
            self._save_history()

    def get_leaderboard(self) -> dict:
        group_stats = {}   # group_name -> {calls, total_mult, peak_mult, callers: {name -> {calls, total_mult, peak_mult}}}
    
        for address, entries in self._ca_history.items():
            seen_callers = set()  # track (group, sender) to count only first call
            for entry in sorted(entries, key=lambda e: e["timestamp"]):  # oldest first
                group   = entry.get("group_name", "Unknown")
                sender  = entry.get("sender_name", "Unknown")
                first_mc = entry.get("first_mc", 0)
                peak_mc  = entry.get("peak_mc", 0)
                ticker   = entry.get("ticker", "")
                addr     = entry.get("address", address)

                if first_mc <= 0 or peak_mc <= 0:
                    continue

                caller_key = (group, sender)
                if caller_key in seen_callers:
                    continue  # skip duplicate calls from same user in same group
                seen_callers.add(caller_key)

                mult = peak_mc / first_mc

                # Group stats
                if group not in group_stats:
                    group_stats[group] = {
                        "calls": 0, "total_mult": 0, "peak_mult": 0,
                        "best_call": None,  # {sender, ticker, address, first_mc, mult}
                        "callers": {}
                }
                group_stats[group]["calls"] += 1
                group_stats[group]["total_mult"] += mult

                if mult > group_stats[group]["peak_mult"]:
                    group_stats[group]["peak_mult"] = mult
                    group_stats[group]["best_call"] = {
                        "sender":   sender,
                        "ticker":   ticker,
                        "address":  addr,
                        "first_mc": first_mc,
                        "mult":     mult,
                }

                # Caller stats within group
                callers = group_stats[group]["callers"]
                if sender not in callers:
                    callers[sender] = {"calls": 0, "total_mult": 0, "peak_mult": 0, "best_ticker": "", "best_address": "", "best_first_mc": 0, "best_group": group}
                callers[sender]["calls"]      += 1
                callers[sender]["total_mult"] += mult
                if mult > callers[sender]["peak_mult"]:
                    callers[sender]["peak_mult"]     = mult
                    callers[sender]["best_ticker"]   = ticker
                    callers[sender]["best_address"]  = addr
                    callers[sender]["best_first_mc"] = first_mc
                    callers[sender]["best_group"]    = group

        return group_stats



# Singleton shared across all scrapers
store = MentionStore()
