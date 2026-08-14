from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main
from app.api.comments import CommentScanRequest, _expand_bilibili_children
from app.crawlers.runtime import CommentRecord
from app.services.bluesky_api import stable_post_id
from app.services.mastodon_api import mastodon_external_id


def seed_reddit_item(mongo_store):
    now = datetime.now(UTC)
    return mongo_store.save_item(
        {
            "source_id": "reddit",
            "external_id": "abc123",
            "canonical_url": "https://www.reddit.com/comments/abc123",
            "title": "A post",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )


class FakeRedditComments:
    async def scan_comments(self, target, budgets, *, sort):
        assert target == "https://www.reddit.com/comments/abc123"
        assert budgets.max_root_comments == 2
        assert budgets.max_children_per_root == 3
        assert budgets.max_total_comments == 4
        assert budgets.max_requests == 5
        assert budgets.max_depth == 6
        assert sort == "top"
        return SimpleNamespace(
            records=(
                CommentRecord(
                    source_id="reddit",
                    external_id="root01",
                    content_external_id="abc123",
                    body="Root",
                    author_pseudonym="reddit_anon",
                    published_at=datetime.now(UTC),
                    like_count=2,
                    child_count=1,
                    root_external_id="root01",
                    provenance={"provider_id": "reddit_public"},
                ),
                CommentRecord(
                    source_id="reddit",
                    external_id="child01",
                    content_external_id="abc123",
                    body="Child",
                    author_pseudonym="reddit_anon2",
                    published_at=datetime.now(UTC),
                    parent_external_id="root01",
                    root_external_id="root01",
                    provenance={"provider_id": "reddit_public"},
                ),
            ),
            root_count=1,
            child_count=1,
            request_count=2,
            truncated=False,
            provider_id="reddit_public",
        )


def test_manual_reddit_comment_scan_persists_and_lists(
    mongo_store, monkeypatch
) -> None:
    item = seed_reddit_item(mongo_store)
    monkeypatch.setitem(main.connectors, "reddit", FakeRedditComments())

    with TestClient(main.app) as client:
        response = client.post(
            f"/api/v1/items/{item['id']}/comments/scan",
            json={
                "max_root_comments": 2,
                "max_children_per_root": 3,
                "max_total_comments": 4,
                "max_requests": 5,
                "max_depth": 6,
                "sort": "top",
            },
        )
        listed = client.get(f"/api/v1/items/{item['id']}/comments")

    assert response.status_code == 200
    assert response.json() == {
        "content_item_id": item["id"],
        "source_id": "reddit",
        "content_external_id": "abc123",
        "fetched_count": 2,
        "stored_count": 2,
        "root_count": 1,
        "child_count": 1,
        "request_count": 2,
        "truncated": False,
        "provider_id": "reddit_public",
    }
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["count"] == 2
    assert {row["external_id"] for row in payload["items"]} == {
        "root01",
        "child01",
    }
    assert all("raw-author" not in repr(row) for row in payload["items"])


def test_comment_scan_rejects_source_without_runnable_provider(
    mongo_store,
) -> None:
    now = datetime.now(UTC)
    item = mongo_store.save_item(
        {
            "source_id": "steam",
            "external_id": "game01:review01",
            "canonical_url": "https://store.steampowered.com/app/123/reviews/",
            "title": "Review",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/v1/items/{item['id']}/comments/scan", json={}
        )
    assert response.status_code == 409
    assert mongo_store.db.comments.count_documents({}) == 0


class FakeYouTubeComments:
    async def scan_comments(self, target, budgets, *, sort):
        assert target.endswith("watch?v=a1B2c3D4e5F")
        assert budgets.max_root_comments == 1
        assert budgets.max_children_per_root == 2
        assert budgets.max_total_comments == 3
        assert budgets.max_requests == 4
        assert sort == "new"
        return SimpleNamespace(
            records=(
                CommentRecord(
                    source_id="youtube",
                    external_id="youtube-root",
                    content_external_id="a1B2c3D4e5F",
                    body="Root",
                    author_pseudonym="youtube_anon",
                    root_external_id="youtube-root",
                    provenance={"provider_id": "youtube_public"},
                ),
            ),
            root_count=1,
            child_count=0,
            request_count=1,
            truncated=False,
            provider_id="youtube_public",
        )


def test_manual_youtube_comment_scan_uses_shared_persistence(
    mongo_store, monkeypatch
) -> None:
    now = datetime.now(UTC)
    item = mongo_store.save_item(
        {
            "source_id": "youtube",
            "external_id": "a1B2c3D4e5F",
            "canonical_url": "https://www.youtube.com/watch?v=a1B2c3D4e5F",
            "title": "Video",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )
    monkeypatch.setitem(main.connectors, "youtube", FakeYouTubeComments())
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/v1/items/{item['id']}/comments/scan",
            json={
                "max_root_comments": 1,
                "max_children_per_root": 2,
                "max_total_comments": 3,
                "max_requests": 4,
            },
        )
    assert response.status_code == 200
    assert response.json()["provider_id"] == "youtube_public"
    assert mongo_store.comment_by_source("youtube", "youtube-root") is not None


class FakeXComments:
    async def scan_comments(self, target, budgets, *, sort):
        assert target == "https://x.com/i/web/status/9000"
        assert budgets.max_total_comments == 4
        assert budgets.max_requests == 3
        assert sort == "new"
        return SimpleNamespace(
            content_external_id="9000",
            records=(
                CommentRecord(
                    source_id="x",
                    external_id="9001",
                    content_external_id="9000",
                    body="Direct reply",
                    author_pseudonym="x_anon",
                    parent_external_id="9000",
                    root_external_id="9000",
                    provenance={"provider_id": "x_api"},
                ),
                CommentRecord(
                    source_id="x",
                    external_id="9002",
                    content_external_id="9000",
                    body="Nested reply",
                    author_pseudonym="x_anon2",
                    parent_external_id="9001",
                    root_external_id="9000",
                    provenance={"provider_id": "x_api"},
                ),
            ),
            root_count=1,
            child_count=1,
            request_count=1,
            truncated=False,
            provider_id="x_api",
        )


def test_manual_x_comment_scan_accepts_content_root_anchor(
    mongo_store, monkeypatch
) -> None:
    now = datetime.now(UTC)
    item = mongo_store.save_item(
        {
            "source_id": "x",
            "external_id": "9000",
            "canonical_url": "https://x.com/i/web/status/9000",
            "title": "Post",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )
    monkeypatch.setitem(main.connectors, "x", FakeXComments())
    monkeypatch.setattr("app.services.connectors.settings.x_bearer_token", "token")
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/v1/items/{item['id']}/comments/scan",
            json={"max_total_comments": 4, "max_requests": 3},
        )

    assert response.status_code == 200
    assert response.json()["provider_id"] == "x_api"
    rows = mongo_store.comments_for_content("x", "9000")
    assert {row["external_id"] for row in rows} == {"9001", "9002"}


class FakeBlueskyComments:
    async def scan_comments(self, target, budgets, *, sort):
        assert target == "https://bsky.app/profile/player.bsky.social/post/post1"
        assert budgets.max_root_comments == 2
        assert budgets.max_children_per_root == 2
        assert budgets.max_total_comments == 4
        assert budgets.max_requests == 3
        assert budgets.max_depth == 5
        assert sort == "provider"
        content_id = stable_post_id(
            "at://did:plc:content/app.bsky.feed.post/post1"
        )
        root_id = stable_post_id(
            "at://did:plc:author/app.bsky.feed.post/root1"
        )
        assert content_id is not None and root_id is not None
        return SimpleNamespace(
            content_external_id=content_id,
            records=(
                CommentRecord(
                    source_id="bluesky",
                    external_id=root_id,
                    content_external_id=content_id,
                    body="Public reply",
                    author_pseudonym="bluesky_anon",
                    root_external_id=root_id,
                    provenance={"provider_id": "bluesky_public"},
                ),
            ),
            root_count=1,
            child_count=0,
            request_count=2,
            truncated=False,
            provider_id="bluesky_public",
        )


def test_manual_bluesky_comment_scan_uses_provider_order_and_persists(
    mongo_store, monkeypatch
) -> None:
    now = datetime.now(UTC)
    content_id = stable_post_id(
        "at://did:plc:content/app.bsky.feed.post/post1"
    )
    assert content_id is not None
    item = mongo_store.save_item(
        {
            "source_id": "bluesky",
            "external_id": content_id,
            "canonical_url": "https://bsky.app/profile/player.bsky.social/post/post1",
            "title": "Post",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )
    monkeypatch.setitem(main.connectors, "bluesky", FakeBlueskyComments())
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/v1/items/{item['id']}/comments/scan",
            json={
                "max_root_comments": 2,
                "max_children_per_root": 2,
                "max_total_comments": 4,
                "max_requests": 3,
                "max_depth": 5,
            },
        )
    assert response.status_code == 200
    assert response.json()["provider_id"] == "bluesky_public"
    assert response.json()["request_count"] == 2
    rows = mongo_store.comments_for_content("bluesky", content_id)
    assert len(rows) == 1
    assert rows[0]["author_pseudonym"] == "bluesky_anon"


def test_bluesky_comment_scan_rejects_recycled_handle_identity(
    mongo_store, monkeypatch
) -> None:
    now = datetime.now(UTC)
    stored_id = stable_post_id(
        "at://did:plc:original/app.bsky.feed.post/post1"
    )
    resolved_id = stable_post_id(
        "at://did:plc:recycled/app.bsky.feed.post/post1"
    )
    assert stored_id is not None and resolved_id is not None
    item = mongo_store.save_item(
        {
            "source_id": "bluesky",
            "external_id": stored_id,
            "canonical_url": "https://bsky.app/profile/player.bsky.social/post/post1",
            "title": "Post",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )

    class RecycledHandle:
        async def scan_comments(self, target, budgets, *, sort):
            del target, budgets, sort
            return SimpleNamespace(
                content_external_id=resolved_id,
                records=(),
                root_count=0,
                child_count=0,
                request_count=2,
                truncated=False,
                provider_id="bluesky_public",
            )

    monkeypatch.setitem(main.connectors, "bluesky", RecycledHandle())
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/v1/items/{item['id']}/comments/scan", json={}
        )
    assert response.status_code == 502
    assert mongo_store.db.comments.count_documents({}) == 0


class FakeMastodonComments:
    async def scan_comments(self, target, budgets, *, sort):
        assert target == "https://mastodon.social/@player/100"
        assert budgets.max_root_comments == 2
        assert budgets.max_children_per_root == 2
        assert budgets.max_total_comments == 4
        assert budgets.max_requests == 3
        assert budgets.max_depth == 5
        assert sort == "provider"
        content_id = mastodon_external_id(
            "https://mastodon.social/users/player/statuses/100"
        )
        root_id = mastodon_external_id(
            "https://mastodon.social/users/replier/statuses/101"
        )
        return SimpleNamespace(
            content_external_id=content_id,
            records=(
                CommentRecord(
                    source_id="mastodon",
                    external_id=root_id,
                    content_external_id=content_id,
                    body="Public reply",
                    author_pseudonym="mastodon_anon",
                    root_external_id=root_id,
                    provenance={"provider_id": "mastodon_public"},
                ),
            ),
            root_count=1,
            child_count=0,
            request_count=2,
            truncated=False,
            provider_id="mastodon_public",
        )


def test_manual_mastodon_context_scan_uses_shared_persistence(
    mongo_store, monkeypatch
) -> None:
    now = datetime.now(UTC)
    content_id = mastodon_external_id(
        "https://mastodon.social/users/player/statuses/100"
    )
    item = mongo_store.save_item(
        {
            "source_id": "mastodon",
            "external_id": content_id,
            "canonical_url": "https://mastodon.social/@player/100",
            "title": "Status",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )
    monkeypatch.setitem(main.connectors, "mastodon", FakeMastodonComments())
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/v1/items/{item['id']}/comments/scan",
            json={
                "max_root_comments": 2,
                "max_children_per_root": 2,
                "max_total_comments": 4,
                "max_requests": 3,
                "max_depth": 5,
            },
        )
    assert response.status_code == 200
    assert response.json()["provider_id"] == "mastodon_public"
    assert response.json()["request_count"] == 2
    rows = mongo_store.comments_for_content("mastodon", content_id)
    assert len(rows) == 1
    assert rows[0]["author_pseudonym"] == "mastodon_anon"


class FakeTiebaComments:
    async def scan_comments(self, target, budgets, *, sort):
        assert target == "https://tieba.baidu.com/p/1234567890"
        assert budgets.max_root_comments == 2
        assert budgets.max_total_comments == 4
        assert budgets.max_requests == 3
        assert sort == "new"
        return SimpleNamespace(
            records=(
                CommentRecord(
                    source_id="tieba",
                    external_id="8001",
                    content_external_id="1234567890",
                    body="Rendered root",
                    author_pseudonym="tieba_anon",
                    child_count=2,
                    root_external_id="8001",
                    provenance={
                        "provider_id": "cbce_tieba",
                        "coverage": "rendered_root_comments_only",
                    },
                ),
            ),
            root_count=1,
            child_count=0,
            request_count=1,
            truncated=True,
            provider_id="cbce_tieba",
        )


def test_manual_tieba_root_comments_require_rollout_and_persist(
    mongo_store, monkeypatch
) -> None:
    now = datetime.now(UTC)
    item = mongo_store.save_item(
        {
            "source_id": "tieba",
            "external_id": "1234567890",
            "canonical_url": "https://tieba.baidu.com/p/1234567890",
            "title": "Thread",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )
    monkeypatch.setitem(main.connectors, "tieba", FakeTiebaComments())
    monkeypatch.setattr(
        "app.api.comments.cbce_provider_rollout_status",
        lambda source, provider, operation: {
            "ready": True,
            "reason_code": None,
            "detail": "ready",
        },
    )
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/v1/items/{item['id']}/comments/scan",
            json={
                "max_root_comments": 2,
                "max_children_per_root": 2,
                "max_total_comments": 4,
                "max_requests": 3,
            },
        )
    assert response.status_code == 200
    assert response.json()["provider_id"] == "cbce_tieba"
    assert response.json()["truncated"] is True
    stored = mongo_store.comment_by_source("tieba", "8001")
    assert stored is not None
    assert stored["content_item_id"] == item["id"]


def test_tieba_comment_api_fails_closed_when_cbce_rollout_is_disabled(
    mongo_store, monkeypatch
) -> None:
    now = datetime.now(UTC)
    item = mongo_store.save_item(
        {
            "source_id": "tieba",
            "external_id": "1234567890",
            "canonical_url": "https://tieba.baidu.com/p/1234567890",
            "title": "Thread",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )
    monkeypatch.setattr(
        "app.api.comments.cbce_provider_rollout_status",
        lambda source, provider, operation: {
            "ready": False,
            "reason_code": "CBCE_FEATURE_DISABLED",
            "detail": "Enable clean-room runtime.",
        },
    )
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/v1/items/{item['id']}/comments/scan", json={}
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "Enable clean-room runtime."
    assert mongo_store.db.comments.count_documents({}) == 0


class FakeBilibiliComments:
    async def scan_comments(self, target, budgets, *, sort):
        assert target == "https://www.bilibili.com/video/BV1ab411c7De"
        assert budgets.max_root_comments == 2
        assert budgets.max_total_comments == 4
        assert budgets.max_requests == 3
        assert sort == "new"
        return SimpleNamespace(
            records=(
                CommentRecord(
                    source_id="bilibili",
                    external_id="9001",
                    content_external_id="BV1ab411c7De",
                    body="Rendered root",
                    author_pseudonym="bilibili_anon",
                    child_count=2,
                    root_external_id="9001",
                    provenance={
                        "provider_id": "cbce_bilibili",
                        "coverage": "rendered_root_comments_only",
                    },
                ),
            ),
            root_count=1,
            child_count=0,
            request_count=1,
            truncated=True,
            provider_id="cbce_bilibili",
            pagination_truncated=False,
        )

    async def scan_child_comments(
        self, target, root_comment_id, budgets, *, sort
    ):
        assert target == "https://www.bilibili.com/video/BV1ab411c7De"
        assert root_comment_id == "9001"
        assert budgets.max_items == 2
        assert budgets.max_requests == 2
        assert sort == "new"
        return SimpleNamespace(
            records=(
                CommentRecord(
                    source_id="bilibili",
                    external_id="9002",
                    content_external_id="BV1ab411c7De",
                    body="Rendered child",
                    author_pseudonym="bilibili_child",
                    parent_external_id="9001",
                    root_external_id="9001",
                    provenance={"provider_id": "cbce_bilibili"},
                ),
                CommentRecord(
                    source_id="bilibili",
                    external_id="9003",
                    content_external_id="BV1ab411c7De",
                    body="Rendered child 2",
                    author_pseudonym="bilibili_child_2",
                    parent_external_id="9001",
                    root_external_id="9001",
                    provenance={"provider_id": "cbce_bilibili"},
                ),
            ),
            request_count=1,
            truncated=False,
            provider_id="cbce_bilibili",
            pagination_truncated=False,
        )


def test_manual_bilibili_root_comments_require_rollout_and_persist(
    mongo_store, monkeypatch
) -> None:
    now = datetime.now(UTC)
    item = mongo_store.save_item(
        {
            "source_id": "bilibili",
            "external_id": "BV1ab411c7De",
            "canonical_url": "https://www.bilibili.com/video/BV1ab411c7De",
            "title": "Video",
            "metrics": {},
            "raw_payload": {},
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )
    monkeypatch.setitem(main.connectors, "bilibili", FakeBilibiliComments())
    monkeypatch.setattr(
        "app.api.comments.cbce_provider_rollout_status",
        lambda source, provider, operation: {
            "ready": source == "bilibili" and provider == "cbce_bilibili",
            "reason_code": None,
            "detail": "ready",
        },
    )
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/v1/items/{item['id']}/comments/scan",
            json={
                "max_root_comments": 2,
                "max_children_per_root": 2,
                "max_total_comments": 4,
                "max_requests": 3,
            },
        )

    assert response.status_code == 200
    assert response.json()["provider_id"] == "cbce_bilibili"
    assert response.json()["truncated"] is False
    assert response.json()["fetched_count"] == 3
    assert response.json()["child_count"] == 2
    assert response.json()["request_count"] == 2
    stored = mongo_store.comment_by_source("bilibili", "9001")
    assert stored is not None
    assert stored["content_item_id"] == item["id"]
    assert mongo_store.comment_by_source("bilibili", "9002") is not None


def test_bilibili_child_expansion_reserves_total_budget_for_all_roots() -> None:
    roots = tuple(
        CommentRecord(
            source_id="bilibili",
            external_id=f"root-{index}",
            content_external_id="BV1ab411c7De",
            body="Root",
            author_pseudonym="bilibili_anon",
            child_count=3,
            root_external_id=f"root-{index}",
            provenance={"provider_id": "cbce_bilibili"},
        )
        for index in range(3)
    )

    class ChildScanner:
        async def scan_child_comments(
            self, target, root_comment_id, budgets, *, sort
        ):
            del target, sort
            return SimpleNamespace(
                records=tuple(
                    CommentRecord(
                        source_id="bilibili",
                        external_id=f"{root_comment_id}-child-{index}",
                        content_external_id="BV1ab411c7De",
                        body="Child",
                        author_pseudonym="bilibili_child",
                        parent_external_id=root_comment_id,
                        root_external_id=root_comment_id,
                        provenance={"provider_id": "cbce_bilibili"},
                    )
                    for index in range(budgets.max_items)
                ),
                request_count=1,
                truncated=False,
                pagination_truncated=False,
                provider_id="cbce_bilibili",
            )

    records, child_count, request_count, truncated = asyncio.run(
        _expand_bilibili_children(
            ChildScanner(),
            "https://www.bilibili.com/video/BV1ab411c7De",
            roots,
            provider_id="cbce_bilibili",
            payload=CommentScanRequest(
                max_root_comments=3,
                max_children_per_root=3,
                max_total_comments=4,
                max_requests=4,
            ),
            request_count=1,
            truncated=False,
        )
    )

    assert len(records) == 4
    assert child_count == 1
    assert request_count == 2
    assert truncated is True


def test_comment_api_rejects_provider_identity_mismatch(
    mongo_store, monkeypatch
) -> None:
    item = seed_reddit_item(mongo_store)

    class WrongProvider(FakeRedditComments):
        async def scan_comments(self, target, budgets, *, sort):
            result = await super().scan_comments(
                target, budgets, sort=sort
            )
            result.provider_id = "unexpected_provider"
            return result

    monkeypatch.setitem(main.connectors, "reddit", WrongProvider())
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/v1/items/{item['id']}/comments/scan",
            json={
                "max_root_comments": 2,
                "max_children_per_root": 3,
                "max_total_comments": 4,
                "max_requests": 5,
                "max_depth": 6,
                "sort": "top",
            },
        )
    assert response.status_code == 502
    assert mongo_store.db.comments.count_documents({}) == 0
