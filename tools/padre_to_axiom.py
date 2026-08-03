#!/usr/bin/env python3
"""Convert a Padre wallet-tracker preset into Axiom preset format.

Padre entry:
    {"trackedWalletAddress": "...", "name": "...", "emoji": "...", "alertsOn": true}

Axiom entry:
    {"trackedWalletAddress": "...", "name": "...", "emoji": "...",
     "alertsOnToast": true, "alertsOnBubble": true, "alertsOnFeed": true,
     "groups": ["Main"], "sound": "default"}

Padre's single `alertsOn` flag fans out to all three Axiom alert channels.
Output REPLACES any existing Axiom list (no merge).

Usage:
    python padre_to_axiom.py padre.json -o axiom.json
    python padre_to_axiom.py padre.json --group Copytrade --sound ping
    cat padre.json | python padre_to_axiom.py -          # stdin -> stdout
"""

import argparse
import json
import sys

DEFAULT_GROUP = "Main"
DEFAULT_SOUND = "default"


def convert_entry(entry, group, sound):
    addr = entry.get("trackedWalletAddress") or entry.get("address") or entry.get("wallet")
    if not addr:
        raise ValueError(f"entry has no wallet address: {entry!r}")

    alerts = entry.get("alertsOn", True)

    return {
        "trackedWalletAddress": addr,
        "name": entry.get("name", "") or addr[:4] + ".." + addr[-4:],
        "emoji": entry.get("emoji", "") or "\U0001F464",
        "alertsOnToast": bool(alerts),
        "alertsOnBubble": bool(alerts),
        "alertsOnFeed": bool(alerts),
        "groups": [group],
        "sound": sound,
    }


def convert(padre_list, group=DEFAULT_GROUP, sound=DEFAULT_SOUND):
    out, seen, dupes = [], set(), []
    for entry in padre_list:
        converted = convert_entry(entry, group, sound)
        addr = converted["trackedWalletAddress"]
        if addr in seen:
            dupes.append(converted["name"])
            continue
        seen.add(addr)
        out.append(converted)
    return out, dupes


def load(path):
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    raw = raw.strip()
    # Tolerate a pasted fragment missing its opening/closing bracket.
    if not raw.startswith("["):
        raw = "[" + raw.lstrip(",")
    if not raw.endswith("]"):
        raw = raw.rstrip(",") + "]"
    data = json.loads(raw)
    if isinstance(data, dict):
        for key in ("wallets", "trackedWallets", "presets", "items"):
            if key in data:
                return data[key]
        return [data]
    return data


def main():
    ap = argparse.ArgumentParser(description="Padre -> Axiom wallet preset converter")
    ap.add_argument("input", help="Padre JSON file, or - for stdin")
    ap.add_argument("-o", "--output", help="output file (default: stdout)")
    ap.add_argument("--group", default=DEFAULT_GROUP, help=f"Axiom group name (default: {DEFAULT_GROUP})")
    ap.add_argument("--sound", default=DEFAULT_SOUND, help=f"Axiom sound (default: {DEFAULT_SOUND})")
    args = ap.parse_args()

    padre = load(args.input)
    axiom, dupes = convert(padre, args.group, args.sound)
    text = json.dumps(axiom, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"\U0001FA99 {len(axiom)} wallets -> {args.output}", file=sys.stderr)
    else:
        print(text)

    if dupes:
        print(f"skipped {len(dupes)} duplicate address(es): {', '.join(dupes)}", file=sys.stderr)


if __name__ == "__main__":
    main()
