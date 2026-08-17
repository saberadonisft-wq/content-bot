"""CBCE facade for selectively reused MediaCrawler platform code.

The facade owns policy and lifecycle checks.  A source-derived delegate only
implements platform extraction/request details; it never receives the Content
Bot store, scheduler, process supervisor, raw secrets, or unlimited budgets.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ...runtime import CancellationToken, ContentRecord, RunContext
from ...runtime.paginator import Page
from .policy import LicensedReusePolicy, assert_licensed_reuse_allowed
from .source_map import LicensedSourceMap, load_source_map
from .weibo_field import SearchType


class LicensedDelegate(Protocol):
    async def open(self, context: RunContext, cancellation: CancellationToken) -> None: ...

    async def fetch_page(
        self, context: RunContext, cursor: Any | None, limit: int
    ) -> Page[ContentRecord, Any]: ...

    async def close(self) -> None: ...


class LicensedSearchFacade:
    """Adapt one source-derived search delegate to the CBCE contract."""

    owns_resources = True

    def __init__(
        self,
        *,
        source_id: str,
        provider_id: str,
        delegate: LicensedDelegate,
        reuse_root,
        policy: LicensedReusePolicy | None = None,
        on_delegate_error: Callable[[Exception], Exception] | None = None,
    ) -> None:
        if not source_id.strip() or not provider_id.strip():
            raise ValueError("Licensed facade identity cannot be empty")
        self.source_id = source_id
        self.provider_id = provider_id
        self.delegate = delegate
        self.reuse_root = reuse_root
        self.policy = policy or assert_licensed_reuse_allowed()
        self.source_map: LicensedSourceMap | None = None
        self.on_delegate_error = on_delegate_error
        self._opened = False

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if context.source_id != self.source_id or context.provider_id != self.provider_id:
            raise ValueError("Licensed facade context identity is invalid")
        if context.operation != "search":
            raise ValueError("Licensed search facade only supports search")
        if self.source_id == "weibo":
            raw_search_type = str(context.filters.get("search_type", "default"))
            if raw_search_type.casefold() not in {
                "default",
                "real_time",
                "popular",
                "video",
            } and raw_search_type not in {item.value for item in SearchType}:
                raise ValueError("Licensed Weibo search type is unsupported")
        self.policy.validate_budgets(
            max_items=context.budgets.max_items,
            max_requests=context.budgets.max_requests,
        )
        self.source_map = load_source_map(self.reuse_root)
        await self.delegate.open(context, cancellation)
        self._opened = True

    async def fetch_page(
        self, context: RunContext, cursor: Any | None, limit: int
    ) -> Page[ContentRecord, Any]:
        if not self._opened:
            raise RuntimeError("Licensed facade is not open")
        if limit < 1 or limit > context.budgets.max_items:
            raise ValueError("Licensed facade page limit is invalid")
        try:
            page = await self.delegate.fetch_page(context, cursor, limit)
        except Exception as exc:
            if self.on_delegate_error is None:
                raise
            raise self.on_delegate_error(exc) from exc
        if len(page.items) > limit:
            # Delegates are not trusted to widen the CBCE budget.  Retain only
            # the records the parent worker requested; the next cursor remains
            # the delegate's continuation and is checkpointed after persistence.
            return Page(tuple(page.items[:limit]), page.next_cursor, page.has_more)
        return page

    async def close(self) -> None:
        try:
            await self.delegate.close()
        finally:
            self._opened = False
