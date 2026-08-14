"""Shared application-only Reddit OAuth token lifecycle.

Access tokens stay in memory, are hidden from repr/log output, and are reused by
keyword and saved-channel scans until shortly before provider expiry.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

import httpx

from ..crawlers.runtime import CrawlerErrorCode, CrawlerFailure

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class RedditAccessToken:
    value: str = field(repr=False)
    expires_at: float
    credential_fingerprint: str


class RedditOAuthTokenCache:
    """Async stampede-safe cache for Reddit client-credential grants."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        expiry_margin_seconds: float = 30,
        attempts: int = 3,
    ) -> None:
        if not 0 <= expiry_margin_seconds <= 300 or not 1 <= attempts <= 5:
            raise ValueError("Reddit token-cache limits are invalid")
        self._clock = clock
        self.expiry_margin_seconds = expiry_margin_seconds
        self.attempts = attempts
        self._cached: RedditAccessToken | None = None
        self._lock = asyncio.Lock()

    async def get(
        self,
        client: httpx.AsyncClient,
        *,
        client_id: str,
        client_secret: str,
    ) -> str:
        fingerprint = self._credential_fingerprint(client_id, client_secret)
        cached = self._usable(fingerprint)
        if cached is not None:
            return cached.value
        async with self._lock:
            cached = self._usable(fingerprint)
            if cached is not None:
                return cached.value
            token = await self._request_token(
                client,
                client_id=client_id,
                client_secret=client_secret,
                fingerprint=fingerprint,
            )
            self._cached = token
            return token.value

    def invalidate(self) -> None:
        self._cached = None

    def _usable(self, fingerprint: str) -> RedditAccessToken | None:
        cached = self._cached
        if (
            cached is None
            or cached.credential_fingerprint != fingerprint
            or self._clock() >= cached.expires_at
        ):
            return None
        return cached

    async def _request_token(
        self,
        client: httpx.AsyncClient,
        *,
        client_id: str,
        client_secret: str,
        fingerprint: str,
    ) -> RedditAccessToken:
        response: httpx.Response | Any | None = None
        for attempt in range(self.attempts):
            try:
                response = await client.post(
                    _TOKEN_URL,
                    data={"grant_type": "client_credentials"},
                    auth=httpx.BasicAuth(client_id, client_secret),
                )
            except httpx.TransportError as exc:
                if attempt + 1 >= self.attempts:
                    raise CrawlerFailure(
                        CrawlerErrorCode.TRANSPORT_ERROR,
                        "Reddit OAuth token request failed.",
                        retryable=True,
                    ) from exc
                await asyncio.sleep(0.25 * (2**attempt))
                continue
            status = int(getattr(response, "status_code", 200))
            if status not in _RETRYABLE or attempt + 1 >= self.attempts:
                break
            retry_after = getattr(response, "headers", {}).get("Retry-After")
            try:
                delay = min(max(float(retry_after), 0), 5) if retry_after else 0.25 * (2**attempt)
            except (TypeError, ValueError):
                delay = 0.25 * (2**attempt)
            await asyncio.sleep(delay)

        assert response is not None
        status = int(getattr(response, "status_code", 200))
        if status >= 400:
            raise self._response_failure(status)
        try:
            payload = response.json()
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Reddit OAuth returned invalid JSON.",
            ) from exc
        if not isinstance(payload, dict):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Reddit OAuth returned an invalid response.",
            )
        value = str(payload.get("access_token") or "")
        if not value or len(value) > 4_096:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "Reddit OAuth did not issue an access token.",
            )
        try:
            expires_in = int(payload.get("expires_in") or 3_600)
        except (TypeError, ValueError):
            expires_in = 3_600
        if not 1 <= expires_in <= 86_400:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Reddit OAuth returned an invalid token lifetime.",
            )
        usable_for = max(0.0, expires_in - self.expiry_margin_seconds)
        return RedditAccessToken(value, self._clock() + usable_for, fingerprint)

    @staticmethod
    def _credential_fingerprint(client_id: str, client_secret: str) -> str:
        identifier = client_id.strip()
        secret = client_secret.strip()
        if not identifier or not secret or len(identifier) > 512 or len(secret) > 4_096:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "Reddit OAuth credentials are missing or invalid.",
            )
        return hashlib.sha256(f"{identifier}\0{secret}".encode()).hexdigest()

    @staticmethod
    def _response_failure(status: int) -> CrawlerFailure:
        if status in {400, 401, 403}:
            return CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "Reddit OAuth credentials were rejected.",
            )
        if status == 429:
            return CrawlerFailure(
                CrawlerErrorCode.RATE_LIMITED,
                "Reddit OAuth rate limit was reached.",
                retryable=True,
            )
        return CrawlerFailure(
            CrawlerErrorCode.TRANSPORT_ERROR,
            "Reddit OAuth is temporarily unavailable.",
            retryable=status >= 500,
        )


reddit_token_cache = RedditOAuthTokenCache()
