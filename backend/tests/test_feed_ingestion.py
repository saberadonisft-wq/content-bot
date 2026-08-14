from __future__ import annotations

import asyncio

import httpx
import pytest

from app.crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from app.services.feed_ingestion import fetch_feed_document


class RedirectClient:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, headers=None):
        self.calls.append((url, dict(headers or {})))
        return self.responses.pop(0)


def response(status, content=b"", **headers):
    return httpx.Response(status, content=content, headers=headers)


def test_feed_transport_allows_only_same_allowlisted_redirect_host() -> None:
    client = RedirectClient(
        [
            response(302, Location="/canonical.xml"),
            response(
                200,
                b"<rss><channel></channel></rss>",
                **{"Content-Type": "application/rss+xml"},
            ),
        ]
    )
    document = asyncio.run(
        fetch_feed_document(
            client,
            "https://feed.test/start.xml",
            allowed_hosts=frozenset({"feed.test"}),
        )
    )
    assert document.url == "https://feed.test/canonical.xml"
    assert [url for url, _headers in client.calls] == [
        "https://feed.test/start.xml",
        "https://feed.test/canonical.xml",
    ]


def test_feed_transport_rejects_cross_host_redirect() -> None:
    client = RedirectClient([response(302, Location="https://evil.test/feed.xml")])
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(
            fetch_feed_document(
                client,
                "https://feed.test/start.xml",
                allowed_hosts=frozenset({"feed.test"}),
            )
        )
    assert raised.value.code is CrawlerErrorCode.UNSUPPORTED


def test_feed_transport_rejects_html_even_when_body_looks_like_xml() -> None:
    client = RedirectClient(
        [
            response(
                200,
                b"<rss><channel></channel></rss>",
                **{"Content-Type": "text/html"},
            )
        ]
    )
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(
            fetch_feed_document(
                client,
                "https://feed.test/feed.xml",
                allowed_hosts=frozenset({"feed.test"}),
            )
        )
    assert raised.value.code is CrawlerErrorCode.PARSE_CHANGED
