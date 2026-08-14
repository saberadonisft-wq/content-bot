from __future__ import annotations

import asyncio

import pytest

from app.crawlers.adapters.tieba import (
    TIEBA_SEARCH_DOM_CONTRACT,
    TiebaDomContract,
    TiebaDomSearchProvider,
    TiebaSearchCursor,
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
        run_id="tieba-dom-run",
        keyword_id=1,
        source_id="tieba",
        provider_id="cbce_tieba",
        operation="search",
        target={"kind": "keyword"},
        terms=("game", "indie"),
        filters={},
        budgets=RunBudgets(max_items=5, max_requests=3, deadline_seconds=30),
    )


def contract() -> TiebaDomContract:
    return TiebaDomContract(
        root_selector="main.results",
        card_selector="article.thread",
        link_selector="a.permalink",
        title_selector="h3.title",
        next_selector="a.next",
        body_selector="div.summary",
        author_selector="a.author",
        timestamp_selector="time.published",
        comment_count_selector="span.replies",
    )


def test_observed_tieba_search_contract_is_explicit_and_value_free() -> None:
    assert TIEBA_SEARCH_DOM_CONTRACT.card_selector == ".threadcardclass"
    assert TIEBA_SEARCH_DOM_CONTRACT.link_selector == "a.action-link-bg"
    assert TIEBA_SEARCH_DOM_CONTRACT.title_selector == ".top-title"
    assert TIEBA_SEARCH_DOM_CONTRACT.next_selector == ""
    assert "123" not in repr(TIEBA_SEARCH_DOM_CONTRACT)


def rows():
    return [
        {
            "href": "https://tieba.baidu.com/p/1234567890?pn=2",
            "title": "First public thread",
            "body": "Thread summary",
            "author": "raw-author-name",
            "timestamp": "1786579200",
            "comment_count": "回复 1.2万",
        },
        {
            "href": "https://tieba.baidu.com/p/9876543210",
            "title": "Second public thread",
            "body": "",
            "author": "",
            "timestamp": "2026-08-13T04:00:00+00:00",
            "comment_count": "3",
        },
    ]


class Page:
    def __init__(self, payload) -> None:
        self.payload = payload

    async def evaluate(self, script, argument):
        assert "comment_count" in script
        assert argument["contract"]["card_selector"] == "article.thread"
        return self.payload

    def locator(self, selector):
        assert selector == "a.next"
        return Locator()

    async def wait_for_timeout(self, milliseconds):
        return None


class Locator:
    first = None

    def __init__(self) -> None:
        self.first = self

    async def count(self):
        return 1

    async def click(self, **kwargs):
        return None


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


def test_tieba_dom_provider_extracts_exact_offset_and_counts() -> None:
    owned = OwnedPage({"recognized": True, "cards": rows(), "has_more": True})
    provider = TiebaDomSearchProvider(owned, contract(), auth_timeout_seconds=45)

    async def run():
        await provider.open(context(), CancellationToken())
        try:
            return await provider.search_page(
                context().terms, None, 1, CancellationToken()
            )
        finally:
            await provider.close()

    page = asyncio.run(run())
    assert [item.thread_id for item in page.items] == ["1234567890"]
    assert page.items[0].metrics == {"comment_count": 12_000}
    assert page.items[0].author_id == "raw-author-name"
    assert TiebaSearchCursor.decode(page.next_cursor) == TiebaSearchCursor(0, 1, 1)
    assert owned.navigations[0][0] == (
        "https://tieba.baidu.com/f/search/res?ie=utf-8&qw=game"
    )
    assert owned.navigations[0][1]["auth_timeout_seconds"] == 45
    assert owned.closed is True


def test_tieba_dom_provider_advances_page_and_term() -> None:
    async def run(payload, cursor):
        owned = OwnedPage(payload)
        provider = TiebaDomSearchProvider(owned, contract())
        await provider.open(context(), CancellationToken())
        try:
            return await provider.search_page(
                context().terms, cursor, 10, CancellationToken()
            )
        finally:
            await provider.close()

    next_page = asyncio.run(
        run({"recognized": True, "cards": rows(), "has_more": True}, None)
    )
    assert TiebaSearchCursor.decode(next_page.next_cursor) == TiebaSearchCursor(
        0, 2, 2
    )

    next_term = asyncio.run(
        run(
            {"recognized": True, "cards": rows(), "has_more": False},
            TiebaSearchCursor(0, 2, 0).encode(),
        )
    )
    assert TiebaSearchCursor.decode(next_term.next_cursor) == TiebaSearchCursor(
        1, 1, 0
    )


def test_tieba_dom_provider_rejects_unrecognized_layout() -> None:
    owned = OwnedPage({"recognized": False, "cards": [], "has_more": False})
    provider = TiebaDomSearchProvider(owned, contract())

    async def run():
        await provider.open(context(), CancellationToken())
        try:
            with pytest.raises(CrawlerFailure) as captured:
                await provider.search_page(context().terms, None, 10, CancellationToken())
            return captured.value
        finally:
            await provider.close()

    assert asyncio.run(run()).code is CrawlerErrorCode.PARSE_CHANGED


def test_tieba_dom_contract_rejects_empty_required_selector() -> None:
    with pytest.raises(ValueError, match="required"):
        TiebaDomContract("", "article", "a", "h3")
