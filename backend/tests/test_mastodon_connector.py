import asyncio

import pytest

from app.config import settings
from app.crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from app.services import connectors
from app.services.connectors import MastodonConnector, SearchQuery, stable_external_id


class FakeResponse:
    def __init__(self, payload, *, headers=None, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

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
    def __init__(self, factory, base_url: str):
        self.factory = factory
        self.base_url = base_url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, params=None) -> FakeResponse:
        host = self.base_url.removeprefix("https://")
        self.factory.calls.append((host, url, params))
        payload = self.factory.responses.get(host, [])
        if isinstance(payload, Pages):
            return payload.responses.pop(0) if payload.responses else FakeResponse([])
        if isinstance(payload, Exception):
            raise payload
        return FakeResponse(payload)


class FakeMastodonFactory:
    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple] = []

    def __call__(self, **kwargs):
        return FakeMastodonClient(self, str(kwargs.get("base_url") or ""))


class Pages:
    def __init__(self, *responses):
        self.responses = list(responses)


def test_mastodon_parses_post_correctly(monkeypatch) -> None:
    client = FakeMastodonFactory({"mastodon.social": [FAKE_POST]})
    monkeypatch.setattr(connectors.httpx, "AsyncClient", client)

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
    assert row.external_id == stable_external_id("mastodon", row.canonical_url.casefold())

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
    assert row.raw_payload["fetching_instance"] == "mastodon.social"
    assert row.raw_payload["search_term"] == "Hades II"


def test_mastodon_deduplicates_across_instances(monkeypatch) -> None:
    """Same post URL returned from two instances should only yield once."""
    same_post = dict(FAKE_POST)  # same URL
    client = FakeMastodonFactory({
        "mastodon.social": [same_post],
        "mastodon.gamedev.place": [same_post],
        "dice.camp": [],
    })
    monkeypatch.setattr(connectors.httpx, "AsyncClient", client)

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
    client = FakeMastodonFactory({"mastodon.social": []})
    monkeypatch.setattr(connectors.httpx, "AsyncClient", client)

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
    assert any("blackmythwukong" in call[1] for call in client.calls)


def test_mastodon_skips_unreachable_instance(monkeypatch) -> None:
    """A ConnectError on one instance should not abort the whole search."""
    import httpx as real_httpx

    client = FakeMastodonFactory(
        {
            "mastodon.social": real_httpx.ConnectError("unreachable"),
            "mastodon.gamedev.place": [FAKE_POST],
            "dice.camp": [],
        }
    )
    monkeypatch.setattr(connectors.httpx, "AsyncClient", client)

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
    assert rows[0].raw_payload["fetching_instance"] == "mastodon.gamedev.place"


def _post(index: int) -> dict:
    return {
        **FAKE_POST,
        "id": str(1_000 - index),
        "uri": f"https://origin.example/users/player/statuses/{index}",
        "url": f"https://origin.example/@player/{index}",
    }


def test_mastodon_follows_link_pagination_with_exact_cap(monkeypatch) -> None:
    first = FakeResponse(
        [_post(index) for index in range(40)],
        headers={
            "Link": '<https://mastodon.social/api/v1/timelines/tag/game?max_id=older>; rel="next"'
        },
    )
    second = FakeResponse([_post(index) for index in range(40, 50)])
    client = FakeMastodonFactory({"mastodon.social": Pages(first, second)})
    monkeypatch.setattr(connectors.httpx, "AsyncClient", client)
    monkeypatch.setattr(settings, "mastodon_instances", "mastodon.social")

    async def collect():
        return [
            item
            async for item in MastodonConnector().search(
                SearchQuery(keyword_id=1, name="Game", include_terms=[], max_items=45)
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 45
    timeline_calls = [call for call in client.calls if "/timelines/tag/" in call[1]]
    assert len(timeline_calls) == 2
    assert timeline_calls[1][2]["max_id"] == "older"


def test_mastodon_partial_instance_failure_emits_visible_warning(monkeypatch) -> None:
    import httpx as real_httpx

    request = real_httpx.Request("GET", "https://mastodon.social/api/v1/timelines/tag/game")
    client = FakeMastodonFactory(
        {
            "mastodon.social": real_httpx.ConnectError("unreachable", request=request),
            "mastodon.gamedev.place": [_post(1)],
        }
    )
    monkeypatch.setattr(connectors.httpx, "AsyncClient", client)
    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("app.services.mastodon_api.asyncio.sleep", no_sleep)
    monkeypatch.setattr(
        settings, "mastodon_instances", "mastodon.social,mastodon.gamedev.place"
    )
    warnings = []

    async def warning(code, message):
        warnings.append((code, message))

    async def collect():
        return [
            item
            async for item in MastodonConnector().search(
                SearchQuery(
                    keyword_id=1,
                    name="Game",
                    include_terms=[],
                    max_items=4,
                    warning_callback=warning,
                )
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 1
    assert warnings[0][0] == "MASTODON_INSTANCE_FAILED"
    assert "unreachable" not in warnings[0][1]


def test_mastodon_all_instances_unavailable_is_typed_failure(monkeypatch) -> None:
    import httpx as real_httpx

    request = real_httpx.Request("GET", "https://mastodon.social/api/v1/timelines/tag/game")
    client = FakeMastodonFactory(
        {"mastodon.social": real_httpx.ConnectError("secret", request=request)}
    )
    monkeypatch.setattr(connectors.httpx, "AsyncClient", client)
    monkeypatch.setattr(settings, "mastodon_instances", "mastodon.social")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("app.services.mastodon_api.asyncio.sleep", no_sleep)

    async def collect():
        return [
            item
            async for item in MastodonConnector().search(
                SearchQuery(keyword_id=1, name="Game", include_terms=[], max_items=1)
            )
        ]

    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(collect())
    assert raised.value.code is CrawlerErrorCode.TRANSPORT_ERROR
    assert "secret" not in raised.value.safe_message


def test_mastodon_deep_health_is_not_ready_when_all_instances_fail(monkeypatch) -> None:
    import httpx as real_httpx

    request = real_httpx.Request("GET", "https://mastodon.social/api/v2/instance")
    client = FakeMastodonFactory(
        {"mastodon.social": real_httpx.ConnectError("down", request=request)}
    )
    monkeypatch.setattr(connectors.httpx, "AsyncClient", client)
    monkeypatch.setattr(settings, "mastodon_instances", "mastodon.social")
    status = asyncio.run(MastodonConnector().deep_healthcheck())
    assert status.state == "degraded"
    assert status.reason_code == "ALL_INSTANCES_UNAVAILABLE"
    assert status.probe == "deep"
