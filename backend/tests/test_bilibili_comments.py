import asyncio
from datetime import UTC, datetime

import pytest

from app.crawlers.adapters.bilibili import (
    BilibiliChildCommentCursor,
    BilibiliChildCommentsAdapter,
    BilibiliComment,
    BilibiliCommentCursor,
    BilibiliCommentPage,
    BilibiliCommentsAdapter,
    BilibiliDomCommentsProvider,
    parse_bilibili_comment,
)
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    RunBudgets,
    RunContext,
)


def context() -> RunContext:
    return RunContext(
        run_id="comments-run",
        keyword_id=0,
        source_id="bilibili",
        provider_id="cbce_bilibili",
        operation="list_comments",
        target={
            "kind": "content_url",
            "url": "https://www.bilibili.com/video/BV1ab411c7De",
        },
        terms=(),
        filters={},
        budgets=RunBudgets(
            max_items=10,
            max_requests=2,
            deadline_seconds=30,
            max_root_comments=10,
            max_children_per_root=3,
            max_total_comments=20,
        ),
    )


def test_comment_cursor_round_trip_and_rejects_other_codec() -> None:
    assert BilibiliCommentCursor.decode("v1m:41").offset == 41
    assert BilibiliCommentCursor.decode(None).offset == 0
    with pytest.raises(CrawlerFailure) as captured:
        BilibiliCommentCursor.decode("v1:41")
    assert captured.value.code is CrawlerErrorCode.PARSE_CHANGED


def test_child_comment_cursor_is_bound_to_root_and_page_offset() -> None:
    cursor = BilibiliChildCommentCursor.decode(
        "v1s:8001:3:7", root_id="8001"
    )
    assert (cursor.root_id, cursor.page_number, cursor.offset) == ("8001", 3, 7)
    assert cursor.encode() == "v1s:8001:3:7"
    with pytest.raises(CrawlerFailure) as captured:
        BilibiliChildCommentCursor.decode("v1s:8002:3:7", root_id="8001")
    assert captured.value.code is CrawlerErrorCode.PARSE_CHANGED


def test_parse_bilibili_comment_preserves_hierarchy_and_bounds_counts() -> None:
    comment = parse_bilibili_comment(
        {
            "rpid_str": "9001",
            "root_str": "8001",
            "parent_str": "8002",
            "mid_str": "raw-user-id",
            "ctime": 1_786_576_200,
            "like": 12,
            "count": 3,
            "message": " public comment ",
        },
        "BV1ab411c7De",
    )

    assert comment.comment_id == "9001"
    assert comment.content_id == "BV1ab411c7De"
    assert comment.body == "public comment"
    assert comment.root_id == "8001"
    assert comment.parent_id == "8002"
    assert comment.created_at == datetime.fromtimestamp(1_786_576_200, tz=UTC)
    assert comment.like_count == 12
    assert comment.child_count == 3


class FakeProvider:
    def __init__(self) -> None:
        self.closed = False

    async def open(self, context, cancellation):
        return None

    async def root_comments(self, target, cursor, limit, cancellation):
        assert target == "https://www.bilibili.com/video/BV1ab411c7De"
        return BilibiliCommentPage(
            (
                BilibiliComment(
                    "9001",
                    "BV1ab411c7De",
                    "comment",
                    author_id="raw-user-id",
                    like_count=2,
                    child_count=1,
                ),
            ),
            "v1m:1",
            True,
        )

    async def close(self):
        self.closed = True

    async def child_comments(self, target, root_id, cursor, limit, cancellation):
        assert root_id == "8001"
        return BilibiliCommentPage(
            (
                BilibiliComment(
                    "9002",
                    "BV1ab411c7De",
                    "reply",
                    author_id="raw-child-user-id",
                    parent_id="8001",
                    root_id="8001",
                ),
            ),
            None,
            False,
        )


def test_comments_adapter_hashes_author_and_keeps_platform_comment_id() -> None:
    provider = FakeProvider()

    async def run():
        adapter = BilibiliCommentsAdapter(provider, IdentityPseudonymizer(b"k" * 32))
        await adapter.open(context(), CancellationToken())
        try:
            return await adapter.fetch_page(context(), None, 5)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    record = page.items[0]
    assert record.external_id == "9001"
    assert record.author_pseudonym.startswith("bilibili_")
    assert "raw-user-id" not in repr(record)
    assert record.like_count == 2
    assert record.child_count == 1
    assert record.provenance["coverage"] == "root_comments"
    assert provider.closed is True


def test_child_comments_adapter_preserves_hierarchy_and_separate_coverage() -> None:
    provider = FakeProvider()
    child_context = RunContext(
        run_id="child-comments-run",
        keyword_id=0,
        source_id="bilibili",
        provider_id="cbce_bilibili",
        operation="list_child_comments",
        target={
            "kind": "content_url",
            "url": "https://www.bilibili.com/video/BV1ab411c7De",
            "root_comment_id": "8001",
        },
        terms=(),
        filters={},
        budgets=context().budgets,
    )

    async def run():
        adapter = BilibiliChildCommentsAdapter(
            provider, IdentityPseudonymizer(b"k" * 32)
        )
        await adapter.open(child_context, CancellationToken())
        try:
            return await adapter.fetch_page(child_context, None, 3)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    record = page.items[0]
    assert record.parent_external_id == "8001"
    assert record.root_external_id == "8001"
    assert record.provenance["coverage"] == "child_comments"
    assert "raw-child-user-id" not in repr(record)


class ChildSnapshotProvider(BilibiliDomCommentsProvider):
    def __init__(self, snapshot):
        super().__init__(None)
        self._page = object()
        self._target = "https://www.bilibili.com/video/BV1ab411c7De"
        self.snapshot = snapshot

    async def _load_child_page(self, root_id, page_number, cancellation):
        assert root_id == "8001"
        assert page_number == 1
        return self.snapshot


def test_dom_child_comments_refuses_logged_out_partial_replies() -> None:
    provider = ChildSnapshotProvider(
        {
            "items": [_raw(2)],
            "loaded": 1,
            "declared": 33,
            "current_page": 1,
            "total_pages": 3,
            "show_view_more": True,
            "show_pagination": False,
            "auth_required": True,
        }
    )

    async def run():
        with pytest.raises(CrawlerFailure) as captured:
            await provider.child_comments(
                "https://www.bilibili.com/video/BV1ab411c7De",
                "8001",
                None,
                3,
                CancellationToken(),
            )
        return captured.value

    assert asyncio.run(run()).code is CrawlerErrorCode.AUTH_REQUIRED


def test_dom_child_comments_exact_offset_and_next_page_cursor() -> None:
    snapshot = {
        "items": [_raw(2), _raw(3), _raw(4)],
        "loaded": 3,
        "declared": 20,
        "current_page": 1,
        "total_pages": 2,
        "show_view_more": False,
        "show_pagination": True,
        "auth_required": False,
    }
    for item in snapshot["items"]:
        item["root_str"] = "8001"
        item["parent_str"] = "8001"
    provider = ChildSnapshotProvider(snapshot)

    async def run():
        first = await provider.child_comments(
            "https://www.bilibili.com/video/BV1ab411c7De",
            "8001",
            None,
            2,
            CancellationToken(),
        )
        second = await provider.child_comments(
            "https://www.bilibili.com/video/BV1ab411c7De",
            "8001",
            first.next_cursor,
            2,
            CancellationToken(),
        )
        return first, second

    first, second = asyncio.run(run())
    assert [item.comment_id for item in first.items] == ["2", "3"]
    assert first.next_cursor == "v1s:8001:1:2"
    assert [item.comment_id for item in second.items] == ["4"]
    assert second.next_cursor == "v1s:8001:2:0"


class SnapshotProvider(BilibiliDomCommentsProvider):
    def __init__(self, snapshots):
        super().__init__(None)
        self._page = object()
        self._target = "https://www.bilibili.com/video/BV1ab411c7De"
        self.snapshots = list(snapshots)

    async def _snapshot(self):
        return self.snapshots.pop(0)


class WaitPage:
    async def wait_for_timeout(self, milliseconds):
        return None


class InteractiveSnapshotProvider(SnapshotProvider):
    def __init__(self, snapshots, events):
        BilibiliDomCommentsProvider.__init__(
            self,
            None,
            on_auth_required=lambda: events.append("required"),
            on_authenticated=lambda: events.append("authenticated"),
        )
        self._page = WaitPage()
        self._target = "https://www.bilibili.com/video/BV1ab411c7De"
        self.snapshots = list(snapshots)


def _raw(comment_id):
    return {
        "rpid_str": str(comment_id),
        "root_str": "0",
        "parent_str": "0",
        "mid_str": "123",
        "ctime": 1,
        "like": 0,
        "count": 0,
        "message": f"comment-{comment_id}",
    }


def test_dom_comments_refuses_logged_out_preview_as_complete_feed() -> None:
    provider = SnapshotProvider(
        [
            {
                "items": [_raw(1), _raw(2)],
                "loaded": 2,
                "total": 20_720,
                "end": True,
                "auth_required": True,
            }
        ]
    )

    async def run():
        with pytest.raises(CrawlerFailure) as captured:
            await provider.root_comments(
                "https://www.bilibili.com/video/BV1ab411c7De",
                None,
                2,
                CancellationToken(),
            )
        return captured.value

    assert asyncio.run(run()).code is CrawlerErrorCode.AUTH_REQUIRED


def test_dom_comments_interactive_worker_waits_then_rechecks_full_feed() -> None:
    preview = {
        "items": [_raw(1)],
        "loaded": 1,
        "total": 20,
        "end": True,
        "auth_required": True,
    }
    authenticated = {
        "items": [_raw(1), _raw(2)],
        "loaded": 2,
        "total": 20,
        "end": False,
        "auth_required": False,
    }
    events = []
    provider = InteractiveSnapshotProvider(
        [preview, authenticated, authenticated], events
    )

    async def run():
        return await provider.root_comments(
            "https://www.bilibili.com/video/BV1ab411c7De",
            None,
            2,
            CancellationToken(),
        )

    page = asyncio.run(run())
    assert [item.comment_id for item in page.items] == ["1", "2"]
    assert events == ["required", "authenticated"]


def test_dom_comments_exact_offset_cursor_uses_loaded_component_list() -> None:
    provider = SnapshotProvider(
        [
            {
                "items": [_raw(1), _raw(2), _raw(3), _raw(4)],
                "loaded": 4,
                "total": 10,
                "end": False,
                "auth_required": False,
            },
            {
                "items": [_raw(1), _raw(2), _raw(3), _raw(4)],
                "loaded": 4,
                "total": 10,
                "end": False,
                "auth_required": False,
            },
        ]
    )

    async def run():
        first = await provider.root_comments(
            "https://www.bilibili.com/video/BV1ab411c7De",
            None,
            2,
            CancellationToken(),
        )
        second = await provider.root_comments(
            "https://www.bilibili.com/video/BV1ab411c7De",
            first.next_cursor,
            2,
            CancellationToken(),
        )
        return first, second

    first, second = asyncio.run(run())
    assert [item.comment_id for item in first.items] == ["1", "2"]
    assert first.next_cursor == "v1m:2"
    assert [item.comment_id for item in second.items] == ["3", "4"]
    assert second.next_cursor == "v1m:4"
