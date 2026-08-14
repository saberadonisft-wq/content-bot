"""Bounded, provider-neutral cursor pagination."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from .contracts import CancellationToken, RunBudgets
from .errors import CrawlerErrorCode, CrawlerFailure

ItemT = TypeVar("ItemT")
CursorT = TypeVar("CursorT")


@dataclass(frozen=True, slots=True)
class Page(Generic[ItemT, CursorT]):
    items: tuple[ItemT, ...]
    next_cursor: CursorT | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class PageBatch(Generic[ItemT, CursorT]):
    items: tuple[ItemT, ...]
    input_cursor: CursorT | None
    next_cursor: CursorT | None
    request_number: int
    item_offset: int
    truncated: bool = False


async def bounded_pages(
    fetch_page: Callable[[CursorT | None, int], Awaitable[Page[ItemT, CursorT]]],
    *,
    budgets: RunBudgets,
    cancellation: CancellationToken,
    initial_cursor: CursorT | None = None,
    page_size: int = 50,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncIterator[PageBatch[ItemT, CursorT]]:
    """Yield exact-budget page batches with deadline and cursor-loop guards."""
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    deadline = clock() + budgets.deadline_seconds
    cursor = initial_cursor
    seen_cursors: set[str] = set()
    emitted = 0
    empty_pages = 0

    for request_number in range(1, budgets.max_requests + 1):
        cancellation.raise_if_cancelled()
        if emitted >= budgets.max_items:
            return
        if clock() >= deadline:
            raise CrawlerFailure(
                CrawlerErrorCode.DEADLINE_EXCEEDED,
                "Crawler deadline was exceeded.",
                retryable=True,
            )
        requested = min(page_size, budgets.max_items - emitted)
        page = await fetch_page(cursor, requested)
        cancellation.raise_if_cancelled()
        remaining = budgets.max_items - emitted
        items = tuple(page.items[:remaining])
        truncated = len(page.items) > len(items)
        if items:
            empty_pages = 0
            yield PageBatch(
                items=items,
                input_cursor=cursor,
                next_cursor=page.next_cursor,
                request_number=request_number,
                item_offset=emitted,
                truncated=truncated,
            )
            emitted += len(items)
        else:
            empty_pages += 1

        if emitted >= budgets.max_items or not page.has_more:
            return
        if page.next_cursor is None:
            raise CrawlerFailure(
                CrawlerErrorCode.CURSOR_STALLED,
                "Provider indicated more pages without a cursor.",
            )
        cursor_key = repr(page.next_cursor)
        if page.next_cursor == cursor or cursor_key in seen_cursors:
            raise CrawlerFailure(
                CrawlerErrorCode.CURSOR_STALLED,
                "Provider cursor did not advance.",
            )
        if empty_pages > budgets.max_empty_pages:
            raise CrawlerFailure(
                CrawlerErrorCode.CURSOR_STALLED,
                "Provider returned too many empty pages.",
            )
        seen_cursors.add(cursor_key)
        cursor = page.next_cursor

    raise CrawlerFailure(
        CrawlerErrorCode.BUDGET_EXHAUSTED,
        "Crawler request budget was exhausted.",
        retryable=True,
    )
