import asyncio

from app.config import settings
from app.services import connectors
from app.services.connectors import SearchQuery, YouTubeConnector


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeYouTubeClient:
    def __init__(self, **_kwargs):
        self.search_query = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, params=None) -> FakeResponse:
        if url == "/search":
            self.search_query = params["q"]
            return FakeResponse({"items": [{"id": {"videoId": "video-1"}}]})
        return FakeResponse(
            {
                "items": [
                    {
                        "id": "video-1",
                        "snippet": {
                            "title": "Hades 2 update",
                            "description": "Alias-only coverage",
                            "publishedAt": "2026-08-01T08:00:00+00:00",
                        },
                        "statistics": {"viewCount": "10"},
                    }
                ]
            }
        )


def test_youtube_search_combines_primary_name_and_aliases(monkeypatch) -> None:
    client = FakeYouTubeClient()
    monkeypatch.setattr(settings, "youtube_api_key", "test-key")
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in YouTubeConnector().search(
                SearchQuery(
                    keyword_id=1,
                    name="Hades II",
                    include_terms=["Hades II", "Hades 2", "hades 2"],
                    max_items=4,
                )
            )
        ]

    rows = asyncio.run(collect())
    assert [row.external_id for row in rows] == ["video-1"]
    assert client.search_query == "Hades II|Hades 2"
