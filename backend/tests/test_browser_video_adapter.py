import asyncio

import pytest

from app.crawlers.adapters import (
    BrowserVideo,
    BrowserVideoPage,
    BrowserVideoSearchAdapter,
)
from app.crawlers.adapters.douyin import parse_douyin_target
from app.crawlers.adapters.kuaishou import parse_kuaishou_target
from app.crawlers.adapters.xhs import parse_xhs_target
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    RunBudgets,
    RunContext,
)

CASES = (
    (
        "xhs",
        "cbce_xhs",
        "note",
        parse_xhs_target,
        "64abcdef0123456789abcdef",
        "https://www.xiaohongshu.com/explore/64abcdef0123456789abcdef",
        frozenset({"like_count", "comment_count", "favorite_count"}),
    ),
    (
        "douyin",
        "cbce_douyin",
        "video",
        parse_douyin_target,
        "7123456789012345678",
        "https://www.douyin.com/video/7123456789012345678",
        frozenset({"like_count", "comment_count", "share_count", "view_count"}),
    ),
    (
        "kuaishou",
        "cbce_kuaishou",
        "video",
        parse_kuaishou_target,
        "AbCdEf123",
        "https://www.kuaishou.com/short-video/AbCdEf123",
        frozenset({"like_count", "comment_count", "share_count", "view_count"}),
    ),
)


class FakeProvider:
    def __init__(self, item):
        self.item = item
        self.closed = False

    async def open(self, context, cancellation):
        return None

    async def search_page(self, terms, cursor, limit, cancellation):
        return BrowserVideoPage((self.item,), None, False)

    async def close(self):
        self.closed = True


@pytest.mark.parametrize(
    ("source_id", "provider_id", "kind", "parser", "identity", "url", "metrics"),
    CASES,
)
def test_shared_browser_video_adapter_keeps_platform_identity_and_privacy(
    source_id, provider_id, kind, parser, identity, url, metrics
) -> None:
    item = BrowserVideo(
        identity,
        url,
        " Public content ",
        body="body",
        author_id="raw-user-id",
        metrics={next(iter(metrics)): 2, "unknown": 99},
        media=(
            {"kind": "image", "url": "https://img.example.test/a.jpg"},
            {"kind": "video", "url": "javascript:drop"},
        ),
    )
    provider = FakeProvider(item)
    context = RunContext(
        run_id=f"{source_id}-run",
        keyword_id=1,
        source_id=source_id,
        provider_id=provider_id,
        operation="search",
        target={"kind": "keyword"},
        terms=("game",),
        filters={},
        budgets=RunBudgets(max_items=1, max_requests=1, deadline_seconds=30),
    )

    async def run():
        adapter = BrowserVideoSearchAdapter(
            source_id=source_id,
            provider_id=provider_id,
            provider=provider,
            pseudonymizer=IdentityPseudonymizer(b"k" * 32),
            canonicalize=parser,
            content_kinds=frozenset({kind}),
            metric_ids=metrics,
        )
        await adapter.open(context, CancellationToken())
        try:
            return await adapter.fetch_page(context, None, 1)
        finally:
            await adapter.close()

    record = asyncio.run(run()).items[0]
    assert record.external_id == identity
    assert record.canonical_url == url
    assert set(record.metrics).issubset(metrics)
    assert record.media == (
        {"kind": "image", "url": "https://img.example.test/a.jpg"},
    )
    assert record.author_pseudonym.startswith(f"{source_id}_")
    assert "raw-user-id" not in repr(record)
    assert provider.closed is True


def test_shared_browser_video_adapter_rejects_identity_mismatch() -> None:
    item = BrowserVideo(
        "wrong-id",
        "https://www.douyin.com/video/7123456789012345678",
        "title",
    )
    provider = FakeProvider(item)
    context = RunContext(
        run_id="bad-run",
        keyword_id=1,
        source_id="douyin",
        provider_id="cbce_douyin",
        operation="search",
        target={"kind": "keyword"},
        terms=("game",),
        filters={},
        budgets=RunBudgets(max_items=1, max_requests=1, deadline_seconds=30),
    )

    async def run():
        adapter = BrowserVideoSearchAdapter(
            source_id="douyin",
            provider_id="cbce_douyin",
            provider=provider,
            pseudonymizer=IdentityPseudonymizer(b"k" * 32),
            canonicalize=parse_douyin_target,
            content_kinds=frozenset({"video"}),
            metric_ids=frozenset(),
        )
        await adapter.open(context, CancellationToken())
        try:
            with pytest.raises(CrawlerFailure) as captured:
                await adapter.fetch_page(context, None, 1)
            return captured.value
        finally:
            await adapter.close()

    assert asyncio.run(run()).code is CrawlerErrorCode.PARSE_CHANGED
