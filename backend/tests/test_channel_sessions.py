import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app import main
from app.services.connectors import (
    ConnectorCapabilities,
    ConnectorStatus,
    RawContentItem,
    SearchQuery,
    SourceConnector,
)
from app.services.runs import EventBus, RunManager


class ChannelTestConnector(SourceConnector):
    source_id = "channel-test"
    label = "Channel test"
    group = "Test"
    capabilities = ConnectorCapabilities(True)

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus("ready", "ready")

    async def search(self, query: SearchQuery, checkpoint=None):
        if False:
            yield RawContentItem("", "", "")


def test_topic_saves_and_deduplicates_registered_channel_urls() -> None:
    with TestClient(main.app) as client:
        created = client.post(
            "/api/v1/keywords",
            json={
                "name": "NTE registered channels",
                "channels": [
                    {"url": "https://x.com/NTE_Ani_Info?s=20"},
                    {"url": "https://x.com/NTE_Ani_Info"},
                ],
                "enabled": False,
            },
        )
        assert created.status_code == 201
        topic = created.json()
        assert len(topic["channels"]) == 1
        assert topic["channels"][0]["source_id"] == "x"
        # Saved channels and global-search sources are independent selections.
        assert topic["source_ids"] == []
        assert client.delete(f"/api/v1/keywords/{topic['id']}").status_code == 204


def test_topic_keeps_global_sources_independent_from_saved_channels() -> None:
    with TestClient(main.app) as client:
        created = client.post(
            "/api/v1/keywords",
            json={
                "name": "NTE global and channels",
                "source_ids": ["bluesky"],
                "channels": [{"url": "https://x.com/NTE_Ani_Info"}],
                "enabled": False,
            },
        )
        assert created.status_code == 201
        topic = created.json()
        assert topic["source_ids"] == ["bluesky"]
        assert [channel["source_id"] for channel in topic["channels"]] == ["x"]
        assert client.delete(f"/api/v1/keywords/{topic['id']}").status_code == 204


def test_channel_runs_skip_seen_posts_and_keep_only_five_sessions(
    mongo_store, monkeypatch
) -> None:
    connector = ChannelTestConnector()
    manager = RunManager({connector.source_id: connector}, EventBus(), mongo_store)
    now = datetime.now(UTC)
    keyword_id = mongo_store.create_keyword(
        {
            "name": "Channel session game",
            "normalized_name": "channel session game",
            "include_terms": [],
            "exclude_terms": [],
            "source_ids": [connector.source_id],
            "channels": [
                {
                    "id": "saved-channel",
                    "url": "https://example.test/channel",
                    "normalized_url": "https://example.test/channel",
                    "label": "Saved channel",
                    "source_id": connector.source_id,
                    "mode": "public",
                    "enabled": True,
                    "checkpoint": {},
                },
                {
                    "id": "view-only-x-channel",
                    "url": "https://x.com/game_news",
                    "normalized_url": "https://x.com/game_news",
                    "label": "View-only X channel",
                    "source_id": "x",
                    "mode": "embed_only",
                    "enabled": True,
                    "checkpoint": {},
                },
            ],
            "enabled": False,
            "interval_minutes": 360,
            "max_items_per_source": 10,
            "next_run_at": None,
            "created_at": now,
            "updated_at": now,
        }
    )["id"]
    scan_number = 0

    async def fake_scan(channel, query):
        nonlocal scan_number
        scan_number += 1
        for external_id in ("always-returned", f"session-{scan_number}"):
            yield RawContentItem(
                external_id=external_id,
                canonical_url=f"https://example.test/posts/{external_id}",
                title=f"Channel session game {external_id}",
            )

    monkeypatch.setattr("app.services.runs.scan_channel", fake_scan)

    async def run_six_sessions() -> None:
        for _ in range(6):
            batch_id = await manager.start_batch(keyword_id)
            await manager._tasks[batch_id]

    asyncio.run(run_six_sessions())

    batches = mongo_store.batches(keyword_id, limit=10)
    assert [batch["session_number"] for batch in batches] == [6, 5, 4, 3, 2]
    assert all(len(batch["source_runs"]) == 1 for batch in batches)
    assert mongo_store.db.source_runs.count_documents({}) == 5
    assert mongo_store.db.item_keyword_matches.count_documents(
        {"keyword_id": keyword_id}
    ) == 5
    assert mongo_store.db.content_items.count_documents({}) == 5
    checkpoint = mongo_store.keyword(keyword_id)["channels"][0]["checkpoint"]
    assert "always-returned" in checkpoint["recent_ids"]
    assert "session-6" in checkpoint["recent_ids"]
