from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.config import settings
from app.crawlers.adapters.tiktok import (
    TikTokTokenBundle,
    TikTokTokenVault,
    TikTokVideo,
    TikTokVideoPage,
)
from app.crawlers.runtime import IdentityPseudonymizer
from app.services.channel_scans import channel_mode, normalize_channel
from app.services.connectors import (
    SearchQuery,
    TikTokDisplayConnector,
    default_connectors,
)

OPEN_ID = "authorized-open-id-1234"
VIDEO_ID = "7123456789012345678"


class FakeProvider:
    def __init__(self) -> None:
        self.closed = False
        self.calls = []

    async def open(self, context, cancellation):
        self.calls.append((context.operation, dict(context.target)))

    async def list_videos(self, **kwargs):
        self.calls.append(kwargs)
        return TikTokVideoPage(
            (
                TikTokVideo(
                    VIDEO_ID,
                    f"https://www.tiktok.com/@game.dev/video/{VIDEO_ID}",
                    title="Authorized creator video",
                    created_at=datetime(2026, 8, 13, tzinfo=UTC),
                    metrics={"view_count": 10},
                ),
            ),
            None,
        )

    async def query_videos(self, video_ids):
        return ()

    async def close(self):
        self.closed = True


def configure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tiktok_user_access_token", "access-token")
    monkeypatch.setattr(settings, "tiktok_open_id", OPEN_ID)
    monkeypatch.setattr(settings, "tiktok_authorized_username", "game.dev")
    monkeypatch.setattr(
        settings, "tiktok_granted_scopes", "user.info.basic,video.list"
    )


def test_default_registry_uses_real_tiktok_display_connector() -> None:
    connector = default_connectors()["tiktok"]
    assert isinstance(connector, TikTokDisplayConnector)
    assert connector.capabilities.global_search is False
    assert connector.capabilities.watchlist_filter is True


def test_tiktok_channel_mode_is_scope_and_token_gated(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tiktok_user_access_token", None)
    monkeypatch.setattr(settings, "tiktok_open_id", "")
    monkeypatch.setattr(settings, "tiktok_authorized_username", "")
    monkeypatch.setattr(settings, "tiktok_granted_scopes", "")
    assert channel_mode("tiktok") == "setup_required"

    configure(monkeypatch)
    assert channel_mode("tiktok") == "api"
    channel = normalize_channel({"url": "https://www.tiktok.com/@game.dev"})
    assert channel["source_id"] == "tiktok"
    assert channel["mode"] == "api"
    with pytest.raises(ValueError, match="content targets"):
        normalize_channel(
            {"url": f"https://www.tiktok.com/@game.dev/video/{VIDEO_ID}"}
        )


def test_tiktok_connector_scans_only_the_authorized_creator(monkeypatch) -> None:
    configure(monkeypatch)
    connector = TikTokDisplayConnector()
    provider = FakeProvider()
    monkeypatch.setattr(connector, "_provider", lambda _config: provider)
    monkeypatch.setattr(
        connector,
        "_pseudonymizer",
        lambda: IdentityPseudonymizer(b"k" * 32),
    )
    query = SearchQuery(1, "game", [], 20)

    async def run():
        return [
            item
            async for item in connector.scan_channel(
                {
                    "url": "https://www.tiktok.com/@game.dev",
                    "normalized_url": "https://www.tiktok.com/@game.dev",
                },
                query,
            )
        ]

    items = asyncio.run(run())
    assert len(items) == 1
    assert items[0].external_id == VIDEO_ID
    assert items[0].raw_payload == {
        "provider_id": "tiktok_display",
        "access_basis": "authorized_creator",
    }
    assert provider.calls[1] == {"cursor_ms": None, "max_count": 20}
    assert provider.closed is True


def test_tiktok_connector_rejects_an_arbitrary_creator(monkeypatch) -> None:
    configure(monkeypatch)
    connector = TikTokDisplayConnector()

    async def run():
        with pytest.raises(RuntimeError, match="only the creator"):
            async for _ in connector.scan_channel(
                {
                    "url": "https://www.tiktok.com/@someone.else",
                    "normalized_url": "https://www.tiktok.com/@someone.else",
                },
                SearchQuery(1, "game", [], 20),
            ):
                pass

    asyncio.run(run())


def test_tiktok_connector_loads_encrypted_vault_credential(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path)
    monkeypatch.setattr(settings, "tiktok_user_access_token", None)
    monkeypatch.setattr(settings, "tiktok_open_id", "")
    monkeypatch.setattr(settings, "tiktok_authorized_username", "")
    monkeypatch.setattr(settings, "tiktok_granted_scopes", "")
    vault = TikTokTokenVault(tmp_path / "crawler-secrets" / "tiktok")
    vault.save(
        TikTokTokenBundle(
            OPEN_ID,
            frozenset({"user.info.basic", "user.info.profile", "video.list"}),
            "vault-access-token",
            "vault-refresh-token",
            3600,
            86_400,
        ),
        authorized_username="game.dev",
    )

    connector = TikTokDisplayConnector()
    assert connector.configured is True
    assert channel_mode("tiktok") == "api"
    config = asyncio.run(connector._display_config())
    assert config.open_id == OPEN_ID
    assert config.access_token == "vault-access-token"


def test_tiktok_connector_rotates_near_expiry_vault_token(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path)
    monkeypatch.setattr(settings, "tiktok_user_access_token", None)
    monkeypatch.setattr(settings, "tiktok_open_id", "")
    monkeypatch.setattr(settings, "tiktok_authorized_username", "")
    monkeypatch.setattr(settings, "tiktok_granted_scopes", "")
    monkeypatch.setattr(settings, "tiktok_client_key", "client-key-1234")
    monkeypatch.setattr(settings, "tiktok_client_secret", "client-secret")
    monkeypatch.setattr(
        settings,
        "tiktok_redirect_uri",
        "https://content.example.test/api/v1/tiktok/oauth/callback",
    )
    vault = TikTokTokenVault(tmp_path / "crawler-secrets" / "tiktok")
    vault.save(
        TikTokTokenBundle(
            OPEN_ID,
            frozenset({"user.info.basic", "user.info.profile", "video.list"}),
            "expiring-access-token",
            "current-refresh-token",
            60,
            86_400,
        ),
        authorized_username="game.dev",
    )

    class FakeOAuthClient:
        refresh_value = ""

        def __init__(self, config):
            self.config = config

        async def refresh(self, refresh_token):
            type(self).refresh_value = refresh_token
            return TikTokTokenBundle(
                OPEN_ID,
                frozenset(
                    {"user.info.basic", "user.info.profile", "video.list"}
                ),
                "rotated-access-token",
                "rotated-refresh-token",
                3600,
                86_400,
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        "app.services.connectors.TikTokOAuthClient", FakeOAuthClient
    )
    config = asyncio.run(TikTokDisplayConnector()._display_config())

    assert FakeOAuthClient.refresh_value == "current-refresh-token"
    assert config.access_token == "rotated-access-token"
    reloaded = vault.load()
    assert reloaded is not None
    assert reloaded.refresh_token == "rotated-refresh-token"
    assert b"rotated-access-token" not in vault.token_path.read_bytes()
