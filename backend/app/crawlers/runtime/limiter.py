"""Per-provider adaptive rate limiting with deterministic test hooks."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RatePolicy:
    requests_per_second: float = 1
    base_backoff_seconds: float = 1
    max_backoff_seconds: float = 60

    def __post_init__(self) -> None:
        if self.requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        if self.base_backoff_seconds <= 0 or self.max_backoff_seconds <= 0:
            raise ValueError("Backoff values must be positive")
        if self.base_backoff_seconds > self.max_backoff_seconds:
            raise ValueError("base_backoff_seconds cannot exceed max_backoff_seconds")


@dataclass(slots=True)
class _Bucket:
    next_request_at: float = 0
    blocked_until: float = 0
    strikes: int = 0


class AdaptiveRateLimiter:
    def __init__(
        self,
        policy: RatePolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.policy = policy
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str) -> None:
        if not key.strip():
            raise ValueError("Rate-limit key cannot be empty")
        async with self._lock:
            now = self._clock()
            bucket = self._buckets.setdefault(key, _Bucket())
            permitted_at = max(now, bucket.next_request_at, bucket.blocked_until)
            delay = max(0.0, permitted_at - now)
            bucket.next_request_at = permitted_at + 1 / self.policy.requests_per_second
        if delay:
            await self._sleep(delay)

    async def penalize(self, key: str, retry_after_seconds: float | None = None) -> float:
        async with self._lock:
            now = self._clock()
            bucket = self._buckets.setdefault(key, _Bucket())
            bucket.strikes += 1
            exponential = min(
                self.policy.max_backoff_seconds,
                self.policy.base_backoff_seconds * (2 ** (bucket.strikes - 1)),
            )
            retry_after = max(0.0, float(retry_after_seconds or 0))
            delay = min(
                self.policy.max_backoff_seconds,
                max(exponential, retry_after),
            )
            bucket.blocked_until = max(bucket.blocked_until, now + delay)
            return delay

    async def reward(self, key: str) -> None:
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is not None:
                bucket.strikes = max(0, bucket.strikes - 1)
