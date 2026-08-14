from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.tiktok_auth import OAUTH_COOKIE, build_tiktok_auth_router
from app.crawlers.adapters.tiktok import (
    TikTokOAuthConfig,
    TikTokTokenBundle,
    TikTokTokenVault,
    TikTokUserProfile,
)
from app.crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from app.services.tiktok_oauth import (
    TikTokOAuthService,
    TikTokOAuthStateStore,
    oauth_cookie_matches,
)

OPEN_ID = "authorized-open-id-1234"
STATE = "s" * 43


def token_bundle(
    access="access-token-value",
    refresh="refresh-token-value",
    scopes=frozenset({"user.info.basic", "user.info.profile", "video.list"}),
):
    return TikTokTokenBundle(
        OPEN_ID,
        scopes,
        access,
        refresh,
        3600,
        86_400,
    )


class FakeOAuthClient:
    def __init__(self, config, *, exchange=None, refreshed=None, profile=None) -> None:
        self.config = config
        self.exchange_result = exchange or token_bundle()
        self.refresh_result = refreshed or token_bundle(
            "rotated-access", "rotated-refresh"
        )
        self.profile_result = profile or TikTokUserProfile(OPEN_ID, "game.dev")
        self.calls = []

    @staticmethod
    def new_state():
        return STATE

    def authorization_url(self, state):
        self.calls.append(("authorize", state))
        return f"https://www.tiktok.com/v2/auth/authorize/?state={state}"

    async def exchange_code(self, code):
        self.calls.append(("exchange", code))
        return self.exchange_result

    async def user_profile(self, access_token):
        self.calls.append(("profile", access_token))
        return self.profile_result

    async def refresh(self, refresh_token):
        self.calls.append(("refresh", refresh_token))
        return self.refresh_result

    async def revoke(self, access_token):
        self.calls.append(("revoke", access_token))

    async def close(self):
        self.calls.append(("close",))


def oauth_config():
    return TikTokOAuthConfig(
        "client-key-1234",
        "client-secret-value",
        "https://api.example.test/api/v1/auth/tiktok/callback",
    )


def service(tmp_path, client=None):
    fake = client or FakeOAuthClient(oauth_config())
    result = TikTokOAuthService(
        TikTokTokenVault(tmp_path / "crawler-secrets" / "tiktok"),
        oauth_config,
        "http://127.0.0.1:5173/",
        client_factory=lambda config: fake,
    )
    return result, fake


def test_tiktok_oauth_status_is_setup_aware_and_secret_free(tmp_path) -> None:
    unconfigured = TikTokOAuthService(
        TikTokTokenVault(tmp_path / "empty"),
        lambda: TikTokOAuthConfig("", "", ""),
        "http://127.0.0.1:5173/",
    )
    assert unconfigured.status().public() == {
        "state": "setup_required",
        "configured": False,
        "connected": False,
        "detail": "Configure the approved TikTok client key, secret and HTTPS redirect URI.",
        "authorized_username": "",
        "scopes": [],
        "access_expires_at": None,
        "refresh_expires_at": None,
    }
    configured, _ = service(tmp_path)
    status = configured.status().public()
    assert status["state"] == "disconnected"
    assert "client-secret-value" not in repr(status)


def test_tiktok_oauth_state_is_bound_one_time_and_username_is_validated(tmp_path) -> None:
    oauth, _ = service(tmp_path)
    url = oauth.begin("@Game.Dev")
    assert parse_qs(urlsplit(url).query)["state"] == [STATE]
    assert oauth.states.consume(STATE) == "game.dev"
    with pytest.raises(CrawlerFailure, match="already used"):
        oauth.states.consume(STATE)
    with pytest.raises(ValueError, match="username"):
        oauth.begin("not valid username")
    assert oauth_cookie_matches(STATE, STATE)
    assert not oauth_cookie_matches("different" * 5, STATE)


def test_tiktok_oauth_complete_encrypts_tokens_and_exposes_safe_status(tmp_path) -> None:
    oauth, fake = service(tmp_path)
    oauth.begin("game.dev")
    status = asyncio.run(oauth.complete("single-use-code", STATE))

    assert status.state == "connected"
    assert status.authorized_username == "game.dev"
    assert status.scopes == (
        "user.info.basic",
        "user.info.profile",
        "video.list",
    )
    stored = oauth.vault.load()
    assert stored is not None
    assert stored.access_token == "access-token-value"
    assert b"access-token-value" not in oauth.vault.token_path.read_bytes()
    assert ("exchange", "single-use-code") in fake.calls
    assert ("profile", "access-token-value") in fake.calls


def test_tiktok_oauth_rejects_partial_scope_and_revokes_grant(tmp_path) -> None:
    fake = FakeOAuthClient(
        oauth_config(),
        exchange=token_bundle(scopes=frozenset({"user.info.basic"})),
    )
    oauth, _ = service(tmp_path, fake)
    oauth.begin("game.dev")

    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(oauth.complete("code", STATE))
    assert raised.value.code is CrawlerErrorCode.PERMISSION_REQUIRED
    assert ("revoke", "access-token-value") in fake.calls
    assert oauth.vault.load() is None


def test_tiktok_oauth_rejects_a_different_authorized_creator(tmp_path) -> None:
    fake = FakeOAuthClient(
        oauth_config(),
        profile=TikTokUserProfile(OPEN_ID, "someone.else"),
    )
    oauth, _ = service(tmp_path, fake)
    oauth.begin("game.dev")

    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(oauth.complete("code", STATE))
    assert raised.value.code is CrawlerErrorCode.PERMISSION_REQUIRED
    assert "different creator" in raised.value.safe_message
    assert ("revoke", "access-token-value") in fake.calls
    assert oauth.vault.load() is None


def test_tiktok_oauth_refresh_rotates_both_tokens_and_disconnect_revokes(tmp_path) -> None:
    oauth, fake = service(tmp_path)
    oauth.vault.save(token_bundle(), authorized_username="game.dev")

    status = asyncio.run(oauth.refresh())
    stored = oauth.vault.load()
    assert status.state == "connected"
    assert stored is not None
    assert stored.access_token == "rotated-access"
    assert stored.refresh_token == "rotated-refresh"
    assert ("refresh", "refresh-token-value") in fake.calls

    asyncio.run(oauth.disconnect())
    assert ("revoke", "rotated-access") in fake.calls
    assert oauth.vault.load() is None


def test_tiktok_oauth_disconnect_refreshes_expired_access_before_revoke(tmp_path) -> None:
    oauth, fake = service(tmp_path)
    oauth.vault.save(
        token_bundle(),
        authorized_username="game.dev",
        issued_at=datetime.now(UTC) - timedelta(hours=2),
    )

    asyncio.run(oauth.disconnect())
    assert ("refresh", "refresh-token-value") in fake.calls
    assert ("revoke", "rotated-access") in fake.calls
    assert oauth.vault.load() is None


def test_tiktok_oauth_router_sets_secure_cookie_and_completes_callback(tmp_path) -> None:
    oauth, _ = service(tmp_path)
    app = FastAPI()
    app.include_router(build_tiktok_auth_router(oauth))

    with TestClient(app, base_url="https://api.example.test") as client:
        start = client.get(
            "/api/v1/auth/tiktok/start",
            params={"username": "game.dev"},
            follow_redirects=False,
        )
        assert start.status_code == 302
        assert start.headers["location"].startswith("https://www.tiktok.com/")
        cookie = start.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=lax" in cookie
        callback = client.get(
            "/api/v1/auth/tiktok/callback",
            params={"code": "single-use", "state": STATE},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        location = urlsplit(callback.headers["location"])
        params = parse_qs(location.query)
        assert params["tiktok_oauth"] == ["connected"]
        assert params["view"] == ["library"]
        assert oauth.vault.load() is not None
        assert OAUTH_COOKIE not in client.cookies


def test_tiktok_oauth_router_rejects_mismatched_cookie_without_exchange(tmp_path) -> None:
    oauth, fake = service(tmp_path)
    app = FastAPI()
    app.include_router(build_tiktok_auth_router(oauth))

    with TestClient(app, base_url="https://api.example.test") as client:
        oauth.begin("game.dev")
        client.cookies.set(OAUTH_COOKIE, "x" * 43)
        response = client.get(
            "/api/v1/auth/tiktok/callback",
            params={"code": "attacker-code", "state": STATE},
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert not any(call[0] == "exchange" for call in fake.calls)


def test_tiktok_oauth_start_requires_approved_callback_origin(tmp_path) -> None:
    oauth, fake = service(tmp_path)
    app = FastAPI()
    app.include_router(build_tiktok_auth_router(oauth))

    with TestClient(app, base_url="https://wrong.example.test") as client:
        response = client.get(
            "/api/v1/auth/tiktok/start",
            params={"username": "game.dev"},
            follow_redirects=False,
        )
    assert response.status_code == 409
    assert "same HTTPS origin" in response.json()["detail"]
    assert not fake.calls


def test_tiktok_oauth_state_rejects_non_ascii_without_internal_error() -> None:
    states = TikTokOAuthStateStore()
    with pytest.raises(CrawlerFailure) as raised:
        states.consume("ế" * 32)
    assert raised.value.code is CrawlerErrorCode.AUTH_REQUIRED


def test_tiktok_oauth_state_store_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError, match="limits"):
        TikTokOAuthStateStore(ttl_seconds=10)
