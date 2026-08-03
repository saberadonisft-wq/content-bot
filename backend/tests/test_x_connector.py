import asyncio

from app.services import connectors
from app.services.connectors import SearchQuery, XConnector


class FakeResponse:
    status_code = 200

    def __init__(self):
        self.headers = {}

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "data": [
                {
                    "id": "1900000000000000000",
                    "text": "#NTE looks great today",
                    "author_id": "42",
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "lang": "en",
                    "public_metrics": {
                        "like_count": 12,
                        "reply_count": 3,
                        "retweet_count": 4,
                        "quote_count": 2,
                        "impression_count": 900,
                    },
                }
            ],
            "includes": {"users": [{"id": "42", "username": "game_news"}]},
            "meta": {},
        }


class FakeXClient:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, params=None) -> FakeResponse:
        self.calls.append((url, params))
        return FakeResponse()


def test_x_recent_search_parses_public_metrics(monkeypatch) -> None:
    client = FakeXClient()
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(connectors.settings, "x_bearer_token", "x-test-token")

    async def collect():
        return [
            item
            async for item in XConnector().search(
                SearchQuery(keyword_id=1, name="Neverness to Everness", include_terms=["NTE"], max_items=1)
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 1
    assert rows[0].canonical_url == "https://x.com/game_news/status/1900000000000000000"
    assert rows[0].hashtags == ["#NTE"]
    assert rows[0].metrics == {
        "like_count": 12,
        "comment_count": 3,
        "share_count": 6,
        "view_count": 900,
    }
    assert client.calls[0][0] == "/2/tweets/search/recent"
    assert client.calls[0][1]["query"] == "Neverness to Everness"
