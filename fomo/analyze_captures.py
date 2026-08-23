#!/usr/bin/env python3
"""
analyze_captures.py — turns a signup-experiment capture directory into an answer.

It reads whatever `fomo_experiment_recorder.py` wrote and answers, in order:

  1. Which of `address`, `evmAddress`, `activated`, `createdAt` exist on a
     zero-transaction account, and what they hold.
  2. Every Solana / EVM address that appeared anywhere in any captured body,
     with the endpoint and the JSON path it came from — so "where did this come
     from" is never a guess.
  3. Whether any address you already know (wallet_notes.txt, --expect) shows up
     before a trade.
  4. What changed between Phase A and Phase B.

It draws no conclusion it cannot show the evidence for, and it writes
FINDINGS_REPORT.md next to the captures.

    python analyze_captures.py --dir hunt_out/signup_20260821
    python analyze_captures.py --dir hunt_out/signup_20260821 --expect 93fjdw...,0xabc...
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

B58_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")

ADDRESS_FIELD_HINTS = (
    "address", "wallet", "pubkey", "publickey", "owner", "signer", "account",
    "delegate", "recipient", "receiver", "deposit", "treasury", "payer",
    "smartaccount", "embedded",
)

# A mint is an address and it lives in a field called `tokenAddress`, so a
# substring test for "address" ranks every market on the platform as an
# identity candidate. Markets are excluded structurally instead.
TOKEN_FIELD_RE = re.compile(r"(token.?address|^mint$|mintaddress|pair|pool|"
                            r"contract.?address|lp.?address)", re.I)
TOKEN_PATH_MARKERS = (".token.", "tokens[", ".mint", "intoken", "outtoken")

IDENTITY_LEAVES = {
    "address", "evmaddress", "solanaaddress", "soladdress", "walletaddress",
    "useraddress", "owner", "signer", "recipient", "sender", "publickey",
    "pubkey", "payer", "smartaccount", "smartaccountaddress", "embeddedwallet",
    "depositaddress", "receiveaddress", "fundingaddress",
}

# Structural noise: programs, sentinels and the known FOMO gas sponsor. These
# are excluded from "candidate wallet" but still listed if you ask for -v.
KNOWN_INFRA = {
    "11111111111111111111111111111111": "Solana system program",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "SPL Token program",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "Associated token program",
    "So11111111111111111111111111111111111111112": "Wrapped SOL",
    "ComputeBudget111111111111111111111111111111": "Compute budget program",
    "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51": "FOMO gas sponsor",
    "0x0000000000000000000000000000000000000000": "zero address",
    "0x000000000000000000000000000000000000dEaD": "burn address",
}

PROFILE_FIELDS = ("id", "handle", "userHandle", "address", "evmAddress",
                  "activated", "createdAt", "updatedAt")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------- extraction ----------------

def walk(obj: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    """Yield (json_path, scalar) for every leaf in a parsed body."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def field_kind(path: str) -> str:
    """'token' (a market), 'identity' (someone's wallet), or 'other'."""
    lowered = (path or "").lower()
    leaf = lowered.split("→")[-1].strip().split(".")[-1].split("[")[0]
    if TOKEN_FIELD_RE.search(leaf) or any(m in lowered for m in TOKEN_PATH_MARKERS):
        return "token"
    if leaf in IDENTITY_LEAVES:
        return "identity"
    if any(h in leaf for h in ADDRESS_FIELD_HINTS):
        return "identity"
    return "other"


def field_is_addressy(path: str) -> bool:
    return field_kind(path) == "identity"


def hit_scope(hit: "Hit", me_tokens: tuple[str, ...]) -> str:
    """
    Whose address is this likely to be? The experiment only cares about the
    test account's own, and the platform's feeds are full of other people's.
    """
    if hit.url == "localStorage":
        return "client"
    if "privy.io" in hit.url or "privy.systems" in hit.url:
        return "privy"
    if me_tokens and any(t and t in hit.url for t in me_tokens):
        return "mine"
    return "global"


class Hit:
    __slots__ = ("addr", "kind", "phase", "url", "path", "addressy")

    def __init__(self, addr, kind, phase, url, path, addressy):
        self.addr, self.kind, self.phase = addr, kind, phase
        self.url, self.path, self.addressy = url, path, addressy


def scan_body(body: str, phase: str, url: str) -> list[Hit]:
    hits: list[Hit] = []
    parsed = None
    try:
        parsed = json.loads(body)
    except Exception:
        parsed = None

    if parsed is not None:
        for path, value in walk(parsed):
            if not isinstance(value, str):
                continue
            addressy = field_is_addressy(path)
            for m in EVM_RE.findall(value):
                hits.append(Hit(m, "evm", phase, url, path, addressy))
            for m in B58_RE.findall(value):
                hits.append(Hit(m, "sol", phase, url, path, addressy))
    else:
        for m in EVM_RE.findall(body):
            hits.append(Hit(m, "evm", phase, url, "<raw>", False))
        for m in B58_RE.findall(body):
            hits.append(Hit(m, "sol", phase, url, "<raw>", False))
    return hits


def scan_localstorage(capture_dir: Path, phase: str) -> list[Hit]:
    """
    The client's own localStorage is part of the pre-trade surface: Privy keeps
    its embedded-wallet connection there, and the analytics SDK keeps whatever
    the app told it about the user. Both are readable with zero requests, so
    anything an address shows up in here is observable before any trade.
    """
    path = capture_dir / f"phase_{phase}_localstorage.json"
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    hits: list[Hit] = []
    for key, value in (doc.get("keys") or {}).items():
        if not isinstance(value, str) or "[REDACTED" in value:
            continue
        inner = None
        try:
            inner = json.loads(value)
        except Exception:
            inner = None
        if inner is not None and isinstance(inner, (dict, list)):
            for jpath, leaf in walk(inner):
                if not isinstance(leaf, str):
                    continue
                full = f"{key} → {jpath}" if jpath else key
                addressy = field_is_addressy(jpath) or field_is_addressy(key)
                for m in EVM_RE.findall(leaf):
                    hits.append(Hit(m, "evm", phase, "localStorage", full, addressy))
                for m in B58_RE.findall(leaf):
                    hits.append(Hit(m, "sol", phase, "localStorage", full, addressy))
        else:
            for m in EVM_RE.findall(value):
                hits.append(Hit(m, "evm", phase, "localStorage", key, field_is_addressy(key)))
            for m in B58_RE.findall(value):
                hits.append(Hit(m, "sol", phase, "localStorage", key, field_is_addressy(key)))
    return hits


def short_url(url: str) -> str:
    if url == "localStorage":
        return "localStorage"
    url = url.split("?", 1)[0]
    for prefix in ("https://prod-api.fomo.family", "https://fomo.family",
                   "https://auth.privy.io"):
        if url.startswith(prefix):
            return prefix.split("//")[1].split(".")[0] + url[len(prefix):]
    return url


# ---------------- profile handling ----------------

def unwrap_user(obj: Any) -> dict | None:
    """FOMO wraps every payload in `responseObject`; Privy uses `user`."""
    if not isinstance(obj, dict):
        return None
    for key in ("responseObject", "user"):
        inner = obj.get(key)
        if isinstance(inner, dict):
            found = unwrap_user(inner)
            if found is not None:
                return found
    if any(f in obj for f in ("address", "evmAddress", "handle", "userHandle")):
        return obj
    return None


def load_profile(capture_dir: Path, phase: str) -> dict:
    """Return {url: user_object} for a phase's snapshot, if there is one."""
    path = capture_dir / f"phase_{phase}_profile.json"
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for entry in doc.get("results", []):
        user = unwrap_user(entry.get("parsed"))
        if user is not None:
            out[entry.get("url", "?")] = user
    return out


def profile_table(users: dict) -> list[str]:
    lines = []
    for url, user in users.items():
        lines.append(f"**`{url}`**\n")
        lines.append("| field | present? | value |")
        lines.append("|---|---|---|")
        for f in PROFILE_FIELDS:
            if f in user:
                v = user[f]
                lines.append(f"| `{f}` | yes | `{v!r}` |")
            else:
                lines.append(f"| `{f}` | **absent** | — |")
        lines.append("")
    return lines


# ---------------- main ----------------

def load_phase(capture_dir: Path, phase: str) -> list[dict]:
    path = capture_dir / f"phase_{phase}_responses.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def read_expected(capture_dir: Path, extra: str | None) -> dict[str, str]:
    """Addresses you already know, from wallet_notes.txt and --expect."""
    known: dict[str, str] = {}
    for candidate in (capture_dir / "wallet_notes.txt",
                      capture_dir.parent.parent / "wallet_notes.txt",
                      Path("wallet_notes.txt")):
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                for m in EVM_RE.findall(line) + B58_RE.findall(line):
                    known[m] = line.strip()[:80] or str(candidate)
    if extra:
        for token in re.split(r"[,\s]+", extra):
            token = token.strip()
            if token:
                known[token] = "--expect"
    return known


def main() -> int:
    ap = argparse.ArgumentParser(description="analyse signup-experiment captures")
    ap.add_argument("--dir", required=True, help="capture directory")
    ap.add_argument("--expect", help="comma-separated addresses you already know")
    ap.add_argument("--me", help="your handle / user id / privy did — any URL "
                                 "containing one is treated as your own data")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="list every address, including infra and one-offs")
    args = ap.parse_args()

    capture_dir = Path(args.dir).resolve()
    if not capture_dir.exists():
        print(f"no such directory: {capture_dir}")
        return 2

    phases = [p for p in ("a", "b", "c")
              if (capture_dir / f"phase_{p}_responses.jsonl").exists()
              or (capture_dir / f"phase_{p}_localstorage.json").exists()]
    if not phases:
        print(f"no capture files in {capture_dir}")
        return 2

    known = read_expected(capture_dir, args.expect)
    profiles = {p: load_profile(capture_dir, p) for p in ("a", "b", "c")}

    # The account's own published (synthetic) addresses, from any snapshot.
    synthetic: dict[str, str] = {}
    for p, users in profiles.items():
        for user in users.values():
            for f in ("address", "evmAddress"):
                v = user.get(f)
                if isinstance(v, str) and v:
                    synthetic[v] = f"user.{f} (phase {p})"

    all_hits: list[Hit] = []
    counts_per_phase: dict[str, int] = {}
    ls_hits_per_phase: dict[str, int] = {}
    for p in phases:
        rows = load_phase(capture_dir, p)
        counts_per_phase[p] = len(rows)
        for row in rows:
            body = row.get("body") or ""
            if not body or body.startswith("[body unavailable"):
                continue
            all_hits.extend(scan_body(body, p, row.get("url", "?")))
        ls = scan_localstorage(capture_dir, p)
        ls_hits_per_phase[p] = len({h.addr for h in ls})
        all_hits.extend(ls)

    by_addr: dict[str, list[Hit]] = defaultdict(list)
    for h in all_hits:
        by_addr[h.addr].append(h)

    def classify(addr: str) -> str:
        if addr in KNOWN_INFRA:
            return "infra"
        if addr in synthetic:
            return "synthetic"
        if addr in known:
            return "KNOWN"
        return "candidate"

    me_tokens = tuple(t.strip() for t in re.split(r"[,\s]+", args.me or "") if t.strip())

    # Tiered, because "appeared in a field with 'address' in the name" is not a
    # useful filter on a social trading app: most such fields are markets, and
    # most of the rest are other people. What the experiment asks about is the
    # test account's own pre-trade surface.
    tiers: dict[str, dict[str, list[Hit]]] = {
        "client": defaultdict(list),   # localStorage — zero requests to read
        "privy": defaultdict(list),    # the wallet provider's own responses
        "mine": defaultdict(list),     # endpoints addressed to this account
        "global": defaultdict(list),   # everyone else's data
    }
    n_token_fields = set()
    for addr, hits in by_addr.items():
        for h in hits:
            if field_kind(h.path) == "token":
                n_token_fields.add(addr)
                continue
            if not h.addressy:
                continue
            tiers[hit_scope(h, me_tokens)][addr].append(h)

    def tier_list(name: str) -> list[str]:
        return sorted((a for a in tiers[name] if classify(a) in ("candidate", "KNOWN")),
                      key=lambda a: -len(tiers[name][a]))

    candidates = tier_list("client") + [a for a in tier_list("privy")
                                        if a not in tiers["client"]]
    for a in tier_list("mine"):
        if a not in tiers["client"] and a not in tiers["privy"]:
            candidates.append(a)
    known_found = [a for a in by_addr if a in known]

    # ---------------- console ----------------
    print("=" * 74)
    print(f"CAPTURE ANALYSIS — {capture_dir}")
    print("=" * 74)
    for p in phases:
        print(f"  phase {p.upper()}: {counts_per_phase[p]} responses with bodies, "
              f"{ls_hits_per_phase.get(p, 0)} distinct addresses in localStorage")
    print(f"  distinct addresses seen: {len(by_addr)}")
    print(f"  account's own published addresses: {len(synthetic)}")
    print(f"  addresses you already knew: {len(known)} "
          f"({len(known_found)} of them appear in the captures)")
    print()

    if synthetic:
        print("Published (synthetic) addresses on this account:")
        for addr, where in synthetic.items():
            print(f"  {addr}   ← {where}")
        print()

    if known_found:
        print("!! ADDRESSES YOU ALREADY KNEW, FOUND IN THE CAPTURES:")
        for addr in known_found:
            hits = by_addr[addr]
            first = min(hits, key=lambda h: (h.phase, h.url))
            print(f"  {addr}")
            print(f"     note      : {known[addr]}")
            print(f"     first seen: phase {first.phase.upper()} "
                  f"{short_url(first.url)} → {first.path}")
            print(f"     occurrences: {len(hits)} across "
                  f"{len({h.url for h in hits})} endpoints")
        print()
    else:
        print("No address from wallet_notes.txt / --expect appears in any capture.\n")

    TIER_TITLES = {
        "client": "CLIENT-SIDE SURFACE — localStorage, readable with zero requests",
        "privy": "PRIVY RESPONSES — the wallet provider's own view of this account",
        "mine": "THIS ACCOUNT'S OWN ENDPOINTS",
        "global": "PLATFORM-WIDE FEEDS — other people's wallets",
    }
    for tier in ("client", "privy", "mine", "global"):
        addrs = tier_list(tier)
        if tier == "global" and not args.verbose:
            print(f"{TIER_TITLES[tier]}: {len(addrs)} addresses (use -v to list)\n")
            continue
        print(f"{TIER_TITLES[tier]}: {len(addrs)} addresses")
        if not addrs:
            print("  (none)\n")
            continue
        for addr in addrs[: (None if args.verbose else 20)]:
            hits = tiers[tier][addr]
            phases_seen = "".join(sorted({h.phase.upper() for h in hits}))
            mark = "  ← YOU ALREADY KNEW THIS ONE" if addr in known else ""
            print(f"  [{phases_seen}] {addr}{mark}")
            for pth in sorted({f"{short_url(h.url)} → {h.path}" for h in hits})[:6]:
                print(f"        {pth}")
        print()

    print(f"Suppressed as market/token fields: {len(n_token_fields)} addresses "
          f"(tokenAddress, mint, token.*, pair/pool)\n")

    # ---------------- A vs B diff ----------------
    diff_lines: list[str] = []
    ua = next(iter(profiles.get("a", {}).values()), None)
    ub = next(iter(profiles.get("b", {}).values()), None)
    if ua and ub:
        for f in sorted(set(ua) | set(ub)):
            va, vb = ua.get(f, "<absent>"), ub.get(f, "<absent>")
            if va != vb:
                diff_lines.append(f"| `{f}` | `{va!r}` | `{vb!r}` |")
        print("Phase A → Phase B profile diff:")
        if diff_lines:
            for line in diff_lines:
                print("  " + line.replace("|", " ").strip())
        else:
            print("  nothing changed.")
        print()

    # ---------------- report ----------------
    out = [
        "# Signup wallet-capture — findings",
        "",
        f"*Generated {now()} from `{capture_dir.name}`.*",
        "",
        "## What was captured",
        "",
        "| phase | responses with bodies |",
        "|---|---|",
    ]
    out += [f"| {p.upper()} | {counts_per_phase[p]} |" for p in phases]
    out += ["", "## Profile object on a zero-transaction account", ""]
    for p in ("a", "b", "c"):
        if profiles.get(p):
            out.append(f"### Phase {p.upper()}")
            out.append("")
            out += profile_table(profiles[p])
    if not any(profiles.values()):
        out += ["_No profile snapshot was taken._", ""]

    out += ["## Phase A → Phase B diff", ""]
    if ua and ub:
        if diff_lines:
            out += ["| field | phase A | phase B |", "|---|---|---|"] + diff_lines
        else:
            out.append("No field changed between the two snapshots.")
    else:
        out.append("_Not available — both phases need a snapshot._")
    out.append("")

    out += ["## Addresses you already knew", ""]
    if known_found:
        out += ["| address | first seen | endpoint | path |", "|---|---|---|---|"]
        for addr in known_found:
            first = min(by_addr[addr], key=lambda h: (h.phase, h.url))
            out.append(f"| `{addr}` | phase {first.phase.upper()} | "
                       f"`{short_url(first.url)}` | `{first.path}` |")
    else:
        out.append("**None of them appear anywhere in the captured traffic.**")
    out.append("")

    out += ["## Addresses tied to this account, by where they were readable", ""]
    any_tier = False
    for tier in ("client", "privy", "mine"):
        addrs = tier_list(tier)
        if not addrs:
            continue
        any_tier = True
        out += [f"### {TIER_TITLES[tier]}", "",
                "| address | phases | where |", "|---|---|---|"]
        for addr in addrs:
            hits = tiers[tier][addr]
            where = "<br>".join(sorted({f"`{short_url(h.url)} → {h.path}`" for h in hits})[:6])
            out.append(f"| `{addr}` | {''.join(sorted({h.phase.upper() for h in hits}))} "
                       f"| {where} |")
        out.append("")
    if not any_tier:
        out += ["No address outside the platform-wide feeds was tied to this "
                "account.", ""]
    out += [f"Platform-wide feeds contributed {len(tier_list('global'))} other "
            f"people's addresses; {len(n_token_fields)} more were market/token "
            f"fields. Both are excluded above.", ""]
    out += ["", "## Interpretation (fill in by hand)", "",
            "- [ ] Real wallet appeared pre-trade → **condition 1**, name the endpoint.",
            "- [ ] Only a signer/owner pubkey appeared pre-trade → check whether the "
            "EVM address is counterfactually derivable → **condition 2**.",
            "- [ ] Nothing pre-trade → earliest exposure remains the first sponsored trade.",
            ""]

    report = capture_dir / "FINDINGS_REPORT.md"
    report.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"Wrote {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
