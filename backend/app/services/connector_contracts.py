from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ConnectorCapabilities:
    global_search: bool
    watchlist_filter: bool = False
    requires_login: bool = False
    interaction_fields: tuple[str, ...] = ()


@dataclass
class ConnectorStatus:
    state: str
    detail: str
    reason_code: str | None = None
    probe: str = "local"


@dataclass
class SearchQuery:
    keyword_id: int
    name: str
    include_terms: list[str]
    max_items: int
    request_budget: int | None = None
    deadline_seconds: float | None = None
    progress_callback: Callable[[str, str], Awaitable[None]] | None = field(
        default=None,
        repr=False,
    )
    warning_callback: Callable[[str, str], Awaitable[None]] | None = field(
        default=None,
        repr=False,
    )
    checkpoint_tracker: Any | None = field(default=None, repr=False)
    legacy_checkpoint: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.max_items <= 0:
            raise ValueError("Search item budget must be positive")
        if self.request_budget is not None and self.request_budget <= 0:
            raise ValueError("Search request budget must be positive")
        if self.deadline_seconds is not None and self.deadline_seconds <= 0:
            raise ValueError("Search deadline must be positive")

    def request_limit(self, default: int, *, maximum: int) -> int:
        selected = default
        if self.request_budget is not None:
            selected = min(selected, self.request_budget)
        return max(1, min(selected, maximum))

    def deadline_limit(self, default: float, *, maximum: float = 7_200) -> float:
        selected = default
        if self.deadline_seconds is not None:
            selected = min(selected, self.deadline_seconds)
        return max(1.0, min(selected, maximum))

    @property
    def search_terms(self) -> list[str]:
        seen: set[str] = set()
        terms: list[str] = []
        for term in (self.name, *self.include_terms):
            cleaned = term.strip()
            key = cleaned.casefold()
            if cleaned and key not in seen:
                seen.add(key)
                terms.append(cleaned)
        return terms

    def resume_cursor(
        self,
        kind: str,
        *,
        default: Any = None,
        legacy_key: str | None = None,
        **scope: Any,
    ) -> Any:
        if self.checkpoint_tracker is not None:
            from .checkpoints import cursor_scope

            value = self.checkpoint_tracker.cursor(
                cursor_scope(kind, **scope), default=None
            )
            if value is not None:
                return value
        if legacy_key:
            return self.legacy_checkpoint.get(legacy_key, default)
        return default

    def report_cursor(self, kind: str, value: Any | None, **scope: Any) -> None:
        if self.checkpoint_tracker is None:
            return
        from .checkpoints import cursor_scope

        self.checkpoint_tracker.report_cursor(cursor_scope(kind, **scope), value)

    @property
    def recent_ids(self) -> tuple[str, ...]:
        if self.checkpoint_tracker is None:
            return tuple(
                str(item) for item in self.legacy_checkpoint.get("recent_ids", [])
            )
        return tuple(self.checkpoint_tracker.recent_ids)


@dataclass
class RawContentItem:
    external_id: str
    canonical_url: str
    title: str
    body_snippet: str = ""
    author: str = ""
    hashtags: list[str] = field(default_factory=list)
    locale: str | None = None
    published_at: datetime | None = None
    metrics: dict[str, int] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    media: list[dict[str, Any]] = field(default_factory=list)


class SourceConnector(abc.ABC):
    source_id: str
    label: str
    group: str
    capabilities: ConnectorCapabilities

    @property
    def configured(self) -> bool:
        """Return whether the connector has its local prerequisites.

        This is intentionally a synchronous, side-effect-free signal for run
        planning and catalog defaults. Remote health remains the responsibility
        of ``healthcheck``.
        """
        return True

    @abc.abstractmethod
    async def healthcheck(self) -> ConnectorStatus:
        raise NotImplementedError

    async def deep_healthcheck(self) -> ConnectorStatus:
        """Perform an explicit remote probe when the caller accepts its cost."""
        return await self.healthcheck()

    def checkpoint_fingerprint_fields(
        self,
        operation: str,
        channel: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Declare provider options that change cursor compatibility."""
        del operation, channel
        return {}

    @abc.abstractmethod
    async def search(
        self, query: SearchQuery, checkpoint: dict[str, Any] | None = None
    ) -> AsyncIterator[RawContentItem]:
        raise NotImplementedError
