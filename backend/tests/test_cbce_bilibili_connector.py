import asyncio
from datetime import UTC, datetime

import pytest

from app.config import settings
from app.crawlers.runtime import (
    CrawlerErrorCode,
    CrawlerFailure,
    RunBudgets,
    WorkerEnvelope,
    WorkerMessageKind,
    WorkerProcessResult,
)
from app.services.cbce_connectors import CbceBilibiliConnector, CbceTiebaConnector
from app.services.connectors import SearchQuery, default_connectors

pytestmark = pytest.mark.usefixtures("available_browser_dependency")


class FakeTracker:
    def __init__(self) -> None:
        self.reported = []

    def cursor(self, scope, default=None):
        return "v1:0:1:2"

    def report_cursor(self, scope, value):
        self.reported.append((scope, value))


class FakeProcessSupervisor:
    def __init__(self) -> None:
        self.starts = []

    async def execute(self, spec, start, *, on_message, control=None):
        self.starts.append(start)
        identity = {
            "run_id": start.run_id,
            "source_run_id": start.source_run_id,
            "source_id": start.source_id,
            "provider_id": start.provider_id,
            "operation": start.operation,
        }
        await on_message(
            WorkerEnvelope(
                kind=WorkerMessageKind.ITEM,
                sequence=1,
                payload={
                    "external_id": "BV1ab411c7De",
                    "canonical_url": "https://www.bilibili.com/video/BV1ab411c7De",
                    "title": "Synthetic public video",
                    "body": "",
                    "author_pseudonym": "bilibili_abc",
                    "published_at": datetime(2026, 8, 13, tzinfo=UTC).isoformat(),
                    "metrics": {"view_count": 12},
                    "media": [
                        {"kind": "cover", "url": "https://i.example.test/cover.jpg"},
                        {"kind": "video_metadata", "duration_seconds": 42},
                    ],
                },
                **identity,
            )
        )
        await on_message(
            WorkerEnvelope(
                kind=WorkerMessageKind.CHECKPOINT,
                sequence=2,
                payload={"cursor": "v1:0:1:3"},
                **identity,
            )
        )
        await on_message(
            WorkerEnvelope(
                kind=WorkerMessageKind.COMPLETE,
                sequence=3,
                payload={"item_count": 1},
                **identity,
            )
        )
        return WorkerProcessResult(WorkerMessageKind.COMPLETE, 0, 3, "")


class FailingProcessSupervisor:
    async def execute(self, spec, start, *, on_message, control=None):
        raise RuntimeError("synthetic worker startup failure")


class FakeBilibiliCommentSupervisor:
    def __init__(self, *, fail: bool = False) -> None:
        self.starts = []
        self.fail = fail

    async def execute(self, spec, start, *, on_message, control=None):
        self.starts.append(start)
        identity = {
            "run_id": start.run_id,
            "source_run_id": start.source_run_id,
            "source_id": start.source_id,
            "provider_id": start.provider_id,
            "operation": start.operation,
        }
        if self.fail:
            await on_message(
                WorkerEnvelope(
                    kind=WorkerMessageKind.ERROR,
                    sequence=1,
                    payload={
                        "code": "AUTH_REQUIRED",
                        "message": "Complete visible Bilibili login.",
                        "retryable": False,
                    },
                    **identity,
                )
            )
            return WorkerProcessResult(WorkerMessageKind.ERROR, 2, 1, "")
        child = start.operation == "list_child_comments"
        await on_message(
            WorkerEnvelope(
                kind=WorkerMessageKind.ITEM,
                sequence=1,
                payload={
                    "external_id": "9002" if child else "9001",
                    "content_external_id": "BV1ab411c7De",
                    "body": "Synthetic Bilibili comment",
                    "author_pseudonym": "bilibili_anon",
                    "published_at": "2026-08-13T00:00:00+00:00",
                    "like_count": 4,
                    "child_count": 0 if child else 1,
                    "parent_external_id": "9001" if child else None,
                    "root_external_id": "9001" if child else None,
                    "provenance": {
                        "provider_id": "cbce_bilibili",
                        "contract_version": "cbce.bilibili.comment.v1",
                        "coverage": "child_comments" if child else "root_comments",
                    },
                },
                **identity,
            )
        )
        await on_message(
            WorkerEnvelope(
                kind=WorkerMessageKind.CHECKPOINT,
                sequence=2,
                payload={"cursor": None},
                **identity,
            )
        )
        await on_message(
            WorkerEnvelope(
                kind=WorkerMessageKind.COMPLETE,
                sequence=3,
                payload={"item_count": 1, "request_count": 1, "cursor": None},
                **identity,
            )
        )
        return WorkerProcessResult(WorkerMessageKind.COMPLETE, 0, 3, "")


class FakeTiebaProcessSupervisor:
    def __init__(self) -> None:
        self.start = None

    async def execute(self, spec, start, *, on_message, control=None):
        self.start = start
        identity = {
            "run_id": start.run_id,
            "source_run_id": start.source_run_id,
            "source_id": start.source_id,
            "provider_id": start.provider_id,
            "operation": start.operation,
        }
        is_comment = start.payload.get("action") == "tieba_comments"
        await on_message(
            WorkerEnvelope(
                kind=WorkerMessageKind.ITEM,
                sequence=1,
                payload=(
                    {
                        "external_id": "8001",
                        "content_external_id": "1234567890",
                        "body": "Synthetic root comment",
                        "author_pseudonym": "tieba_abc",
                        "published_at": "2026-08-13T00:00:00+00:00",
                        "like_count": 3,
                        "child_count": 2,
                        "parent_external_id": None,
                        "root_external_id": None,
                        "provenance": {
                            "provider_id": "cbce_tieba",
                            "contract_version": "cbce.tieba.comment.v1",
                            "coverage": "rendered_root_comments_only",
                        },
                    }
                    if is_comment
                    else {
                        "external_id": "1234567890",
                        "canonical_url": "https://tieba.baidu.com/p/1234567890",
                        "title": "Synthetic public thread",
                        "body": "",
                        "author_pseudonym": "tieba_abc",
                        "metrics": {"comment_count": 12},
                    }
                ),
                **identity,
            )
        )
        await on_message(
            WorkerEnvelope(
                kind=WorkerMessageKind.CHECKPOINT,
                sequence=2,
                payload={"cursor": None},
                **identity,
            )
        )
        await on_message(
            WorkerEnvelope(
                kind=WorkerMessageKind.COMPLETE,
                sequence=3,
                payload={"item_count": 1, "request_count": 1, "cursor": None},
                **identity,
            )
        )
        return WorkerProcessResult(WorkerMessageKind.COMPLETE, 0, 3, "")


def test_cbce_bilibili_connector_yields_item_before_reporting_checkpoint(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "browser.exe"
    executable.touch()
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings, "content_bot_cbce_browser_executable_path", executable
    )
    monkeypatch.setattr(
        settings, "content_bot_cbce_profile_root", tmp_path / "profiles"
    )
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path / "data")
    tracker = FakeTracker()
    query = SearchQuery(1, "game", [], 3, checkpoint_tracker=tracker)

    async def run():
        connector = CbceBilibiliConnector(FakeProcessSupervisor())
        iterator = connector.search(query)
        first = await anext(iterator)
        assert tracker.reported == []
        remainder = [item async for item in iterator]
        return first, remainder

    first, remainder = asyncio.run(run())
    assert first.external_id == "BV1ab411c7De"
    assert first.author == "bilibili_abc"
    assert first.media == [
        {"kind": "cover", "url": "https://i.example.test/cover.jpg"},
        {"kind": "video_metadata", "duration_seconds": 42},
    ]
    assert remainder == []
    assert tracker.reported[-1][1] == "v1:0:1:3"


def test_cbce_bilibili_connector_routes_custom_account_to_worker(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "browser.exe"
    executable.touch()
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings, "content_bot_cbce_browser_executable_path", executable
    )
    monkeypatch.setattr(
        settings, "content_bot_cbce_profile_root", tmp_path / "profiles"
    )
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path / "data")
    supervisor = FakeProcessSupervisor()

    async def run():
        connector = CbceBilibiliConnector(
            supervisor, account_ref=" Account One "
        )
        return [
            item
            async for item in connector.search(SearchQuery(1, "game", [], 1))
        ]

    items = asyncio.run(run())
    assert items[0].external_id == "BV1ab411c7De"
    assert supervisor.starts[0].payload["account_ref"] == "account one"


def test_cbce_bilibili_connector_exposes_creator_worker_operation(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "browser.exe"
    executable.touch()
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings, "content_bot_cbce_browser_executable_path", executable
    )
    monkeypatch.setattr(
        settings, "content_bot_cbce_profile_root", tmp_path / "profiles"
    )
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path / "data")
    supervisor = FakeProcessSupervisor()

    async def run():
        connector = CbceBilibiliConnector(supervisor)
        return [
            item
            async for item in connector.list_creator(
                "https://space.bilibili.com/2", max_items=2
            )
        ]

    items = asyncio.run(run())
    assert items[0].external_id == "BV1ab411c7De"
    assert supervisor.starts[0].operation == "list_creator"
    assert supervisor.starts[0].payload["action"] == "bilibili_creator"


def test_cbce_bilibili_connector_exposes_detail_worker_operation(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "browser.exe"
    executable.touch()
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings, "content_bot_cbce_browser_executable_path", executable
    )
    monkeypatch.setattr(
        settings, "content_bot_cbce_profile_root", tmp_path / "profiles"
    )
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path / "data")
    supervisor = FakeProcessSupervisor()

    async def run():
        connector = CbceBilibiliConnector(supervisor, account_ref="detail-profile")
        return await connector.fetch_detail(
            "https://www.bilibili.com/video/BV1ab411c7De?p=2"
        )

    item = asyncio.run(run())
    assert item.external_id == "BV1ab411c7De"
    assert supervisor.starts[0].operation == "fetch_detail"
    assert supervisor.starts[0].payload["action"] == "bilibili_detail"
    assert supervisor.starts[0].payload["account_ref"] == "detail-profile"


def test_cbce_bilibili_connector_does_not_deadlock_on_early_worker_failure(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "browser.exe"
    executable.touch()
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings, "content_bot_cbce_browser_executable_path", executable
    )
    monkeypatch.setattr(
        settings, "content_bot_cbce_profile_root", tmp_path / "profiles"
    )
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path / "data")

    async def run():
        connector = CbceBilibiliConnector(FailingProcessSupervisor())
        with pytest.raises(RuntimeError, match="startup failure"):
            await asyncio.wait_for(
                anext(connector.search(SearchQuery(1, "game", [], 1))),
                timeout=0.5,
            )

    asyncio.run(run())


def test_cbce_tieba_connector_uses_isolated_partial_dom_worker(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "browser.exe"
    executable.touch()
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings, "content_bot_cbce_browser_executable_path", executable
    )
    monkeypatch.setattr(
        settings, "content_bot_cbce_profile_root", tmp_path / "profiles"
    )
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path / "data")
    supervisor = FakeTiebaProcessSupervisor()

    async def run():
        connector = CbceTiebaConnector(supervisor)
        return [item async for item in connector.search(SearchQuery(1, "game", [], 4))]

    items = asyncio.run(run())
    assert items[0].external_id == "1234567890"
    assert items[0].metrics == {"comment_count": 12}
    assert items[0].raw_payload == {
        "provider_id": "cbce_tieba",
        "contract_version": "cbce.tieba.content.v1",
    }
    assert supervisor.start.provider_id == "cbce_tieba"
    assert supervisor.start.payload["action"] == "tieba_search"


def test_cbce_tieba_connector_exposes_target_worker_operations(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "browser.exe"
    executable.touch()
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings, "content_bot_cbce_browser_executable_path", executable
    )
    monkeypatch.setattr(
        settings, "content_bot_cbce_profile_root", tmp_path / "profiles"
    )
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path / "data")
    supervisor = FakeTiebaProcessSupervisor()
    connector = CbceTiebaConnector(supervisor)

    async def run():
        detail = await connector.fetch_detail(
            "https://tieba.baidu.com/p/1234567890"
        )
        creator = [
            item
            async for item in connector.list_creator(
                "https://tieba.baidu.com/home/main?id=public-author",
                max_items=4,
            )
        ]
        channel = [
            item
            async for item in connector.scan_channel(
                {
                    "url": "https://tieba.baidu.com/f?kw=game",
                    "normalized_url": "https://tieba.baidu.com/f?kw=game",
                },
                SearchQuery(1, "game", [], 4),
            )
        ]
        comments = [
            item
            async for item in connector.list_comments(
                "https://tieba.baidu.com/p/1234567890",
                max_items=4,
            )
        ]
        return detail, creator, channel, comments

    detail, creator, channel, comments = asyncio.run(run())
    assert detail.external_id == "1234567890"
    assert creator[0].external_id == "1234567890"
    assert channel[0].external_id == "1234567890"
    assert comments[0].external_id == "8001"
    assert comments[0].content_external_id == "1234567890"
    assert comments[0].root_external_id == "8001"
    assert supervisor.start.payload["action"] == "tieba_comments"


def test_default_registry_selects_tieba_cbce_only_with_explicit_override(
    monkeypatch
) -> None:
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings,
        "content_bot_cbce_provider_overrides",
        '{"tieba":{"search":"cbce_tieba"}}',
    )
    assert isinstance(default_connectors()["tieba"], CbceTiebaConnector)

    monkeypatch.setattr(settings, "content_bot_cbce_enabled", False)
    assert not isinstance(default_connectors()["tieba"], CbceTiebaConnector)


def test_default_registry_selects_bilibili_cbce_only_with_explicit_override(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings,
        "content_bot_cbce_provider_overrides",
        '{"bilibili":{"search":"cbce_bilibili"}}',
    )
    assert isinstance(default_connectors()["bilibili"], CbceBilibiliConnector)
    assert callable(default_connectors()["bilibili"].list_comments)

    monkeypatch.setattr(settings, "content_bot_cbce_enabled", False)
    assert not isinstance(default_connectors()["bilibili"], CbceBilibiliConnector)


def test_bilibili_comment_connector_prepares_root_and_child_persistence_records(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "browser.exe"
    executable.touch()
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings, "content_bot_cbce_browser_executable_path", executable
    )
    monkeypatch.setattr(
        settings, "content_bot_cbce_profile_root", tmp_path / "profiles"
    )
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path / "data")
    supervisor = FakeBilibiliCommentSupervisor()
    connector = CbceBilibiliConnector(supervisor)
    budgets = RunBudgets(
        max_items=5,
        max_requests=5,
        deadline_seconds=60,
        max_root_comments=5,
        max_children_per_root=5,
        max_total_comments=10,
    )

    async def run():
        root = await connector.scan_comments(
            "https://www.bilibili.com/video/BV1ab411c7De", budgets
        )
        child = await connector.scan_child_comments(
            "https://www.bilibili.com/video/BV1ab411c7De",
            "9001",
            budgets,
        )
        return root, child

    root, child = asyncio.run(run())
    assert root.records[0].external_id == "9001"
    assert root.records[0].root_external_id == "9001"
    assert root.provider_id == "cbce_bilibili"
    assert root.truncated is True  # declared child remains a separate operation
    assert child.records[0].parent_external_id == "9001"
    assert child.records[0].root_external_id == "9001"
    assert supervisor.starts[1].payload["root_comment_id"] == "9001"


def test_browser_comment_connector_propagates_typed_worker_error(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "browser.exe"
    executable.touch()
    monkeypatch.setattr(settings, "content_bot_cbce_enabled", True)
    monkeypatch.setattr(
        settings, "content_bot_cbce_browser_executable_path", executable
    )
    monkeypatch.setattr(
        settings, "content_bot_cbce_profile_root", tmp_path / "profiles"
    )
    connector = CbceBilibiliConnector(
        FakeBilibiliCommentSupervisor(fail=True)
    )

    with pytest.raises(CrawlerFailure) as caught:
        asyncio.run(
            connector.scan_comments(
                "https://www.bilibili.com/video/BV1ab411c7De",
                RunBudgets(
                    max_items=1,
                    max_requests=1,
                    deadline_seconds=30,
                    max_root_comments=1,
                    max_total_comments=1,
                ),
            )
        )
    assert caught.value.code is CrawlerErrorCode.AUTH_REQUIRED
    assert "Bilibili login" in caught.value.safe_message
