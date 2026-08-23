#!/usr/bin/env python3
"""
fomo_experiment_recorder.py — response recorder for the signup wallet-capture
experiment (see SIGNUP_CAPTURE_EXPERIMENT.md).

Scope, restated so it stays honest:

  * Observation only. This records traffic the browser makes anyway while a
    real person clicks through signup. It probes nothing except the two
    first-party profile reads the checklist asks for (§2), and only against
    the throwaway account you just created.
  * Nothing here writes to wallet_cache.json or touches any resolver.
  * It refuses to run against the live .chrome-profile, so the throwaway
    account can never share a Privy session with Johan's real one.

Why it is not the obvious 20-line page.on("response") script:

  1. Signup happens across more than one page. Privy's email/OTP flow and any
     OAuth step run in an iframe or a popup, and page-level listeners miss
     popups entirely. So the listener is attached to the *context*.
  2. Phase B has to still be logged in as the account Phase A created. A
     fresh browser context per phase would log out between them, so this uses
     one persistent profile directory for the whole experiment.
  3. Captures are appended to disk as they arrive. A crash, a Ctrl+C or a
     closed window then costs nothing.
  4. Privy is privy.io, not privy.com.

Usage (in fomo/.venv):

    python fomo_experiment_recorder.py --phase a
    python fomo_experiment_recorder.py --phase a --handle myhandle
    python fomo_experiment_recorder.py --phase b --handle myhandle
    python fomo_experiment_recorder.py --phase c --handle myhandle   # gated

Finish a phase by pressing ENTER in this terminal (leave the browser open so
the profile snapshot can run), or just close the browser window.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Windows redirects (`> log.txt`) fall back to the ANSI code page, which cannot
# encode the symbols below. Never let a print kill a running capture.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

APP_ORIGIN = "https://fomo.family"
API_BASE = "https://prod-api.fomo.family"
SUPPORTED_CHAINS_HEADER = "1,56,143,4663,8453,1399811149"

# Hosts whose bodies are worth keeping. Everything else is logged URL-only.
INTERESTING_HOSTS = (
    "fomo.family",
    "privy.io",
    "privy.systems",
    "turnkey.com",
    "helius",
    "alchemy",
    "relay.link",
    "hyperliquid",
)

SKIP_SUFFIXES = (
    ".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".webp", ".avif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".mp4",
    ".webm", ".wasm",
)

MAX_BODY = 2_000_000          # per response, characters
MAX_POST_DATA = 40_000
SETTLE_MS = 9_000             # matches the resolver's SETTLE_MS

# A JWT anywhere inside a value, not just as the whole value. The first version
# of this only matched whole strings, so `privy:token` — whose localStorage value
# is a JSON-quoted string, i.e. `"eyJ…"` with the quotes included — sailed
# straight through unredacted. Substring matching is the fix.
JWT_SUB_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

# Keys whose *entire* value is a credential, JWT-shaped or not. Privy's refresh
# token is opaque base64url, so no shape rule would ever catch it.
CREDENTIAL_KEY_RE = re.compile(
    r"(privy:(token|id_token|refresh_token|pat|access_token))"
    r"|(^authorization$)|(^cookie$)|(^set-cookie$)"
    r"|(refresh_token)|(access_token)|(_secret)|(private_?key)",
    re.I,
)

LIVE_PROFILE_NAMES = {".chrome-profile"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(value: str) -> str:
    """Strip credentials wherever they appear. Tokens are not evidence."""
    if not isinstance(value, str):
        return value
    return JWT_SUB_RE.sub(lambda m: f"[REDACTED JWT len={len(m.group(0))}]", value)


def redact_pair(key: str, value: Any) -> Any:
    """Redact by key first (opaque tokens have no shape), then by shape."""
    if isinstance(value, str) and CREDENTIAL_KEY_RE.search(key or ""):
        return f"[REDACTED {key} len={len(value)}]"
    return redact(value)


class Recorder:
    def __init__(self, capture_dir: Path, phase: str) -> None:
        self.dir = capture_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.phase = phase
        self.responses = self.dir / f"phase_{phase}_responses.jsonl"
        self.requests = self.dir / f"phase_{phase}_requests.jsonl"
        self.urls = self.dir / f"phase_{phase}_urls.txt"
        self._rf = self.responses.open("a", encoding="utf-8")
        self._qf = self.requests.open("a", encoding="utf-8")
        self._uf = self.urls.open("a", encoding="utf-8")
        self.n_resp = 0
        self.n_body = 0
        self.n_req = 0

    # ---------------- filtering ----------------

    @staticmethod
    def _interesting(url: str) -> bool:
        return any(h in url for h in INTERESTING_HOSTS)

    @staticmethod
    def _is_asset(url: str) -> bool:
        path = url.split("?", 1)[0].split("#", 1)[0].lower()
        return path.endswith(SKIP_SUFFIXES)

    def _wants_body(self, url: str, content_type: str) -> bool:
        if self._is_asset(url):
            return False
        if not self._interesting(url):
            return False
        ct = (content_type or "").lower()
        if not ct:
            return True
        return any(k in ct for k in ("json", "text/plain", "text/html", "javascript-object"))

    # ---------------- writers ----------------

    def _write(self, handle, obj: dict) -> None:
        handle.write(json.dumps(obj, ensure_ascii=False) + "\n")
        handle.flush()

    def on_request(self, request) -> None:
        try:
            url = request.url
            self._uf.write(f"{now()}\t{request.method}\t{url}\n")
            self._uf.flush()
            if self._is_asset(url) or not self._interesting(url):
                return
            post = None
            try:
                post = request.post_data
            except Exception:
                post = None
            self.n_req += 1
            self._write(self._qf, {
                "ts": now(),
                "method": request.method,
                "url": url,
                "resource_type": request.resource_type,
                "post_data": redact((post or "")[:MAX_POST_DATA]),
            })
        except Exception as exc:  # never let capture break the browsing
            print(f"  ! request capture error: {exc}", file=sys.stderr)

    async def on_response(self, response) -> None:
        try:
            url = response.url
            headers = {}
            try:
                headers = {k: redact_pair(k, v) for k, v in (await response.all_headers()).items()}
            except Exception:
                pass
            ctype = headers.get("content-type", "")
            if not self._wants_body(url, ctype):
                return
            body = ""
            try:
                body = redact((await response.text())[:MAX_BODY])
            except Exception as exc:
                body = f"[body unavailable: {exc}]"
            self.n_resp += 1
            if body and not body.startswith("[body unavailable"):
                self.n_body += 1
            self._write(self._rf, {
                "ts": now(),
                "url": url,
                "status": response.status,
                "content_type": ctype,
                "headers": headers,
                "body": body,
            })
            if self.n_resp % 10 == 0:
                print(f"  … {self.n_resp} responses captured", flush=True)
        except Exception as exc:
            print(f"  ! response capture error: {exc}", file=sys.stderr)

    def save_json(self, name: str, obj: Any) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def close(self) -> None:
        for f in (self._rf, self._qf, self._uf):
            try:
                f.close()
            except Exception:
                pass


# ---------------- in-page helpers ----------------

# Runs inside the fomo.family page, so Cloudflare sees Chrome and the app's own
# live Privy token supplies auth. Same pattern as fomo_browser.py.
_FETCH_JS = """
async ({ urls, chains }) => {
  const readToken = () => {
    for (const key of ['privy:token', 'privy:id_token']) {
      const raw = localStorage.getItem(key);
      if (!raw) continue;
      try { const v = JSON.parse(raw); if (typeof v === 'string' && v) return v; }
      catch (_) { if (raw) return raw; }
    }
    return null;
  };
  const token = readToken();
  const out = [];
  for (const url of urls) {
    try {
      const headers = {
        'Accept': 'application/json, text/plain, */*',
        'x-supported-chains': chains,
      };
      if (token) headers['Authorization'] = 'Bearer ' + token;
      const r = await fetch(url, { method: 'GET', credentials: 'include',
                                   cache: 'no-store', headers });
      out.push({ url, ok: true, status: r.status, body: await r.text(),
                 hadToken: !!token });
    } catch (e) {
      out.push({ url, ok: false, error: String(e && e.message ? e.message : e),
                 hadToken: !!token });
    }
  }
  return out;
}
"""

_LOCALSTORAGE_JS = """
() => {
  const out = {};
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    out[k] = localStorage.getItem(k);
  }
  return out;
}
"""


def _parse_body(body: str) -> Any:
    try:
        return json.loads(body)
    except Exception:
        return body


async def snapshot_profile(page, rec: Recorder, handle: str | None) -> None:
    """The checklist's §2 snapshot: the base profile object, nothing else."""
    if not handle:
        print("\n(no --handle given, skipping the profile snapshot; you can re-run "
              "this script later with --snapshot-only --handle yourhandle)")
        return

    urls = [f"{API_BASE}/v2/users/userHandle/{handle}"]
    print(f"\n📸 snapshotting {urls[0]} …")
    try:
        results = await page.evaluate(_FETCH_JS,
                                      {"urls": urls, "chains": SUPPORTED_CHAINS_HEADER})
    except Exception as exc:
        print(f"  ! snapshot failed: {exc}")
        return

    parsed = [{**r, "parsed": _parse_body(r.get("body", ""))} for r in results]

    # If the handle lookup gave us an id, take /v2/users/{id} too (also §2).
    uid = None
    first = parsed[0].get("parsed")
    if isinstance(first, dict):
        for container in (first, first.get("user") if isinstance(first.get("user"), dict) else {}):
            if isinstance(container, dict) and container.get("id"):
                uid = container["id"]
                break
    if uid:
        try:
            more = await page.evaluate(_FETCH_JS, {
                "urls": [f"{API_BASE}/v2/users/{uid}"],
                "chains": SUPPORTED_CHAINS_HEADER,
            })
            parsed += [{**r, "parsed": _parse_body(r.get("body", ""))} for r in more]
        except Exception as exc:
            print(f"  ! /v2/users/{{id}} snapshot failed: {exc}")

    path = rec.save_json(f"phase_{rec.phase}_profile.json",
                         {"ts": now(), "handle": handle, "results": parsed})
    print(f"  saved {path}")

    # Print the four fields the checklist asks about, right here in the terminal.
    for entry in parsed:
        obj = entry.get("parsed")
        obj = obj.get("user") if isinstance(obj, dict) and isinstance(obj.get("user"), dict) else obj
        if not isinstance(obj, dict):
            continue
        print(f"\n  {entry['url']}  → HTTP {entry.get('status')}")
        for field in ("id", "handle", "userHandle", "address", "evmAddress",
                      "activated", "createdAt"):
            if field in obj:
                print(f"    {field:<12} = {obj[field]!r}")
            else:
                print(f"    {field:<12} = <absent>")


async def snapshot_localstorage(page, rec: Recorder) -> None:
    try:
        store = await page.evaluate(_LOCALSTORAGE_JS)
    except Exception as exc:
        print(f"  ! localStorage snapshot failed: {exc}")
        return
    safe = {k: redact_pair(k, v) for k, v in (store or {}).items()}
    n_redacted = sum(1 for k, v in safe.items() if isinstance(v, str) and "[REDACTED" in v)
    path = rec.save_json(f"phase_{rec.phase}_localstorage.json",
                         {"ts": now(), "origin": page.url, "keys": safe})
    print(f"  saved {path} ({len(safe)} keys, {n_redacted} redacted)")


# ---------------- browser lifecycle ----------------

async def launch(profile_dir: Path, channel: str):
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    launch_args: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "viewport": {"width": 1280, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if channel:
        launch_args["channel"] = channel
    try:
        ctx = await pw.chromium.launch_persistent_context(**launch_args)
    except Exception as exc:
        if channel:
            print(f"  Chrome channel {channel!r} unavailable ({exc}); using bundled chromium")
            launch_args.pop("channel", None)
            ctx = await pw.chromium.launch_persistent_context(**launch_args)
        else:
            await pw.stop()
            raise
    return pw, ctx


PHASE_BRIEF = {
    "a": [
        "PHASE A — signup / onboarding",
        "  1. Sign up with the THROWAWAY email. Complete the whole flow.",
        "  2. Land on your profile / home screen.",
        "  3. Let every panel settle (~9s) — lazy XHRs fire late.",
        "  4. DO NOT TRADE.",
        "  5. Press ENTER here when done (leave the browser open).",
    ],
    "b": [
        "PHASE B — the `activated` transition, still with no trade",
        "  1. You should already be logged in from Phase A.",
        "  2. Find the minimal action that flips activated false -> true:",
        "     an 'activate' / 'enable trading' / deposit prompt, if one exists.",
        "  3. Perform it. Let it settle (~9s).",
        "  4. STILL DO NOT TRADE.",
        "  5. Press ENTER here when done (leave the browser open).",
        "",
        "  If no such action exists in the UI, that is itself a finding — press",
        "  ENTER and say so.",
    ],
    "c": [
        "PHASE C — one minimal funded trade (this creates on-chain history)",
        "  1. Fund the account through FOMO's normal flow.",
        "  2. Make ONE smallest-possible trade.",
        "  3. Note the resulting transaction signature / hash.",
        "  4. Press ENTER here when done (leave the browser open).",
    ],
}


async def run_phase(args) -> int:
    profile_dir = Path(args.profile).resolve()
    if profile_dir.name in LIVE_PROFILE_NAMES:
        print(f"refusing to use {profile_dir}: that is the live logged-in FOMO "
              f"profile. The experiment needs its own throwaway profile.")
        return 2
    profile_dir.mkdir(parents=True, exist_ok=True)

    capture_dir = Path(args.dir).resolve()
    rec = Recorder(capture_dir, args.phase)

    if args.phase == "c":
        print("\n⚠  Phase C places a real trade with real funds and deliberately")
        print("   creates the on-chain history this experiment is measuring.")
        if input("   Type 'yes' to continue: ").strip().lower() != "yes":
            print("   cancelled.")
            rec.close()
            return 0

    print("\n" + "=" * 74)
    for line in PHASE_BRIEF[args.phase]:
        print(line)
    print("=" * 74)
    print(f"capture dir : {capture_dir}")
    print(f"profile dir : {profile_dir}")
    print(f"handle      : {args.handle or '<not given — no profile snapshot>'}")
    print("=" * 74 + "\n")

    pw, ctx = await launch(profile_dir, args.channel)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    pending: set[asyncio.Task] = set()

    def on_response(response):
        task = loop.create_task(rec.on_response(response))
        pending.add(task)
        task.add_done_callback(pending.discard)

    ctx.on("request", rec.on_request)
    ctx.on("response", on_response)
    ctx.on("close", lambda _=None: stop.set())

    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    print("→ opening fomo.family …\n")
    try:
        await page.goto(APP_ORIGIN, wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:
        print(f"  ! navigation warning: {exc}")

    print("🔴 RECORDING. Press ENTER here when the phase is done.\n")

    async def wait_for_enter():
        try:
            await asyncio.to_thread(sys.stdin.readline)
        except Exception:
            await asyncio.Event().wait()
        stop.set()

    enter_task = loop.create_task(wait_for_enter())
    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass
    enter_task.cancel()

    print(f"\n⏹  stopping — {rec.n_resp} responses with bodies, {rec.n_req} requests logged")

    # Let in-flight body reads finish before the context goes away.
    if pending:
        await asyncio.gather(*list(pending), return_exceptions=True)

    # Snapshots need a live page on the app origin.
    live = None
    for p in ctx.pages:
        if not p.is_closed() and APP_ORIGIN in p.url:
            live = p
            break
    if live is None:
        for p in ctx.pages:
            if not p.is_closed():
                live = p
                break
    if live is not None:
        try:
            if APP_ORIGIN not in live.url:
                await live.goto(APP_ORIGIN, wait_until="domcontentloaded", timeout=45_000)
                await live.wait_for_timeout(2500)
            await snapshot_profile(live, rec, args.handle)
            await snapshot_localstorage(live, rec)
        except Exception as exc:
            print(f"  ! snapshot step failed: {exc}")
    else:
        print("  (browser was closed, so no snapshot was taken — re-run with "
              "--snapshot-only --handle yourhandle to get one)")

    try:
        await ctx.close()
    except Exception:
        pass
    await pw.stop()
    rec.close()

    print("\n" + "=" * 74)
    print(f"PHASE {args.phase.upper()} CAPTURED")
    print("=" * 74)
    for f in sorted(capture_dir.glob(f"phase_{args.phase}_*")):
        print(f"  {f.name:<34} {f.stat().st_size:>10,} bytes")
    print("\nNext: python analyze_captures.py --dir "
          f"{capture_dir}")
    return 0


async def run_snapshot_only(args) -> int:
    profile_dir = Path(args.profile).resolve()
    capture_dir = Path(args.dir).resolve()
    rec = Recorder(capture_dir, args.phase)
    pw, ctx = await launch(profile_dir, args.channel)
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto(APP_ORIGIN, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(3000)
    await snapshot_profile(page, rec, args.handle)
    await snapshot_localstorage(page, rec)
    await ctx.close()
    await pw.stop()
    rec.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="FOMO signup wallet-capture recorder")
    ap.add_argument("--phase", choices=["a", "b", "c"], required=True)
    ap.add_argument("--handle", help="the throwaway account's FOMO handle")
    ap.add_argument("--dir", default=None,
                    help="capture directory (default hunt_out/signup_YYYYMMDD)")
    ap.add_argument("--profile", default=".chrome-profile-exp",
                    help="throwaway Chrome profile dir (never the live one)")
    ap.add_argument("--channel", default="chrome",
                    help="Chrome channel; '' forces bundled chromium")
    ap.add_argument("--snapshot-only", action="store_true",
                    help="skip recording, just re-take the profile snapshot")
    args = ap.parse_args()

    if args.dir is None:
        args.dir = Path("hunt_out") / f"signup_{datetime.now().strftime('%Y%m%d')}"

    try:
        if args.snapshot_only:
            return asyncio.run(run_snapshot_only(args))
        return asyncio.run(run_phase(args))
    except KeyboardInterrupt:
        print("\n⏹  interrupted — whatever was captured is already on disk")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
