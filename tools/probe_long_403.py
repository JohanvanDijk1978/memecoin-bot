"""
tools/probe_long_403.py
───────────────────────
`app.long.xyz` returned 403 from the VPS on 2026-09-04. This answers, in one
run, exactly WHAT is blocked and WHICH client gets through — so the fix is
chosen from evidence instead of guessed at.

It crosses four clients with every host the watcher needs:

  clients   bare aiohttp · aiohttp + full Chrome headers · curl_cffi (Chrome
            TLS/JA3 impersonation) · the system `curl` binary as a control
  targets   app.long.xyz page · app.long.xyz static chunk · api.long.xyz REST ·
            api.long.xyz GraphQL · Robinhood RPC · Blockscout

Why the cross matters: the fomo work already established that Cloudflare scores
the TLS fingerprint, not just the IP — so "headers were not enough, curl_cffi
got through" and "everything is blocked" need completely different fixes, and
"only the HTML document is blocked, the API is fine" means three of the four
detectors were never affected at all.

Run on the box:

    python3 tools/probe_long_403.py
    pip install curl_cffi --break-system-packages   # if the report asks for it
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp                                  # noqa: E402
from src import long_sources as S               # noqa: E402

CHUNK_HINT = os.getenv("LONG_CHUNK_HINT",
                       "https://app.long.xyz/_next/static/chunks/b8b55d42383fbfe5.js")

TARGETS = [
    ("app page       ", "https://app.long.xyz/", "doc"),
    ("app /create    ", "https://app.long.xyz/create", "doc"),
    ("app static js  ", CHUNK_HINT, "script"),
    ("api /health    ", "https://api.long.xyz/v1/health", ""),
    ("api /config    ", "https://api.long.xyz/v1/config", ""),
    ("api /assets    ", "https://api.long.xyz/v1/assets", ""),
    ("blockscout     ", "https://robinhoodchain.blockscout.com/api/v2/stats", ""),
    ("pons page      ", "https://www.ponsfamily.com/launchpad", "doc"),
    ("pons api       ", "https://www.ponsfamily.com/api/pons-launches?explore=1&sort=newest"
                        "&age=all&page=1&pageSize=1&graduatedPage=1&graduatedPageSize=1"
                        "&includeGraduated=0&version=all&v=22", ""),
    ("o1 page        ", "https://launch.o1.exchange/", "doc"),
    # Does the challenge cover STATIC assets too, or only the document? If a
    # content-hashed chunk answers 200 while the page is challenged, the watcher
    # can read o1's asset array straight from the remembered chunk URL and only
    # needs the page when that 404s. The hash rots on every o1 deploy — a 404
    # here still answers the question (404 = the edge served us, not a block).
    ("o1 static asset", "https://launch.o1.exchange/assets/contracts-V2GJF_L9.js", "script"),
    ("o1 convex      ", "https://exciting-fox-990.convex.cloud/api/query", ""),
]

INTERESTING = ("cf-ray", "cf-mitigated", "cf-cache-status", "server",
               "content-type", "x-nextjs-cache", "x-railway-edge")


def summarize(status: int, body: str, headers: dict) -> str:
    h = {k.lower(): v for k, v in (headers or {}).items()}
    bits = [f"{status}"]
    if status < 400:
        bits.append(f"{len(body)}B")
        if "_next/static/chunks/" in body:
            bits.append("HAS CHUNK URLS ✔")
        elif "s(\"" in body or '"stock"' in body:
            bits.append("looks like the config chunk ✔")
    else:
        low = (body or "").lower()
        if any(m.lower() in low for m in S._CF_BODY_MARKERS):
            bits.append("CF CHALLENGE PAGE")
        elif h.get("cf-mitigated"):
            bits.append(f"cf-mitigated={h['cf-mitigated']}")
        else:
            bits.append("origin refusal (no CF markers)")
    tags = [f"{k}={h[k]}" for k in INTERESTING if h.get(k)]
    if tags:
        bits.append("· " + " ".join(tags[:4]))
    return "  ".join(bits)


async def via_aiohttp(url: str, kind: str, browserish: bool):
    headers = {}
    if browserish:
        headers = dict(S._BROWSERISH_DOC if kind == "doc"
                       else S._BROWSERISH_SCRIPT if kind == "script"
                       else {"User-Agent": S.USER_AGENT, "Accept": "application/json"})
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(url, headers=headers) as r:
            return r.status, await r.text(), dict(r.headers)


async def via_curl_cffi(url: str, kind: str):
    from curl_cffi.requests import AsyncSession        # type: ignore
    headers = dict(S._BROWSERISH_DOC if kind == "doc" else {})
    headers.pop("User-Agent", None)      # let the impersonation own the UA
    async with AsyncSession(impersonate=os.getenv("LONG_IMPERSONATE", "chrome"),
                            timeout=25) as s:
        r = await s.get(url, headers=headers)
        return r.status_code, r.text, dict(r.headers)


def via_curl_binary(url: str):
    try:
        p = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-D", "-", "-A", S.USER_AGENT,
             "--max-time", "20", url],
            capture_output=True, text=True, timeout=25)
    except Exception as e:
        return None, str(e), {}
    head = p.stdout or ""
    status = 0
    headers = {}
    for line in head.splitlines():
        if line.startswith("HTTP/"):
            parts = line.split()
            if len(parts) > 1 and parts[1].isdigit():
                status = int(parts[1])
        elif ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, "", headers


async def main() -> None:
    have_curl_cffi = True
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        have_curl_cffi = False

    print(f"probe from this host — {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"curl_cffi installed: {'yes' if have_curl_cffi else 'NO'}")
    print("=" * 100)

    verdict: dict[str, dict] = {}

    for label, url, kind in TARGETS:
        print(f"\n{label}  {url}")
        row = {}

        for name, browserish in (("aiohttp bare      ", False),
                                 ("aiohttp browserish", True)):
            try:
                st, body, hd = await via_aiohttp(url, kind, browserish)
                row[name.strip()] = st
                print(f"   {name}  {summarize(st, body, hd)}")
            except Exception as e:
                row[name.strip()] = None
                print(f"   {name}  ERROR {type(e).__name__}: {str(e)[:90]}")

        if have_curl_cffi:
            try:
                st, body, hd = await via_curl_cffi(url, kind)
                row["curl_cffi"] = st
                print(f"   curl_cffi chrome    {summarize(st, body, hd)}")
            except Exception as e:
                row["curl_cffi"] = None
                print(f"   curl_cffi chrome    ERROR {type(e).__name__}: {str(e)[:90]}")
        else:
            print("   curl_cffi chrome    ⟨not installed⟩")

        st, _b, hd = via_curl_binary(url)
        row["curl bin"] = st
        print(f"   curl binary         {summarize(st or 0, '', hd)}")
        verdict[label.strip()] = row

    # POST, because a GraphQL block can differ from a GET block.
    print("\napi graphql POST  https://api.long.xyz/v1/graphql")
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post("https://api.long.xyz/v1/graphql",
                              json={"query": "{ chain_metadata { chain_id block_height } }"},
                              headers={"User-Agent": S.USER_AGENT}) as r:
                body = await r.text()
                print(f"   aiohttp             {r.status}  {body[:180]}")
                verdict["graphql"] = {"aiohttp": r.status}
    except Exception as e:
        print(f"   aiohttp             ERROR {type(e).__name__}: {str(e)[:90]}")
        verdict["graphql"] = {"aiohttp": None}

    print("\n" + "=" * 100)
    print("VERDICT")
    app_ok = any(v and v < 400 for k, r in verdict.items() if k.startswith("app")
                 for v in r.values())
    api_ok = any(v and v < 400 for k, r in verdict.items()
                 if k.startswith("api") or k == "graphql" for v in r.values())
    cffi_beats_aiohttp = any(
        r.get("curl_cffi") and r["curl_cffi"] < 400
        and (r.get("aiohttp browserish") or 999) >= 400
        for k, r in verdict.items() if k.startswith("app"))

    if not api_ok:
        print("  ✗ api.long.xyz is ALSO blocked — this is host- or IP-level, not a")
        print("    bot score. Nothing on Long is reachable from this box.")
    elif app_ok and cffi_beats_aiohttp:
        print("  ✓ curl_cffi gets through where aiohttp does not — a TLS/JA3 bot-score")
        print("    block, exactly as with fomo. FIX: pip install curl_cffi")
        print("    --break-system-packages, then restart. LONG_TRANSPORT=auto picks it")
        print("    up on its own; no code change needed.")
    elif app_ok:
        print("  ✓ app.long.xyz is reachable — the browser headers were the missing")
        print("    piece. No further change needed; restart memebot.")
    else:
        print("  ✗ app.long.xyz is blocked to every client tried, but api.long.xyz")
        print("    works. The frontend detector stays degraded; the factory, indexer")
        print("    and feed detectors are unaffected. See HANDOFF_LONG.md §9 for the")
        print("    remaining options (proxy the page fetch through borz, or run the")
        print("    page fetch under the Chrome profile fomo already uses).")
    pons_ok = any(v and v < 400 for k, r in verdict.items() if k.startswith("pons")
                  for v in r.values())
    o1_ok = any(v and v < 400 for k, r in verdict.items() if k.startswith("o1")
                for v in r.values())
    print(f"\n  Pons reachable: {'yes' if pons_ok else 'NO'}   "
          f"o1 reachable: {'yes' if o1_ok else 'NO'}")
    print("  (both are on Vercel with no Cloudflare bot-scoring, so these are expected")
    print("   to work from the VPS even while app.long.xyz does not.)")
    if not have_curl_cffi:
        print("\n  Run `pip install curl_cffi --break-system-packages` and re-run this")
        print("  probe — the most likely fix has not actually been tested yet.")


if __name__ == "__main__":
    asyncio.run(main())
