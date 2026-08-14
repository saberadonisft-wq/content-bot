import asyncio
from datetime import UTC, datetime

import pytest

from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    Page,
    RunBudgets,
    RunContext,
    bounded_pages,
)


def test_run_context_freezes_nested_input_and_normalizes_terms() -> None:
    target = {"kind": "keyword", "options": ["latest"]}
    context = RunContext(
        run_id="run-1",
        keyword_id=1,
        source_id="bluesky",
        provider_id="bluesky_public",
        operation="search",
        target=target,
        terms=(" Hades II ", "Hades II", "Hades 2"),
        filters={"languages": ["vi", "en"]},
        started_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    target["options"].append("top")

    assert context.terms == ("Hades II", "Hades 2")
    assert context.target["options"] == ("latest",)
    assert context.filters["languages"] == ("vi", "en")
    assert context.started_at.tzinfo is UTC
    with pytest.raises(TypeError):
        context.target["kind"] = "creator"


def test_bounded_pages_enforces_exact_item_limit_and_reports_offsets() -> None:
    calls: list[tuple[str | None, int]] = []

    async def fetch(cursor: str | None, limit: int) -> Page[int, str]:
        calls.append((cursor, limit))
        start = 0 if cursor is None else int(cursor)
        return Page(tuple(range(start, start + 4)), str(start + 4), True)

    async def collect():
        return [
            batch
            async for batch in bounded_pages(
                fetch,
                budgets=RunBudgets(max_items=6, max_requests=5, deadline_seconds=10),
                cancellation=CancellationToken(),
                page_size=4,
                clock=lambda: 0,
            )
        ]

    batches = asyncio.run(collect())
    assert [batch.items for batch in batches] == [(0, 1, 2, 3), (4, 5)]
    assert [batch.item_offset for batch in batches] == [0, 4]
    assert batches[1].truncated is True
    assert calls == [(None, 4), ("4", 2)]


def test_repeated_cursor_fails_instead_of_looping() -> None:
    async def fetch(cursor: str | None, limit: int) -> Page[int, str]:
        del limit
        return Page((1,), cursor or "same", True)

    async def collect() -> None:
        async for _ in bounded_pages(
            fetch,
            budgets=RunBudgets(max_items=5, max_requests=5, deadline_seconds=10),
            cancellation=CancellationToken(),
            clock=lambda: 0,
        ):
            pass

    with pytest.raises(CrawlerFailure) as captured:
        asyncio.run(collect())
    assert captured.value.code is CrawlerErrorCode.CURSOR_STALLED


def test_cancellation_is_observed_before_provider_request() -> None:
    token = CancellationToken()
    token.cancel()
    called = False

    async def fetch(cursor, limit):
        nonlocal called
        called = True
        return Page((), None, False)

    async def collect() -> None:
        async for _ in bounded_pages(
            fetch,
            budgets=RunBudgets(),
            cancellation=token,
        ):
            pass

    with pytest.raises(CrawlerFailure) as captured:
        asyncio.run(collect())
    assert captured.value.code is CrawlerErrorCode.CANCELLED
    assert called is False


def test_deadline_and_request_budget_have_distinct_errors() -> None:
    async def fetch(cursor, limit):
        del limit
        return Page((1,), f"{cursor or ''}x", True)

    async def deadline_run() -> None:
        times = iter((0.0, 2.0))
        async for _ in bounded_pages(
            fetch,
            budgets=RunBudgets(max_items=5, max_requests=5, deadline_seconds=1),
            cancellation=CancellationToken(),
            clock=lambda: next(times),
        ):
            pass

    with pytest.raises(CrawlerFailure) as deadline:
        asyncio.run(deadline_run())
    assert deadline.value.code is CrawlerErrorCode.DEADLINE_EXCEEDED

    async def request_run() -> None:
        async for _ in bounded_pages(
            fetch,
            budgets=RunBudgets(max_items=5, max_requests=1, deadline_seconds=10),
            cancellation=CancellationToken(),
            clock=lambda: 0,
        ):
            pass

    with pytest.raises(CrawlerFailure) as requests:
        asyncio.run(request_run())
    assert requests.value.code is CrawlerErrorCode.BUDGET_EXHAUSTED
