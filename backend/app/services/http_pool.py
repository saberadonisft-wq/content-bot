"""Persistent HTTP client pool for Content Bot connectors and crawlers."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

try:
    import h2  # noqa: F401

    _HTTP2_AVAILABLE = True
except ImportError:
    _HTTP2_AVAILABLE = False

_DEFAULT_LIMITS = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=50,
    keepalive_expiry=30.0,
)

_pool: dict[str, httpx.AsyncClient] = {}
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _cache_key(base_url: str | None, headers: dict[str, str] | None) -> str:
    base = (base_url or "").rstrip("/")
    h_str = ""
    if headers:
        ua = headers.get("User-Agent", "")
        auth = headers.get("Authorization", "")
        h_str = f"{ua}|{auth}"
    return f"{base}::{h_str}"


async def get_client(
    *,
    base_url: str | httpx.URL | None = None,
    timeout: float | httpx.Timeout = 30.0,
    follow_redirects: bool = False,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> httpx.AsyncClient:
    """Return a pooled persistent AsyncClient for the given configuration."""
    base_str = str(base_url) if base_url is not None else ""
    header_dict = dict(headers) if headers else None
    key = _cache_key(base_str, header_dict)

    client = _pool.get(key)
    if client is not None and not client.is_closed:
        return client

    async with _get_lock():
        client = _pool.get(key)
        if client is not None and not client.is_closed:
            return client

        client_headers = {"User-Agent": "ContentBot/0.1"}
        if header_dict:
            client_headers.update(header_dict)

        client_timeout = (
            timeout
            if isinstance(timeout, httpx.Timeout)
            else httpx.Timeout(float(timeout), connect=10.0)
        )

        client_kwargs: dict[str, Any] = {
            "http2": _HTTP2_AVAILABLE,
            "limits": _DEFAULT_LIMITS,
            "timeout": client_timeout,
            "follow_redirects": follow_redirects,
            "headers": client_headers,
        }
        if base_str:
            client_kwargs["base_url"] = base_str.rstrip("/")

        new_client = httpx.AsyncClient(**client_kwargs)
        _pool[key] = new_client
        return new_client


async def close_http_pools() -> None:
    """Close all shared persistent AsyncClients cleanly."""
    async with _get_lock():
        for client in list(_pool.values()):
            if not client.is_closed:
                await client.aclose()
        _pool.clear()

