from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.crawlers.adapters.facebook import (
    FacebookGraphPageProvider,
    FacebookPageCursor,
    FacebookPageFeedAdapter,
    FacebookPagePost,
    FacebookPagePostPage,
    MetaPageGraphConfig,
    normalize_facebook_page_post,
)
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    RunBudgets,
    RunContext,
)

PAGE_ID = "123456789012345"
POST_ID = f"{PAGE_ID}_987654321098765"


def context() -> RunContext:
    return RunContext(
        run_id="facebook-page-run",
        keyword_id=1,
        source_id="facebook",
        provider_id="meta_pages",
        operation="scan_channel",
        target={"kind": "page", "page_id": PAGE_ID},
        terms=(),
        filters={"access_basis": "authorized_page"},
        budgets=RunBudgets(max_items=10, max_requests=2, deadline_seconds=30),
    )


def post() -> FacebookPagePost:
    return FacebookPagePost(
        POST_ID,
        "https://www.facebook.com/GameStudio/posts/987654321098765/?tracking=drop",
        message="New game update\nWishlist now",
        author_id=PAGE_ID,
        published_at=datetime(2026, 8, 13, tzinfo=UTC),
        metrics={
            "reaction_count": 8,
            "comment_count": 3,
            "share_count": 2,
            "unknown": 100,
        },
    )


def test_meta_page_config_is_pinned_and_hides_page_token() -> None:
    config = MetaPageGraphConfig("v99.0", "page-secret", PAGE_ID, "GameStudio")
    assert "page-secret" not in repr(config)
    with pytest.raises(ValueError, match="version"):
        MetaPageGraphConfig("latest", "secret", PAGE_ID, "GameStudio")
    with pytest.raises(ValueError, match="username"):
        MetaPageGraphConfig("v99.0", "secret", PAGE_ID, "bad/page")


def test_facebook_cursor_round_trip_and_invalid_shape() -> None:
    cursor = FacebookPageCursor("opaque_after-1")
    assert FacebookPageCursor.decode(cursor.encode()) == cursor
    with pytest.raises(CrawlerFailure) as captured:
        FacebookPageCursor.decode("invalid")
    assert captured.value.code is CrawlerErrorCode.PARSE_CHANGED


class FakeProvider:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []
        self.closed = False

    async def open(self, run_context, cancellation):
        self.calls.append((run_context.operation, cancellation.cancelled))

    async def page_feed(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages.pop(0)

    async def close(self):
        self.closed = True


def test_facebook_adapter_checkpoints_after_cursor_and_closes() -> None:
    provider = FakeProvider([FacebookPagePostPage((post(),), "next_after")])

    async def run():
        adapter = FacebookPageFeedAdapter(
            provider, IdentityPseudonymizer(b"k" * 32)
        )
        await adapter.open(context(), CancellationToken())
        try:
            return await adapter.fetch_page(context(), None, 5)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    assert provider.calls[1] == {"after": None, "limit": 5}
    assert FacebookPageCursor.decode(page.next_cursor).after == "next_after"
    assert page.items[0].external_id == POST_ID
    assert page.items[0].canonical_url == (
        "https://www.facebook.com/GameStudio/posts/987654321098765/"
    )
    assert page.items[0].author_pseudonym.startswith("facebook_")
    assert provider.closed is True


def test_facebook_normalizer_allowlists_metrics_and_drops_tracking_query() -> None:
    record = normalize_facebook_page_post(
        post(), IdentityPseudonymizer(b"k" * 32)
    )
    assert record.title == "New game update"
    assert record.metrics == {
        "reaction_count": 8,
        "comment_count": 3,
        "share_count": 2,
    }
    assert record.media == ()
    assert record.provenance["access_basis"] == "authorized_page"
    assert "tracking" not in record.canonical_url


def test_facebook_graph_client_uses_bearer_header_and_page_feed_path() -> None:
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": POST_ID,
                        "message": "Official Page post",
                        "created_time": "2026-08-13T03:00:00+0000",
                        "permalink_url": "https://www.facebook.com/GameStudio/posts/987654321098765/",
                        "from": {"id": PAGE_ID, "name": "must-not-persist"},
                        "shares": {"count": 2},
                        "reactions": {"summary": {"total_count": 9}},
                        "comments": {"summary": {"total_count": 4}},
                    }
                ],
                "paging": {
                    "cursors": {"after": "next_cursor"},
                    "next": "https://graph.facebook.com/redacted",
                },
            },
        )

    async def run():
        client = httpx.AsyncClient(
            base_url="https://graph.facebook.com/v99.0",
            transport=httpx.MockTransport(handler),
        )
        provider = FacebookGraphPageProvider(
            MetaPageGraphConfig(
                "v99.0", "page-secret", PAGE_ID, "GameStudio"
            ),
            client,
        )
        await provider.open(context(), CancellationToken())
        try:
            return await provider.page_feed(after=None, limit=10)
        finally:
            await provider.close()
            await client.aclose()

    page = asyncio.run(run())
    request = observed[0]
    assert request.url.path == f"/v99.0/{PAGE_ID}/feed"
    assert request.headers["Authorization"] == "Bearer page-secret"
    assert "access_token" not in request.url.params
    assert "from{id}" in request.url.params["fields"]
    assert page.next_after == "next_cursor"
    assert page.items[0].metrics == {
        "reaction_count": 9,
        "comment_count": 4,
        "share_count": 2,
    }
    assert "must-not-persist" not in repr(page.items[0])


@pytest.mark.parametrize(
    ("status", "provider_code", "code", "retryable"),
    [
        (401, 190, CrawlerErrorCode.AUTH_REQUIRED, False),
        (403, 10, CrawlerErrorCode.PERMISSION_REQUIRED, False),
        (429, 4, CrawlerErrorCode.RATE_LIMITED, True),
        (404, None, CrawlerErrorCode.NOT_FOUND, False),
        (503, None, CrawlerErrorCode.TRANSPORT_ERROR, True),
    ],
)
def test_facebook_graph_errors_are_typed_and_redacted(
    status, provider_code, code, retryable
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": provider_code,
                    "message": "provider-secret-detail-must-not-leak",
                }
            },
        )

    async def run():
        client = httpx.AsyncClient(
            base_url="https://graph.facebook.com/v99.0",
            transport=httpx.MockTransport(handler),
        )
        provider = FacebookGraphPageProvider(
            MetaPageGraphConfig("v99.0", "page-secret", PAGE_ID, "GameStudio"),
            client,
        )
        await provider.open(context(), CancellationToken())
        try:
            with pytest.raises(CrawlerFailure) as captured:
                await provider.page_feed(after=None, limit=10)
            return captured.value
        finally:
            await provider.close()
            await client.aclose()

    failure = asyncio.run(run())
    assert failure.code is code
    assert failure.retryable is retryable
    assert "provider-secret" not in failure.safe_message
