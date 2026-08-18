"""
diag.py — figure out exactly what Cloudflare is rejecting.

Run this from the same VS Code terminal that gave you the 403:

    pip install httpx[http2]
    python diag.py

Optional, only if the header tests all fail:

    pip install curl_cffi

It tries the same request in several shapes and prints which ones get through.
Nothing here is destructive — it's all GETs against one public profile.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys

import aiohttp
from dotenv import load_dotenv

from fomo_api import API_BASE, FomoClient

load_dotenv()

TARGET = "/v2/users/userHandle/Binkieee"
HEALTH = "/health"

# What the current client sends today (the shape that is failing).
CURRENT = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Origin": "https://fomo.family",
    "Referer": "https://fomo.family/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua-platform": '"Windows"',
}

# Everything Chrome actually attaches to a same-site XHR from the app.
# The three Sec-Fetch-* headers are the interesting ones: a browser always
# sends them, and requests/aiohttp never do.
FULL = {
    "User-Agent": CURRENT["User-Agent"],
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://fomo.family",
    "Referer": "https://fomo.family/",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Priority": "u=1, i",
}


def verdict(status: int, body: str) -> str:
    if status == 200:
        return "PASS"
    if status == 403 or "you have been blocked" in body.lower():
        return "403 blocked"
    if status == 401:
        return "reached the API (401 = auth, not a block)"
    return f"HTTP {status}"


def snippet(body: str) -> str:
    one_line = " ".join(body.split())
    return one_line[:110]


async def aiohttp_try(label: str, headers: dict[str, str], path: str, token: str | None) -> None:
    h = dict(headers)
    if token:
        h["Authorization"] = f"Bearer {token}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(API_BASE + path, headers=h) as r:
                body = await r.text()
                ray = r.headers.get("cf-ray", "-")
                print(f"  {label:<34} {verdict(r.status, body):<34} http/1.1  cf-ray={ray}")
                if r.status != 200:
                    print(f"      {snippet(body)}")
    except Exception as exc:
        print(f"  {label:<34} ERROR {type(exc).__name__}: {exc}")


async def httpx_try(label: str, headers: dict[str, str], path: str, token: str | None, http2: bool) -> None:
    try:
        import httpx
    except ImportError:
        print(f"  {label:<34} SKIPPED (pip install httpx[http2])")
        return
    h = dict(headers)
    if token:
        h["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(http2=http2, timeout=20, headers=h) as c:
            r = await c.get(API_BASE + path)
            body = r.text
            print(f"  {label:<34} {verdict(r.status_code, body):<34} {r.http_version.lower():<9} cf-ray={r.headers.get('cf-ray','-')}")
            if r.status_code != 200:
                print(f"      {snippet(body)}")
    except Exception as exc:
        msg = str(exc)
        if http2 and "h2" in msg:
            msg += "   <- pip install httpx[http2]"
        print(f"  {label:<34} ERROR {type(exc).__name__}: {msg[:120]}")


def curlcffi_try(label: str, path: str, token: str | None) -> None:
    try:
        from curl_cffi import requests as creq
    except ImportError:
        print(f"  {label:<34} SKIPPED (pip install curl_cffi)")
        return
    h = {"Authorization": f"Bearer {token}"} if token else {}
    h["Origin"] = "https://fomo.family"
    h["Referer"] = "https://fomo.family/"
    try:
        r = creq.get(API_BASE + path, headers=h, impersonate="chrome", timeout=20)
        print(f"  {label:<34} {verdict(r.status_code, r.text):<34} cf-ray={r.headers.get('cf-ray','-')}")
        if r.status_code != 200:
            print(f"      {snippet(r.text)}")
    except Exception as exc:
        print(f"  {label:<34} ERROR {type(exc).__name__}: {exc}")


async def main() -> int:
    print("=" * 92)
    print("FOMO 403 diagnostic")
    print("=" * 92)

    host = API_BASE.split("//", 1)[1]
    try:
        print(f"\nDNS  {host} -> {', '.join(sorted({i[4][0] for i in socket.getaddrinfo(host, 443)}))}")
    except OSError as exc:
        print(f"\nDNS  failed: {exc}")
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if os.getenv(var):
            print(f"     !! {var}={os.getenv(var)}  <- a proxy will change your egress IP")
    print(f"     python {sys.version.split()[0]}, aiohttp {aiohttp.__version__}")

    # ---- Stage 0: what IP does Cloudflare think you are? ----
    print("\n[0] Your egress IP, as Cloudflare sees it")
    print("    Open https://fomo.family/cdn-cgi/trace in Chrome and compare the ip= line.")
    print("    If they differ, a VPN/proxy is sending Python out on a datacenter IP —")
    print("    that alone explains the 403, and nothing else below matters.\n")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s0:
            async with s0.get("https://fomo.family/cdn-cgi/trace") as r0:
                trace = dict(
                    line.split("=", 1)
                    for line in (await r0.text()).splitlines()
                    if "=" in line
                )
        print(f"    python sees: ip={trace.get('ip')}  loc={trace.get('loc')}  colo={trace.get('colo')}")
    except Exception as exc:
        print(f"    could not reach cdn-cgi/trace: {exc}")

    # ---- Stage 1: is the HOST reachable at all, before auth is involved? ----
    print("\n[1] Unauthenticated /health  (from the browser this returns a clean 401 JSON)")
    print("    401 here => your IP is fine and this is a header problem.")
    print("    403 here => the whole host is refusing this client.\n")
    await aiohttp_try("aiohttp, no headers", {}, HEALTH, None)
    await aiohttp_try("aiohttp, browser headers", FULL, HEALTH, None)

    # ---- Stage 2: get a real token ----
    print("\n[2] Privy auth")
    token = None
    try:
        client = FomoClient(
            refresh_token=os.getenv("FOMO_PRIVY_REFRESH_TOKEN") or None,
            access_token=os.getenv("FOMO_PRIVY_ACCESS_TOKEN") or None,
            transport="http",  # this file probes raw HTTP; don't launch Chrome
        )
        async with client as c:
            token = await c._ensure_token()
        print("    token OK  (so auth.privy.io is reachable from this machine —")
        print("               only prod-api.fomo.family is refusing you)")
    except Exception as exc:
        print(f"    could not get a token: {exc}")
        print("    continuing unauthenticated — a 401 below still proves reachability")

    # ---- Stage 3: the real request, in five shapes ----
    print(f"\n[3] GET {TARGET}\n")
    await aiohttp_try("A bare + auth", {}, TARGET, token)
    await aiohttp_try("B current client headers", CURRENT, TARGET, token)
    await aiohttp_try("C full Sec-Fetch header set", FULL, TARGET, token)
    await httpx_try("D full headers, HTTP/2", FULL, TARGET, token, http2=True)
    await httpx_try("E full headers, HTTP/1.1", FULL, TARGET, token, http2=False)
    curlcffi_try("F curl_cffi (Chrome TLS)", TARGET, token)

    print("\n" + "=" * 92)
    print("How to read this:")
    print("  C or E passes  -> it was the missing Sec-Fetch-* headers. Easy fix, no tricks.")
    print("  only D passes  -> Cloudflare wants HTTP/2. Also an easy fix (switch to httpx).")
    print("  only F passes  -> it's the TLS fingerprint. Tell me and we'll talk about")
    print("                    whether to go there, or use the browser-relay instead.")
    print("  nothing passes -> check [0] first. Same IP as Chrome and still nothing?")
    print("                    Then the IP really is blocklisted; we relay through Chrome.")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
