from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.crawlers.adapters.x import (
    XCommentsAdapter,
    XCreatorAdapter,
    XDetailAdapter,
    XPost,
    XPostPage,
    XTargetCursor,
    XTargetKind,
    parse_x_target,
)
from app.crawlers.runtime import (
    CancellationToken,
    IdentityPseudonymizer,
    RunBudgets,
    RunContext,
)


@pytest.mark.parametrize(
    ("value", "kind", "identity", "canonical"),
    [
        (
            "https://twitter.com/example/status/9005?s=20",
            XTargetKind.POST,
            "9005",
            "https://x.com/i/web/status/9005",
        ),
        (
            "https://x.com/i/web/status/9005/photo/1",
            XTargetKind.POST,
            "9005",
            "https://x.com/i/web/status/9005",
        ),
        (
            "https://x.com/Game_Dev",
            XTargetKind.CREATOR,
            "Game_Dev",
            "https://x.com/Game_Dev",
        ),
    ],
)
def test_parse_x_target(value, kind, identity, canonical) -> None:
    target = parse_x_target(value)
    assert (target.kind, target.external_id, target.canonical_url) == (
        kind,
        identity,
        canonical,
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://evilx.com/user/status/9005",
        "https://x.com@evil.test/user/status/9005",
        "https://x.com:8443/user/status/9005",
        "https://x.com/user/likes",
    ],
)
def test_parse_x_target_rejects_hostile_or_unsupported_values(value) -> None:
    with pytest.raises(ValueError):
        parse_x_target(value)


def context(operation: str, target: dict[str, str]) -> RunContext:
    return RunContext(
        run_id=f"x-{operation}",
        keyword_id=1,
        source_id="x",
        provider_id="x_api",
        operation=operation,
        target=target,
        terms=(),
        filters={},
        budgets=RunBudgets(
            max_items=2,
            max_requests=2,
            deadline_seconds=30,
            max_root_comments=2,
            max_total_comments=2,
        ),
    )


def post(post_id: str, *, parent_id: str | None = None) -> XPost:
    return XPost(
        post_id,
        f"post-{post_id}",
        author_id="raw-user",
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        conversation_id="9000",
        parent_id=parent_id,
        metrics={"like_count": 2, "reply_count": 1},
    )


class FakeProvider:
    def __init__(self) -> None:
        self.closed = False
        self.calls = []

    async def open(self, context, cancellation):
        return None

    async def close(self):
        self.closed = True

    async def fetch_post(self, post_id):
        self.calls.append(("detail", post_id))
        return post(post_id)

    async def creator_posts(self, username, **kwargs):
        self.calls.append(("creator", username, kwargs))
        return XPostPage((post("9005"), post("9004"), post("9003")), "next")

    async def conversation_replies(self, post_id, **kwargs):
        self.calls.append(("comments", post_id, kwargs))
        return XPostPage((post("9005", parent_id="9001"),), None)


def test_x_detail_adapter_fetches_canonical_post_and_closes() -> None:
    provider = FakeProvider()
    run_context = context(
        "fetch_detail", {"url": "https://twitter.com/user/status/9005"}
    )

    async def run():
        adapter = XDetailAdapter(provider, IdentityPseudonymizer(b"k" * 32))
        await adapter.open(run_context, CancellationToken())
        try:
            return await adapter.fetch()
        finally:
            await adapter.close()

    record = asyncio.run(run())
    assert record.external_id == "9005"
    assert provider.calls == [("detail", "9005")]
    assert provider.closed is True


def test_x_creator_adapter_uses_until_id_for_exact_partial_page() -> None:
    provider = FakeProvider()
    run_context = context("list_creator", {"url": "https://x.com/Game_Dev"})

    async def run():
        adapter = XCreatorAdapter(provider, IdentityPseudonymizer(b"k" * 32))
        await adapter.open(run_context, CancellationToken())
        try:
            return await adapter.fetch_page(run_context, None, 2)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    assert [item.external_id for item in page.items] == ["9005", "9004"]
    assert XTargetCursor.decode(page.next_cursor).until_id == "9003"
    assert provider.calls[0][1] == "Game_Dev"


def test_x_comments_adapter_preserves_reply_parent_and_root() -> None:
    provider = FakeProvider()
    run_context = context(
        "list_comments", {"url": "https://x.com/user/status/9000"}
    )

    async def run():
        adapter = XCommentsAdapter(provider, IdentityPseudonymizer(b"k" * 32))
        await adapter.open(run_context, CancellationToken())
        try:
            return await adapter.fetch_page(run_context, None, 2)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    reply = page.items[0]
    assert reply.content_external_id == "9000"
    assert reply.parent_external_id == "9001"
    assert reply.root_external_id == "9000"
    assert reply.provenance["coverage"] == "recent_conversation_replies"
    assert "raw-user" not in repr(reply)
