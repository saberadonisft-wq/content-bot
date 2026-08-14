from __future__ import annotations

import asyncio
from urllib.parse import quote

import pytest

from app.crawlers.adapters import (
    BrowserVideoDomContract,
    BrowserVideoDomCursor,
    BrowserVideoDomSearchProvider,
)
from app.crawlers.adapters.douyin import parse_douyin_target
from app.crawlers.adapters.kuaishou import parse_kuaishou_target
from app.crawlers.adapters.xhs import parse_xhs_target
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
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
    ),
    (
        "douyin",
        "cbce_douyin",
        "video",
        parse_douyin_target,
        "7123456789012345678",
        "https://www.douyin.com/video/7123456789012345678",
    ),
    (
        "kuaishou",
        "cbce_kuaishou",
        "video",
        parse_kuaishou_target,
        "AbCdEf123",
        "https://www.kuaishou.com/short-video/AbCdEf123",
    ),
)


def contract() -> BrowserVideoDomContract:
    return BrowserVideoDomContract(
        root_selector="main.results",
        card_selector="article.content",
        link_selector="a.permalink",
        title_selector="h3.title",
        next_selector="button.next",
        body_selector="div.summary",
        author_selector="a.author",
        timestamp_selector="time.published",
        metric_selectors={
            "like_count": "span.likes",
            "comment_count": "span.comments",
            "share_count": "span.shares",
            "view_count": "span.views",
            "favorite_count": "span.favorites",
        },
        image_selector="img.gallery",
        cover_selector="img.cover",
    )


def context(source_id, provider_id) -> RunContext:
    return RunContext(
        run_id=f"{source_id}-dom-run",
        keyword_id=1,
        source_id=source_id,
        provider_id=provider_id,
        operation="search",
        target={"kind": "keyword"},
        terms=("game", "indie"),
        filters={},
        budgets=RunBudgets(max_items=5, max_requests=3, deadline_seconds=30),
    )


class Page:
    def __init__(self, payload) -> None:
        self.payload = payload

    async def evaluate(self, script, argument):
        assert "currentSrc" in script
        assert argument["contract"]["card_selector"] == "article.content"
        return self.payload


class OwnedPage:
    def __init__(self, payload) -> None:
        self.page = Page(payload)
        self.closed = False
        self.navigations = []

    async def open(self, context, cancellation):
        return None

    async def navigate(self, url, cancellation, **kwargs):
        self.navigations.append((url, kwargs))
        return self.page

    async def close(self):
        self.closed = True


def provider(source_id, provider_id, kind, parser, payload):
    owned = OwnedPage(payload)
    instance = BrowserVideoDomSearchProvider(
        source_id=source_id,
        provider_id=provider_id,
        content_kinds=frozenset({kind}),
        browser_page=owned,
        contract=contract(),
        build_search_url=lambda term, page: (
            f"https://search.example.test/{source_id}?q={quote(term)}&page={page}"
        ),
        canonicalize=parser,
        media_hosts=("img.example.test",),
        metric_ids=frozenset({"like_count", "comment_count"}),
        auth_timeout_seconds=45,
    )
    return instance, owned


@pytest.mark.parametrize(
    ("source_id", "provider_id", "kind", "parser", "identity", "url"),
    CASES,
)
def test_browser_video_dom_provider_keeps_platform_identity_and_exact_cursor(
    source_id, provider_id, kind, parser, identity, url
) -> None:
    payload = {
        "recognized": True,
        "has_more": True,
        "cards": [
            {
                "href": url,
                "title": "Public video",
                "body": "description",
                "author": "raw-author-id",
                "timestamp": "2026-08-13T04:00:00+00:00",
                "metrics": {"like_count": "1.2万", "unknown": "99"},
                "media": [
                    "https://img.example.test/a.jpg?token=drop",
                    "https://evil.example/a.jpg",
                ],
                "cover": "https://img.example.test/cover.jpg?signature=drop",
            },
            {
                "href": url,
                "title": "duplicate",
                "metrics": {},
                "media": [],
                "cover": "",
            },
        ],
    }
    instance, owned = provider(source_id, provider_id, kind, parser, payload)

    async def run():
        run_context = context(source_id, provider_id)
        await instance.open(run_context, CancellationToken())
        try:
            return await instance.search_page(
                run_context.terms, None, 1, CancellationToken()
            )
        finally:
            await instance.close()

    page = asyncio.run(run())
    assert page.items[0].external_id == identity
    assert page.items[0].metrics == {"like_count": 12_000}
    assert page.items[0].media == (
        {"kind": "image", "url": "https://img.example.test/a.jpg"},
        {"kind": "video_cover", "url": "https://img.example.test/cover.jpg"},
    )
    assert BrowserVideoDomCursor.decode(source_id, page.next_cursor) == (
        BrowserVideoDomCursor(source_id, 0, 1, 1)
    )
    assert owned.navigations[0][1]["auth_timeout_seconds"] == 45
    assert owned.closed is True


def test_browser_video_dom_provider_advances_page_then_term() -> None:
    source_id, provider_id, kind, parser, _, url = CASES[1]

    async def run(has_more, cursor):
        payload = {
            "recognized": True,
            "has_more": has_more,
            "cards": [
                {
                    "href": url,
                    "title": "Public video",
                    "metrics": {},
                    "media": [],
                    "cover": "",
                }
            ],
        }
        instance, _ = provider(source_id, provider_id, kind, parser, payload)
        run_context = context(source_id, provider_id)
        await instance.open(run_context, CancellationToken())
        try:
            return await instance.search_page(
                run_context.terms, cursor, 10, CancellationToken()
            )
        finally:
            await instance.close()

    next_page = asyncio.run(run(True, None))
    assert BrowserVideoDomCursor.decode(source_id, next_page.next_cursor) == (
        BrowserVideoDomCursor(source_id, 0, 2, 0)
    )
    next_term = asyncio.run(
        run(False, BrowserVideoDomCursor(source_id, 0, 2, 0).encode())
    )
    assert BrowserVideoDomCursor.decode(source_id, next_term.next_cursor) == (
        BrowserVideoDomCursor(source_id, 1, 1, 0)
    )


def test_browser_video_dom_provider_rejects_unknown_layout_and_cross_source_cursor() -> None:
    source_id, provider_id, kind, parser, _, _ = CASES[0]
    instance, _ = provider(
        source_id,
        provider_id,
        kind,
        parser,
        {"recognized": False, "cards": [], "has_more": False},
    )

    async def run():
        run_context = context(source_id, provider_id)
        await instance.open(run_context, CancellationToken())
        try:
            with pytest.raises(CrawlerFailure) as captured:
                await instance.search_page(
                    run_context.terms, None, 10, CancellationToken()
                )
            return captured.value
        finally:
            await instance.close()

    assert asyncio.run(run()).code is CrawlerErrorCode.PARSE_CHANGED
    with pytest.raises(CrawlerFailure):
        BrowserVideoDomCursor.decode(
            "xhs", BrowserVideoDomCursor("douyin").encode()
        )


def test_browser_video_dom_contract_rejects_invalid_metric_contract() -> None:
    with pytest.raises(ValueError, match="metric"):
        BrowserVideoDomContract(
            "main",
            "article",
            "a",
            "h3",
            "button",
            metric_selectors={"Bad Metric": "span"},
        )
