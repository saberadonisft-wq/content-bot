"""Small, typed YouTube Data API transport used by both scan paths."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..crawlers.runtime import CrawlerErrorCode, CrawlerFailure

_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})
_QUOTA_REASONS = frozenset(
    {
        "dailyLimitExceeded",
        "quotaExceeded",
        "rateLimitExceeded",
        "userRateLimitExceeded",
    }
)
_AUTH_REASONS = frozenset(
    {
        "accessNotConfigured",
        "forbidden",
        "ipRefererBlocked",
        "keyExpired",
        "keyInvalid",
    }
)


@dataclass(slots=True)
class YouTubeQuotaBudget:
    """Per-run request budget matching YouTube's current quota buckets."""

    max_search_requests: int
    max_general_requests: int
    max_total_requests: int | None = None
    search_requests: int = 0
    general_requests: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.max_search_requests <= 100:
            raise ValueError("YouTube search request budget must be between 1 and 100")
        if not 1 <= self.max_general_requests <= 10_000:
            raise ValueError("YouTube general request budget must be between 1 and 10000")
        if self.max_total_requests is not None and not 1 <= self.max_total_requests <= 10_000:
            raise ValueError("YouTube total request budget must be between 1 and 10000")

    def spend(self, endpoint: str) -> bool:
        if (
            self.max_total_requests is not None
            and self.search_requests + self.general_requests
            >= self.max_total_requests
        ):
            return False
        if endpoint == "/search":
            if self.search_requests >= self.max_search_requests:
                return False
            self.search_requests += 1
            return True
        if self.general_requests >= self.max_general_requests:
            return False
        self.general_requests += 1
        return True


def is_invalid_page_token(failure: CrawlerFailure) -> bool:
    return failure.details.get("provider_reason") == "invalidPageToken"


async def youtube_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any],
    attempts: int = 3,
    before_request: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Return a JSON object or a redacted typed provider failure."""
    if not 1 <= attempts <= 5:
        raise ValueError("YouTube request attempts must be between 1 and 5")
    request_params = dict(params)
    api_key = request_params.pop("key", None)
    response: httpx.Response | Any | None = None
    for attempt in range(attempts):
        if before_request is not None and not before_request():
            raise CrawlerFailure(
                CrawlerErrorCode.BUDGET_EXHAUSTED,
                "YouTube request budget was exhausted.",
            )
        try:
            if api_key:
                try:
                    response = await client.get(
                        url,
                        params=request_params,
                        headers={"x-goog-api-key": str(api_key)},
                    )
                except TypeError:
                    # Lightweight test doubles and older adapters may not expose
                    # the headers keyword; production httpx clients use the header.
                    response = await client.get(url, params=request_params)
            else:
                response = await client.get(url, params=request_params)
        except httpx.TransportError as exc:
            if attempt + 1 >= attempts:
                raise CrawlerFailure(
                    CrawlerErrorCode.TRANSPORT_ERROR,
                    "YouTube Data API request failed.",
                    retryable=True,
                ) from exc
            await asyncio.sleep(0.25 * (2**attempt))
            continue

        status = int(getattr(response, "status_code", 200))
        if status not in _RETRYABLE or attempt + 1 >= attempts:
            break
        await asyncio.sleep(_retry_after_seconds(response, attempt))

    assert response is not None
    status = int(getattr(response, "status_code", 200))
    if status >= 400:
        raise _response_failure(response, status)
    try:
        payload = response.json()
    except ValueError as exc:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "YouTube Data API returned invalid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "YouTube Data API returned an invalid response.",
        )
    return payload


def _response_failure(response: httpx.Response | Any, status: int) -> CrawlerFailure:
    reason = _safe_reason(response)
    details = {"provider_reason": reason} if reason else {}
    if reason == "commentsDisabled":
        return CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "Comments are disabled for this YouTube video.",
            details=details,
        )
    if status == 429 or reason in _QUOTA_REASONS:
        retry_after = _retry_after_seconds(response, 0, default=None)
        return CrawlerFailure(
            CrawlerErrorCode.RATE_LIMITED,
            "YouTube API quota or request limit was reached.",
            retryable=status == 429 or reason in {"rateLimitExceeded", "userRateLimitExceeded"},
            retry_after_seconds=retry_after,
            details=details,
        )
    if status in {401, 403} or reason in _AUTH_REASONS:
        return CrawlerFailure(
            CrawlerErrorCode.AUTH_REQUIRED,
            "YouTube API key was rejected or lacks Data API access.",
            details=details,
        )
    if status == 404:
        return CrawlerFailure(
            CrawlerErrorCode.NOT_FOUND,
            "YouTube resource was not found.",
            details=details,
        )
    if status == 400:
        return CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "YouTube rejected the request or checkpoint cursor.",
            details=details,
        )
    return CrawlerFailure(
        CrawlerErrorCode.TRANSPORT_ERROR,
        "YouTube Data API is temporarily unavailable.",
        retryable=status >= 500,
        details=details,
    )


def _safe_reason(response: httpx.Response | Any) -> str | None:
    try:
        payload = response.json()
    except (AttributeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    errors = error.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                reason = str(item.get("reason") or "")
                if reason and len(reason) <= 100 and reason.replace("_", "").isalnum():
                    return reason
    status = str(error.get("status") or "")
    return status if status and len(status) <= 100 and status.replace("_", "").isalnum() else None


def _retry_after_seconds(
    response: httpx.Response | Any,
    attempt: int,
    *,
    default: float | None = 0.25,
) -> float | None:
    value = getattr(response, "headers", {}).get("Retry-After")
    if value:
        try:
            return min(max(float(value), 0.0), 60.0)
        except (TypeError, ValueError):
            try:
                parsed = parsedate_to_datetime(str(value))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return min(max((parsed - datetime.now(UTC)).total_seconds(), 0.0), 60.0)
            except (TypeError, ValueError):
                pass
    return None if default is None else default * (2**attempt)
