import asyncio
from datetime import UTC, datetime

import pytest

from app.crawlers.adapters.bilibili import (
    BilibiliDomCursor,
    BilibiliDomDetailProvider,
    BilibiliDomSearchProvider,
    parse_compact_count,
    parse_public_datetime,
)
from app.crawlers.runtime import CancellationToken, CrawlerErrorCode, CrawlerFailure


class FakeNode:
    def __init__(self, *, count=1, attributes=None, text="", parent=None) -> None:
        self._count = count
        self.attributes = attributes or {}
        self.text = text
        self.parent = parent

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    async def count(self):
        return self._count

    async def get_attribute(self, name):
        return self.attributes.get(name)

    async def inner_text(self):
        return self.text

    def locator(self, selector):
        assert selector == "xpath=.."
        return self.parent


class FakeCard:
    def __init__(self, video_id: str, views: str) -> None:
        self.link = FakeNode(
            attributes={"href": f"https://www.bilibili.com/video/{video_id}"}
        )
        self.title = FakeNode(
            attributes={"title": f"Synthetic {video_id}"}, parent=self.link
        )
        self.view = FakeNode(text=views)

    def locator(self, selector):
        if selector == ".bili-video-card__info--tit":
            return self.title
        if selector == ".bili-video-card__stats--item span":
            return self.view
        raise AssertionError(selector)


class FakeCards:
    def __init__(self, cards) -> None:
        self.cards = cards

    async def count(self):
        return len(self.cards)

    def nth(self, index):
        return self.cards[index]


class FakeSides(FakeNode):
    pass


class FakePage:
    def __init__(self, cards, *, has_next: bool) -> None:
        self.cards = FakeCards(cards)
        classes = "vui_pagenation--btn-side"
        if not has_next:
            classes += " vui_button--disabled"
        self.sides = FakeSides(attributes={"class": classes})

    def locator(self, selector):
        if selector == ".video-list-item .bili-video-card":
            return self.cards
        if selector == ".search-page":
            return FakeNode()
        if selector == ".vui_pagenation--btn-side":
            return self.sides
        raise AssertionError(selector)


class FakeDetailPage:
    def __init__(self) -> None:
        self.nodes = {
            "h1.video-title": FakeNode(attributes={"title": "Synthetic detail title"}),
            "meta[name='description']": FakeNode(
                attributes={"content": "Public description"}
            ),
            "meta[property='video:release_date']": FakeNode(
                attributes={"content": "2026-08-13T12:30:00+08:00"}
            ),
            "meta[property='og:image']": FakeNode(
                attributes={"content": "https://i.example.test/public-cover.jpg"}
            ),
            "meta[property='video:duration']": FakeNode(
                attributes={"content": "123"}
            ),
            ".view-text": FakeNode(text="1.2万"),
            ".video-like-info": FakeNode(text="25"),
            ".video-fav-info": FakeNode(text="3"),
        }

    def locator(self, selector):
        return self.nodes.get(selector, FakeNode(count=0))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0),
        ("1,234", 1234),
        ("1.2万", 12000),
        ("3亿", 300000000),
        ("4.5K", 4500),
        ("2M", 2000000),
        ("--", None),
        ("private", None),
    ],
)
def test_parse_compact_count(value, expected) -> None:
    assert parse_compact_count(value) == expected


def test_bilibili_dom_cursor_round_trip_preserves_page_offset() -> None:
    cursor = BilibiliDomCursor(term_index=2, page_number=3, offset=41)
    assert BilibiliDomCursor.decode(cursor.encode()) == cursor
    assert BilibiliDomCursor.decode(None) == BilibiliDomCursor()


@pytest.mark.parametrize("value", ["", "v2:0:1:0", "v1:-1:1:0", "v1:0:0:0"])
def test_bilibili_dom_cursor_rejects_malformed_checkpoint(value) -> None:
    with pytest.raises(CrawlerFailure) as captured:
        BilibiliDomCursor.decode(value)
    assert captured.value.code is CrawlerErrorCode.PARSE_CHANGED


def test_dom_provider_cursor_preserves_unconsumed_cards_within_page() -> None:
    async def run():
        provider = BilibiliDomSearchProvider(None)
        provider._page = FakePage(
            [
                FakeCard("BV1ab411c7De", "1.2万"),
                FakeCard("BV1ab411c7Df", "2"),
                FakeCard("BV1ab411c7Dg", "3"),
                FakeCard("BV1ab411c7Dh", "4"),
                FakeCard("BV1ab411c7Di", "5"),
            ],
            has_next=True,
        )
        provider._cancellation = CancellationToken()
        provider._loaded_key = (0, 1)
        first = await provider.search_page(("game",), None, 2, provider._cancellation)
        second = await provider.search_page(
            ("game",), first.next_cursor, 2, provider._cancellation
        )
        return first, second

    first, second = asyncio.run(run())
    assert [item.video_id for item in first.items] == [
        "BV1ab411c7De",
        "BV1ab411c7Df",
    ]
    assert first.items[0].metrics == {"view_count": 12000}
    assert first.next_cursor == "v1:0:1:2"
    assert [item.video_id for item in second.items] == [
        "BV1ab411c7Dg",
        "BV1ab411c7Dh",
    ]
    assert second.next_cursor == "v1:0:1:4"


def test_dom_provider_moves_to_next_term_only_after_last_page_is_consumed() -> None:
    async def run():
        provider = BilibiliDomSearchProvider(None)
        provider._page = FakePage([FakeCard("BV1ab411c7De", "1")], has_next=False)
        provider._cancellation = CancellationToken()
        provider._loaded_key = (0, 1)
        return await provider.search_page(
            ("first", "second"), None, 10, provider._cancellation
        )

    page = asyncio.run(run())
    assert page.next_cursor == "v1:1:1:0"
    assert page.has_more is True


def test_dom_detail_provider_extracts_only_observed_public_fields() -> None:
    async def run():
        provider = BilibiliDomDetailProvider(None)
        provider._page = FakeDetailPage()
        provider._loaded_target = "https://www.bilibili.com/video/BV1ab411c7De"
        return await provider.fetch_detail(
            "https://m.bilibili.com/video/BV1ab411c7De?share=test",
            CancellationToken(),
        )

    video = asyncio.run(run())
    assert video.video_id == "BV1ab411c7De"
    assert video.canonical_url == "https://www.bilibili.com/video/BV1ab411c7De"
    assert video.title == "Synthetic detail title"
    assert video.description == "Public description"
    assert video.creator_id == ""
    assert video.published_at == datetime(2026, 8, 13, 4, 30, tzinfo=UTC)
    assert video.metrics == {
        "view_count": 12_000,
        "like_count": 25,
        "favorite_count": 3,
    }
    assert video.media == (
        {"kind": "cover", "url": "https://i.example.test/public-cover.jpg"},
        {"kind": "video_metadata", "duration_seconds": 123},
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-13T12:30:00+08:00", datetime(2026, 8, 13, 4, 30, tzinfo=UTC)),
        ("1786576200", datetime.fromtimestamp(1786576200, tz=UTC)),
        ("1786576200000", datetime.fromtimestamp(1786576200, tz=UTC)),
        ("not-a-date", None),
        (None, None),
    ],
)
def test_parse_public_datetime(value, expected) -> None:
    assert parse_public_datetime(value) == expected
