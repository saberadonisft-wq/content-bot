from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.crawlers.adapters.x import (
    XApiClient,
    XPost,
    XPostPage,
    XSearchAdapter,
    XSearchCursor,
    normalize_x_post,
)
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    RunBudgets,
    RunContext,
)


def context() -> RunContext:
    return RunContext(
        run_id="x-run",
        keyword_id=1,
        source_id="x",
        provider_id="x_api",
        operation="search",
        target={"kind": "keyword"},
        terms=("game", "indie"),
        filters={},
        budgets=RunBudgets(max_items=3, max_requests=2, deadline_seconds=30),
    )


def post(post_id: str) -> XPost:
    return XPost(
        post_id,
        f"Synthetic post {post_id}",
        author_id="raw-author-id",
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        metrics={"like_count": 2, "reply_count": 1, "unknown": 99},
        media=({"kind": "photo", "url": "https://pbs.example.test/a.jpg"},),
    )


def test_x_cursor_round_trip_and_rejects_token_plus_until_id() -> None:
    cursor = XSearchCursor(2, next_token="opaque-token")
    assert XSearchCursor.decode(cursor.encode()) == cursor
    with pytest.raises(ValueError):
        XSearchCursor(0, next_token="next", until_id="123")
    with pytest.raises(CrawlerFailure) as captured:
        XSearchCursor.decode("invalid")
    assert captured.value.code is CrawlerErrorCode.PARSE_CHANGED


class FakeProvider:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []
        self.closed = False

    async def open(self, context, cancellation):
        return None

    async def recent_search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return self.pages.pop(0)

    async def close(self):
        self.closed = True


def test_x_search_adapter_preserves_unconsumed_api_rows_with_until_id() -> None:
    provider = FakeProvider(
        [XPostPage((post("9005"), post("9004"), post("9003")), "provider-next")]
    )

    async def run():
        adapter = XSearchAdapter(provider, IdentityPseudonymizer(b"k" * 32))
        await adapter.open(context(), CancellationToken())
        try:
            return await adapter.fetch_page(context(), None, 2)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    assert [item.external_id for item in page.items] == ["9005", "9004"]
    cursor = XSearchCursor.decode(page.next_cursor)
    assert cursor.term_index == 0
    assert cursor.until_id == "9003"
    assert cursor.next_token is None
    assert provider.calls[0][1]["max_results"] == 2
    assert provider.closed is True
    assert "raw-author-id" not in repr(page.items[0])


def test_x_search_moves_to_next_term_after_natural_exhaustion() -> None:
    provider = FakeProvider([XPostPage((post("9005"),), None)])

    async def run():
        adapter = XSearchAdapter(provider, IdentityPseudonymizer(b"k" * 32))
        await adapter.open(context(), CancellationToken())
        try:
            return await adapter.fetch_page(context(), None, 3)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    assert XSearchCursor.decode(page.next_cursor).term_index == 1


def test_x_normalizer_uses_stable_post_url_metric_allowlist_and_media() -> None:
    record = normalize_x_post(post("9005"), IdentityPseudonymizer(b"k" * 32))
    assert record.canonical_url == "https://x.com/i/web/status/9005"
    assert record.metrics == {"like_count": 2, "reply_count": 1}
    assert record.media == (
        {"kind": "photo", "url": "https://pbs.example.test/a.jpg"},
    )
    assert record.author_pseudonym.startswith("x_")


def api_payload() -> dict:
    return {
        "data": [
            {
                "id": "9005",
                "text": "Public post",
                "author_id": "42",
                "created_at": "2026-08-13T03:00:00Z",
                "lang": "en",
                "conversation_id": "9000",
                "public_metrics": {
                    "like_count": 4,
                    "reply_count": 2,
                    "retweet_count": 3,
                    "quote_count": 1,
                },
                "entities": {"hashtags": [{"tag": "GameDev"}]},
                "attachments": {"media_keys": ["3_1"]},
            }
        ],
        "includes": {
            "media": [
                {
                    "media_key": "3_1",
                    "type": "photo",
                    "url": "https://pbs.example.test/image.jpg",
                    "width": 1200,
                    "height": 800,
                }
            ]
        },
        "meta": {"next_token": "next-page"},
    }


def test_x_api_client_uses_official_recent_search_and_minimum_page_size() -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["query"] = dict(request.url.params)
        observed["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json=api_payload())

    async def run():
        client = XApiClient(
            "test-bearer-secret",
            transport=httpx.MockTransport(handler),
            base_url="https://api.x.test",
        )
        await client.open(context(), CancellationToken())
        try:
            return await client.recent_search("game", max_results=2)
        finally:
            await client.close()

    page = asyncio.run(run())
    assert observed["path"] == "/2/tweets/search/recent"
    assert observed["query"]["max_results"] == "10"
    assert observed["authorization"] == "Bearer test-bearer-secret"
    assert page.next_token == "next-page"
    assert page.items[0].hashtags == ("#GameDev",)
    assert page.items[0].metrics["repost_count"] == 3
    assert page.items[0].media[0]["kind"] == "photo"


def test_x_api_creator_timeline_applies_saved_channel_filters() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.url.path == "/2/users/by/username/Game_Dev":
            return httpx.Response(200, json={"data": {"id": "12345"}})
        return httpx.Response(200, json=api_payload())

    async def run():
        client = XApiClient(
            "test-bearer-secret",
            transport=httpx.MockTransport(handler),
            base_url="https://api.x.test",
        )
        await client.open(context(), CancellationToken())
        try:
            return await client.creator_posts(
                "Game_Dev",
                max_results=5,
                exclude_replies=True,
                exclude_reposts=True,
            )
        finally:
            await client.close()

    page = asyncio.run(run())
    assert len(observed) == 2
    assert observed[1].url.path == "/2/users/12345/tweets"
    assert observed[1].url.params["max_results"] == "5"
    assert observed[1].url.params["exclude"] == "replies,retweets"
    assert page.items[0].post_id == "9005"


@pytest.mark.parametrize(
    ("status", "problem_type", "code", "retryable"),
    [
        (401, "", CrawlerErrorCode.AUTH_REQUIRED, False),
        (402, "", CrawlerErrorCode.PAYMENT_OR_ACCESS_REQUIRED, False),
        (403, "", CrawlerErrorCode.PERMISSION_REQUIRED, False),
        (404, "", CrawlerErrorCode.NOT_FOUND, False),
        (429, "rate-limit-exceeded", CrawlerErrorCode.RATE_LIMITED, True),
        (429, "usage-capped", CrawlerErrorCode.PAYMENT_OR_ACCESS_REQUIRED, False),
        (503, "", CrawlerErrorCode.TRANSPORT_ERROR, True),
    ],
)
def test_x_api_errors_are_typed_safe_and_do_not_expose_response(
    status, problem_type, code, retryable
) -> None:
    secret_response = "provider-secret-detail-that-must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"x-rate-limit-reset": "4102444800"} if status == 429 else {}
        return httpx.Response(
            status,
            headers=headers,
            content=json.dumps(
                {
                    "type": f"https://api.x.com/2/problems/{problem_type}",
                    "detail": secret_response,
                }
            ),
        )

    async def run():
        client = XApiClient("token", transport=httpx.MockTransport(handler))
        await client.open(context(), CancellationToken())
        try:
            with pytest.raises(CrawlerFailure) as captured:
                await client.recent_search("game", max_results=10)
            return captured.value
        finally:
            await client.close()

    failure = asyncio.run(run())
    assert failure.code is code
    assert failure.retryable is retryable
    assert secret_response not in str(failure)
    assert "token" not in str(failure)
