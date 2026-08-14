from __future__ import annotations

import asyncio

import httpx
import pytest

from app.crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from app.services.reddit_oauth import RedditOAuthTokenCache


class FakeClient:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, *, data, auth):
        self.calls.append((url, dict(data), auth))
        await asyncio.sleep(0)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(status: int, payload: dict, **headers) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


def test_reddit_token_cache_reuses_grant_and_hides_token_from_repr() -> None:
    now = [100.0]
    cache = RedditOAuthTokenCache(clock=lambda: now[0])
    client = FakeClient([response(200, {"access_token": "secret-token", "expires_in": 3600})])

    async def run():
        first = await cache.get(client, client_id="id", client_secret="secret")
        second = await cache.get(client, client_id="id", client_secret="secret")
        return first, second

    assert asyncio.run(run()) == ("secret-token", "secret-token")
    assert len(client.calls) == 1
    assert "secret-token" not in repr(cache._cached)


def test_reddit_token_cache_prevents_concurrent_stampede() -> None:
    cache = RedditOAuthTokenCache()
    client = FakeClient([response(200, {"access_token": "one-token", "expires_in": 3600})])

    async def run():
        return await asyncio.gather(
            *(
                cache.get(client, client_id="id", client_secret="secret")
                for _ in range(10)
            )
        )

    assert asyncio.run(run()) == ["one-token"] * 10
    assert len(client.calls) == 1


def test_reddit_token_cache_refreshes_on_expiry_and_credential_rotation() -> None:
    now = [100.0]
    cache = RedditOAuthTokenCache(clock=lambda: now[0], expiry_margin_seconds=0)
    client = FakeClient(
        [
            response(200, {"access_token": "first", "expires_in": 10}),
            response(200, {"access_token": "second", "expires_in": 10}),
            response(200, {"access_token": "third", "expires_in": 10}),
        ]
    )

    async def run():
        assert await cache.get(client, client_id="id", client_secret="secret") == "first"
        now[0] = 111
        assert await cache.get(client, client_id="id", client_secret="secret") == "second"
        assert await cache.get(client, client_id="id", client_secret="rotated") == "third"

    asyncio.run(run())
    assert len(client.calls) == 3


def test_reddit_token_cache_retries_transient_response_without_leaking_detail() -> None:
    cache = RedditOAuthTokenCache(attempts=2)
    client = FakeClient(
        [
            response(503, {"detail": "provider-secret"}),
            response(200, {"access_token": "token", "expires_in": 3600}),
        ]
    )
    assert asyncio.run(cache.get(client, client_id="id", client_secret="secret")) == "token"
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, CrawlerErrorCode.AUTH_REQUIRED, False),
        (429, CrawlerErrorCode.RATE_LIMITED, True),
        (503, CrawlerErrorCode.TRANSPORT_ERROR, True),
    ],
)
def test_reddit_token_errors_are_typed_and_redacted(status, code, retryable) -> None:
    cache = RedditOAuthTokenCache(attempts=1)
    client = FakeClient([response(status, {"detail": "provider-secret-detail"})])

    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(cache.get(client, client_id="id", client_secret="client-secret"))

    assert raised.value.code is code
    assert raised.value.retryable is retryable
    assert "provider-secret" not in str(raised.value)
    assert "client-secret" not in str(raised.value)


def test_reddit_token_cache_rejects_invalid_lifetime() -> None:
    cache = RedditOAuthTokenCache()
    client = FakeClient([response(200, {"access_token": "token", "expires_in": -1})])
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(cache.get(client, client_id="id", client_secret="secret"))
    assert raised.value.code is CrawlerErrorCode.PARSE_CHANGED
