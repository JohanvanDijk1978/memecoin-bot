"""One place that knows how to talk to Solscan, on whatever plan the key has.

Solscan serves the same data under two prefixes on the same host:

* ``/v2.0/...``       -- the paid Pro API.
* ``/playground/...`` -- the same engine, opened to any free Solscan account.

A free key answers 401 to every `/v2.0` path and 200 to the `/playground` one,
and the gateway rejects an unauthenticated request before it routes, so the
status code alone never says which of the two you are entitled to. The
authorisation header has the same problem: the v1 API took a bare ``token``
header, the v2 documentation shows ``Authorization: Bearer``, and a rejected
key looks identical either way.

So this module resolves all of it at runtime. The first successful call for an
endpoint remembers which prefix, which header style and which parameter
spelling worked, and every later call goes straight there. When nothing works
it logs the response body, which is the only thing that ever named the reason.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable, Sequence

log = logging.getLogger("solscan.api")

SOLSCAN_HOST = os.getenv("SOLSCAN_HOST", "https://pro-api.solscan.io").rstrip("/")

# Playground first: it is what a free key can reach, and a paid key can reach
# it too, so trying it first costs a paid plan nothing. Override with
# SOLSCAN_PREFIXES="v2.0" to pin the paid API.
SOLSCAN_PREFIXES: tuple[str, ...] = tuple(
    part.strip().strip("/")
    for part in os.getenv("SOLSCAN_PREFIXES", "playground,v2.0").split(",")
    if part.strip()
) or ("playground",)

# `token` is the only header Solscan reads: everything else comes back
# "Token is missing", which is its way of saying no key arrived. The rest are
# kept so a future API change has somewhere to be found, but a style that
# delivers nothing is abandoned as soon as one that does gets a real answer.
AUTH_STYLES: tuple[str, ...] = ("token", "bearer", "raw", "x-api-key")

# Solscan's word for "your plan does not include this endpoint". It is not
# worth retrying on any schedule, so a path that earns it is dropped for the
# life of the process rather than re-probed every few minutes.
PLAN_REFUSAL = "upgrade your api key"

# Pin either of these to skip that half of the negotiation.
_forced_style = os.getenv("SOLSCAN_AUTH_STYLE", "").strip().lower() or None
_forced_prefix = os.getenv("SOLSCAN_PREFIX", "").strip().strip("/") or None

# logical path -> (prefix, auth style, index into the parameter variants)
_resolved: dict[str, tuple[str, str, int]] = {}

# A path that answered nothing is not re-probed for this long. Without it every
# `/token` would spend a dozen doomed requests before falling back to Helius.
RETRY_AFTER = float(os.getenv("SOLSCAN_RETRY_SECONDS", "300"))
_unavailable: dict[str, float] = {}

# Paths this key is not entitled to, or that do not exist. Never re-probed.
_forbidden: set[str] = set()


def solscan_key() -> str:
    """The key as it is *now*, not as it was when this module was imported.

    Reading it at call time is what lets a refreshed `.env` take effect on a
    reload without the value being frozen into a module constant.
    """
    return os.getenv("SOLSCAN_API_KEY", "").strip()


def reset_resolution() -> None:
    """Forget every negotiated prefix/header (used by the diagnostics script)."""
    _resolved.clear()
    _unavailable.clear()
    _forbidden.clear()


def _headers(style: str, key: str) -> dict[str, str]:
    base = {"accept": "application/json"}
    if style == "token":
        base["token"] = key
    elif style == "bearer":
        base["Authorization"] = f"Bearer {key}"
    elif style == "raw":
        base["Authorization"] = key
    elif style == "x-api-key":
        base["x-api-key"] = key
    return base


def _url(prefix: str, path: str) -> str:
    return f"{SOLSCAN_HOST}/{prefix}/{path.lstrip('/')}"


def _body_snippet(response: Any) -> str:
    """Whatever the server said, short enough to log."""
    try:
        text = response.text
    except Exception:
        text = ""
    if not text:
        try:
            text = str(response.json())
        except Exception:
            text = ""
    return " ".join(str(text).split())[:300]


def _failed(payload: Any) -> bool:
    """Solscan answers 200 with `success: false` for some refusals."""
    return isinstance(payload, dict) and payload.get("success") is False


async def solscan_get(
    http: Any,
    path: str,
    params: dict[str, Any] | Sequence[dict[str, Any]],
    *,
    timeout: int = 30,
    key: str | None = None,
) -> dict[str, Any] | None:
    """GET a Solscan endpoint, resolving prefix, auth header and parameters.

    `path` is a logical path such as ``token/holders``. Pass a full ``http://``
    or ``https://`` URL to bypass prefix resolution entirely.

    `params` may be a single dict, or several dicts in preference order when
    the parameter spelling differs between the two APIs (``page``/``page_size``
    against ``offset``/``limit``, say) -- each is tried in turn against a 400.

    Returns the decoded JSON body, or None. Never raises: both callers have a
    fallback path and want to take it quietly.
    """
    key = (key or "").strip() or solscan_key()
    if not key:
        log.debug("solscan: no SOLSCAN_API_KEY set, skipping %s", path)
        return None

    variants: list[dict[str, Any]] = (
        [dict(params)] if isinstance(params, dict) else [dict(v) for v in params]
    )
    absolute = path.startswith(("http://", "https://"))

    if absolute:
        prefixes: Iterable[str] = ("",)
    elif _forced_prefix:
        prefixes = (_forced_prefix,)
    else:
        prefixes = SOLSCAN_PREFIXES
    styles = (_forced_style,) if _forced_style else AUTH_STYLES

    # A path that already worked once goes straight back to what worked.
    remembered = _resolved.get(path)
    if remembered:
        prefixes, styles = (remembered[0],), (remembered[1],)
        index = remembered[2]
        if index < len(variants):
            variants = variants[index:] + variants[:index]

    if path in _forbidden:
        log.debug("solscan: %s is not on this key's plan, not asking again", path)
        return None

    since = _unavailable.get(path)
    if since is not None and time.monotonic() - since < RETRY_AFTER:
        log.debug("solscan: %s answered nothing %.0fs ago, not re-probing yet",
                  path, time.monotonic() - since)
        return None

    # Every attempt is kept. Reporting only the last one is how the earlier
    # version blamed `x-api-key` -- a style that sends no token at all, so
    # Solscan says "Token is missing" and the real refusal never gets seen.
    attempts: list[str] = []
    routeless = False
    delivered = False
    for prefix in prefixes:
        url = path if absolute else _url(str(prefix), path)
        routeless = False
        delivered = False
        for style in styles:
            for offset, variant in enumerate(variants):
                try:
                    response = await http.get(
                        url,
                        params=variant,
                        headers=_headers(str(style), key),
                        timeout=timeout,
                    )
                except Exception as exc:
                    log.info("solscan: request to %s failed: %s", url, exc)
                    return None

                status = int(getattr(response, "status_code", 200))

                def record(note: str = "") -> None:
                    attempts.append(
                        f"    /{prefix} [{style}] ({','.join(variant)}) "
                        f"-> HTTP {status} {note or _body_snippet(response)}"
                    )

                if status == 404:
                    # Routing happened, so the key was accepted -- this prefix
                    # simply has no such endpoint. No header will conjure one.
                    record()
                    routeless = True
                    break
                if status in (401, 403):
                    body = _body_snippet(response)
                    record(body)
                    if PLAN_REFUSAL in body.lower():
                        _forbidden.add(path)
                        delivered = True
                        break
                    if "Token is missing" not in body:
                        # The header was read and the key still refused; a
                        # different way of spelling the same key will not help.
                        delivered = True
                        break
                    # This style handed Solscan nothing. Try one that does.
                    break
                if status == 400 and offset + 1 < len(variants):
                    record()
                    continue
                if status >= 400:
                    record()
                    break

                try:
                    payload = response.json()
                except Exception as exc:
                    log.info("solscan: %s returned a non-JSON body: %s", url, exc)
                    return None
                if _failed(payload):
                    record(_body_snippet(response) or str(payload)[:300])
                    continue

                if not absolute and _resolved.get(path) != (prefix, style, offset):
                    log.info(
                        "solscan: %s answered under /%s with the '%s' header",
                        path, prefix, style,
                    )
                    _resolved[path] = (str(prefix), str(style), offset)
                return payload if isinstance(payload, dict) else None
            if routeless or delivered:
                break

    _resolved.pop(path, None)
    _unavailable[path] = time.monotonic()
    log.warning(
        "solscan: nothing answered %s. Every attempt:\n%s\n%s",
        path, "\n".join(attempts) or "    (none made)", _read_the_attempts(attempts),
    )
    return None


def _read_the_attempts(attempts: list[str]) -> str:
    """Turn the attempt log into the one sentence worth acting on.

    Solscan's gateway authenticates before it routes and says so precisely:
    "Token is missing" means no key reached it, "Token is invalid" means one
    did and was rejected. That distinction is the whole diagnosis, so say it
    rather than making the reader infer it.
    """
    sent = [line for line in attempts if "Token is missing" not in line]
    if any(PLAN_REFUSAL in line.lower() for line in sent):
        return ("    -> The key is valid and this endpoint is not on its plan. "
                "A 404 under /playground alongside this means the endpoint is "
                "Pro-only: there is no free route to it, so let the other "
                "source answer instead of paying for a Solscan plan.")
    if sent and all("404" in line for line in sent):
        return ("    -> Solscan routed the request and has no such endpoint. "
                "Check the path against its reference.")
    if any("Token is invalid" in line for line in sent):
        return ("    -> The key reached Solscan and was rejected. It is expired, "
                "revoked, or superseded by a newer one: copy the current value "
                "from https://solscan.io/user/profile#api_management into .env "
                "and restart.")
    if sent and all("Token is missing" in line for line in sent):
        return ("    -> Solscan saw no key at all on styles that send one, which "
                "means SOLSCAN_API_KEY is empty in this process. Check that "
                ".env is loaded and that the bot restarted after it changed.")
    if any("not have access" in line or "plan" in line.lower() for line in sent):
        return ("    -> The key is valid but this endpoint is not on its plan. "
                "A free key reaches /playground only; this path may be Pro-only, "
                "in which case leave Solscan out of it and let Helius answer.")
    return ("    -> Run `python solscan_diag.py` for the full picture; the bodies "
            "above name the reason.")
