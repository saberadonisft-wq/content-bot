import asyncio

from app.services import connectors
from app.services.connectors import FeedConnector, SearchQuery


class FakeResponse:
    content = b"""<?xml version='1.0'?><rss><channel>
      <item><guid>1</guid><title>Game one</title><link>https://example.test/1</link></item>
      <item><guid>2</guid><title>Game two</title><link>https://example.test/2</link></item>
      <item><guid>3</guid><title>Game three</title><link>https://example.test/3</link></item>
    </channel></rss>"""

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    def __init__(self):
        self.urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str) -> FakeResponse:
        self.urls.append(url)
        return FakeResponse()


def test_feed_connector_enforces_item_cap(monkeypatch) -> None:
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
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
    async def get(self, url: str) -> FakeResponse:
        self.urls.append(url)
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
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)

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
