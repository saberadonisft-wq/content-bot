import asyncio

from app.services import connectors
from app.services.connectors import MastodonConnector, SearchQuery


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


FAKE_POST = {
    "id": "116999848189671269",
    "url": "https://mastodon.social/@testuser/116999848189671269",
    "content": "<p><strong>Hades II Hotfix 5 Released</strong></p><p>Bug fixes across combat and bosses.</p>",
    "created_at": "2026-07-28T21:33:57.000Z",
    "language": "en",
    "favourites_count": 12,
    "replies_count": 3,
    "reblogs_count": 5,
    "account": {
        "acct": "testuser@mastodon.social",
        "username": "testuser",
    },
    "tags": [
        {"name": "hadesii"},
        {"name": "indiegame"},
    ],
}


class FakeMastodonClient:
    def __init__(self, responses: dict):
        self.calls: list[tuple] = []
        self.responses = responses  # url substring -> payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, params=None) -> FakeResponse:
        self.calls.append((url, params))
        for key, payload in self.responses.items():
            if key in url:
                return FakeResponse(payload)
        return FakeResponse([])


def test_mastodon_parses_post_correctly(monkeypatch) -> None:
    client = FakeMastodonClient({"mastodon.social": [FAKE_POST]})
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in MastodonConnector().search(
                SearchQuery(keyword_id=1, name="Hades II", include_terms=[], max_items=5)
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 1
    row = rows[0]

    # URL and external_id
    assert row.canonical_url == "https://mastodon.social/@testuser/116999848189671269"
    assert row.external_id == "mastodon.social:116999848189671269"

    # HTML stripped from content
    assert "<p>" not in row.body_snippet
    assert "<strong>" not in row.body_snippet
    assert "Hades II Hotfix 5 Released" in row.body_snippet

    # Title is first line of stripped content
    assert row.title == "Hades II Hotfix 5 Released"

    # Author
    assert row.author == "testuser@mastodon.social"

    # Locale
    assert row.locale == "en"

    # Hashtags
    assert "#hadesii" in row.hashtags
    assert "#indiegame" in row.hashtags

    # Metrics
    assert row.metrics == {"like_count": 12, "comment_count": 3, "share_count": 5}

    # raw_payload does not leak account details
    assert "account" not in row.raw_payload
    assert row.raw_payload["instance"] == "mastodon.social"
    assert row.raw_payload["search_term"] == "Hades II"


def test_mastodon_deduplicates_across_instances(monkeypatch) -> None:
    """Same post URL returned from two instances should only yield once."""
    same_post = dict(FAKE_POST)  # same URL
    client = FakeMastodonClient({
        "mastodon.social": [same_post],
        "mastodon.gamedev.place": [same_post],
        "dice.camp": [],
    })
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in MastodonConnector().search(
                SearchQuery(keyword_id=1, name="Hades II", include_terms=[], max_items=10)
            )
        ]

    rows = asyncio.run(collect())
    urls = [r.canonical_url for r in rows]
    assert len(urls) == len(set(urls)), "Duplicate URLs should be deduplicated"


def test_mastodon_healthcheck_is_always_ready() -> None:
    async def check():
        return await MastodonConnector().healthcheck()

    status = asyncio.run(check())
    assert status.state == "ready"


def test_mastodon_hashtag_normalisation(monkeypatch) -> None:
    """'Black Myth: Wukong' should be searched as 'blackmythwukong'."""
    client = FakeMastodonClient({"mastodon.social": []})
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in MastodonConnector().search(
                SearchQuery(
                    keyword_id=2,
                    name="Black Myth: Wukong",
                    include_terms=[],
                    max_items=5,
                )
            )
        ]

    asyncio.run(collect())
    # The URL path should contain the normalised hashtag
    assert any("blackmythwukong" in call[0] for call in client.calls)


def test_mastodon_skips_unreachable_instance(monkeypatch) -> None:
    """A ConnectError on one instance should not abort the whole search."""
    import httpx as real_httpx

    call_count = 0

    class PartialClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, url: str, params=None):
            nonlocal call_count
            call_count += 1
            if "mastodon.social" in url:
                raise real_httpx.ConnectError("unreachable")
            if "gamedev" in url:
                return FakeResponse([FAKE_POST])
            return FakeResponse([])

    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: PartialClient())

    async def collect():
        return [
            item
            async for item in MastodonConnector().search(
                SearchQuery(keyword_id=1, name="Hades II", include_terms=[], max_items=5)
            )
        ]

    rows = asyncio.run(collect())
    # Should still yield the post from gamedev instance
    assert len(rows) == 1
    assert rows[0].raw_payload["instance"] == "mastodon.gamedev.place"
