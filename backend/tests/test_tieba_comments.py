from __future__ import annotations

import asyncio

import pytest

from app.crawlers.adapters.tieba import (
    TiebaCommentCursor,
    TiebaCommentsAdapter,
    TiebaDomCommentsProvider,
)
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    RunBudgets,
    RunContext,
)


class Page:
    def __init__(self, payload) -> None:
        self.payload = payload

    async def evaluate(self, script, *args):
        assert "closest('[data-id]')" in script
        return self.payload


class OwnedPage:
    def __init__(self, payload) -> None:
        self.page = Page(payload)
        self.closed = False

    async def open(self, context, cancellation):
        return None

    async def navigate(self, url, cancellation, **kwargs):
        return self.page

    async def close(self):
        self.closed = True


def context() -> RunContext:
    return RunContext(
        run_id="tieba-comments",
        keyword_id=0,
        source_id="tieba",
        provider_id="cbce_tieba",
        operation="list_comments",
        target={
            "kind": "content_url",
            "url": "https://tieba.baidu.com/p/1234567890",
        },
        terms=(),
        filters={},
        budgets=RunBudgets(
            max_items=2,
            max_requests=1,
            deadline_seconds=30,
            max_root_comments=2,
            max_total_comments=2,
        ),
    )


def payload():
    return {
        "recognized": True,
        "items": [
            {
                "id": "8001",
                "body": "First public reply",
                "author": "https://tieba.baidu.com/home/main?id=author-token",
                "like_count": "12",
                "child_count": 2,
            },
            {
                "id": "8002",
                "body": "Second public reply",
                "author": "",
                "like_count": "",
                "child_count": 0,
            },
        ],
    }


def test_tieba_comment_cursor_is_bound_to_thread() -> None:
    cursor = TiebaCommentCursor("1234567890", 2)
    assert TiebaCommentCursor.decode("1234567890", cursor.encode()) == cursor
    with pytest.raises(CrawlerFailure) as captured:
        TiebaCommentCursor.decode("9876543210", cursor.encode())
    assert captured.value.code is CrawlerErrorCode.PARSE_CHANGED


def test_tieba_root_comments_use_stable_data_id_and_partial_provenance() -> None:
    owned = OwnedPage(payload())
    provider = TiebaDomCommentsProvider(owned)

    async def run():
        adapter = TiebaCommentsAdapter(provider, IdentityPseudonymizer(b"k" * 32))
        await adapter.open(context(), CancellationToken())
        try:
            return await adapter.fetch_page(context(), None, 1)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    record = page.items[0]
    assert record.external_id == "8001"
    assert record.content_external_id == "1234567890"
    assert record.like_count == 12
    assert record.child_count == 2
    assert record.author_pseudonym.startswith("tieba_")
    assert record.provenance["coverage"] == "rendered_root_comments_only"
    assert TiebaCommentCursor.decode("1234567890", page.next_cursor).offset == 1
    assert owned.closed is True


def test_tieba_comments_reject_unrecognized_dom() -> None:
    provider = TiebaDomCommentsProvider(
        OwnedPage({"recognized": False, "items": []})
    )

    async def run():
        await provider.open(context(), CancellationToken())
        try:
            with pytest.raises(CrawlerFailure) as captured:
                await provider.root_comments(
                    "https://tieba.baidu.com/p/1234567890",
                    None,
                    10,
                    CancellationToken(),
                )
            return captured.value
        finally:
            await provider.close()

    assert asyncio.run(run()).code is CrawlerErrorCode.PARSE_CHANGED
