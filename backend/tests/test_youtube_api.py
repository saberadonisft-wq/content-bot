from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from app.services import connector_youtube as connectors
from app.services.connectors import ConnectorStatus, YouTubeConnector
from app.services.youtube_api import YouTubeQuotaBudget, youtube_json


class FakeClient:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(status: int, payload) -> httpx.Response:
    return httpx.Response(status, json=payload)


@pytest.mark.parametrize(
    ("status", "reason", "code"),
    [
        (403, "quotaExceeded", CrawlerErrorCode.RATE_LIMITED),
        (403, "keyInvalid", CrawlerErrorCode.AUTH_REQUIRED),
        (400, "invalidPageToken", CrawlerErrorCode.PARSE_CHANGED),
        (404, "notFound", CrawlerErrorCode.NOT_FOUND),
    ],
)
def test_youtube_api_errors_are_typed_and_redacted(status, reason, code) -> None:
    client = FakeClient(
        [
            response(
                status,
                {
                    "error": {
                        "errors": [{"reason": reason}],
                        "message": "provider-secret-detail",
                    }
                },
            )
        ]
    )
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(
            youtube_json(client, "/search", params={"key": "secret"}, attempts=1)
        )
    assert raised.value.code is code
    assert raised.value.details == {"provider_reason": reason}
    assert "provider-secret" not in str(raised.value)
    assert "secret" not in str(raised.value)


def test_youtube_api_retries_transient_transport(monkeypatch) -> None:
    client = FakeClient(
        [
            httpx.ConnectError("network detail"),
            response(200, {"items": []}),
        ]
    )

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("app.services.youtube_api.asyncio.sleep", no_sleep)
    assert asyncio.run(youtube_json(client, "/videos", params={"key": "secret"})) == {
        "items": []
    }
    assert len(client.calls) == 2


def test_youtube_quota_budget_tracks_separate_request_buckets() -> None:
    budget = YouTubeQuotaBudget(max_search_requests=1, max_general_requests=2)
    assert budget.spend("/search") is True
    assert budget.spend("/search") is False
    assert budget.spend("/videos") is True
    assert budget.spend("/channels") is True
    assert budget.spend("/playlistItems") is False
    assert budget.search_requests == 1
    assert budget.general_requests == 2


def test_youtube_deep_health_uses_one_unit_probe(monkeypatch) -> None:
    client = FakeClient([response(200, {"items": []})])
    kwargs = {}

    def factory(**values):
        kwargs.update(values)
        return client

    monkeypatch.setattr(settings, "youtube_api_key", "test-key")
    monkeypatch.setattr(connectors, "pooled_client", factory)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    status = asyncio.run(YouTubeConnector().deep_healthcheck())

    assert status.state == "ready"
    assert status.probe == "remote"
    assert client.calls == [("/i18nLanguages", {"part": "snippet"})]
    assert kwargs["follow_redirects"] is False


def test_youtube_deep_health_reports_quota_without_claiming_ready(monkeypatch) -> None:
    client = FakeClient(
        [
            response(
                403,
                {"error": {"errors": [{"reason": "quotaExceeded"}]}},
            )
        ]
    )
    monkeypatch.setattr(settings, "youtube_api_key", "test-key")
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    status = asyncio.run(YouTubeConnector().deep_healthcheck())
    assert status.state == "rate_limited"
    assert status.reason_code == CrawlerErrorCode.RATE_LIMITED.value
    assert status.probe == "remote"


def test_catalog_deep_probe_disables_rate_limited_youtube_operations(
    application_services, monkeypatch
) -> None:
    async def rate_limited():
        return ConnectorStatus(
            "rate_limited",
            "YouTube API quota or request limit was reached.",
            CrawlerErrorCode.RATE_LIMITED.value,
            "remote",
        )

    monkeypatch.setattr(
        application_services.connectors["youtube"],
        "deep_healthcheck",
        rate_limited,
    )
    with TestClient(main.app) as client:
        response = client.get("/api/v1/sources", params={"deep": True})
    assert response.status_code == 200
    youtube = next(row for row in response.json() if row["id"] == "youtube")
    assert youtube["health_summary"]["state"] == "rate_limited"
    assert youtube["health_summary"]["probe"] == "remote"
    assert all(
        operation["availability"] == "rate_limited"
        and operation["reason_code"] == CrawlerErrorCode.RATE_LIMITED.value
        and operation["enabled"] is False
        for operation in youtube["operations"]
    )
