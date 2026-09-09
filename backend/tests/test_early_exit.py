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
from tests.test_global_checkpoints import add_keyword, finish_batch


class InfiniteStreamConnector(SourceConnector):
    source_id = "infinite-stream"
    label = "Infinite stream test"
    group = "Test"
    capabilities = ConnectorCapabilities(True)

    def __init__(self) -> None:
        self.batch_run_count = 0
        self.items_yielded_in_run2 = 0

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "ready")

    async def search(self, query: SearchQuery, checkpoint=None):
        self.batch_run_count += 1
        if self.batch_run_count == 1:
            # First run: yield 10 new items
            for i in range(1, 11):
                yield RawContentItem(
                    external_id=f"known-{i}",
                    canonical_url=f"https://example.test/item/{i}",
                    title=f"Game update {i}",
                    published_at=datetime(2026, 8, i, tzinfo=UTC),
                )
        else:
            # Second run: yields the known items first, then 20 more items
            for i in range(1, 11):
                self.items_yielded_in_run2 += 1
                yield RawContentItem(
                    external_id=f"known-{i}",
                    canonical_url=f"https://example.test/item/{i}",
                    title=f"Game update {i}",
                    published_at=datetime(2026, 8, i, tzinfo=UTC),
                )
            for i in range(11, 31):
                self.items_yielded_in_run2 += 1
                yield RawContentItem(
                    external_id=f"new-{i}",
                    canonical_url=f"https://example.test/item/{i}",
                    title=f"Game update {i}",
                    published_at=datetime(2026, 8, i, tzinfo=UTC),
                )


def test_early_exit_stops_after_5_consecutive_known_items(mongo_store) -> None:
    connector = InfiniteStreamConnector()
    manager = RunManager({connector.source_id: connector}, EventBus(), mongo_store)
    keyword_id = add_keyword(mongo_store, connector.source_id)

    # First run establishes checkpoint with 10 known items
    first_id = asyncio.run(finish_batch(manager, keyword_id))
    first_batch = mongo_store.batch(first_id)
    assert first_batch["state"] == "succeeded"

    # Second run should detect the 10 known items and exit after seeing 5 consecutive known items
    second_id = asyncio.run(finish_batch(manager, keyword_id))
    second_batch = mongo_store.batch(second_id)
    source_run = second_batch["source_runs"][0]

    # In second run, connector should have stopped after exactly 5 items!
    assert connector.items_yielded_in_run2 == 5
    assert source_run["phase"] == "completed_with_warnings"
    assert "warning(s)" in source_run["message"]
