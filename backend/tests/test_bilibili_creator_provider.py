import asyncio

import pytest

from app.crawlers.adapters.bilibili import (
    BilibiliCreatorCursor,
    BilibiliDomCreatorProvider,
)
from app.crawlers.runtime import CancellationToken, CrawlerErrorCode, CrawlerFailure


class Node:
    def __init__(self, *, text="", attrs=None, count=1) -> None:
        self.text = text
        self.attrs = attrs or {}
        self._count = count

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    async def count(self):
        return self._count

    async def get_attribute(self, name):
        return self.attrs.get(name)

    async def inner_text(self):
        return self.text


class Card:
    def __init__(self, video_id, title) -> None:
        self.title = Node(text=title, attrs={"title": title})
        self.link = Node(attrs={"href": f"https://www.bilibili.com/video/{video_id}/"})

    def locator(self, selector):
        if selector == ".bili-video-card__title":
            return self.title
        if selector == "a[href*='/video/']":
            return self.link
        raise AssertionError(selector)


class Cards:
    def __init__(self, cards) -> None:
        self.cards = cards

    async def count(self):
        return len(self.cards)

    def nth(self, index):
        return self.cards[index]


class Page:
    def __init__(self, cards, *, has_next) -> None:
        self.cards = Cards(cards)
        classes = "vui_pagenation--btn-side"
        if not has_next:
            classes += " vui_button--disabled"
        self.side = Node(attrs={"class": classes})

    def locator(self, selector):
        if selector == ".video-list .upload-video-card":
            return self.cards
        if selector == ".vui_pagenation.video-pagination":
            return Node()
        if selector == ".vui_pagenation--btn-side":
            return self.side
        raise AssertionError(selector)


def test_creator_cursor_round_trip_and_validation() -> None:
    cursor = BilibiliCreatorCursor(3, 17)
    assert BilibiliCreatorCursor.decode(cursor.encode()) == cursor
    assert BilibiliCreatorCursor.decode(None) == BilibiliCreatorCursor()
    with pytest.raises(CrawlerFailure) as captured:
        BilibiliCreatorCursor.decode("v1:3:17")
    assert captured.value.code is CrawlerErrorCode.PARSE_CHANGED


def test_creator_provider_preserves_unconsumed_card_offset() -> None:
    async def run():
        provider = BilibiliDomCreatorProvider(None)
        provider._page = Page(
            [
                Card("BV1ab411c7De", "first"),
                Card("BV1ab411c7Df", "second"),
                Card("BV1ab411c7Dg", "third"),
            ],
            has_next=True,
        )
        provider._creator_url = "https://space.bilibili.com/123/upload/video"
        provider._current_page = 1
        first = await provider.creator_page(
            "https://space.bilibili.com/123", None, 2, CancellationToken()
        )
        second = await provider.creator_page(
            "https://space.bilibili.com/123",
            first.next_cursor,
            2,
            CancellationToken(),
        )
        return first, second

    first, second = asyncio.run(run())
    assert [video.title for video in first.items] == ["first", "second"]
    assert first.next_cursor == "v1c:1:2"
    assert [video.title for video in second.items] == ["third"]
    assert second.next_cursor == "v1c:2:0"


def test_creator_provider_ends_without_phantom_page() -> None:
    async def run():
        provider = BilibiliDomCreatorProvider(None)
        provider._page = Page([Card("BV1ab411c7De", "only")], has_next=False)
        provider._creator_url = "https://space.bilibili.com/123/upload/video"
        provider._current_page = 1
        return await provider.creator_page(
            "https://space.bilibili.com/123", None, 20, CancellationToken()
        )

    page = asyncio.run(run())
    assert page.next_cursor is None
    assert page.has_more is False
