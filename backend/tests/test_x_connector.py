import asyncio
from datetime import UTC, datetime

import pytest

from app.crawlers.adapters.x import XPost, XPostPage
from app.crawlers.runtime import IdentityPseudonymizer, RunBudgets
from app.services import channel_scans
from app.services.channel_scans import (
    ChannelUnavailable,
    channel_mode,
    normalize_channel,
    scan_channel,
)
from app.services.connectors import (
    RawContentItem,
    SearchQuery,
    XConnector,
    default_connectors,
)
from app.services.runs import EventBus, RunManager


def test_x_channel_falls_back_to_embed_without_api_token(monkeypatch) -> None:
    monkeypatch.setattr("app.services.connectors.settings.x_bearer_token", None)
    channel = normalize_channel({"url": "https://x.com/game_news?s=20"})

    assert channel_mode("x") == "embed_only"
    assert channel["mode"] == "embed_only"


def test_x_channel_scan_never_opens_an_api_client_without_token(monkeypatch) -> None:
    monkeypatch.setattr("app.services.connectors.settings.x_bearer_token", None)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("X scan must not create an API client")

    monkeypatch.setattr(channel_scans.httpx, "AsyncClient", fail_if_called)
    channel = normalize_channel({"url": "https://x.com/game_news"})

    async def collect() -> None:
        async for _item in scan_channel(
            channel,
            SearchQuery(keyword_id=1, name="Game news", include_terms=[], max_items=5),
        ):
            pass

    with pytest.raises(ChannelUnavailable, match="X API User Timeline"):
        asyncio.run(collect())


def test_x_channel_switches_to_api_when_token_is_configured(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.connectors.settings.x_bearer_token", "configured-token"
    )
    channel = normalize_channel({"url": "https://twitter.com/Game_News?s=20"})

    assert channel_mode("x") == "api"
    assert channel["mode"] == "api"
    assert channel["normalized_url"] == "https://x.com/Game_News"
    with pytest.raises(ValueError, match="content targets"):
        normalize_channel({"url": "https://x.com/Game_News/status/9005"})


class FakeCreatorProvider:
    def __init__(self) -> None:
        self.calls = []
        self.closed = False

    async def open(self, context, cancellation):
        self.calls.append(("open", context.operation, dict(context.filters)))

    async def creator_posts(self, username, **kwargs):
        self.calls.append(("creator", username, kwargs))
        return XPostPage(
            (
                XPost(
                    "9005",
                    "Creator update",
                    author_id="raw-author-id",
                    created_at=datetime(2026, 8, 13, tzinfo=UTC),
                    metrics={"like_count": 5},
                ),
            ),
            None,
        )

    async def close(self):
        self.closed = True


def test_x_connector_scans_saved_creator_with_channel_filters(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.connectors.settings.x_bearer_token", "configured-token"
    )
    connector = XConnector()
    provider = FakeCreatorProvider()
    monkeypatch.setattr(connector, "_provider", lambda _token: provider)
    monkeypatch.setattr(
        connector,
        "_pseudonymizer",
        lambda: IdentityPseudonymizer(b"k" * 32),
    )

    async def collect():
        return [
            item
            async for item in connector.scan_channel(
                {
                    "url": "https://x.com/Game_News",
                    "normalized_url": "https://x.com/Game_News",
                    "include_replies": False,
                    "include_reposts": True,
                },
                SearchQuery(
                    keyword_id=1,
                    name="Game news",
                    include_terms=[],
                    max_items=5,
                ),
            )
        ]

    items = asyncio.run(collect())
    assert [item.external_id for item in items] == ["9005"]
    assert items[0].author.startswith("x_")
    assert "raw-author-id" not in repr(items[0])
    assert provider.calls[1] == (
        "creator",
        "Game_News",
        {
            "max_results": 5,
            "pagination_token": None,
            "until_id": None,
            "exclude_replies": True,
            "exclude_reposts": False,
        },
    )
    assert provider.closed is True


def _saved_x_keyword(mongo_store, *, mode: str) -> int:
    now = datetime.now(UTC)
    return mongo_store.create_keyword(
        {
            "name": "Game news",
            "normalized_name": "game news",
            "include_terms": [],
            "exclude_terms": [],
            "source_ids": [],
            "source_selection_version": 2,
            "channels": [
                {
                    "id": "x-game-news",
                    "url": "https://x.com/Game_News",
                    "normalized_url": "https://x.com/Game_News",
                    "label": "Game News",
                    "source_id": "x",
                    "mode": mode,
                    "enabled": True,
                    "include_replies": False,
                    "include_reposts": False,
                    "checkpoint": {},
                }
            ],
            "enabled": False,
            "interval_minutes": 360,
            "max_items_per_source": 5,
            "next_run_at": None,
            "created_at": now,
            "updated_at": now,
        }
    )["id"]


def test_x_channel_run_is_not_queued_without_api_access(
    mongo_store, monkeypatch
) -> None:
    monkeypatch.setattr("app.services.connectors.settings.x_bearer_token", None)
    manager = RunManager({"x": XConnector()}, EventBus(), mongo_store)
    keyword_id = _saved_x_keyword(mongo_store, mode="embed_only")

    with pytest.raises(ValueError, match="No scannable channels"):
        asyncio.run(manager.start_batch(keyword_id))


def test_x_channel_run_uses_x_api_checkpoint_provenance(
    mongo_store, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.services.connectors.settings.x_bearer_token", "configured-token"
    )
    manager = RunManager({"x": XConnector()}, EventBus(), mongo_store)
    keyword_id = _saved_x_keyword(mongo_store, mode="api")

    async def fake_scan(channel, query):
        assert channel["normalized_url"] == "https://x.com/Game_News"
        yield RawContentItem(
            external_id="9005",
            canonical_url="https://x.com/i/web/status/9005",
            title="Game news update",
        )

    monkeypatch.setattr("app.services.runs.scan_channel", fake_scan)

    async def run():
        batch_id = await manager.start_batch(keyword_id)
        await manager._tasks[batch_id]
        return mongo_store.batch(batch_id)

    batch = asyncio.run(run())
    source_run = batch["source_runs"][0]
    assert source_run["state"] == "succeeded"
    assert source_run["checkpoint"]["provider"] == "x_api"
    assert source_run["checkpoint"]["operation"] == "scan_channel"


def test_x_official_connector_is_registered_but_requires_configuration(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.services.connectors.settings.x_bearer_token", None)
    connector = default_connectors()["x"]

    assert isinstance(connector, XConnector)
    assert connector.configured is False
    assert connector.capabilities.global_search is True
    status = asyncio.run(connector.healthcheck())
    assert status.state == "not_configured"
    assert "X_BEARER_TOKEN" in status.detail


class FakeXOperationsProvider(FakeCreatorProvider):
    async def fetch_post(self, post_id):
        self.calls.append(("detail", post_id))
        return XPost(
            post_id,
            "Detail body",
            author_id="detail-author",
            created_at=datetime(2026, 8, 13, tzinfo=UTC),
        )

    async def conversation_replies(self, post_id, **kwargs):
        self.calls.append(("comments", post_id, kwargs))
        return XPostPage(
            (
                XPost(
                    "9002",
                    "Child",
                    author_id="child-author",
                    conversation_id=post_id,
                    parent_id="9001",
                ),
                XPost(
                    "9001",
                    "Direct reply",
                    author_id="root-author",
                    conversation_id=post_id,
                ),
            ),
            None,
        )


def _configured_x_connector(monkeypatch, provider):
    monkeypatch.setattr(
        "app.services.connectors.settings.x_bearer_token", "configured-token"
    )
    connector = XConnector()
    monkeypatch.setattr(connector, "_provider", lambda _token: provider)
    monkeypatch.setattr(
        connector,
        "_pseudonymizer",
        lambda: IdentityPseudonymizer(b"k" * 32),
    )
    return connector


def test_x_connector_fetch_detail_uses_official_provider(monkeypatch) -> None:
    provider = FakeXOperationsProvider()
    connector = _configured_x_connector(monkeypatch, provider)

    item = asyncio.run(
        connector.fetch_detail("https://twitter.com/player/status/9000")
    )

    assert item.external_id == "9000"
    assert item.canonical_url == "https://x.com/i/web/status/9000"
    assert item.author.startswith("x_")
    assert provider.calls[-1] == ("detail", "9000")
    assert provider.closed is True


def test_x_connector_orders_conversation_parent_before_child(monkeypatch) -> None:
    provider = FakeXOperationsProvider()
    connector = _configured_x_connector(monkeypatch, provider)

    result = asyncio.run(
        connector.scan_comments(
            "https://x.com/player/status/9000",
            RunBudgets(
                max_items=10,
                max_requests=2,
                deadline_seconds=30,
                max_root_comments=10,
                max_children_per_root=10,
                max_total_comments=10,
            ),
        )
    )

    assert [record.external_id for record in result.records] == ["9001", "9002"]
    assert result.records[0].parent_external_id == "9000"
    assert result.records[1].parent_external_id == "9001"
    assert result.root_count == 1
    assert result.child_count == 1
    assert result.truncated is False
    assert provider.closed is True
