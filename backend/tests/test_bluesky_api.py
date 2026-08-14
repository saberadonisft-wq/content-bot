from __future__ import annotations

import asyncio

import httpx
import pytest

from app.crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from app.services.bluesky_api import bluesky_json, is_invalid_cursor, post_identity


class FakeClient:
    def __init__(self, responses, *, base_url=None) -> None:
        self.responses = list(responses)
        self.calls = []
        self.base_url = base_url

    async def get(self, endpoint, params=None):
        self.calls.append((endpoint, dict(params or {})))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(status, payload):
    return httpx.Response(status, json=payload)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, CrawlerErrorCode.PARSE_CHANGED),
        (403, CrawlerErrorCode.PERMISSION_REQUIRED),
        (404, CrawlerErrorCode.NOT_FOUND),
        (429, CrawlerErrorCode.RATE_LIMITED),
        (503, CrawlerErrorCode.TRANSPORT_ERROR),
    ],
)
def test_bluesky_errors_are_typed_and_provider_detail_is_redacted(status, code) -> None:
    client = FakeClient(
        [response(status, {"error": "UpstreamError", "message": "provider-secret"})]
    )
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(
            bluesky_json(
                client,
                "/xrpc/app.bsky.feed.searchPosts",
                params={"q": "game"},
                attempts=1,
            )
        )
    assert raised.value.code is code
    assert "provider-secret" not in str(raised.value)


def test_bluesky_transport_retries_outage_instead_of_reporting_eof(monkeypatch) -> None:
    client = FakeClient(
        [
            httpx.ConnectError("private network detail"),
            response(200, {"posts": []}),
        ]
    )

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("app.services.bluesky_api.asyncio.sleep", no_sleep)
    payload = asyncio.run(
        bluesky_json(
            client,
            "/xrpc/app.bsky.feed.searchPosts",
            params={"q": "game"},
        )
    )
    assert payload == {"posts": []}
    assert len(client.calls) == 2


def test_bluesky_public_appview_403_fails_over_once_to_official_direct_host() -> None:
    client = FakeClient(
        [response(403, {}), response(200, {"posts": []})],
        base_url="https://public.api.bsky.app/",
    )
    requests = []
    payload = asyncio.run(
        bluesky_json(
            client,
            "/xrpc/app.bsky.feed.searchPosts",
            params={"q": "game"},
            attempts=1,
            before_request=lambda: requests.append(True),
        )
    )
    assert payload == {"posts": []}
    assert client.calls == [
        ("/xrpc/app.bsky.feed.searchPosts", {"q": "game"}),
        (
            "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
            {"q": "game"},
        ),
    ]
    assert len(requests) == 2


def test_bluesky_post_identity_requires_post_at_uri() -> None:
    assert post_identity(
        {"uri": "at://did:plc:stable/app.bsky.feed.post/abc123"}
    ) == ("at://did:plc:stable/app.bsky.feed.post/abc123", "abc123")
    assert post_identity({"uri": "https://bsky.app/profile/test/post/abc"}) is None
    assert post_identity({"uri": "at://did:plc:stable/app.bsky.actor.profile/self"}) is None


def test_bluesky_invalid_cursor_is_narrowly_classified() -> None:
    client = FakeClient([response(400, {"error": "InvalidRequest"})])
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(
            bluesky_json(
                client,
                "/xrpc/app.bsky.feed.searchPosts",
                params={"q": "game", "cursor": "stale"},
                attempts=1,
            )
        )
    assert is_invalid_cursor(raised.value) is True


@pytest.mark.parametrize("reason", ["HandleNotFound", "NotFound"])
def test_bluesky_official_not_found_reasons_are_typed(reason: str) -> None:
    client = FakeClient([response(400, {"error": reason})])
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(
            bluesky_json(
                client,
                "/xrpc/com.atproto.identity.resolveHandle",
                params={"handle": "missing.example"},
                attempts=1,
            )
        )
    assert raised.value.code is CrawlerErrorCode.NOT_FOUND
