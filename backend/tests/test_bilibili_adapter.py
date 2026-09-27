from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.crawlers.adapters.bilibili import (
    BilibiliDetailAdapter,
    BilibiliSearchAdapter,
    BilibiliVideo,
    BilibiliVideoPage,
)
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    CrawlerSupervisor,
    IdentityPseudonymizer,
    RunBudgets,
    RunContext,
)


class FakeBilibiliProvider:
    def __init__(self, videos: tuple[BilibiliVideo, ...]) -> None:
        self.videos = videos
        self.opened = False
        self.closed = False
        self.calls = []

    async def open(self, context, cancellation) -> None:
        self.opened = True

    async def search_page(self, terms, cursor, limit, cancellation):
        self.calls.append((terms, cursor, limit))
        return BilibiliVideoPage(self.videos[:limit], None, False)

    async def close(self) -> None:
        self.closed = True


class FakeBilibiliDetailProvider:
    def __init__(self, result: BilibiliVideo) -> None:
        self.result = result
        self.opened = False
        self.closed = False
        self.target = None

    async def open(self, context, cancellation) -> None:
        self.opened = True

    async def fetch_detail(self, target, cancellation):
        self.target = target
        return self.result

    async def close(self) -> None:
        self.closed = True


def run_context(max_items: int = 1) -> RunContext:
    return RunContext(
        run_id="run-bili",
        keyword_id=1,
        source_id="bilibili",
        provider_id="cbce_bilibili",
        operation="search",
        target={"kind": "keyword"},
        terms=("game",),
        filters={},
        budgets=RunBudgets(max_items=max_items, max_requests=2, deadline_seconds=10),
    )


def video(**overrides) -> BilibiliVideo:
    values = {
        "video_id": "BV1ab411c7De",
        "canonical_url": "https://m.bilibili.com/video/BV1ab411c7De?share_source=test",
        "title": " Public game video ",
        "description": " description ",
        "creator_id": "raw-uid-123",
        "published_at": datetime(2026, 8, 1, tzinfo=UTC),
        "metrics": {
            "view_count": 10,
            "like_count": 2,
            "unknown_metric": 99,
            "comment_count": -1,
        },
        "media": (
            {"kind": "cover", "url": "https://i.example.test/cover.jpg"},
            {"kind": "video_metadata", "duration_seconds": 90},
            {"kind": "video", "url": "javascript:blocked"},
        ),
    }
    values.update(overrides)
    return BilibiliVideo(**values)


def test_bilibili_adapter_runs_through_supervisor_with_exact_limit_and_privacy() -> (
    None
):
    async def run():
        provider = FakeBilibiliProvider(
            (
                video(),
                video(
                    video_id="av123",
                    canonical_url="https://www.bilibili.com/video/av123",
                    title="second",
                ),
            )
        )
        adapter = BilibiliSearchAdapter(provider, IdentityPseudonymizer(b"k" * 32))
        persisted = []
        cursors = []
        result = await CrawlerSupervisor().run_search(
            run_context(max_items=1),
            adapter,
            persist=lambda record: _append(persisted, record),
            commit_cursor=lambda cursor: _append(cursors, cursor),
            page_size=20,
        )
        return provider, persisted, cursors, result

    provider, persisted, cursors, result = asyncio.run(run())
    assert provider.calls == [(("game",), None, 1)]
    assert provider.closed is True
    assert result.persisted_count == 1
    assert cursors == [None]
    record = persisted[0]
    assert record.external_id == "BV1ab411c7De"
    assert record.canonical_url == "https://www.bilibili.com/video/BV1ab411c7De"
    assert record.metrics == {"view_count": 10, "like_count": 2}
    assert record.media == (
        {"kind": "cover", "url": "https://i.example.test/cover.jpg"},
        {"kind": "video_metadata", "duration_seconds": 90},
    )
    assert record.author_pseudonym.startswith("bilibili_")
    assert "raw-uid-123" not in repr(record)
    assert record.provenance["contract_version"] == "cbce.bilibili.video.v1"


@pytest.mark.parametrize(
    "invalid",
    [
        video(canonical_url="https://evil.test/video/BV1ab411c7De"),
        video(video_id="av999"),
        video(title="   "),
    ],
)
def test_bilibili_adapter_rejects_invalid_provider_contract(invalid) -> None:
    async def run():
        adapter = BilibiliSearchAdapter(
            FakeBilibiliProvider((invalid,)), IdentityPseudonymizer(b"k" * 32)
        )
        with pytest.raises(CrawlerFailure) as captured:
            await CrawlerSupervisor().run_search(
                run_context(),
                adapter,
                persist=lambda record: _append([], record),
                commit_cursor=lambda cursor: _append([], cursor),
            )
        return captured.value

    assert asyncio.run(run()).code is CrawlerErrorCode.PARSE_CHANGED


def test_pseudonymizer_is_stable_source_scoped_and_requires_strong_key() -> None:
    pseudonymizer = IdentityPseudonymizer(b"p" * 32)
    assert pseudonymizer.pseudonym("bilibili", "123") == pseudonymizer.pseudonym(
        "bilibili", "123"
    )
    assert pseudonymizer.pseudonym("bilibili", "123") != pseudonymizer.pseudonym(
        "youtube", "123"
    )
    with pytest.raises(ValueError, match="32 bytes"):
        IdentityPseudonymizer(b"weak")


def test_bilibili_detail_adapter_canonicalizes_target_and_closes() -> None:
    provider = FakeBilibiliDetailProvider(video(creator_id=""))
    context = RunContext(
        run_id="detail-run",
        keyword_id=1,
        source_id="bilibili",
        provider_id="cbce_bilibili",
        operation="fetch_detail",
        target={
            "kind": "content_url",
            "url": "https://m.bilibili.com/video/BV1ab411c7De?p=2",
        },
        terms=(),
        filters={},
        budgets=RunBudgets(max_items=1, max_requests=1, deadline_seconds=10),
    )

    async def run():
        adapter = BilibiliDetailAdapter(provider, IdentityPseudonymizer(b"k" * 32))
        cancellation = CancellationToken()
        await adapter.open(context, cancellation)
        try:
            return await adapter.fetch()
        finally:
            await adapter.close()

    record = asyncio.run(run())
    assert provider.target == "https://www.bilibili.com/video/BV1ab411c7De?p=2"
    assert provider.closed is True
    assert record.author_pseudonym == ""
    assert record.external_id == "BV1ab411c7De"


async def _append(target, value) -> None:
    target.append(value)
