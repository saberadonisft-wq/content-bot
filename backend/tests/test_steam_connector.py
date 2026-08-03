import asyncio

from app.services import connectors
from app.services.connectors import SearchQuery, SteamReviewsConnector


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeSteamClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, params=None) -> FakeResponse:
        if "storesearch" in url:
            return FakeResponse({"items": [{"id": 42, "name": "Test Game"}]})
        return FakeResponse(
            {
                "success": 1,
                "reviews": [
                    {
                        "recommendationid": "1001",
                        "author": {"steamid": "private-id"},
                        "review": "A useful player review",
                        "language": "english",
                        "timestamp_created": 1_700_000_000,
                        "voted_up": True,
                        "votes_up": 12,
                        "comment_count": 3,
                    },
                    {
                        "recommendationid": "1002",
                        "review": "Second review",
                        "timestamp_created": 1_700_000_100,
                        "voted_up": False,
                    },
                ],
            }
        )


def test_steam_reviews_are_capped_and_anonymized(monkeypatch) -> None:
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: FakeSteamClient())
    connector = SteamReviewsConnector()

    async def collect():
        return [
            item
            async for item in connector.search(
                SearchQuery(keyword_id=1, name="Test Game", include_terms=["Test Game"], max_items=1),
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 1
    assert rows[0].author == "Steam reviewer"
    assert rows[0].metrics == {"like_count": 12, "comment_count": 3}
    assert "author" not in rows[0].raw_payload


class AliasSteamClient(FakeSteamClient):
    def __init__(self):
        self.search_terms = []

    async def get(self, url: str, params=None) -> FakeResponse:
        if "storesearch" in url:
            self.search_terms.append(params["term"])
            if params["term"] == "Alias Game":
                return FakeResponse({"items": [{"id": 99, "name": "Alias Game"}]})
            return FakeResponse({"items": []})
        return await super().get(url, params)


def test_steam_search_discovers_alias_only_game(monkeypatch) -> None:
    client = AliasSteamClient()
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in SteamReviewsConnector().search(
                SearchQuery(
                    keyword_id=1,
                    name="Primary Name",
                    include_terms=["Primary Name", "Alias Game"],
                    max_items=4,
                )
            )
        ]

    rows = asyncio.run(collect())
    assert rows
    assert all("Alias Game" in row.title for row in rows)
    assert client.search_terms == ["Primary Name", "Alias Game"]
