from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.config import settings
from app.crawlers.adapters.instagram import (
    InstagramGraphHashtagProvider,
    InstagramHashtagBudget,
    InstagramHashtagCursor,
    InstagramHashtagSearchAdapter,
    InstagramMedia,
    InstagramMediaPage,
    MetaGraphConfig,
    normalize_instagram_media,
)
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    RunBudgets,
    RunContext,
)
from app.services.connectors import (
    InstagramHashtagConnector,
    SearchQuery,
    default_connectors,
)


def context() -> RunContext:
    return RunContext(
        run_id="instagram-run",
        keyword_id=1,
        source_id="instagram",
        provider_id="instagram_hashtag",
        operation="search",
        target={"kind": "hashtag"},
        terms=("#gamedev", "indiegames"),
        filters={},
        budgets=RunBudgets(max_items=3, max_requests=3, deadline_seconds=30),
    )


def media(media_id="17890000000000001") -> InstagramMedia:
    return InstagramMedia(
        media_id=media_id,
        permalink="https://www.instagram.com/p/AbCdE12/",
        caption="Synthetic game post",
        author_id="raw-professional-account",
        media_type="IMAGE",
        published_at=datetime(2026, 8, 13, tzinfo=UTC),
        metrics={"like_count": 4, "comment_count": 2, "unknown": 99},
    )


def test_meta_graph_config_requires_explicit_version_and_hides_token_repr() -> None:
    config = MetaGraphConfig("v99.0", "secret-access-token", "17840000000000001")
    assert "secret-access-token" not in repr(config)
    with pytest.raises(ValueError, match="version"):
        MetaGraphConfig("latest", "secret", "17840000000000001")


def test_instagram_provider_rejects_injected_client_on_unapproved_host() -> None:
    client = httpx.AsyncClient(base_url="https://evil.example/v99.0")
    try:
        with pytest.raises(ValueError, match="unapproved"):
            InstagramGraphHashtagProvider(
                MetaGraphConfig(
                    "v99.0", "secret-access-token", "17840000000000001"
                ),
                client,
            )
    finally:
        asyncio.run(client.aclose())


def test_instagram_hashtag_cursor_round_trip_and_invalid_shape() -> None:
    cursor = InstagramHashtagCursor(2, "17843800000000001", "opaque_after-1")
    assert InstagramHashtagCursor.decode(cursor.encode()) == cursor
    with pytest.raises(CrawlerFailure) as captured:
        InstagramHashtagCursor.decode("invalid")
    assert captured.value.code is CrawlerErrorCode.PARSE_CHANGED


class FakeProvider:
    def __init__(self, pages):
        self.pages = list(pages)
        self.resolved = []
        self.calls = []
        self.closed = False

    async def open(self, context, cancellation):
        return None

    async def resolve_hashtag(self, hashtag):
        self.resolved.append(hashtag)
        return "17843800000000001"

    async def recent_media(self, hashtag_id, **kwargs):
        self.calls.append((hashtag_id, kwargs))
        return self.pages.pop(0)

    async def close(self):
        self.closed = True


def test_instagram_adapter_resolves_hashtag_and_checkpoints_provider_cursor() -> None:
    provider = FakeProvider([InstagramMediaPage((media(),), "next_after")])

    async def run():
        adapter = InstagramHashtagSearchAdapter(
            provider, IdentityPseudonymizer(b"k" * 32)
        )
        await adapter.open(context(), CancellationToken())
        try:
            return await adapter.fetch_page(context(), None, 2)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    assert provider.resolved == ["#gamedev"]
    assert provider.calls[0][1] == {"after": None, "limit": 2}
    cursor = InstagramHashtagCursor.decode(page.next_cursor)
    assert cursor.term_index == 0
    assert cursor.after == "next_after"
    assert page.items[0].external_id == "17890000000000001"
    assert "raw-professional-account" not in repr(page.items[0])
    assert provider.closed is True


def test_instagram_adapter_moves_to_next_hashtag_after_natural_exhaustion() -> None:
    provider = FakeProvider([InstagramMediaPage((media(),), None)])

    async def run():
        adapter = InstagramHashtagSearchAdapter(
            provider, IdentityPseudonymizer(b"k" * 32)
        )
        await adapter.open(context(), CancellationToken())
        try:
            return await adapter.fetch_page(context(), None, 2)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    assert InstagramHashtagCursor.decode(page.next_cursor).term_index == 1


def test_instagram_normalizer_uses_metric_allowlist_and_no_ephemeral_media_url() -> None:
    record = normalize_instagram_media(
        media(), IdentityPseudonymizer(b"k" * 32)
    )
    assert record.canonical_url == "https://www.instagram.com/p/AbCdE12/"
    assert record.metrics == {"like_count": 4, "comment_count": 2}
    assert record.media == ()
    assert record.author_pseudonym.startswith("instagram_")
    assert record.provenance["coverage"] == "best_effort"


def test_instagram_graph_client_uses_bearer_header_and_official_paths() -> None:
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.url.path.endswith("/ig_hashtag_search"):
            return httpx.Response(200, json={"data": [{"id": "17843800000000001"}]})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "17890000000000001",
                        "permalink": "https://www.instagram.com/reel/AbCdE12/",
                        "caption": "Public caption",
                        "username": "professional_account",
                        "media_type": "VIDEO",
                        "timestamp": "2026-08-13T03:00:00Z",
                        "like_count": 5,
                        "comments_count": 3,
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
            headers={"Authorization": "Bearer secret-access-token"},
            transport=httpx.MockTransport(handler),
        )
        provider = InstagramGraphHashtagProvider(
            MetaGraphConfig(
                "v99.0", "secret-access-token", "17840000000000001"
            ),
            client,
        )
        await provider.open(context(), CancellationToken())
        try:
            hashtag_id = await provider.resolve_hashtag("#gamedev")
            return await provider.recent_media(hashtag_id, after=None, limit=10)
        finally:
            await provider.close()
            await client.aclose()

    page = asyncio.run(run())
    assert observed[0].url.path == "/v99.0/ig_hashtag_search"
    assert observed[1].url.path == "/v99.0/17843800000000001/recent_media"
    assert all(request.headers["Authorization"] == "Bearer secret-access-token" for request in observed)
    assert all("access_token" not in request.url.params for request in observed)
    assert page.next_after == "next_cursor"
    assert page.items[0].metrics == {"like_count": 5, "comment_count": 3}


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
def test_instagram_graph_errors_are_typed_and_do_not_leak_provider_detail(
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
        provider = InstagramGraphHashtagProvider(
            MetaGraphConfig("v99.0", "secret-token", "17840000000000001"),
            client,
        )
        await provider.open(context(), CancellationToken())
        try:
            with pytest.raises(CrawlerFailure) as captured:
                await provider.resolve_hashtag("gamedev")
            return captured.value
        finally:
            await provider.close()
            await client.aclose()

    failure = asyncio.run(run())
    assert failure.code is code
    assert failure.retryable is retryable
    assert "provider-secret" not in failure.safe_message


def test_instagram_hashtag_budget_is_rolling_cached_and_does_not_store_terms(
    tmp_path,
) -> None:
    now = [datetime(2026, 8, 1, tzinfo=UTC)]
    path = tmp_path / "budget.json"
    budget = InstagramHashtagBudget(
        path,
        "17840000000000001",
        max_unique=1,
        clock=lambda: now[0],
    )

    assert budget.reserve("#GameDev") is None
    budget.remember("gamedev", "17843800000000001")
    assert budget.reserve("GAMEDEV") == "17843800000000001"
    with pytest.raises(CrawlerFailure) as captured:
        budget.reserve("indiegames")
    assert captured.value.code is CrawlerErrorCode.RATE_LIMITED
    assert captured.value.retry_after_seconds == 7 * 24 * 60 * 60
    payload = path.read_bytes()
    assert b"gamedev" not in payload.lower()

    now[0] = datetime(2026, 8, 9, tzinfo=UTC)
    assert budget.reserve("indiegames") is None


def test_instagram_hashtag_budget_fails_closed_on_corrupt_state(tmp_path) -> None:
    path = tmp_path / "budget.json"
    path.write_text("not-json", encoding="utf-8")
    budget = InstagramHashtagBudget(path, "17840000000000001")
    with pytest.raises(CrawlerFailure) as captured:
        budget.reserve("gamedev")
    assert captured.value.code is CrawlerErrorCode.STORAGE_ERROR


def test_default_registry_uses_real_instagram_hashtag_connector() -> None:
    connector = default_connectors()["instagram"]
    assert isinstance(connector, InstagramHashtagConnector)
    assert connector.capabilities.global_search is True
    assert connector.capabilities.watchlist_filter is False


def test_instagram_connector_runs_only_when_official_settings_are_complete(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "meta_graph_api_version", "v99.0")
    monkeypatch.setattr(settings, "meta_access_token", "approved-token")
    monkeypatch.setattr(
        settings, "instagram_professional_user_id", "17840000000000001"
    )
    provider = FakeProvider([InstagramMediaPage((media(),), None)])
    connector = InstagramHashtagConnector()
    monkeypatch.setattr(connector, "_provider", lambda _config: provider)
    monkeypatch.setattr(
        connector,
        "_pseudonymizer",
        lambda: IdentityPseudonymizer(b"k" * 32),
    )

    async def run():
        return [
            item
            async for item in connector.search(
                SearchQuery(1, "#gamedev", [], 10)
            )
        ]

    items = asyncio.run(run())
    assert connector.configured is True
    assert len(items) == 1
    assert items[0].raw_payload == {
        "provider_id": "instagram_hashtag",
        "access_basis": "hashtag",
        "coverage": "best_effort",
    }
    assert "raw-professional-account" not in repr(items[0])
