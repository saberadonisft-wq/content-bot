from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.config import settings
from app.crawlers.adapters.facebook import (
    FacebookPagePost,
    FacebookPagePostPage,
)
from app.crawlers.runtime import IdentityPseudonymizer
from app.services.channel_scans import channel_mode, normalize_channel
from app.services.connectors import (
    FacebookPageConnector,
    SearchQuery,
    default_connectors,
)

PAGE_ID = "123456789012345"
POST_ID = f"{PAGE_ID}_987654321098765"


def configure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "meta_graph_api_version", "v99.0")
    monkeypatch.setattr(settings, "facebook_page_access_token", "page-token")
    monkeypatch.setattr(settings, "facebook_page_id", PAGE_ID)
    monkeypatch.setattr(settings, "facebook_page_username", "GameStudio")


class FakeProvider:
    def __init__(self) -> None:
        self.calls = []
        self.closed = False

    async def open(self, context, cancellation):
        self.calls.append((context.operation, dict(context.target)))

    async def page_feed(self, **kwargs):
        self.calls.append(kwargs)
        return FacebookPagePostPage(
            (
                FacebookPagePost(
                    POST_ID,
                    "https://www.facebook.com/GameStudio/posts/987654321098765/",
                    message="Authorized Page post",
                    author_id=PAGE_ID,
                    published_at=datetime(2026, 8, 13, tzinfo=UTC),
                    metrics={"reaction_count": 5},
                ),
            ),
            None,
        )

    async def close(self):
        self.closed = True


def test_default_registry_uses_real_facebook_page_connector() -> None:
    connector = default_connectors()["facebook"]
    assert isinstance(connector, FacebookPageConnector)
    assert connector.capabilities.global_search is False
    assert connector.capabilities.watchlist_filter is True


def test_facebook_channel_mode_and_normalization_are_permission_gated(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "facebook_page_access_token", None)
    monkeypatch.setattr(settings, "facebook_page_id", "")
    monkeypatch.setattr(settings, "facebook_page_username", "")
    assert channel_mode("facebook") == "setup_required"

    configure(monkeypatch)
    assert channel_mode("facebook") == "api"
    channel = normalize_channel({"url": "https://m.facebook.com/GameStudio/"})
    assert channel["source_id"] == "facebook"
    assert channel["normalized_url"] == "https://www.facebook.com/GameStudio/"
    assert channel["mode"] == "api"
    with pytest.raises(ValueError, match="not Page channels"):
        normalize_channel(
            {
                "url": "https://www.facebook.com/GameStudio/posts/987654321098765/"
            }
        )


def test_facebook_connector_scans_only_the_authorized_page(monkeypatch) -> None:
    configure(monkeypatch)
    connector = FacebookPageConnector()
    provider = FakeProvider()
    monkeypatch.setattr(connector, "_provider", lambda _config: provider)
    monkeypatch.setattr(
        connector,
        "_pseudonymizer",
        lambda: IdentityPseudonymizer(b"k" * 32),
    )

    async def run():
        return [
            item
            async for item in connector.scan_channel(
                {
                    "url": "https://www.facebook.com/GameStudio/",
                    "normalized_url": "https://www.facebook.com/GameStudio/",
                },
                SearchQuery(1, "game", [], 20),
            )
        ]

    items = asyncio.run(run())
    assert len(items) == 1
    assert items[0].external_id == POST_ID
    assert items[0].raw_payload == {
        "provider_id": "meta_pages",
        "access_basis": "authorized_page",
        "coverage": "partial",
    }
    assert provider.calls[1] == {"after": None, "limit": 20}
    assert provider.closed is True


def test_facebook_connector_rejects_arbitrary_page(monkeypatch) -> None:
    configure(monkeypatch)
    connector = FacebookPageConnector()

    async def run():
        with pytest.raises(RuntimeError, match="only the explicitly authorized"):
            async for _ in connector.scan_channel(
                {
                    "url": "https://www.facebook.com/AnotherPage/",
                    "normalized_url": "https://www.facebook.com/AnotherPage/",
                },
                SearchQuery(1, "game", [], 20),
            ):
                pass

    asyncio.run(run())
