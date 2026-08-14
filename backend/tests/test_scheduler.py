import asyncio
from datetime import UTC, datetime, timedelta

from app.crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from app.schemas import SourceRunOutput
from app.services.connectors import (
    ConnectorCapabilities,
    ConnectorStatus,
    RawContentItem,
    SearchQuery,
    SourceConnector,
    login_progress_from_stderr,
)
from app.services.runs import EventBus, RunManager, next_scheduled_time


def test_login_progress_parser_accepts_runner_signal_without_exposing_cookies() -> None:
    assert login_progress_from_stderr(
        'CONTENT_BOT_PROGRESS {"status":"authenticated","message":"Login confirmed"}'
    ) == ("authenticated", "Login confirmed")
    assert login_progress_from_stderr(
        'CONTENT_BOT_PROGRESS {"status":"retrying","message":"Reopening browser"}'
    ) == ("retrying", "Reopening browser")
    assert login_progress_from_stderr(
        "INFO Login successful then wait for redirect"
    ) == ("authenticated", "Login confirmed by MediaCrawler")
    assert login_progress_from_stderr("cookies=secret-value") is None


class LoginConnector(SourceConnector):
    source_id = "login-source"
    label = "Login source"
    group = "Test"
    capabilities = ConnectorCapabilities(True, requires_login=True)

    def __init__(self):
        self.search_called = False

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "ready")

    async def search(self, query: SearchQuery, checkpoint=None):
        self.search_called = True
        if False:
            yield RawContentItem("", "", "")


class SlowConnector(SourceConnector):
    source_id = "slow-source"
    label = "Slow source"
    group = "Test"
    capabilities = ConnectorCapabilities(True)

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "ready")

    async def search(self, query: SearchQuery, checkpoint=None):
        await asyncio.Future()
        if False:
            yield RawContentItem("", "", "")


class PublicProgressConnector(SourceConnector):
    source_id = "progress-source"
    label = "Progress source"
    group = "Test"
    capabilities = ConnectorCapabilities(True)

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "ready")

    async def search(self, query: SearchQuery, checkpoint=None):
        for index in range(2):
            yield RawContentItem(
                external_id=f"item-{index}",
                canonical_url=f"https://example.test/item-{index}",
                title=f"{query.name} public update {index}",
            )


class ClosedLoginConnector(SourceConnector):
    source_id = "closed-login-source"
    label = "Closed login source"
    group = "Test"
    capabilities = ConnectorCapabilities(True, requires_login=True)

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "ready")

    async def search(self, query: SearchQuery, checkpoint=None):
        raise RuntimeError("playwright TargetClosedError: BrowserContext has been closed")
        if False:
            yield RawContentItem("", "", "")


class ParserDriftConnector(SourceConnector):
    source_id = "parser-drift-source"
    label = "Parser drift source"
    group = "Test"
    capabilities = ConnectorCapabilities(True)

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "ready")

    async def search(self, query: SearchQuery, checkpoint=None):
        del query, checkpoint
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Provider response contract changed safely.",
            details={"raw_provider_payload": "must-not-be-persisted"},
        )
        if False:
            yield RawContentItem("", "", "")


class CapturingEventBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.published.append(dict(event))
        await super().publish(event)


def add_keyword(storage, name: str, source_id: str, *, enabled: bool, next_run_at=None) -> int:
    now = datetime.now(UTC)
    return storage.create_keyword(
        {
            "name": name,
            "normalized_name": name.casefold(),
            "include_terms": [],
            "exclude_terms": [],
            "source_ids": [source_id],
            "enabled": enabled,
            "interval_minutes": 60,
            "max_items_per_source": 500,
            "next_run_at": next_run_at,
            "created_at": now,
            "updated_at": now,
        }
    )["id"]


def test_next_scheduled_time_preserves_cadence_after_downtime() -> None:
    previous = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 8, 1, 3, 17, tzinfo=UTC)
    assert next_scheduled_time(previous, 60, now) == datetime(2026, 8, 1, 4, 0, tzinfo=UTC)
    assert next_scheduled_time(previous, 60, previous - timedelta(minutes=1)) == previous


def test_scheduler_skips_login_gated_connector_without_calling_search(mongo_store) -> None:
    connector = LoginConnector()
    manager = RunManager({connector.source_id: connector}, EventBus(), mongo_store)
    keyword_id = add_keyword(
        mongo_store,
        "Scheduled login safety",
        connector.source_id,
        enabled=True,
        next_run_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    async def run_tick() -> None:
        await manager.scheduler_tick()
        await asyncio.gather(*manager._tasks.values())

    asyncio.run(run_tick())
    assert connector.search_called is False
    keyword = mongo_store.keyword(keyword_id)
    assert keyword["next_run_at"] > datetime.now(UTC)
    batch = mongo_store.batches(keyword_id)[0]
    assert batch["trigger"] == "schedule"
    assert batch["state"] == "skipped"
    source_run = batch["source_runs"][0]
    assert source_run["state"] == "skipped"
    assert "never open login-gated" in source_run["error_message"]

    async def run_manual() -> str:
        batch_id = await manager.start_batch(keyword_id, trigger="manual")
        await manager._tasks[batch_id]
        return batch_id

    manual_batch_id = asyncio.run(run_manual())
    assert connector.search_called is True
    assert mongo_store.batch(manual_batch_id)["state"] == "succeeded"


def test_cancelled_batch_reaches_terminal_state(mongo_store) -> None:
    connector = SlowConnector()
    manager = RunManager({connector.source_id: connector}, EventBus(), mongo_store)
    keyword_id = add_keyword(mongo_store, "Cancellation state", connector.source_id, enabled=False)

    async def run_and_cancel() -> str:
        batch_id = await manager.start_batch(keyword_id)
        for _ in range(50):
            await asyncio.sleep(0.01)
            source_run = mongo_store.source_runs(batch_id)[0]
            if source_run["state"] == "running":
                break
        assert await manager.cancel_batch(batch_id) is True
        return batch_id

    batch_id = asyncio.run(run_and_cancel())
    batch = mongo_store.batch(batch_id)
    source_run = batch["source_runs"][0]
    assert batch["state"] == "cancelled"
    assert batch["finished_at"] is not None
    assert source_run["state"] == "cancelled"
    assert source_run["finished_at"] is not None


def test_public_scan_persists_progress_and_counts(mongo_store) -> None:
    connector = PublicProgressConnector()
    manager = RunManager({connector.source_id: connector}, EventBus(), mongo_store)
    keyword_id = add_keyword(mongo_store, "Progress game", connector.source_id, enabled=False)

    async def run_batch() -> str:
        batch_id = await manager.start_batch(keyword_id)
        await manager._tasks[batch_id]
        return batch_id

    batch_id = asyncio.run(run_batch())
    source_run = mongo_store.batch(batch_id)["source_runs"][0]
    assert source_run["state"] == "succeeded"
    assert source_run["phase"] == "completed"
    assert source_run["progress_mode"] == "determinate"
    assert source_run["progress_current"] == 2
    assert source_run["progress_percent"] == 100.0
    assert source_run["fetched_count"] == 2
    assert source_run["ingested_count"] == 2


def test_closed_login_browser_is_reported_as_a_recoverable_source_error(mongo_store) -> None:
    connector = ClosedLoginConnector()
    manager = RunManager({connector.source_id: connector}, EventBus(), mongo_store)
    keyword_id = add_keyword(mongo_store, "Closed browser", connector.source_id, enabled=False)

    async def run_batch() -> str:
        batch_id = await manager.start_batch(keyword_id)
        await manager._tasks[batch_id]
        return batch_id

    batch_id = asyncio.run(run_batch())
    source_run = mongo_store.batch(batch_id)["source_runs"][0]
    assert source_run["state"] == "failed"
    assert source_run["phase"] == "browser_closed"
    assert source_run["message"] == "Cốc Cốc was closed before the scan finished."


def test_parser_drift_is_structured_persisted_and_emitted_without_raw_payload(
    mongo_store,
) -> None:
    connector = ParserDriftConnector()
    events = CapturingEventBus()
    manager = RunManager({connector.source_id: connector}, events, mongo_store)
    keyword_id = add_keyword(
        mongo_store,
        "Parser drift",
        connector.source_id,
        enabled=False,
    )

    async def run_batch() -> str:
        batch_id = await manager.start_batch(keyword_id)
        await manager._tasks[batch_id]
        return batch_id

    batch_id = asyncio.run(run_batch())
    source_run = mongo_store.batch(batch_id)["source_runs"][0]
    assert source_run["state"] == "failed"
    assert source_run["phase"] == "parser_drift"
    assert source_run["error_code"] == "PARSE_CHANGED"
    assert source_run["provider_id"] == "legacy_connector"
    assert source_run["operation"] == "search"
    assert source_run["retryable"] is False
    serialized = SourceRunOutput.model_validate(source_run).model_dump()
    assert serialized["error_code"] == "PARSE_CHANGED"
    assert serialized["provider_id"] == "legacy_connector"
    assert serialized["operation"] == "search"
    alerts = [
        event
        for event in events.published
        if event.get("type") == "parser-drift-alert"
    ]
    assert len(alerts) == 1
    assert alerts[0]["source_id"] == connector.source_id
    assert alerts[0]["error_code"] == "PARSE_CHANGED"
    assert "must-not-be-persisted" not in repr(source_run)
    assert "must-not-be-persisted" not in repr(events.published)


def test_startup_closes_interrupted_batches(mongo_store) -> None:
    keyword_id = add_keyword(mongo_store, "Interrupted run", "slow-source", enabled=False)
    mongo_store.create_batch(
        {"id": "interrupted-batch", "keyword_id": keyword_id, "trigger": "manual", "state": "running"},
        [{"id": "interrupted-source", "batch_id": "interrupted-batch", "source_id": "slow-source", "state": "running"}],
    )
    manager = RunManager({}, EventBus(), mongo_store)
    assert manager.cleanup_interrupted() == 1
    assert manager.cleanup_interrupted() == 0

    batch = mongo_store.batch("interrupted-batch")
    source_run = batch["source_runs"][0]
    assert batch["state"] == "failed"
    assert batch["finished_at"] is not None
    assert "restarted" in batch["error_message"]
    assert source_run["state"] == "failed"
    assert source_run["phase"] == "failed"
    assert source_run["finished_at"] is not None
