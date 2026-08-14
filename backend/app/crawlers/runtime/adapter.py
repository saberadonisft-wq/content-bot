"""Minimal adapter boundary used by the Phase 2 runtime vertical slice."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .contracts import CancellationToken, ContentRecord, RunContext
from .paginator import Page


@runtime_checkable
class SearchAdapter(Protocol):
    source_id: str
    provider_id: str
    owns_resources: bool

    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def fetch_page(
        self,
        context: RunContext,
        cursor: Any | None,
        limit: int,
    ) -> Page[ContentRecord, Any]: ...

    async def close(self) -> None: ...
