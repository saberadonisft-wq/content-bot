from __future__ import annotations

import asyncio

import pytest

from app.crawlers.adapters.tieba import (
    TIEBA_CREATOR_DOM_CONTRACT,
    TIEBA_DETAIL_DOM_CONTRACT,
    TIEBA_FORUM_DOM_CONTRACT,
    TiebaDetailAdapter,
    TiebaDomTargetProvider,
    TiebaTargetCursor,
    TiebaTargetKind,
    TiebaTargetThreadsAdapter,
    parse_tieba_target,
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

    async def evaluate(self, script, argument):
        return self.payload


class OwnedPage:
    def __init__(self, payload) -> None:
        self.page = Page(payload)
        self.urls: list[str] = []
        self.closed = False

    async def open(self, context, cancellation):
        return None

    async def navigate(self, url, cancellation, **kwargs):
        self.urls.append(url)
        return self.page

    async def close(self):
        self.closed = True


def context(operation: str, url: str) -> RunContext:
    return RunContext(
        run_id=f"tieba-{operation}",
        keyword_id=1,
        source_id="tieba",
        provider_id="cbce_tieba",
        operation=operation,
        target={"kind": "content_url", "url": url},
        terms=(),
        filters={},
        budgets=RunBudgets(max_items=5, max_requests=2, deadline_seconds=30),
    )


def provider(payload):
    owned = OwnedPage(payload)
    return (
        TiebaDomTargetProvider(
            owned,
            forum_contract=TIEBA_FORUM_DOM_CONTRACT,
            creator_contract=TIEBA_CREATOR_DOM_CONTRACT,
            detail_contract=TIEBA_DETAIL_DOM_CONTRACT,
        ),
        owned,
    )


def rows():
    return [
        {
            "href": "https://tieba.baidu.com/p/1234567890?pn=2",
            "title": "Observed public thread",
            "body": "summary",
            "author": "https://tieba.baidu.com/home/main?id=public-author",
            "comment_count": "12",
        },
        {
            "href": "https://tieba.baidu.com/p/9876543210",
            "title": "Second public thread",
            "body": "",
            "author": "",
            "comment_count": "3",
        },
    ]


def test_tieba_target_cursor_is_bound_to_kind_and_target() -> None:
    target = parse_tieba_target("https://tieba.baidu.com/f?kw=game")
    cursor = TiebaTargetCursor.decode(target, None)
    assert TiebaTargetCursor.decode(target, cursor.encode()) == cursor
    other = parse_tieba_target("https://tieba.baidu.com/f?kw=other")
    with pytest.raises(CrawlerFailure) as captured:
        TiebaTargetCursor.decode(other, cursor.encode())
    assert captured.value.code is CrawlerErrorCode.PARSE_CHANGED


@pytest.mark.parametrize(
    ("operation", "url", "kind"),
    [
        ("scan_channel", "https://tieba.baidu.com/f?kw=game", TiebaTargetKind.FORUM),
        (
            "list_creator",
            "https://tieba.baidu.com/home/main?id=public-author",
            TiebaTargetKind.CREATOR,
        ),
    ],
)
def test_tieba_target_listing_is_bounded_and_normalized(operation, url, kind) -> None:
    instance, owned = provider(
        {"recognized": True, "cards": rows(), "has_more": False}
    )

    async def run():
        adapter = TiebaTargetThreadsAdapter(
            instance, IdentityPseudonymizer(b"k" * 32)
        )
        run_context = context(operation, url)
        await adapter.open(run_context, CancellationToken())
        try:
            return await adapter.fetch_page(run_context, None, 1)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    assert page.items[0].external_id == "1234567890"
    assert page.items[0].metrics == {"comment_count": 12}
    assert page.items[0].author_pseudonym.startswith("tieba_")
    target = parse_tieba_target(url)
    assert target.kind is kind
    assert TiebaTargetCursor.decode(target, page.next_cursor).offset == 1
    assert owned.closed is True


def test_tieba_detail_extracts_canonical_thread() -> None:
    instance, owned = provider(
        {
            "recognized": True,
            "auth_required": False,
            "title": " Public detail title ",
            "body": "First-floor body",
        }
    )

    async def run():
        adapter = TiebaDetailAdapter(instance, IdentityPseudonymizer(b"k" * 32))
        run_context = context("fetch_detail", "https://tieba.baidu.com/p/1234567890")
        await adapter.open(run_context, CancellationToken())
        try:
            return await adapter.fetch()
        finally:
            await adapter.close()

    record = asyncio.run(run())
    assert record.external_id == "1234567890"
    assert record.title == "Public detail title"
    assert record.body == "First-floor body"
    assert owned.closed is True


def test_tieba_detail_reports_auth_instead_of_empty_success() -> None:
    instance, _ = provider(
        {"recognized": False, "auth_required": True, "title": "", "body": ""}
    )

    async def run():
        await instance.open(
            context("fetch_detail", "https://tieba.baidu.com/p/1234567890"),
            CancellationToken(),
        )
        try:
            with pytest.raises(CrawlerFailure) as captured:
                await instance.fetch_detail(
                    "https://tieba.baidu.com/p/1234567890", CancellationToken()
                )
            return captured.value
        finally:
            await instance.close()

    assert asyncio.run(run()).code is CrawlerErrorCode.AUTH_REQUIRED
