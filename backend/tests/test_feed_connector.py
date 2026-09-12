import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from app.services import connector_feed as connectors
from app.services.connectors import FeedConnector, SearchQuery
from app.services.feed_ingestion import parse_feed_templates


class FakeResponse:
    default_content = b"""<?xml version='1.0'?><rss><channel>
      <item><guid>1</guid><title>Game one</title><link>https://example.test/1</link></item>
      <item><guid>2</guid><title>Game two</title><link>https://example.test/2</link></item>
      <item><guid>3</guid><title>Game three</title><link>https://example.test/3</link></item>
    </channel></rss>"""

    def __init__(self, content=None, *, status_code=200, headers=None) -> None:
        self.content = self.default_content if content is None else content
        self.status_code = status_code
        self.headers = dict(headers or {})

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    def __init__(self):
        self.urls = []
        self.request_headers = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, headers=None) -> FakeResponse:
        self.urls.append(url)
        self.request_headers.append(dict(headers or {}))
        return FakeResponse()


class FakeTracker:
    def __init__(self, cursor=None) -> None:
        self.cursor_value = cursor
        self.recent_ids = ()
        self.reported = []

    def cursor(self, scope, default=None):
        del scope
        return self.cursor_value if self.cursor_value is not None else default

    def report_cursor(self, scope, value) -> None:
        self.reported.append((scope, value))


def test_feed_connector_enforces_item_cap(monkeypatch) -> None:
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    connector = FeedConnector("web", "Web", "", "https://example.test?q={query}")

    async def collect():
        return [
            item
            async for item in connector.search(
                SearchQuery(keyword_id=1, name="game", include_terms=["game"], max_items=2),
            )
        ]

    rows = asyncio.run(collect())
    assert [row.external_id for row in rows] == ["1", "2"]


class AliasFeedClient(FakeClient):
    async def get(self, url: str, headers=None) -> FakeResponse:
        self.urls.append(url)
        self.request_headers.append(dict(headers or {}))
        response = FakeResponse()
        if "Alias+Game" not in url:
            response.content = b"<?xml version='1.0'?><rss><channel></channel></rss>"
        else:
            response.content = b"""<?xml version='1.0'?><rss><channel>
              <item><guid>alias-only</guid><title>Alias Game news</title><link>https://example.test/alias</link></item>
            </channel></rss>"""
        return response


def test_feed_connector_searches_aliases(monkeypatch) -> None:
    client = AliasFeedClient()
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in FeedConnector("web", "Web", "", "https://example.test?q={query}").search(
                SearchQuery(
                    keyword_id=1,
                    name="Primary Name",
                    include_terms=["Primary Name", "Alias Game"],
                    max_items=4,
                )
            )
        ]

    rows = asyncio.run(collect())
    assert [row.external_id for row in rows] == ["alias-only"]
    assert rows[0].raw_payload["search_term"] == "Alias Game"
    assert any("Primary+Name" in url for url in client.urls)
    assert any("Alias+Game" in url for url in client.urls)


@pytest.mark.parametrize(
    "value",
    [
        "http://example.test/feed.xml",
        "https://localhost/feed.xml",
        "https://127.0.0.1/feed.xml",
        "https://example.test/feed?q={other}",
        "https://example.test/feed?q={query}&again={query}",
    ],
)
def test_feed_templates_reject_unsafe_or_ambiguous_configuration(value) -> None:
    with pytest.raises(ValueError):
        parse_feed_templates(value)


def test_feed_template_json_preserves_commas_inside_url() -> None:
    value = json.dumps(["https://example.test/feed?q={query}&fields=a,b"])
    templates = parse_feed_templates(value)
    assert len(templates) == 1
    assert templates[0].render("Game News").endswith("q=Game+News&fields=a,b")


class ConditionalFeedClient(FakeClient):
    async def get(self, url: str, headers=None) -> FakeResponse:
        self.urls.append(url)
        self.request_headers.append(dict(headers or {}))
        return FakeResponse(b"", status_code=304)


def test_feed_etag_and_last_modified_are_sent_from_checkpoint(monkeypatch) -> None:
    client = ConditionalFeedClient()
    tracker = FakeTracker(
        {"etag": '"feed-v1"', "last_modified": "Wed, 13 Aug 2026 10:00:00 GMT"}
    )
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    connector = FeedConnector("web", "Web", "", "https://example.test/feed.xml")

    async def collect():
        return [
            item
            async for item in connector.search(
                SearchQuery(1, "game", [], 10, checkpoint_tracker=tracker)
            )
        ]

    assert asyncio.run(collect()) == []
    assert client.request_headers == [
        {
            "If-None-Match": '"feed-v1"',
            "If-Modified-Since": "Wed, 13 Aug 2026 10:00:00 GMT",
        }
    ]
    assert tracker.reported == []


class ValidatorFeedClient(FakeClient):
    async def get(self, url: str, headers=None) -> FakeResponse:
        self.urls.append(url)
        self.request_headers.append(dict(headers or {}))
        return FakeResponse(
            b"""<rss><channel><item>
            <guid>dated</guid><title>Game &amp; news</title>
            <link>https://publisher.test/article</link>
            <pubDate>2026-08-13T10:30:00+07:00</pubDate>
            <source url="https://publisher.test/">Publisher</source>
            <enclosure url="https://cdn.publisher.test/image.jpg"
              type="image/jpeg" length="1234" />
            </item></channel></rss>""",
            headers={
                "Content-Type": "application/rss+xml; charset=utf-8",
                "ETag": '"feed-v2"',
                "Last-Modified": "Wed, 13 Aug 2026 03:30:00 GMT",
            },
        )


def test_feed_normalizes_date_publisher_raw_allowlist_and_reports_validators(
    monkeypatch,
) -> None:
    client = ValidatorFeedClient()
    tracker = FakeTracker()
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in FeedConnector(
                "web", "Web", "", "https://example.test/feed.xml"
            ).search(SearchQuery(1, "game", [], 10, checkpoint_tracker=tracker))
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 1
    assert rows[0].title == "Game & news"
    assert rows[0].canonical_url == "https://publisher.test/article"
    assert rows[0].published_at == datetime(2026, 8, 13, 3, 30, tzinfo=UTC)
    assert rows[0].raw_payload["publisher_name"] == "Publisher"
    assert rows[0].raw_payload["publisher_url"] == "https://publisher.test/"
    assert rows[0].raw_payload["media"] == [
        {
            "kind": "enclosure",
            "url": "https://cdn.publisher.test/image.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1234,
        }
    ]
    assert "https://example.test/feed.xml" not in repr(rows[0].raw_payload)
    assert tracker.reported[-1][1] == {
        "etag": '"feed-v2"',
        "last_modified": "Wed, 13 Aug 2026 03:30:00 GMT",
    }


class PartialFailureClient(FakeClient):
    async def get(self, url: str, headers=None) -> FakeResponse:
        self.urls.append(url)
        self.request_headers.append(dict(headers or {}))
        if "broken.test" in url:
            return FakeResponse(
                b"<html>not a feed</html>",
                headers={"Content-Type": "text/html"},
            )
        return FakeResponse(
            b"""<feed xmlns="http://www.w3.org/2005/Atom">
              <entry><id>atom-1</id><title>Atom game</title>
              <link rel="alternate" href="https://publisher.test/atom-1" />
              <updated>2026-08-13T04:00:00Z</updated>
              <author><name>Atom Author</name></author></entry></feed>""",
            headers={"Content-Type": "application/atom+xml"},
        )


def test_one_invalid_feed_does_not_discard_other_configured_feed(monkeypatch) -> None:
    client = PartialFailureClient()
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    templates = json.dumps(
        [
            "https://broken.test/feed.xml",
            "https://working.test/feed.xml",
        ]
    )

    async def collect():
        return [
            item
            async for item in FeedConnector("web", "Web", "", templates).search(
                SearchQuery(1, "game", [], 10)
            )
        ]

    rows = asyncio.run(collect())
    assert [row.external_id for row in rows] == ["atom-1"]
    assert rows[0].author == "Atom Author"


def test_all_invalid_feeds_raise_typed_parse_failure(monkeypatch) -> None:
    client = PartialFailureClient()
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in FeedConnector(
                "web", "Web", "", "https://broken.test/feed.xml"
            ).search(SearchQuery(1, "game", [], 10))
        ]

    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(collect())
    assert raised.value.code is CrawlerErrorCode.PARSE_CHANGED
