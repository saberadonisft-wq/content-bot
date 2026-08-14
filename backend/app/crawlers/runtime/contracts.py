"""Immutable run context, canonical records and event protocol."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .errors import CrawlerErrorCode, CrawlerFailure


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class RunBudgets:
    max_items: int = 500
    max_requests: int = 100
    deadline_seconds: float = 900
    max_empty_pages: int = 2
    max_root_comments: int = 0
    max_children_per_root: int = 0
    max_total_comments: int = 0
    max_media_files: int = 0
    max_media_bytes: int = 0

    def __post_init__(self) -> None:
        positive = (self.max_items, self.max_requests, self.deadline_seconds)
        if any(value <= 0 for value in positive):
            raise ValueError("Item, request and deadline budgets must be positive")
        if self.max_empty_pages < 0:
            raise ValueError("max_empty_pages cannot be negative")
        optional = (
            self.max_root_comments,
            self.max_children_per_root,
            self.max_total_comments,
            self.max_media_files,
            self.max_media_bytes,
        )
        if any(value < 0 for value in optional):
            raise ValueError("Optional budgets cannot be negative")


@dataclass(frozen=True, slots=True)
class RunContext:
    run_id: str
    keyword_id: int
    source_id: str
    provider_id: str
    operation: str
    target: Mapping[str, Any]
    terms: tuple[str, ...]
    filters: Mapping[str, Any]
    budgets: RunBudgets = field(default_factory=RunBudgets)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        for label, value in (
            ("run_id", self.run_id),
            ("source_id", self.source_id),
            ("provider_id", self.provider_id),
            ("operation", self.operation),
        ):
            if not str(value).strip():
                raise ValueError(f"{label} cannot be empty")
        object.__setattr__(self, "target", _freeze(dict(self.target)))
        object.__setattr__(self, "filters", _freeze(dict(self.filters)))
        object.__setattr__(
            self,
            "terms",
            tuple(dict.fromkeys(term.strip() for term in self.terms if term.strip())),
        )
        if self.started_at.tzinfo is None:
            object.__setattr__(self, "started_at", self.started_at.replace(tzinfo=UTC))


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CrawlerFailure(
                CrawlerErrorCode.CANCELLED,
                "Crawler run was cancelled.",
            )


@dataclass(frozen=True, slots=True)
class ContentRecord:
    source_id: str
    external_id: str
    canonical_url: str
    title: str
    body: str = ""
    author_pseudonym: str = ""
    published_at: datetime | None = None
    metrics: Mapping[str, int] = field(default_factory=dict)
    media: tuple[Mapping[str, Any], ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_id or not self.external_id or not self.canonical_url:
            raise ValueError("Content identity fields cannot be empty")
        object.__setattr__(self, "metrics", _freeze(dict(self.metrics)))
        object.__setattr__(self, "media", tuple(_freeze(item) for item in self.media))
        object.__setattr__(self, "provenance", _freeze(dict(self.provenance)))


@dataclass(frozen=True, slots=True)
class CommentRecord:
    source_id: str
    external_id: str
    content_external_id: str
    body: str
    author_pseudonym: str = ""
    published_at: datetime | None = None
    like_count: int = 0
    child_count: int = 0
    parent_external_id: str | None = None
    root_external_id: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = (self.source_id, self.external_id, self.content_external_id)
        if any(not str(value).strip() for value in required):
            raise ValueError("Comment identity fields cannot be empty")
        if self.like_count < 0 or self.child_count < 0:
            raise ValueError("Comment counters cannot be negative")
        if self.published_at is not None and self.published_at.tzinfo is None:
            object.__setattr__(
                self, "published_at", self.published_at.replace(tzinfo=UTC)
            )
        object.__setattr__(self, "provenance", _freeze(dict(self.provenance)))


class CrawlerEventType(StrEnum):
    RUN_STARTED = "run_started"
    BROWSER_OPENING = "browser_opening"
    AUTH_REQUIRED = "auth_required"
    AUTHENTICATED = "authenticated"
    PAGE_SCANNED = "page_scanned"
    ITEM = "item"
    CHECKPOINT = "checkpoint"
    RATE_LIMITED = "rate_limited"
    WARNING = "warning"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class CrawlerEvent:
    type: CrawlerEventType
    run_id: str
    source_id: str
    provider_id: str
    operation: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    progress_current: int | None = None
    progress_total: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _freeze(dict(self.payload)))
        if self.timestamp.tzinfo is None:
            object.__setattr__(self, "timestamp", self.timestamp.replace(tzinfo=UTC))
