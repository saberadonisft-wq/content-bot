import asyncio

import pytest

from app.crawlers.runtime import (
    CancellationToken,
    ContentRecord,
    CrawlerErrorCode,
    CrawlerEventType,
    CrawlerFailure,
    CrawlerSupervisor,
    Page,
    RunBudgets,
    RunContext,
)


def context(*, max_items: int = 3) -> RunContext:
    return RunContext(
        run_id="run-1",
        keyword_id=1,
        source_id="fake",
        provider_id="fake_provider",
        operation="search",
        target={"kind": "keyword"},
        terms=("game",),
        filters={},
        budgets=RunBudgets(max_items=max_items, max_requests=5, deadline_seconds=10),
    )


def record(index: int) -> ContentRecord:
    return ContentRecord(
        source_id="fake",
        external_id=f"item-{index}",
        canonical_url=f"https://example.test/items/{index}",
        title=f"Item {index}",
    )


class FakeAdapter:
    source_id = "fake"
    provider_id = "fake_provider"
    owns_resources = True

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.closed = False

    async def open(self, context, cancellation) -> None:
        self.log.append("open")

    async def fetch_page(self, context, cursor, limit):
        self.log.append(f"fetch:{cursor}:{limit}")
        if cursor is None:
            return Page((record(1), record(2)), "page-2", True)
        return Page((record(3),), None, False)

    async def close(self) -> None:
        self.closed = True
        self.log.append("close")


async def drain(supervisor: CrawlerSupervisor) -> list:
    events = []
    async for event in supervisor.events():
        events.append(event)
    return events


def test_supervisor_persists_page_before_committing_cursor_and_closes() -> None:
    async def run():
        log: list[str] = []
        adapter = FakeAdapter(log)
        supervisor = CrawlerSupervisor(event_queue_size=20)

        async def persist(item):
            log.append(f"persist:{item.external_id}")

        async def commit(cursor):
            log.append(f"commit:{cursor}")

        consumer = asyncio.create_task(drain(supervisor))
        result = await supervisor.run_search(
            context(), adapter, persist=persist, commit_cursor=commit, page_size=2
        )
        return log, adapter, result, await consumer

    log, adapter, result, events = asyncio.run(run())
    assert log.index("persist:item-2") < log.index("commit:page-2")
    assert log.index("persist:item-3") < log.index("commit:None")
    assert log[-1] == "close"
    assert adapter.closed is True
    assert result.persisted_count == 3
    assert [event.type for event in events] == [
        CrawlerEventType.RUN_STARTED,
        CrawlerEventType.ITEM,
        CrawlerEventType.ITEM,
        CrawlerEventType.PAGE_SCANNED,
        CrawlerEventType.ITEM,
        CrawlerEventType.PAGE_SCANNED,
        CrawlerEventType.COMPLETED,
    ]


def test_storage_failure_does_not_commit_page_cursor() -> None:
    async def run():
        log: list[str] = []
        adapter = FakeAdapter(log)
        supervisor = CrawlerSupervisor(event_queue_size=20)

        async def persist(item):
            log.append(f"persist:{item.external_id}")
            if item.external_id == "item-2":
                raise OSError("disk unavailable")

        async def commit(cursor):
            log.append(f"commit:{cursor}")

        consumer = asyncio.create_task(drain(supervisor))
        with pytest.raises(CrawlerFailure) as captured:
            await supervisor.run_search(
                context(), adapter, persist=persist, commit_cursor=commit
            )
        return captured.value, log, adapter, await consumer

    failure, log, adapter, events = asyncio.run(run())
    assert failure.code is CrawlerErrorCode.STORAGE_ERROR
    assert not any(entry.startswith("commit:") for entry in log)
    assert adapter.closed is True
    assert events[-1].type is CrawlerEventType.FAILED


def test_cooperative_cancellation_stops_before_checkpoint_and_cleans_up() -> None:
    async def run():
        log: list[str] = []
        adapter = FakeAdapter(log)
        supervisor = CrawlerSupervisor(event_queue_size=20)
        token = CancellationToken()

        async def persist(item):
            log.append(f"persist:{item.external_id}")
            token.cancel()

        async def commit(cursor):
            log.append(f"commit:{cursor}")

        consumer = asyncio.create_task(drain(supervisor))
        with pytest.raises(CrawlerFailure) as captured:
            await supervisor.run_search(
                context(),
                adapter,
                persist=persist,
                commit_cursor=commit,
                cancellation=token,
            )
        return captured.value, log, adapter, await consumer

    failure, log, adapter, events = asyncio.run(run())
    assert failure.code is CrawlerErrorCode.CANCELLED
    assert log.count("persist:item-1") == 1
    assert not any(entry.startswith("commit:") for entry in log)
    assert adapter.closed is True
    assert events[-1].type is CrawlerEventType.CANCELLED


def test_bounded_event_queue_applies_backpressure() -> None:
    async def run():
        supervisor = CrawlerSupervisor(event_queue_size=1)
        adapter = FakeAdapter([])

        async def persist(item):
            del item

        async def commit(cursor):
            del cursor

        task = asyncio.create_task(
            supervisor.run_search(
                context(max_items=1), adapter, persist=persist, commit_cursor=commit
            )
        )
        await asyncio.sleep(0)
        assert supervisor.event_queue_size == 1
        assert not task.done()
        events = []
        while not task.done() or supervisor.event_queue_size:
            events.append(await supervisor.next_event())
            await asyncio.sleep(0)
        await task
        return events

    events = asyncio.run(run())
    assert events[0].type is CrawlerEventType.RUN_STARTED
    assert events[-1].type is CrawlerEventType.COMPLETED
