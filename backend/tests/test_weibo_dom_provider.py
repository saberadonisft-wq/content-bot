from __future__ import annotations

import asyncio

import pytest

from app.crawlers.adapters.weibo import (
    WeiboDomContract,
    WeiboDomSearchProvider,
    WeiboSearchCursor,
)
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    RunBudgets,
    RunContext,
)


def context() -> RunContext:
    return RunContext(
        run_id="weibo-dom-run",
        keyword_id=1,
        source_id="weibo",
        provider_id="cbce_weibo",
        operation="search",
        target={"kind": "keyword"},
        terms=("game", "indie"),
        filters={},
        budgets=RunBudgets(max_items=5, max_requests=3, deadline_seconds=30),
    )


def contract() -> WeiboDomContract:
    return WeiboDomContract(
        root_selector="main.results",
        card_selector="article.post",
        link_selector="a.permalink",
        text_selector="div.body",
        author_selector="a.author",
        timestamp_selector="time.published",
        next_selector="a.next",
        metric_selectors={
            "like_count": "span.likes",
            "comment_count": "span.comments",
            "share_count": "span.shares",
        },
        image_selector="img.content",
    )


def rows():
    return [
        {
            "href": "https://weibo.com/123456/AbCdEf12?drop=1",
            "text": "First public post",
            "author": "https://weibo.com/u/123456",
            "timestamp": "1786579200",
            "metrics": {
                "like_count": "赞 1.2万",
                "comment_count": "评论 3",
                "share_count": "转发 4",
            },
            "images": [
                "https://wx1.sinaimg.cn/large/a.jpg?token=drop",
                "https://evil.example/a.jpg",
            ],
        },
        {
            "href": "https://m.weibo.cn/detail/ZyXwVu98",
            "text": "Second public post",
            "author": "999999",
            "timestamp": "2026-08-13T04:00:00+00:00",
            "metrics": {"like_count": "2"},
            "images": [],
        },
    ]


class Page:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.contract_payload = None

    async def evaluate(self, script, argument):
        assert "innerText" in script
        self.contract_payload = argument["contract"]
        return self.payload


class OwnedPage:
    def __init__(self, payload) -> None:
        self.page = Page(payload)
        self.opened = False
        self.closed = False
        self.navigations = []

    async def open(self, context, cancellation):
        self.opened = True

    async def navigate(self, url, cancellation, **kwargs):
        self.navigations.append((url, kwargs))
        return self.page

    async def close(self):
        self.closed = True


def test_weibo_dom_provider_extracts_exact_offset_and_safe_fields() -> None:
    owned = OwnedPage({"recognized": True, "cards": rows(), "has_more": True})
    provider = WeiboDomSearchProvider(owned, contract(), auth_timeout_seconds=45)

    async def run():
        await provider.open(context(), CancellationToken())
        try:
            return await provider.search_page(
                context().terms,
                WeiboSearchCursor(0, 1, 0).encode(),
                1,
                CancellationToken(),
            )
        finally:
            await provider.close()

    page = asyncio.run(run())
    assert [item.post_id for item in page.items] == ["AbCdEf12"]
    assert page.items[0].metrics == {
        "like_count": 12_000,
        "comment_count": 3,
        "share_count": 4,
    }
    assert page.items[0].media == (
        {"kind": "image", "url": "https://wx1.sinaimg.cn/large/a.jpg"},
    )
    assert page.items[0].author_id == "123456"
    assert WeiboSearchCursor.decode(page.next_cursor) == WeiboSearchCursor(0, 1, 1)
    assert owned.navigations[0][0] == "https://s.weibo.com/weibo?q=game"
    assert owned.navigations[0][1]["auth_timeout_seconds"] == 45
    assert owned.closed is True


def test_weibo_dom_provider_advances_page_then_term() -> None:
    async def run(payload, cursor):
        owned = OwnedPage(payload)
        provider = WeiboDomSearchProvider(owned, contract())
        await provider.open(context(), CancellationToken())
        try:
            return await provider.search_page(
                context().terms, cursor, 10, CancellationToken()
            )
        finally:
            await provider.close()

    page = asyncio.run(
        run({"recognized": True, "cards": rows(), "has_more": True}, None)
    )
    assert WeiboSearchCursor.decode(page.next_cursor) == WeiboSearchCursor(0, 2, 0)

    last = asyncio.run(
        run(
            {"recognized": True, "cards": rows(), "has_more": False},
            WeiboSearchCursor(0, 2, 0).encode(),
        )
    )
    assert WeiboSearchCursor.decode(last.next_cursor) == WeiboSearchCursor(1, 1, 0)


def test_weibo_dom_provider_does_not_treat_unknown_layout_as_empty() -> None:
    owned = OwnedPage({"recognized": False, "cards": [], "has_more": False})
    provider = WeiboDomSearchProvider(owned, contract())

    async def run():
        await provider.open(context(), CancellationToken())
        try:
            with pytest.raises(CrawlerFailure) as captured:
                await provider.search_page(context().terms, None, 10, CancellationToken())
            return captured.value
        finally:
            await provider.close()

    failure = asyncio.run(run())
    assert failure.code is CrawlerErrorCode.PARSE_CHANGED


def test_weibo_dom_contract_rejects_unknown_metric_or_empty_selector() -> None:
    with pytest.raises(ValueError, match="required"):
        WeiboDomContract("", "article", "a", "div", "", "", "a.next")
    with pytest.raises(ValueError, match="metric"):
        WeiboDomContract(
            "main", "article", "a", "div", "", "", "a.next", {"views": "b"}
        )
