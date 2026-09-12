"""Loop-scoped HTTP clients, shared only when their full configuration agrees."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary

import httpx

try:
    import h2  # noqa: F401

    _HTTP2_AVAILABLE = True
except ImportError:
    _HTTP2_AVAILABLE = False

_DEFAULT_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=30.0)
_client_factory: ContextVar[Callable[..., httpx.AsyncClient]] = ContextVar("http_client_factory", default=httpx.AsyncClient)


class PoolScope:
    """One application lifespan's HTTP ownership boundary."""


_default_scope = PoolScope()
_scope: ContextVar[PoolScope] = ContextVar("http_pool_scope", default=_default_scope)


@dataclass(frozen=True, eq=False)
class _Identity:
    """Keep opaque configuration objects alive and compare them by identity."""

    value: Any

    def __hash__(self):
        return id(self.value)

    def __eq__(self, other):
        return isinstance(other, _Identity) and self.value is other.value


def _freeze(value):
    if isinstance(value, httpx.Timeout):
        return (httpx.Timeout, tuple((key, None if seconds is None else float(seconds)) for key, seconds in sorted(value.as_dict().items())))
    if isinstance(value, httpx.Limits):
        return (httpx.Limits, value.max_connections, value.max_keepalive_connections, value.keepalive_expiry)
    if isinstance(value, httpx.Headers):
        return (httpx.Headers, tuple(sorted(value.multi_items())))
    if isinstance(value, Mapping):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bytes, bool, int, float)):
        return (type(value), value)
    return _Identity(value)


@dataclass
class _Pool:
    clients: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_pools: WeakKeyDictionary = WeakKeyDictionary()


def _pool() -> _Pool:
    loop = asyncio.get_running_loop()
    if loop not in _pools:
        _pools[loop] = WeakKeyDictionary()
    scopes = _pools[loop]
    scope = _scope.get()
    if scope not in scopes:
        scopes[scope] = _Pool()
    return scopes[scope]


@contextmanager
def use_pool_scope(scope: PoolScope):
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


@contextmanager
def use_client_factory(factory: Callable[..., httpx.AsyncClient]):
    """Inject a transport/factory without bypassing pooling or changing other tasks."""
    token = _client_factory.set(factory)
    try:
        yield
    finally:
        _client_factory.reset(token)


async def get_client(
    *, base_url: str | httpx.URL | None = None,
    timeout: float | httpx.Timeout = 30.0, follow_redirects: bool = False,
    headers: dict[str, str] | None = None, **extra: Any,
) -> httpx.AsyncClient:
    client_headers = httpx.Headers({"User-Agent": "ContentBot/0.1"})
    client_headers.update(headers or {})
    kwargs = {"http2": _HTTP2_AVAILABLE, "limits": _DEFAULT_LIMITS,
              "timeout": timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(float(timeout), connect=10.0),
              "follow_redirects": follow_redirects, "headers": client_headers, **extra}
    if base_url is not None:
        kwargs["base_url"] = str(base_url).rstrip("/")
    factory = _client_factory.get()
    key = (_Identity(factory), _freeze(kwargs))
    pool = _pool()
    async with pool.lock:
        client = pool.clients.get(key)
        if client is None or client.is_closed:
            # HTTPX validates unsupported kwargs instead of silently ignoring them.
            client = factory(**kwargs)
            pool.clients[key] = client
        return client


async def close_http_pools() -> None:
    """Close clients owned by the current event loop after producers have stopped."""
    pool = _pool()
    async with pool.lock:
        clients = list(pool.clients.values())
        pool.clients.clear()
        results = await asyncio.gather(*(client.aclose() for client in clients if not client.is_closed), return_exceptions=True)
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise ExceptionGroup("HTTP clients failed to close", failures)
