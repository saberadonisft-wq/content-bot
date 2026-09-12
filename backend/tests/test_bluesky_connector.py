import asyncio

import httpx

from app.config import settings
from app.services import channel_scans
from app.services import connector_bluesky as connectors
from app.services.connectors import BlueskyConnector, SearchQuery, stable_external_id


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeBlueskyClient:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, params=None) -> FakeResponse:
        self.calls.append((url, params))
        return FakeResponse(
            {
                "cursor": "next-page",
                "posts": [
                    {
                        "uri": "at://did:plc:sensitive/app.bsky.feed.post/abc123",
                        "cid": "public-content-id",
                        "author": {
                            "did": "did:plc:sensitive",
                            "handle": "player.bsky.social",
                            "displayName": "Player",
                        },
                        "record": {
                            "text": "Hades II is excellent\nMore detail",
                            "createdAt": "2026-08-01T08:00:00.000Z",
                            "langs": ["en"],
                            "facets": [
                                {
                                    "features": [
                                        {
                                            "$type": "app.bsky.richtext.facet#tag",
                                            "tag": "Hades2",
                                        }
                                    ]
                                }
                            ],
                        },
                        "likeCount": 12,
                        "replyCount": 3,
                        "repostCount": 2,
                        "quoteCount": 1,
                    }
                ],
            }
        )


def test_bluesky_search_is_capped_and_omits_profile_payload(monkeypatch) -> None:
    client = FakeBlueskyClient()
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in BlueskyConnector().search(
                SearchQuery(keyword_id=1, name="Hades II", include_terms=["Hades II"], max_items=1)
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 1
    assert rows[0].canonical_url == "https://bsky.app/profile/player.bsky.social/post/abc123"
    assert rows[0].author == "player.bsky.social"
    assert rows[0].hashtags == ["#Hades2"]
    assert rows[0].metrics == {"like_count": 12, "comment_count": 3, "share_count": 3}
    assert "did" not in rows[0].raw_payload
    assert "author" not in rows[0].raw_payload
    assert rows[0].raw_payload["reply_count"] == 3
    assert rows[0].raw_payload["repost_count"] == 2
    assert rows[0].raw_payload["quote_count"] == 1
    assert client.calls[0][1]["limit"] == 1


class AliasBlueskyClient(FakeBlueskyClient):
    async def get(self, url: str, params=None) -> FakeResponse:
        self.calls.append((url, params))
        if params["q"] != "Hades 2":
            return FakeResponse({"posts": []})
        return FakeResponse(
            {
                "posts": [
                    {
                        "uri": "at://did:plc:test/app.bsky.feed.post/alias123",
                        "cid": "alias-content-id",
                        "author": {"handle": "alias-player.bsky.social"},
                        "record": {
                            "text": "Hades 2 build discussion",
                            "createdAt": "2026-08-01T08:00:00Z",
                        },
                    }
                ]
            }
        )


def test_bluesky_search_discovers_alias_only_posts(monkeypatch) -> None:
    client = AliasBlueskyClient()
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in BlueskyConnector().search(
                SearchQuery(
                    keyword_id=1,
                    name="Hades II",
                    include_terms=["Hades II", "Hades 2"],
                    max_items=4,
                )
            )
        ]

    rows = asyncio.run(collect())
    assert [row.external_id for row in rows] == [
        stable_external_id("bsky", "at://did:plc:test/app.bsky.feed.post/alias123")
    ]
    assert rows[0].raw_payload["search_term"] == "Hades 2"
    assert [call[1]["q"] for call in client.calls] == ["Hades II", "Hades 2"]


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


class TermTracker(FakeTracker):
    def __init__(self, cursors, *, recent_ids=()) -> None:
        super().__init__(recent_ids=recent_ids)
        self.cursors = dict(cursors)

    def cursor(self, scope, default=None):
        return self.cursors.get(scope.get("term"), default)


def post(index: str, *, cid="cid-v1", handle="player.bsky.social") -> dict:
    return {
        "uri": f"at://did:plc:stable/app.bsky.feed.post/{index}",
        "cid": cid,
        "author": {
            "did": "did:plc:stable",
            "handle": handle,
            "displayName": "Must not persist",
        },
        "record": {
            "text": f"Post {index}",
            "createdAt": "2026-08-13T08:00:00Z",
            "langs": ["en"],
        },
        "indexedAt": "2026-08-13T08:01:00Z",
        "likeCount": 2,
        "replyCount": 3,
        "repostCount": 4,
        "quoteCount": 5,
    }


class PagedBlueskyClient:
    def __init__(self, *, channel=False, filtered_first=False) -> None:
        self.channel = channel
        self.filtered_first = filtered_first
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, params=None):
        params = dict(params or {})
        self.calls.append((url, params))
        cursor = params.get("cursor")
        if self.channel:
            if self.filtered_first and not cursor:
                return FakeResponse(
                    {
                        "feed": [{"reason": {"$type": "repost"}, "post": post("rp")}],
                        "cursor": "page-2",
                    }
                )
            values = [post("normal")]
            return FakeResponse({"feed": [{"post": value} for value in values]})
        if not cursor:
            return FakeResponse(
                {"posts": [post("known-1"), post("known-2")], "cursor": "frontier-next"}
            )
        return FakeResponse(
            {"posts": [post("old-1"), post("old-2")], "cursor": "backlog-next"}
        )


def test_bluesky_keyword_checks_frontier_before_backlog(monkeypatch) -> None:
    known = tuple(
        stable_external_id(
            "bsky", f"at://did:plc:stable/app.bsky.feed.post/known-{index}"
        )
        for index in (1, 2)
    )
    tracker = FakeTracker(recent_ids=known, cursor="backlog")
    client = PagedBlueskyClient()
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in BlueskyConnector().search(
                SearchQuery(1, "game", [], 2, checkpoint_tracker=tracker)
            )
        ]

    rows = asyncio.run(collect())
    assert [call[1].get("cursor") for call in client.calls] == [None, "backlog"]
    assert [row.canonical_url.rsplit("/", 1)[-1] for row in rows] == ["old-1", "old-2"]
    assert tracker.reported[-1][1] == "backlog-next"


class MultiTermBlueskyClient:
    def __init__(self) -> None:
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, params=None):
        params = dict(params or {})
        self.calls.append((url, params))
        term_key = params["q"].casefold().replace(" ", "-")
        if params.get("cursor"):
            return FakeResponse(
                {
                    "posts": [post(f"{term_key}-old-1"), post(f"{term_key}-old-2")],
                    "cursor": f"{term_key}-next",
                }
            )
        return FakeResponse(
            {
                "posts": [post(f"{term_key}-known")],
                "cursor": f"{term_key}-frontier",
            }
        )


def test_bluesky_uses_independent_cursor_for_every_search_term(monkeypatch) -> None:
    terms = ("Game One", "Game Two")
    known = tuple(
        stable_external_id(
            "bsky",
            "at://did:plc:stable/app.bsky.feed.post/"
            + term.casefold().replace(" ", "-")
            + "-known",
        )
        for term in terms
    )
    tracker = TermTracker(
        {"game one": "cursor-one", "game two": "cursor-two"},
        recent_ids=known,
    )
    client = MultiTermBlueskyClient()
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in BlueskyConnector().search(
                SearchQuery(
                    1,
                    "Game One",
                    ["Game Two"],
                    4,
                    checkpoint_tracker=tracker,
                )
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 4
    assert [(call[1]["q"], call[1].get("cursor")) for call in client.calls] == [
        ("Game One", None),
        ("Game One", "cursor-one"),
        ("Game Two", None),
        ("Game Two", "cursor-two"),
    ]


def test_bluesky_edit_changes_cid_not_stable_external_identity() -> None:
    first = post("record", cid="cid-v1")
    edited = post("record", cid="cid-v2")
    identity = "at://did:plc:stable/app.bsky.feed.post/record"
    external_id = stable_external_id("bsky", identity)
    first_item = BlueskyConnector.raw_post(
        first,
        record_key="record",
        external_id=external_id,
        discovery={"search_term": "game"},
    )
    edited_item = BlueskyConnector.raw_post(
        edited,
        record_key="record",
        external_id=external_id,
        discovery={"search_term": "game"},
    )
    assert first_item is not None and edited_item is not None
    assert first_item.external_id == edited_item.external_id
    assert first_item.raw_payload["cid"] == "cid-v1"
    assert edited_item.raw_payload["cid"] == "cid-v2"


def test_bluesky_relations_and_media_are_bounded_without_raw_at_uris() -> None:
    value = post("record")
    value["record"]["reply"] = {
        "parent": {
            "uri": "at://did:plc:parent/app.bsky.feed.post/parent",
            "cid": "parent-cid",
        },
        "root": {
            "uri": "at://did:plc:root/app.bsky.feed.post/root",
            "cid": "root-cid",
        },
    }
    value["embed"] = {
        "$type": "app.bsky.embed.recordWithMedia#view",
        "record": {
            "record": {
                "uri": "at://did:plc:quote/app.bsky.feed.post/quoted",
                "cid": "quote-cid",
            }
        },
        "media": {
            "$type": "app.bsky.embed.images#view",
            "images": [
                {
                    "fullsize": "https://cdn.bsky.app/img/feed_fullsize/plain/test",
                    "thumb": "https://cdn.bsky.app/img/feed_thumbnail/plain/test",
                    "alt": "Screenshot",
                    "aspectRatio": {"width": 16, "height": 9},
                }
            ],
        },
    }
    identity = "at://did:plc:stable/app.bsky.feed.post/record"
    item = BlueskyConnector.raw_post(
        value,
        record_key="record",
        external_id=stable_external_id("bsky", identity),
        discovery={"search_term": "game"},
    )
    assert item is not None
    payload = item.raw_payload
    assert payload["is_reply"] is True
    assert payload["reply_parent_id"].startswith("bsky:")
    assert payload["reply_root_id"].startswith("bsky:")
    assert payload["is_quote"] is True
    assert payload["quoted_post_id"].startswith("bsky:")
    assert payload["media"] == [
        {
            "kind": "image",
            "url": "https://cdn.bsky.app/img/feed_fullsize/plain/test",
            "thumbnail_url": "https://cdn.bsky.app/img/feed_thumbnail/plain/test",
            "alt": "Screenshot",
            "aspect_ratio": {"width": 16, "height": 9},
        }
    ]
    assert "did:plc:" not in repr(payload)


def test_bluesky_channel_filtered_repost_page_continues_to_next_cursor(
    monkeypatch,
) -> None:
    client = PagedBlueskyClient(channel=True, filtered_first=True)
    tracker = FakeTracker()
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in channel_scans._scan_bluesky(
                {
                    "url": "https://bsky.app/profile/player.bsky.social",
                    "include_replies": False,
                    "include_reposts": False,
                },
                SearchQuery(1, "game", [], 1, checkpoint_tracker=tracker),
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 1
    assert [call[1].get("cursor") for call in client.calls] == [None, "page-2"]
    assert client.calls[0][1]["filter"] == "posts_no_replies"
    assert "did" not in rows[0].raw_payload
    assert "author" not in rows[0].raw_payload


def test_bluesky_request_budget_is_checkpointed_with_warning(monkeypatch) -> None:
    known = tuple(
        stable_external_id(
            "bsky", f"at://did:plc:stable/app.bsky.feed.post/known-{index}"
        )
        for index in (1, 2)
    )
    tracker = FakeTracker(recent_ids=known, cursor="backlog")
    warnings = []
    client = PagedBlueskyClient()
    monkeypatch.setattr(settings, "bluesky_request_budget", 1)
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def warning(code, detail):
        warnings.append((code, detail))

    async def collect():
        return [
            item
            async for item in BlueskyConnector().search(
                SearchQuery(
                    1,
                    "game",
                    [],
                    2,
                    warning_callback=warning,
                    checkpoint_tracker=tracker,
                )
            )
        ]

    assert asyncio.run(collect()) == []
    assert tracker.reported[-1][1] == "backlog"
    assert warnings[0][0] == "BUDGET_EXHAUSTED"


class BulkAuthorFeedClient:
    def __init__(self, *, repeated=False) -> None:
        self.repeated = repeated
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, params=None):
        params = dict(params or {})
        self.calls.append((url, params))
        count = int(params["limit"])
        cursor = params.get("cursor")
        start = 101 if cursor == "page-2" else 1
        values = [post(str(index)) for index in range(start, start + count)]
        next_cursor = (
            cursor
            if self.repeated and cursor
            else ("page-2" if cursor is None else "page-3")
        )
        return FakeResponse(
            {
                "feed": [{"post": value} for value in values],
                "cursor": next_cursor,
            }
        )


def test_bluesky_saved_channel_paginates_past_100_with_exact_cap(monkeypatch) -> None:
    client = BulkAuthorFeedClient()
    tracker = FakeTracker()
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in channel_scans._scan_bluesky(
                {
                    "url": "https://bsky.app/profile/player.bsky.social",
                    "include_replies": True,
                    "include_reposts": True,
                },
                SearchQuery(1, "game", [], 150, checkpoint_tracker=tracker),
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 150
    assert [call[1]["limit"] for call in client.calls] == [100, 50]
    assert tracker.reported[-1][1] == "page-3"


def test_bluesky_repeated_channel_cursor_stops_without_loop(monkeypatch) -> None:
    client = BulkAuthorFeedClient(repeated=True)
    tracker = FakeTracker(cursor="page-2")
    known = tuple(
        stable_external_id(
            "bsky", f"at://did:plc:stable/app.bsky.feed.post/{index}"
        )
        for index in range(1, 101)
    )
    tracker.recent_ids = known
    monkeypatch.setattr(connectors, "pooled_client", lambda **_kwargs: client)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)

    async def collect():
        return [
            item
            async for item in channel_scans._scan_bluesky(
                {
                    "url": "https://bsky.app/profile/player.bsky.social",
                    "include_replies": True,
                    "include_reposts": True,
                },
                SearchQuery(1, "game", [], 150, checkpoint_tracker=tracker),
            )
        ]

    rows = asyncio.run(collect())
    assert len(rows) == 100
    assert [call[1].get("cursor") for call in client.calls] == [None, "page-2"]
    assert tracker.reported[-1][1] is None
