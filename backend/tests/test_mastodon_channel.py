import asyncio

from app.config import settings
from app.services import channel_scans
from app.services.channel_scans import normalize_channel, scan_channel
from app.services.connectors import SearchQuery


class Response:
    def __init__(self, payload, *, headers=None):
        self.payload = payload
        self.status_code = 200
        self.headers = headers or {}

    def json(self):
        return self.payload


class Factory:
    def __init__(self, responses):
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls = []

    def __call__(self, **kwargs):
        factory = self

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, endpoint, params=None):
                factory.calls.append((str(kwargs.get("base_url")), endpoint, params))
                key = "lookup" if endpoint.endswith("/lookup") else "statuses"
                return factory.responses[key].pop(0)

        return Client()


def _post(index: int) -> dict:
    return {
        "id": str(1_000 - index),
        "uri": f"https://mastodon.social/users/player/statuses/{index}",
        "url": f"https://mastodon.social/@player/{index}",
        "content": f"<p>Post {index}</p>",
        "created_at": "2026-08-13T01:00:00Z",
        "account": {"acct": "player"},
    }


def test_saved_account_paginates_beyond_40_with_exact_cap(monkeypatch) -> None:
    first = Response(
        [_post(index) for index in range(40)],
        headers={
            "Link": '<https://mastodon.social/api/v1/accounts/42/statuses?max_id=older>; rel="next"'
        },
    )
    second = Response([_post(index) for index in range(40, 50)])
    factory = Factory(
        {
            "lookup": [Response({"id": "42"})],
            "statuses": [first, second],
        }
    )
    monkeypatch.setattr(channel_scans.httpx, "AsyncClient", factory)
    monkeypatch.setattr(settings, "mastodon_instances", "mastodon.social")
    channel = normalize_channel(
        {
            "url": "https://mastodon.social/@player",
            "include_replies": True,
            "include_reposts": False,
        }
    )

    async def collect():
        return [
            item
            async for item in scan_channel(
                channel,
                SearchQuery(keyword_id=1, name="Game", include_terms=[], max_items=45),
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 45
    status_calls = [call for call in factory.calls if "/statuses" in call[1]]
    assert len(status_calls) == 2
    assert status_calls[0][2]["exclude_replies"] == "false"
    assert status_calls[0][2]["exclude_reblogs"] == "true"
    assert status_calls[1][2]["max_id"] == "older"
    assert all("account" not in row.raw_payload for row in rows)


def test_saved_account_repeated_link_cursor_stops(monkeypatch) -> None:
    repeated = {
        "Link": '<https://mastodon.social/api/v1/accounts/42/statuses?max_id=same>; rel="next"'
    }
    factory = Factory(
        {
            "lookup": [Response({"id": "42"})],
            "statuses": [Response([_post(1)], headers=repeated), Response([_post(2)], headers=repeated)],
        }
    )
    monkeypatch.setattr(channel_scans.httpx, "AsyncClient", factory)
    monkeypatch.setattr(settings, "mastodon_instances", "mastodon.social")
    warnings = []

    async def warning(code, message):
        warnings.append((code, message))

    async def collect():
        return [
            item
            async for item in scan_channel(
                normalize_channel({"url": "https://mastodon.social/@player"}),
                SearchQuery(
                    keyword_id=1,
                    name="Game",
                    include_terms=[],
                    max_items=10,
                    warning_callback=warning,
                ),
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 2
    assert warnings[0][0] == "MASTODON_CURSOR_STALLED"
    assert len([call for call in factory.calls if "/statuses" in call[1]]) == 2
