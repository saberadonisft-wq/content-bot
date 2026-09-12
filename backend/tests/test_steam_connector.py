import asyncio

import httpx
import pytest

from app.config import settings
from app.services import channel_scans
from app.services import connector_steam as connectors
from app.services.connectors import SearchQuery, SteamReviewsConnector
from app.services.steam_reviews import (
    SteamRequestBudget,
    parse_steam_app_id,
    select_discovered_apps,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeSteamClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, params=None) -> FakeResponse:
        if "storesearch" in url:
            return FakeResponse({"items": [{"id": 42, "name": "Test Game"}]})
        return FakeResponse(
            {
                "success": 1,
                "reviews": [
                    {
                        "recommendationid": "1001",
                        "author": {"steamid": "private-id"},
                        "review": "A useful player review",
                        "language": "english",
                        "timestamp_created": 1_700_000_000,
                        "voted_up": True,
                        "votes_up": 12,
                        "comment_count": 3,
                    },
                    {
                        "recommendationid": "1002",
                        "review": "Second review",
                        "timestamp_created": 1_700_000_100,
                        "voted_up": False,
                    },
                ],
            }
        )


def test_steam_reviews_are_capped_and_anonymized(monkeypatch) -> None:
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: FakeSteamClient())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FakeSteamClient())
    connector = SteamReviewsConnector()

    async def collect():
        return [
            item
            async for item in connector.search(
                SearchQuery(keyword_id=1, name="Test Game", include_terms=["Test Game"], max_items=1),
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 1
    assert rows[0].author == "Steam reviewer"
    assert rows[0].metrics == {"like_count": 12, "comment_count": 3}
    assert "author" not in rows[0].raw_payload


class AliasSteamClient(FakeSteamClient):
    def __init__(self):
        self.search_terms = []

    async def get(self, url: str, params=None) -> FakeResponse:
        if "storesearch" in url:
            self.search_terms.append(params["term"])
            if params["term"] == "Alias Game":
                return FakeResponse({"items": [{"id": 99, "name": "Alias Game"}]})
            return FakeResponse({"items": []})
        return await super().get(url, params)


def test_steam_search_discovers_alias_only_game(monkeypatch) -> None:
    client = AliasSteamClient()
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in SteamReviewsConnector().search(
                SearchQuery(
                    keyword_id=1,
                    name="Primary Name",
                    include_terms=["Primary Name", "Alias Game"],
                    max_items=4,
                )
            )
        ]

    rows = asyncio.run(collect())
    assert rows
    assert all("Alias Game" in row.title for row in rows)
    assert client.search_terms == ["Primary Name", "Alias Game"]


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


def review(index: int, *, app_id: str = "42") -> dict:
    return {
        "recommendationid": str(index),
        "author": {
            "steamid": f"private-{index}",
            "num_games_owned": 999,
            "playtime_forever": 12345,
        },
        "review": f"Review {index}",
        "language": "english",
        "timestamp_created": 1_700_000_000 + index,
        "timestamp_updated": 1_700_000_100 + index,
        "voted_up": index % 2 == 0,
        "votes_up": index,
        "votes_funny": 2,
        "weighted_vote_score": "0.75",
        "comment_count": 3,
        "received_for_free": False,
        "written_during_early_access": True,
        "primarily_steam_deck": False,
        "unexpected_identity": f"must-not-persist-{app_id}",
    }


class PagedSteamClient:
    def __init__(self, *, repeated_cursor=False, frontier_known=False) -> None:
        self.calls = []
        self.repeated_cursor = repeated_cursor
        self.frontier_known = frontier_known

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, params=None) -> FakeResponse:
        params = dict(params or {})
        self.calls.append((url, params))
        if "storesearch" in url:
            return FakeResponse(
                {
                    "items": [
                        {"id": 42, "name": "Test Game"},
                        {"id": 43, "name": "Test Game Demo"},
                    ]
                }
            )
        cursor = params.get("cursor")
        count = int(params.get("num_per_page") or 100)
        if self.frontier_known and cursor == "*":
            reviews = [review(1), review(2)]
            next_cursor = "frontier-next"
        elif cursor == "backlog":
            reviews = [review(index) for index in range(101, 101 + count)]
            next_cursor = "backlog-next"
        elif cursor == "page-2":
            reviews = [review(index) for index in range(101, 101 + count)]
            next_cursor = "page-2" if self.repeated_cursor else "page-3"
        else:
            reviews = [review(index) for index in range(1, count + 1)]
            next_cursor = "*" if self.repeated_cursor else "page-2"
        return FakeResponse(
            {"success": 1, "reviews": reviews, "cursor": next_cursor}
        )


def test_steam_discovery_prefers_exact_game_over_demo_and_rejects_tie() -> None:
    selected, ambiguous = select_discovered_apps(
        "Test Game",
        [
            {"id": 43, "name": "Test Game Demo"},
            {"id": 42, "name": "Test Game"},
        ],
        limit=3,
    )
    assert ambiguous is False
    assert selected == [{"id": 42, "name": "Test Game"}]

    selected, ambiguous = select_discovered_apps(
        "Game",
        [
            {"id": 1, "name": "Game Alpha"},
            {"id": 2, "name": "Game Beta"},
        ],
        limit=3,
    )
    assert selected == []
    assert ambiguous is True


def test_steam_target_requires_numeric_app_id() -> None:
    assert parse_steam_app_id("https://store.steampowered.com/app/42/Test_Game/") == "42"
    assert parse_steam_app_id("https://steamcommunity.com/games/42/") == "42"
    with pytest.raises(ValueError):
        parse_steam_app_id("https://store.steampowered.com/dlc/42/")


def test_steam_keyword_frontier_precedes_backlog_and_raw_is_minimized(
    monkeypatch,
) -> None:
    client = PagedSteamClient(frontier_known=True)
    tracker = FakeTracker(recent_ids=("42:1", "42:2"), cursor="backlog")
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in SteamReviewsConnector().search(
                SearchQuery(1, "Test Game", [], 2, checkpoint_tracker=tracker)
            )
        ]

    rows = asyncio.run(collect())
    review_calls = [params for url, params in client.calls if "appreviews" in url]
    assert [params["cursor"] for params in review_calls] == ["*", "backlog"]
    assert [row.external_id for row in rows] == ["42:101", "42:102"]
    assert rows[0].raw_payload["helpful_votes"] == 101
    assert "author" not in rows[0].raw_payload
    assert "private-101" not in repr(rows[0])
    assert "unexpected_identity" not in rows[0].raw_payload
    assert tracker.reported[-1][1] == "backlog-next"


def test_steam_saved_app_paginates_past_100_with_exact_cap_and_privacy(
    monkeypatch,
) -> None:
    client = PagedSteamClient()
    tracker = FakeTracker()
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in channel_scans._scan_steam(
                {
                    "url": "https://store.steampowered.com/app/42/Test_Game/",
                    "label": "Test Game",
                },
                SearchQuery(1, "Test Game", [], 150, checkpoint_tracker=tracker),
            )
        ]

    rows = asyncio.run(collect())
    review_calls = [params for url, params in client.calls if "appreviews" in url]
    assert len(rows) == 150
    assert [params["num_per_page"] for params in review_calls] == [100, 50]
    assert rows[0].raw_payload.keys() == rows[-1].raw_payload.keys()
    assert all("author" not in row.raw_payload for row in rows)
    assert tracker.reported[-1][1] == "page-3"


def test_steam_repeated_cursor_stops_without_loop(monkeypatch) -> None:
    client = PagedSteamClient(repeated_cursor=True)
    tracker = FakeTracker()
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in channel_scans._scan_steam(
                {"url": "https://store.steampowered.com/app/42/", "label": "Test Game"},
                SearchQuery(1, "Test Game", [], 150, checkpoint_tracker=tracker),
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 100
    assert len(client.calls) == 1
    assert tracker.reported[-1][1] is None


def test_steam_request_budgets_are_separate() -> None:
    budget = SteamRequestBudget(1, 2)
    assert budget.spend_discovery() is True
    assert budget.spend_discovery() is False
    assert budget.spend_review() is True
    assert budget.spend_review() is True
    assert budget.spend_review() is False


def test_steam_checkpoint_semantics_include_filter_language_and_purchase(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "steam_review_filter", "recent")
    monkeypatch.setattr(settings, "steam_review_language", "vietnamese")
    monkeypatch.setattr(settings, "steam_purchase_type", "steam")
    fields = SteamReviewsConnector().checkpoint_fingerprint_fields("search")
    assert fields["review_filter"] == "recent"
    assert fields["language"] == "vietnamese"
    assert fields["purchase_type"] == "steam"


def test_steam_search_caches_discovered_app(monkeypatch) -> None:
    storesearch_calls = 0

    class TrackingSteamClient(FakeSteamClient):
        async def get(self, url: str, params=None) -> FakeResponse:
            nonlocal storesearch_calls
            if "storesearch" in url:
                storesearch_calls += 1
                return FakeResponse({"items": [{"id": 42, "name": "Cached Game"}]})
            return await super().get(url, params)

    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: TrackingSteamClient())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: TrackingSteamClient())
    connector = SteamReviewsConnector()

    async def run_searches():
        q1 = SearchQuery(keyword_id=1, name="Cached Game", include_terms=["Cached Game"], max_items=1)
        async for _ in connector.search(q1):
            pass
        # Second search for the same term
        q2 = SearchQuery(keyword_id=1, name="Cached Game", include_terms=["Cached Game"], max_items=1)
        async for _ in connector.search(q2):
            pass

    asyncio.run(run_searches())
    # storesearch should have only been called once due to discovery cache!
    assert storesearch_calls == 1

