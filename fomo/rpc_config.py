"""Small helpers for ordered, secret-safe RPC endpoint configuration."""

from __future__ import annotations

import os
from collections.abc import Iterable
from urllib.parse import urlsplit


def unique_urls(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def env_rpc_urls(
    primary_name: str,
    fallback_name: str,
    default: str | None = None,
) -> list[str]:
    primary = os.getenv(primary_name, default or "")
    fallbacks = os.getenv(fallback_name, "").split(",")
    return unique_urls([primary, *fallbacks])


def normalize_rpc_urls(value: str | Iterable[str]) -> list[str]:
    return unique_urls([value] if isinstance(value, str) else value)


def rpc_display_name(url: str) -> str:
    """Return a useful endpoint label without leaking path-based API keys."""
    parsed = urlsplit(url)
    host = parsed.hostname or "configured RPC"
    if parsed.port:
        host += f":{parsed.port}"
    return f"{parsed.scheme}://{host}" if parsed.scheme else host
