"""Typed failure taxonomy shared by adapters and the supervisor."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class CrawlerErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_TIMEOUT = "AUTH_TIMEOUT"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED"
    PAYMENT_OR_ACCESS_REQUIRED = "PAYMENT_OR_ACCESS_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_FOUND = "NOT_FOUND"
    PARSE_CHANGED = "PARSE_CHANGED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    CURSOR_STALLED = "CURSOR_STALLED"
    CANCELLED = "CANCELLED"
    UNSUPPORTED = "UNSUPPORTED"


class CrawlerFailure(RuntimeError):
    """A safe, serializable failure; raw provider responses are never embedded."""

    def __init__(
        self,
        code: CrawlerErrorCode,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message[:500]
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.details = dict(details or {})

    def as_event_error(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.safe_message,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
            "details": self.details,
        }
