from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.crawlers.adapters.tiktok import (
    TikTokDisplayApiProvider,
    TikTokDisplayConfig,
    TikTokDisplayCursor,
    TikTokDisplayDetailAdapter,
    TikTokDisplayVideoListAdapter,
    TikTokOAuthClient,
    TikTokOAuthConfig,
    TikTokTokenBundle,
    TikTokUserProfile,
    TikTokVideo,
    TikTokVideoPage,
    normalize_tiktok_video,
)
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    RunBudgets,
    RunContext,
)

API_ROOT = "https://open.tiktokapis.com"
OPEN_ID = "authorized-open-id-1234"
VIDEO_ID = "7123456789012345678"
VIDEO_URL = f"https://www.tiktok.com/@game.dev/video/{VIDEO_ID}"


def display_context(operation="scan_channel", target=None) -> RunContext:
    return RunContext(
        run_id="tiktok-display-run",
        keyword_id=1,
        source_id="tiktok",
        provider_id="tiktok_display",
        operation=operation,
        target=target or {"kind": "authorized_account", "open_id": OPEN_ID},
        terms=(),
        filters={},
        budgets=RunBudgets(max_items=20, max_requests=2, deadline_seconds=30),
    )


def config() -> TikTokDisplayConfig:
    return TikTokDisplayConfig(
        OPEN_ID,
        frozenset({"user.info.basic", "video.list"}),
        "act.secret-user-token",
    )


def video() -> TikTokVideo:
    return TikTokVideo(
        VIDEO_ID,
        VIDEO_URL,
        title="Authorized game clip",
        description="Public creator video",
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        duration_seconds=15,
        width=1080,
        height=1920,
        metrics={
            "like_count": 5,
            "comment_count": 2,
            "share_count": 1,
            "view_count": 40,
            "unknown": 999,
        },
    )


def test_tiktok_configs_require_exact_scopes_and_hide_secrets() -> None:
    display = config()
    oauth = TikTokOAuthConfig(
        "client-key-1234",
        "client-secret-value",
        "https://content.example.test/api/tiktok/callback",
    )
    bundle = TikTokTokenBundle(
        OPEN_ID,
        frozenset({"user.info.basic", "user.info.profile", "video.list"}),
        "access-secret",
        "refresh-secret",
        86_400,
        31_536_000,
    )
    assert "act.secret" not in repr(display)
    assert "client-secret" not in repr(oauth)
    assert "access-secret" not in repr(bundle)
    assert "refresh-secret" not in repr(bundle)
    with pytest.raises(ValueError, match="video.list"):
        TikTokDisplayConfig(OPEN_ID, frozenset({"user.info.basic"}), "token")
    with pytest.raises(ValueError, match="scopes"):
        TikTokOAuthConfig(
            "client-key-1234",
            "secret",
            "https://content.example.test/callback",
            frozenset({"user.info.basic"}),
        )
    with pytest.raises(ValueError, match="static HTTPS"):
        TikTokOAuthConfig(
            "client-key-1234",
            "secret",
            "https://content.example.test/callback?tenant=1",
        )


def test_tiktok_oauth_builds_state_bound_official_authorization_url() -> None:
    oauth = TikTokOAuthClient(
        TikTokOAuthConfig(
            "client-key-1234",
            "client-secret-value",
            "https://content.example.test/api/tiktok/callback",
        )
    )
    state = oauth.new_state()
    parsed = urlsplit(oauth.authorization_url(state))
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.tiktok.com"
    assert parsed.path == "/v2/auth/authorize/"
    assert params["state"] == [state]
    assert params["scope"] == ["user.info.basic,user.info.profile,video.list"]
    assert params["response_type"] == ["code"]
    assert "client_secret" not in params
    with pytest.raises(ValueError, match="state"):
        oauth.authorization_url("predictable")


def test_tiktok_oauth_exchange_refresh_and_revoke_are_server_side_forms() -> None:
    observed: list[httpx.Request] = []
    call = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call
        observed.append(request)
        call += 1
        if request.url.path.endswith("/revoke/"):
            return httpx.Response(200, content=b"")
        return httpx.Response(
            200,
            json={
                "open_id": OPEN_ID,
                "scope": "user.info.basic,user.info.profile,video.list",
                "access_token": f"access-{call}",
                "refresh_token": f"refresh-{call}",
                "expires_in": 86_400,
                "refresh_expires_in": 31_536_000,
                "token_type": "Bearer",
            },
        )

    async def run():
        client = httpx.AsyncClient(
            base_url=API_ROOT,
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        oauth = TikTokOAuthClient(
            TikTokOAuthConfig(
                "client-key-1234",
                "client-secret-value",
                "https://content.example.test/api/tiktok/callback",
            ),
            client,
        )
        try:
            first = await oauth.exchange_code("one-time-code")
            refreshed = await oauth.refresh(first.refresh_token)
            await oauth.revoke(refreshed.access_token)
            return first, refreshed
        finally:
            await oauth.close()
            await client.aclose()

    first, refreshed = asyncio.run(run())
    assert first.access_token == "access-1"
    assert refreshed.refresh_token == "refresh-2"
    assert [request.url.path for request in observed] == [
        "/v2/oauth/token/",
        "/v2/oauth/token/",
        "/v2/oauth/revoke/",
    ]
    assert all(not request.url.query for request in observed)
    forms = [parse_qs(request.content.decode()) for request in observed]
    assert forms[0]["grant_type"] == ["authorization_code"]
    assert forms[1]["grant_type"] == ["refresh_token"]
    assert forms[2]["token"] == ["access-2"]


def test_tiktok_oauth_verifies_authorized_profile_with_bearer_header() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "user": {
                        "open_id": OPEN_ID,
                        "username": "Game.Dev",
                    }
                },
                "error": {"code": "ok"},
            },
        )

    async def run():
        client = httpx.AsyncClient(
            base_url=API_ROOT,
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        oauth = TikTokOAuthClient(
            TikTokOAuthConfig(
                "client-key-1234",
                "client-secret-value",
                "https://content.example.test/api/tiktok/callback",
            ),
            client,
        )
        try:
            return await oauth.user_profile("profile-access-token")
        finally:
            await oauth.close()
            await client.aclose()

    profile = asyncio.run(run())
    assert profile == TikTokUserProfile(OPEN_ID, "game.dev")
    assert observed[0].url.path == "/v2/user/info/"
    assert observed[0].url.params["fields"] == "open_id,username"
    assert observed[0].headers["Authorization"] == "Bearer profile-access-token"
    assert "profile-access-token" not in str(observed[0].url)


def test_tiktok_display_client_uses_official_list_and_query_contracts() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        payload = {
            "id": VIDEO_ID,
            "share_url": VIDEO_URL,
            "title": "Authorized game clip",
            "video_description": "Public creator video",
            "create_time": 1_786_579_200,
            "duration": 15,
            "width": 1080,
            "height": 1920,
            "like_count": 5,
            "comment_count": 2,
            "share_count": 1,
            "view_count": 40,
        }
        if request.url.path.endswith("/list/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "videos": [payload],
                        "cursor": 1_786_579_200_000,
                        "has_more": True,
                    },
                    "error": {"code": "ok"},
                },
            )
        return httpx.Response(
            200,
            json={"data": {"videos": [payload]}, "error": {"code": "ok"}},
        )

    async def run():
        client = httpx.AsyncClient(
            base_url=API_ROOT,
            headers={"Authorization": "Bearer act.secret-user-token"},
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        provider = TikTokDisplayApiProvider(config(), client)
        await provider.open(display_context(), CancellationToken())
        try:
            page = await provider.list_videos(cursor_ms=None, max_count=20)
            detail = await provider.query_videos((VIDEO_ID, VIDEO_ID))
            return page, detail
        finally:
            await provider.close()
            await client.aclose()

    page, detail = asyncio.run(run())
    assert page.next_cursor_ms == 1_786_579_200_000
    assert page.items[0].metrics["view_count"] == 40
    assert detail[0].video_id == VIDEO_ID
    assert [request.url.path for request in observed] == [
        "/v2/video/list/",
        "/v2/video/query/",
    ]
    assert all(request.headers["Authorization"] == "Bearer act.secret-user-token" for request in observed)
    assert all("access_token" not in request.url.params for request in observed)
    assert b'"max_count":20' in observed[0].content
    assert observed[1].content.count(VIDEO_ID.encode()) == 1


class FakeProvider:
    def __init__(self, page=None, queried=()):
        self.page = page or TikTokVideoPage((video(),), 1_786_579_200_000)
        self.queried = tuple(queried)
        self.closed = False
        self.calls = []

    async def open(self, context, cancellation):
        self.calls.append((context.operation, context.target))

    async def list_videos(self, **kwargs):
        self.calls.append(kwargs)
        return self.page

    async def query_videos(self, video_ids):
        self.calls.append(video_ids)
        return self.queried

    async def close(self):
        self.closed = True


def test_tiktok_display_list_adapter_is_authorized_account_only_and_bounded() -> None:
    provider = FakeProvider()

    async def run():
        adapter = TikTokDisplayVideoListAdapter(
            provider, IdentityPseudonymizer(b"k" * 32), OPEN_ID
        )
        context = display_context()
        await adapter.open(context, CancellationToken())
        try:
            return await adapter.fetch_page(context, None, 100)
        finally:
            await adapter.close()

    page = asyncio.run(run())
    assert provider.calls[1] == {"cursor_ms": None, "max_count": 20}
    assert page.items[0].external_id == VIDEO_ID
    assert page.items[0].provenance["access_basis"] == "authorized_creator"
    assert TikTokDisplayCursor.decode(page.next_cursor).cursor_ms == 1_786_579_200_000
    assert provider.closed is True

    async def reject_other_account():
        adapter = TikTokDisplayVideoListAdapter(
            FakeProvider(), IdentityPseudonymizer(b"k" * 32), OPEN_ID
        )
        context = display_context(target={"kind": "authorized_account", "open_id": "other-open-id-1234"})
        with pytest.raises(CrawlerFailure) as captured:
            await adapter.open(context, CancellationToken())
        return captured.value

    assert asyncio.run(reject_other_account()).code is CrawlerErrorCode.PERMISSION_REQUIRED


def test_tiktok_display_detail_only_returns_authorized_account_video() -> None:
    provider = FakeProvider(queried=(video(),))

    async def run():
        adapter = TikTokDisplayDetailAdapter(
            provider, IdentityPseudonymizer(b"k" * 32), OPEN_ID
        )
        context = display_context(
            "fetch_detail", {"kind": "content_url", "url": VIDEO_URL}
        )
        await adapter.open(context, CancellationToken())
        try:
            return await adapter.fetch()
        finally:
            await adapter.close()

    record = asyncio.run(run())
    assert record.external_id == VIDEO_ID
    assert record.metrics == {
        "like_count": 5,
        "comment_count": 2,
        "share_count": 1,
        "view_count": 40,
    }
    assert record.author_pseudonym.startswith("tiktok_")


def test_tiktok_normalizer_drops_ephemeral_cover_and_unknown_metrics() -> None:
    record = normalize_tiktok_video(
        video(), OPEN_ID, IdentityPseudonymizer(b"k" * 32)
    )
    assert record.canonical_url == VIDEO_URL
    assert record.media == ()
    assert "unknown" not in record.metrics
    assert record.provenance["coverage"] == "authorized_account_only"


@pytest.mark.parametrize(
    ("status", "provider_code", "code", "retryable"),
    [
        (401, "access_token_invalid", CrawlerErrorCode.AUTH_REQUIRED, False),
        (401, "scope_not_authorized", CrawlerErrorCode.PERMISSION_REQUIRED, False),
        (400, "scope_permission_missed", CrawlerErrorCode.PERMISSION_REQUIRED, False),
        (429, "rate_limit_exceeded", CrawlerErrorCode.RATE_LIMITED, True),
        (500, "internal_error", CrawlerErrorCode.TRANSPORT_ERROR, True),
    ],
)
def test_tiktok_display_errors_are_typed_and_redacted(
    status, provider_code, code, retryable
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"Retry-After": "7"},
            json={
                "data": {},
                "error": {
                    "code": provider_code,
                    "message": "provider-secret-detail-must-not-leak",
                },
            },
        )

    async def run():
        client = httpx.AsyncClient(
            base_url=API_ROOT,
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )
        provider = TikTokDisplayApiProvider(config(), client)
        await provider.open(display_context(), CancellationToken())
        try:
            with pytest.raises(CrawlerFailure) as captured:
                await provider.list_videos(cursor_ms=None, max_count=10)
            return captured.value
        finally:
            await provider.close()
            await client.aclose()

    failure = asyncio.run(run())
    assert failure.code is code
    assert failure.retryable is retryable
    assert "provider-secret" not in failure.safe_message
    if code is CrawlerErrorCode.RATE_LIMITED:
        assert failure.retry_after_seconds == 7


def test_tiktok_clients_reject_unapproved_hosts_and_redirects() -> None:
    bad_host = httpx.AsyncClient(base_url="https://evil.example")
    redirects = httpx.AsyncClient(base_url=API_ROOT, follow_redirects=True)
    try:
        with pytest.raises(ValueError, match="unapproved"):
            TikTokDisplayApiProvider(config(), bad_host)
        with pytest.raises(ValueError, match="redirect"):
            TikTokOAuthClient(
                TikTokOAuthConfig(
                    "client-key-1234",
                    "secret",
                    "https://content.example.test/callback",
                ),
                redirects,
            )
    finally:
        asyncio.run(bad_host.aclose())
        asyncio.run(redirects.aclose())
