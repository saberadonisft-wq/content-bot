import asyncio
from datetime import UTC, datetime

import pytest

from app.crawlers.adapters.weibo import (
    WeiboPost,
    WeiboPostPage,
    WeiboSearchAdapter,
    WeiboSearchCursor,
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
        run_id="weibo-run",
        keyword_id=1,
        source_id="weibo",
        provider_id="cbce_weibo",
        operation="search",
        target={"kind": "keyword"},
        terms=("game",),
        filters={},
        budgets=RunBudgets(max_items=2, max_requests=2, deadline_seconds=30),
    )


class FakeProvider:
    closed = False

    async def open(self, context, cancellation):
        return None

    async def search_page(self, terms, cursor, limit, cancellation):
        return WeiboPostPage(
            (
                WeiboPost(
                    "AbCdEf12",
                    "https://weibo.com/123456/AbCdEf12?from=drop",
                    " Public post\nbody ",
                    author_id="raw-user-id",
                    published_at=datetime(2026, 8, 13, tzinfo=UTC),
                    metrics={"like_count": 2, "unknown": 99},
                    media=(
                        {"kind": "image", "url": "https://img.example.test/a.jpg"},
                    ),
                ),
            ),
            None,
            False,
        )

    async def close(self):
        self.closed = True


def test_weibo_cursor_round_trip() -> None:
    cursor = WeiboSearchCursor(2, 3, 4)
    assert WeiboSearchCursor.decode(cursor.encode()) == cursor
    with pytest.raises(CrawlerFailure) as captured:
        WeiboSearchCursor.decode("invalid")
    assert captured.value.code is CrawlerErrorCode.PARSE_CHANGED


def test_weibo_adapter_normalizes_privacy_metrics_and_media() -> None:
    provider = FakeProvider()

    async def run():
        adapter = WeiboSearchAdapter(provider, IdentityPseudonymizer(b"k" * 32))
        await adapter.open(context(), CancellationToken())
        try:
            return await adapter.fetch_page(context(), None, 2)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    record = page.items[0]
    assert record.external_id == "AbCdEf12"
    assert record.canonical_url == "https://m.weibo.cn/detail/AbCdEf12"
    assert record.title == "Public post"
    assert record.metrics == {"like_count": 2}
    assert record.media[0]["kind"] == "image"
    assert record.author_pseudonym.startswith("weibo_")
    assert "raw-user-id" not in repr(record)
    assert provider.closed is True
