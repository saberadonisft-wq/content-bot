import asyncio
from datetime import UTC, datetime

from app.services.connectors import (
    ConnectorCapabilities,
    ConnectorStatus,
    RawContentItem,
    SearchQuery,
    SourceConnector,
)
from app.services.runs import EventBus, RunManager


class CheckpointConnector(SourceConnector):
    source_id = "checkpoint-source"
    label = "Checkpoint source"
    group = "Test"
    capabilities = ConnectorCapabilities(True)

    def __init__(self) -> None:
        self.received: list[object] = []
        self.fail_after_yield = False

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "ready")

    async def search(self, query: SearchQuery, checkpoint=None):
        current = query.resume_cursor("query", default=None)
        self.received.append(current)
        index = len(self.received)
        yield RawContentItem(
            external_id=f"item-{index}",
            canonical_url=f"https://example.test/items/{index}",
            title=f"Checkpoint game item {index}",
            published_at=datetime(2026, 8, index, tzinfo=UTC),
        )
        query.report_cursor("query", f"page-{index}")
        if self.fail_after_yield:
            raise RuntimeError("synthetic provider failure")


class RepeatingTiebaConnector(SourceConnector):
    source_id = "tieba"
    label = "Baidu Tieba test provider"
    group = "Test"
    capabilities = ConnectorCapabilities(True, requires_login=True)

    def __init__(self) -> None:
        self.run_number = 0

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "ready")

    async def search(self, query: SearchQuery, checkpoint=None):
        del checkpoint
        self.run_number += 1
        yield RawContentItem(
            external_id="1234567890",
            canonical_url="https://tieba.baidu.com/p/1234567890",
            title="Checkpoint game thread",
            metrics={"comment_count": self.run_number},
            published_at=datetime(2026, 8, 13, tzinfo=UTC),
            raw_payload={"provider_id": "cbce_tieba"},
        )
        query.report_cursor("provider", None, stream="search")


class WarningConnector(SourceConnector):
    source_id = "warning-source"
    label = "Warning source"
    group = "Test"
    capabilities = ConnectorCapabilities(True)

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "ready")

    async def search(self, query: SearchQuery, checkpoint=None):
        del checkpoint
        assert query.warning_callback is not None
        await query.warning_callback("PARSE_CHANGED", "One configured feed was skipped.")
        yield RawContentItem(
            external_id="valid-item",
            canonical_url="https://example.test/valid-item",
            title="Checkpoint game valid item",
        )


def add_keyword(storage, source_id: str, *, channels=None) -> int:
    now = datetime.now(UTC)
    return storage.create_keyword(
        {
            "name": "Checkpoint game",
            "normalized_name": "checkpoint game",
            "include_terms": [],
            "exclude_terms": [],
            "source_ids": [source_id],
            "source_selection_version": 2,
            "source_checkpoints": {},
            "channels": channels or [],
            "enabled": False,
            "interval_minutes": 360,
            "max_items_per_source": 10,
            "next_run_at": None,
            "created_at": now,
            "updated_at": now,
        }
    )["id"]


async def finish_batch(manager: RunManager, keyword_id: int, **selectors) -> str:
    batch_id = await manager.start_batch(keyword_id, **selectors)
    await manager._tasks[batch_id]
    return batch_id


def test_global_checkpoint_resumes_across_batches_and_is_snapshotted(mongo_store) -> None:
    connector = CheckpointConnector()
    manager = RunManager({connector.source_id: connector}, EventBus(), mongo_store)
    keyword_id = add_keyword(mongo_store, connector.source_id)

    first_id = asyncio.run(finish_batch(manager, keyword_id))
    first_checkpoint = mongo_store.keyword(keyword_id)["source_checkpoints"][connector.source_id]["search"]
    assert connector.received == [None]
    assert first_checkpoint["schema_version"] == 1
    assert first_checkpoint["recent_ids"] == ["item-1"]

    second_id = asyncio.run(finish_batch(manager, keyword_id))
    assert connector.received == [None, "page-1"]
    second_run = mongo_store.batch(second_id)["source_runs"][0]
    assert second_run["checkpoint"]["recent_ids"] == ["item-2", "item-1"]
    assert mongo_store.batch(first_id)["source_runs"][0]["state"] == "succeeded"


def test_failed_run_does_not_commit_staged_global_cursor(mongo_store) -> None:
    connector = CheckpointConnector()
    manager = RunManager({connector.source_id: connector}, EventBus(), mongo_store)
    keyword_id = add_keyword(mongo_store, connector.source_id)
    asyncio.run(finish_batch(manager, keyword_id))
    committed = mongo_store.keyword(keyword_id)["source_checkpoints"][connector.source_id]["search"]

    connector.fail_after_yield = True
    failed_id = asyncio.run(finish_batch(manager, keyword_id))
    after_failure = mongo_store.keyword(keyword_id)["source_checkpoints"][connector.source_id]["search"]
    assert after_failure == committed
    assert mongo_store.batch(failed_id)["source_runs"][0]["state"] == "failed"


def test_partial_provider_warning_is_visible_without_discarding_valid_items(
    mongo_store,
) -> None:
    connector = WarningConnector()
    manager = RunManager({connector.source_id: connector}, EventBus(), mongo_store)
    keyword_id = add_keyword(mongo_store, connector.source_id)

    batch_id = asyncio.run(finish_batch(manager, keyword_id))
    batch = mongo_store.batch(batch_id)
    source_run = batch["source_runs"][0]

    assert batch["state"] == "succeeded"
    assert source_run["state"] == "succeeded"
    assert source_run["phase"] == "completed_with_warnings"
    assert source_run["ingested_count"] == 1
    assert "1 warning" in source_run["message"]


def test_query_change_invalidates_provider_cursor(mongo_store) -> None:
    connector = CheckpointConnector()
    manager = RunManager({connector.source_id: connector}, EventBus(), mongo_store)
    keyword_id = add_keyword(mongo_store, connector.source_id)
    asyncio.run(finish_batch(manager, keyword_id))

    mongo_store.update_keyword(
        keyword_id,
        {"include_terms": ["new alias"], "updated_at": datetime.now(UTC)},
    )
    asyncio.run(finish_batch(manager, keyword_id))
    assert connector.received == [None, None]


def test_v2_topic_plans_global_and_channel_as_independent_targets(mongo_store, monkeypatch) -> None:
    connector = CheckpointConnector()
    manager = RunManager({connector.source_id: connector}, EventBus(), mongo_store)
    keyword_id = add_keyword(
        mongo_store,
        connector.source_id,
        channels=[
            {
                "id": "saved-channel",
                "url": "https://example.test/channel",
                "normalized_url": "https://example.test/channel",
                "label": "Saved channel",
                "source_id": connector.source_id,
                "mode": "public",
                "enabled": True,
                "checkpoint": {},
            }
        ],
    )

    async def fake_channel_scan(channel, query):
        yield RawContentItem(
            external_id="channel-item",
            canonical_url="https://example.test/channel/item",
            title="Checkpoint game channel item",
        )

    monkeypatch.setattr("app.services.runs.scan_channel", fake_channel_scan)
    batch_id = asyncio.run(finish_batch(manager, keyword_id))
    source_runs = mongo_store.batch(batch_id)["source_runs"]
    assert [(row["source_id"], row.get("channel_id")) for row in source_runs] == [
        (connector.source_id, None),
        (connector.source_id, "saved-channel"),
    ]


def test_tieba_second_run_updates_snapshot_without_counting_duplicate(
    mongo_store, monkeypatch
) -> None:
    connector = RepeatingTiebaConnector()
    manager = RunManager({connector.source_id: connector}, EventBus(), mongo_store)
    keyword_id = add_keyword(mongo_store, connector.source_id)
    monkeypatch.setattr(manager, "_browser_semaphore", asyncio.Semaphore(1))

    first_id = asyncio.run(finish_batch(manager, keyword_id))
    # Reset the provider watermark through a legitimate query-fingerprint change.
    # The same canonical item is observed again and should refresh metrics without
    # becoming a second content item or a new match.
    mongo_store.update_keyword(
        keyword_id,
        {"include_terms": ["another game alias"], "updated_at": datetime.now(UTC)},
    )
    second_id = asyncio.run(finish_batch(manager, keyword_id))

    first = mongo_store.batch(first_id)
    second = mongo_store.batch(second_id)
    item = mongo_store.item_by_source("tieba", "1234567890")
    assert item is not None
    assert mongo_store.db.content_items.count_documents({"source_id": "tieba"}) == 1
    assert item["metrics"] == {"comment_count": 2}
    assert len(mongo_store.snapshots(item["id"])) == 2
    assert first["new_item_count"] == 1
    assert first["source_runs"][0]["ingested_count"] == 1
    assert second["new_item_count"] == 0
    assert second["source_runs"][0]["ingested_count"] == 0
