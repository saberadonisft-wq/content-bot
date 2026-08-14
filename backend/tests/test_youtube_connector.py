import asyncio

from app.config import settings
from app.crawlers import SOURCE_REGISTRY
from app.crawlers.contracts import AuthMode, Coverage, Operation
from app.services import channel_scans, connectors
from app.services.connectors import SearchQuery, YouTubeConnector


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeTracker:
    def __init__(self, *, recent_ids=(), cursor=None) -> None:
        self.recent_ids = tuple(recent_ids)
        self.cursor_value = cursor
        self.reported = []

    def cursor(self, scope, default=None):
        del scope
        return self.cursor_value if self.cursor_value is not None else default

    def report_cursor(self, scope, value) -> None:
        self.reported.append((scope, value))


def video_detail(video_id: str) -> dict:
    return {
        "id": video_id,
        "snippet": {
            "title": f"Video {video_id}",
            "description": "Public description",
            "publishedAt": "2026-08-01T08:00:00+00:00",
            "channelId": "channel-public",
            "channelTitle": "Public channel",
            "tags": ["game", "#news"],
        },
        "statistics": {"viewCount": "10", "likeCount": "2"},
    }


class PagedYouTubeClient:
    def __init__(self, *, channel=False, invalid_backlog=False) -> None:
        self.channel = channel
        self.invalid_backlog = invalid_backlog
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, params=None):
        params = dict(params or {})
        self.calls.append((url, params))
        if url == "/channels":
            if params.get("part") == "snippet,contentDetails":
                return FakeResponse({"items": [{"id": "UC-test"}]})
            return FakeResponse(
                {
                    "items": [
                        {
                            "contentDetails": {
                                "relatedPlaylists": {"uploads": "UU-test"}
                            }
                        }
                    ]
                }
            )
        if url in {"/search", "/playlistItems"}:
            token = params.get("pageToken")
            if token == "backlog" and self.invalid_backlog:
                import httpx

                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "errors": [{"reason": "invalidPageToken"}],
                            "message": "must not leak",
                        }
                    },
                )
            ids, next_token = (
                (["known-1", "known-2"], "frontier-next")
                if token is None
                else (["backlog-1", "backlog-2"], "backlog-next")
            )
            if url == "/search":
                items = [{"id": {"videoId": video_id}} for video_id in ids]
            else:
                items = [
                    {"contentDetails": {"videoId": video_id}} for video_id in ids
                ]
            return FakeResponse({"items": items, "nextPageToken": next_token})
        ids = str(params.get("id") or "").split(",")
        return FakeResponse({"items": [video_detail(video_id) for video_id in ids if video_id]})


class FakeYouTubeClient:
    def __init__(self, **_kwargs):
        self.search_query = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, params=None) -> FakeResponse:
        if url == "/search":
            self.search_query = params["q"]
            return FakeResponse({"items": [{"id": {"videoId": "video-1"}}]})
        return FakeResponse(
            {
                "items": [
                    {
                        "id": "video-1",
                        "snippet": {
                            "title": "Hades 2 update",
                            "description": "Alias-only coverage",
                            "publishedAt": "2026-08-01T08:00:00+00:00",
                        },
                        "statistics": {"viewCount": "10"},
                    }
                ]
            }
        )


def test_youtube_search_combines_primary_name_and_aliases(monkeypatch) -> None:
    client = FakeYouTubeClient()
    monkeypatch.setattr(settings, "youtube_api_key", "test-key")
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in YouTubeConnector().search(
                SearchQuery(
                    keyword_id=1,
                    name="Hades II",
                    include_terms=["Hades II", "Hades 2", "hades 2"],
                    max_items=4,
                )
            )
        ]

    rows = asyncio.run(collect())
    assert [row.external_id for row in rows] == ["video-1"]
    assert client.search_query == "Hades II|Hades 2"


def test_youtube_search_checks_frontier_before_resuming_backlog(monkeypatch) -> None:
    client = PagedYouTubeClient()
    tracker = FakeTracker(recent_ids=("known-1", "known-2"), cursor="backlog")
    monkeypatch.setattr(settings, "youtube_api_key", "test-key")
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in YouTubeConnector().search(
                SearchQuery(
                    keyword_id=1,
                    name="game",
                    include_terms=[],
                    max_items=2,
                    checkpoint_tracker=tracker,
                )
            )
        ]

    rows = asyncio.run(collect())
    search_calls = [params for url, params in client.calls if url == "/search"]
    video_calls = [params for url, params in client.calls if url == "/videos"]

    assert [row.external_id for row in rows] == ["backlog-1", "backlog-2"]
    assert [params.get("pageToken") for params in search_calls] == [None, "backlog"]
    assert len(video_calls) == 1
    assert rows[0].hashtags == ["#game", "#news"]
    assert rows[0].raw_payload == {
        "provider_id": "youtube_data_v3",
        "video_id": "backlog-1",
        "channel_id": "channel-public",
        "category_id": "",
        "live_broadcast_content": "",
    }
    assert tracker.reported[-1][1] == "backlog-next"


def test_youtube_search_discards_expired_backlog_cursor(monkeypatch) -> None:
    client = PagedYouTubeClient(invalid_backlog=True)
    tracker = FakeTracker(recent_ids=("known-1", "known-2"), cursor="backlog")
    monkeypatch.setattr(settings, "youtube_api_key", "test-key")
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in YouTubeConnector().search(
                SearchQuery(1, "game", [], 2, checkpoint_tracker=tracker)
            )
        ]

    assert asyncio.run(collect()) == []
    assert tracker.reported[-1][1] is None


def test_youtube_search_preserves_backlog_when_run_quota_is_exhausted(
    monkeypatch,
) -> None:
    client = PagedYouTubeClient()
    tracker = FakeTracker(recent_ids=("known-1", "known-2"), cursor="backlog")
    monkeypatch.setattr(settings, "youtube_api_key", "test-key")
    monkeypatch.setattr(settings, "youtube_search_request_budget", 1)
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in YouTubeConnector().search(
                SearchQuery(1, "game", [], 2, checkpoint_tracker=tracker)
            )
        ]

    assert asyncio.run(collect()) == []
    assert [url for url, _params in client.calls] == ["/search"]
    assert tracker.reported[-1][1] == "backlog"


def test_youtube_saved_channel_uses_same_frontier_backlog_policy(monkeypatch) -> None:
    client = PagedYouTubeClient(channel=True)
    tracker = FakeTracker(recent_ids=("known-1", "known-2"), cursor="backlog")
    monkeypatch.setattr(settings, "youtube_api_key", "test-key")
    monkeypatch.setattr(connectors.httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in channel_scans._scan_youtube(
                {"url": "https://www.youtube.com/channel/UC-test"},
                SearchQuery(1, "game", [], 2, checkpoint_tracker=tracker),
            )
        ]

    rows = asyncio.run(collect())
    playlist_calls = [
        params for url, params in client.calls if url == "/playlistItems"
    ]
    assert [row.external_id for row in rows] == ["backlog-1", "backlog-2"]
    assert [params.get("pageToken") for params in playlist_calls] == [None, "backlog"]
    assert tracker.reported[-1][1] == "backlog-next"


def test_youtube_manifest_declares_partial_api_key_operations() -> None:
    specs = {}
    for operation in (Operation.SEARCH, Operation.SCAN_CHANNEL):
        entries = SOURCE_REGISTRY.operation_specs("youtube", operation)
        assert len(entries) == 1
        specs[operation] = entries[0][1]
    assert specs[Operation.SEARCH].coverage is Coverage.PARTIAL
    assert specs[Operation.SEARCH].auth_modes == (AuthMode.API_KEY,)
    assert specs[Operation.SCAN_CHANNEL].auth_modes == (AuthMode.API_KEY,)
    assert YouTubeConnector.capabilities.watchlist_filter is True


def test_youtube_checkpoint_fingerprint_fields_include_query_semantics(monkeypatch) -> None:
    monkeypatch.setattr(settings, "youtube_region_code", "vn")
    monkeypatch.setattr(settings, "youtube_relevance_language", "VI")
    assert YouTubeConnector().checkpoint_fingerprint_fields("search") == {
        "provider_contract": "youtube-data-v3-search-v2",
        "order": "date",
        "region": "VN",
        "language": "vi",
        "time_window": "rolling_90d",
    }
