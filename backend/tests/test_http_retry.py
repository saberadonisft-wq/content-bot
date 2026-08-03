import asyncio

import httpx

from app.services import connectors
from app.services.connectors import get_with_retries


class FakeResponse:
    def __init__(self, status_code: int, retry_after: str | None = None):
        self.status_code = status_code
        self.headers = {"Retry-After": retry_after} if retry_after else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "failed",
                request=httpx.Request("GET", "https://example.test"),
                response=httpx.Response(self.status_code),
            )


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    async def get(self, _url: str, params=None):
        self.calls += 1
        return next(self.responses)


def test_get_retries_transient_status_and_honors_cap(monkeypatch) -> None:
    delays = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(connectors.asyncio, "sleep", fake_sleep)
    client = FakeClient([FakeResponse(429, "99"), FakeResponse(503), FakeResponse(200)])
    response = asyncio.run(get_with_retries(client, "https://example.test"))

    assert response.status_code == 200
    assert client.calls == 3
    assert delays == [5, 1.0]


def test_get_does_not_retry_non_transient_status(monkeypatch) -> None:
    async def fail_sleep(_delay: float) -> None:
        raise AssertionError("sleep should not be called")

    monkeypatch.setattr(connectors.asyncio, "sleep", fail_sleep)
    client = FakeClient([FakeResponse(404)])

    try:
        asyncio.run(get_with_retries(client, "https://example.test"))
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 404
    else:
        raise AssertionError("404 should be raised without retry")
    assert client.calls == 1
