import asyncio

import httpx

from app.crawlers.runtime import IdentityPseudonymizer
from app.services import channel_scans
from app.services import connector_reddit as connectors
from app.services.connectors import RedditConnector, SearchQuery
from app.services.reddit_oauth import reddit_token_cache


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeRedditClient:
    def __init__(self):
        self.calls = []
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, params=None) -> FakeResponse:
        self.calls.append((url, params))
        return FakeResponse(
            {
                "data": {
                    "after": None,
                    "children": [
                        {
                            "kind": "t3",
                            "data": {
                                "id": "abc123",
                                "permalink": "/r/gaming/comments/abc123/hades_ii_is_excellent/",
                                "title": "Hades II is excellent",
                                "selftext": "A longer public discussion.",
                                "author": "player_one",
                                "subreddit": "gaming",
                                "created_utc": 1722508800,
                                "score": 42,
                                "num_comments": 7,
                                "is_self": True,
                                "url": "https://www.reddit.com/r/gaming/comments/abc123/hades_ii_is_excellent/",
                                "link_flair_text": "Discussion",
                            },
                        }
                    ],
                }
            }
        )

    async def post(self, url: str, data=None, auth=None) -> FakeResponse:
        self.calls.append((url, data))
        return FakeResponse({"access_token": "reddit-test-token"})


def test_reddit_search_parses_public_submission(monkeypatch) -> None:
    reddit_token_cache.invalidate()
    client = FakeRedditClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(connectors.settings, "reddit_client_id", "client-id")
    monkeypatch.setattr(connectors.settings, "reddit_client_secret", "client-secret")
    monkeypatch.setattr(
        RedditConnector,
        "_pseudonymizer",
        staticmethod(lambda: IdentityPseudonymizer(b"k" * 32)),
    )

    async def collect():
        return [
            item
            async for item in RedditConnector().search(
                SearchQuery(keyword_id=1, name="Hades II", include_terms=[], max_items=1)
            )
        ]

    rows = asyncio.run(collect())

    assert len(rows) == 1
    assert rows[0].external_id == "abc123"
    assert rows[0].canonical_url == "https://www.reddit.com/r/gaming/comments/abc123/hades_ii_is_excellent/"
    assert rows[0].metrics == {"like_count": 42, "comment_count": 7}
    assert rows[0].hashtags == ["#Discussion"]
    assert rows[0].author.startswith("reddit_")
    assert "player_one" not in repr(rows[0])
    assert rows[0].raw_payload["subreddit"] == "gaming"
    assert rows[0].raw_payload["score"] == 42
    assert client.calls[0][0] == "https://www.reddit.com/api/v1/access_token"
    assert client.calls[1][0] == "https://oauth.reddit.com/search"
    assert client.calls[1][1]["q"] == "Hades II"
    assert client.calls[1][1]["type"] == "link"
    assert client.headers["Authorization"] == "Bearer reddit-test-token"


def test_reddit_keyword_and_channel_share_token_cache_and_privacy(monkeypatch) -> None:
    reddit_token_cache.invalidate()
    client = FakeRedditClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(connectors.settings, "reddit_client_id", "client-id")
    monkeypatch.setattr(connectors.settings, "reddit_client_secret", "client-secret")
    monkeypatch.setattr(
        RedditConnector,
        "_pseudonymizer",
        staticmethod(lambda: IdentityPseudonymizer(b"k" * 32)),
    )

    async def collect():
        keyword_rows = [
            item
            async for item in RedditConnector().search(
                SearchQuery(
                    keyword_id=1,
                    name="Hades II",
                    include_terms=[],
                    max_items=1,
                )
            )
        ]
        channel_rows = [
            item
            async for item in channel_scans._scan_reddit(
                {"url": "https://www.reddit.com/r/gaming/"},
                SearchQuery(
                    keyword_id=1,
                    name="Hades II",
                    include_terms=[],
                    max_items=1,
                ),
            )
        ]
        return keyword_rows, channel_rows

    keyword_rows, channel_rows = asyncio.run(collect())

    assert len(keyword_rows) == len(channel_rows) == 1
    assert keyword_rows[0].author == channel_rows[0].author
    assert keyword_rows[0].metrics == channel_rows[0].metrics
    assert channel_rows[0].raw_payload["score"] == 42
    assert "player_one" not in repr(channel_rows[0])
    assert [call[0] for call in client.calls].count(
        "https://www.reddit.com/api/v1/access_token"
    ) == 1
    assert client.calls[-1][0] == "https://oauth.reddit.com/r/gaming/new"
