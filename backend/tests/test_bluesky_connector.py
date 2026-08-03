import asyncio

from app.services import connectors
from app.services.connectors import BlueskyConnector, SearchQuery


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeBlueskyClient:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, params=None) -> FakeResponse:
        self.calls.append((url, params))
        return FakeResponse(
            {
                "cursor": "next-page",
                "posts": [
                    {
                        "uri": "at://did:plc:sensitive/app.bsky.feed.post/abc123",
                        "cid": "public-content-id",
                        "author": {
                            "did": "did:plc:sensitive",
                            "handle": "player.bsky.social",
                            "displayName": "Player",
                        },
                        "record": {
                            "text": "Hades II is excellent\nMore detail",
                            "createdAt": "2026-08-01T08:00:00.000Z",
                            "langs": ["en"],
                            "facets": [
                                {
                                    "features": [
                                        {
                                            "$type": "app.bsky.richtext.facet#tag",
                                            "tag": "Hades2",
                                        }
                                    ]
                                }
                            ],
                        },
                        "likeCount": 12,
                        "replyCount": 3,
                        "repostCount": 2,
                        "quoteCount": 1,
                    }
                ],
            }
        )


def test_bluesky_search_is_capped_and_omits_profile_payload(monkeypatch) -> None:
    client = FakeBlueskyClient()
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in BlueskyConnector().search(
                SearchQuery(keyword_id=1, name="Hades II", include_terms=["Hades II"], max_items=1)
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 1
    assert rows[0].canonical_url == "https://bsky.app/profile/player.bsky.social/post/abc123"
    assert rows[0].author == "player.bsky.social"
    assert rows[0].hashtags == ["#Hades2"]
    assert rows[0].metrics == {"like_count": 12, "comment_count": 3, "share_count": 3}
    assert "did" not in rows[0].raw_payload
    assert "author" not in rows[0].raw_payload
    assert client.calls[0][1]["limit"] == 1


class AliasBlueskyClient(FakeBlueskyClient):
    async def get(self, url: str, params=None) -> FakeResponse:
        self.calls.append((url, params))
        if params["q"] != "Hades 2":
            return FakeResponse({"posts": []})
        return FakeResponse(
            {
                "posts": [
                    {
                        "uri": "at://did:plc:test/app.bsky.feed.post/alias123",
                        "cid": "alias-content-id",
                        "author": {"handle": "alias-player.bsky.social"},
                        "record": {
                            "text": "Hades 2 build discussion",
                            "createdAt": "2026-08-01T08:00:00Z",
                        },
                    }
                ]
            }
        )


def test_bluesky_search_discovers_alias_only_posts(monkeypatch) -> None:
    client = AliasBlueskyClient()
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in BlueskyConnector().search(
                SearchQuery(
                    keyword_id=1,
                    name="Hades II",
                    include_terms=["Hades II", "Hades 2"],
                    max_items=4,
                )
            )
        ]

    rows = asyncio.run(collect())
    assert [row.external_id for row in rows] == ["alias-content-id"]
    assert rows[0].raw_payload["search_term"] == "Hades 2"
    assert [call[1]["q"] for call in client.calls] == ["Hades II", "Hades 2"]
