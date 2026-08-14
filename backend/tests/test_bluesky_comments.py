from __future__ import annotations

import asyncio

import httpx
import pytest

from app.crawlers.adapters.bluesky import (
    BlueskyApiCommentProvider,
    BlueskyCommentBudgets,
    BlueskyCommentsAdapter,
    parse_bluesky_post_target,
)
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
)
from app.services.bluesky_api import stable_post_id


def post_node(
    uri: str,
    *,
    author: str,
    parent: str | None = None,
    root: str | None = None,
    replies: tuple[dict, ...] = (),
    declared_replies: int | None = None,
) -> dict:
    record: dict = {
        "$type": "app.bsky.feed.post",
        "text": f"body-{uri.rsplit('/', 1)[-1]}",
        "createdAt": "2026-08-01T08:00:00Z",
    }
    if parent is not None and root is not None:
        record["reply"] = {
            "parent": {"uri": parent, "cid": "not-retained"},
            "root": {"uri": root, "cid": "not-retained"},
        }
    return {
        "$type": "app.bsky.feed.defs#threadViewPost",
        "post": {
            "uri": uri,
            "cid": "not-retained",
            "author": {"did": author, "handle": "not-retained.example"},
            "record": record,
            "indexedAt": "2026-08-01T08:01:00Z",
            "likeCount": 2,
            "replyCount": (
                len(replies) if declared_replies is None else declared_replies
            ),
        },
        "replies": list(replies),
    }


class FakeThreadProvider:
    def __init__(self, thread: dict, *, did: str = "did:plc:content") -> None:
        self.thread = thread
        self.did = did
        self.request_count = 0
        self.calls: list[tuple] = []

    async def resolve_handle(self, handle, *, cancellation):
        cancellation.raise_if_cancelled()
        self.request_count += 1
        self.calls.append(("resolve", handle))
        return self.did

    async def post_thread(self, uri, *, depth, cancellation):
        cancellation.raise_if_cancelled()
        self.request_count += 1
        self.calls.append(("thread", uri, depth))
        return {"thread": self.thread}


@pytest.mark.parametrize(
    "value",
    [
        "https://bsky.app/profile/player.bsky.social/post/3lxyz",
        "https://www.bsky.app/profile/player.bsky.social/post/3lxyz?ref=test",
    ],
)
def test_bluesky_post_target_is_strict_and_canonical(value: str) -> None:
    target = parse_bluesky_post_target(value)
    assert target.actor == "player.bsky.social"
    assert target.record_key == "3lxyz"
    assert target.canonical_url == (
        "https://bsky.app/profile/player.bsky.social/post/3lxyz"
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://bsky.app/profile/player.bsky.social/post/3lxyz",
        "https://evilbsky.app/profile/player.bsky.social/post/3lxyz",
        "https://user@bsky.app/profile/player.bsky.social/post/3lxyz",
        "https://bsky.app/profile/singlelabel/post/3lxyz",
        "https://bsky.app/profile/player.bsky.social/post/%2e%2e",
        "https://bsky.app/profile/player.bsky.social",
    ],
)
def test_bluesky_post_target_rejects_ambiguous_urls(value: str) -> None:
    with pytest.raises(ValueError):
        parse_bluesky_post_target(value)


def test_bluesky_comment_tree_obeys_root_child_total_and_depth_budgets() -> None:
    content_uri = "at://did:plc:content/app.bsky.feed.post/post1"
    root1_uri = "at://did:plc:author1/app.bsky.feed.post/root1"
    child1_uri = "at://did:plc:author2/app.bsky.feed.post/child1"
    grandchild_uri = "at://did:plc:author3/app.bsky.feed.post/grandchild1"
    root2_uri = "at://did:plc:author4/app.bsky.feed.post/root2"
    root3_uri = "at://did:plc:author5/app.bsky.feed.post/root3"
    grandchild = post_node(
        grandchild_uri,
        author="did:plc:author3",
        parent=child1_uri,
        root=content_uri,
    )
    child1 = post_node(
        child1_uri,
        author="did:plc:author2",
        parent=root1_uri,
        root=content_uri,
        replies=(grandchild,),
    )
    root1 = post_node(
        root1_uri,
        author="did:plc:author1",
        parent=content_uri,
        root=content_uri,
        replies=(child1,),
    )
    root2 = post_node(
        root2_uri,
        author="did:plc:author4",
        parent=content_uri,
        root=content_uri,
    )
    root3 = post_node(
        root3_uri,
        author="did:plc:author5",
        parent=content_uri,
        root=content_uri,
    )
    provider = FakeThreadProvider(
        post_node(
            content_uri,
            author="did:plc:content-author",
            replies=(root1, root2, root3),
        )
    )
    adapter = BlueskyCommentsAdapter(
        provider,
        IdentityPseudonymizer(b"b" * 32),
    )
    scan = asyncio.run(
        adapter.crawl(
            "https://bsky.app/profile/player.bsky.social/post/post1",
            BlueskyCommentBudgets(
                max_root_comments=2,
                max_children_per_root=1,
                max_total_comments=3,
                max_requests=2,
                max_depth=3,
            ),
        )
    )

    content_id = stable_post_id(content_uri)
    root1_id = stable_post_id(root1_uri)
    child1_id = stable_post_id(child1_uri)
    root2_id = stable_post_id(root2_uri)
    assert [record.external_id for record in scan.records] == [
        root1_id,
        child1_id,
        root2_id,
    ]
    assert all(record.content_external_id == content_id for record in scan.records)
    assert scan.content_external_id == content_id
    assert scan.records[0].parent_external_id is None
    assert scan.records[0].root_external_id == root1_id
    assert scan.records[1].parent_external_id == root1_id
    assert scan.records[1].root_external_id == root1_id
    assert scan.records[2].parent_external_id is None
    assert scan.root_count == 2
    assert scan.child_count == 1
    assert scan.request_count == 2
    assert scan.truncated is True
    assert "did:plc:" not in repr(scan.records)
    assert "at://" not in repr(scan.records)
    assert all(record.author_pseudonym.startswith("bluesky_") for record in scan.records)
    assert provider.calls == [
        ("resolve", "player.bsky.social"),
        ("thread", content_uri, 3),
    ]


def test_bluesky_comment_tree_rejects_wrong_parent_relation() -> None:
    content_uri = "at://did:plc:content/app.bsky.feed.post/post1"
    reply = post_node(
        "at://did:plc:author/app.bsky.feed.post/reply1",
        author="did:plc:author",
        parent="at://did:plc:other/app.bsky.feed.post/other",
        root=content_uri,
    )
    provider = FakeThreadProvider(
        post_node(
            content_uri,
            author="did:plc:content-author",
            replies=(reply,),
        )
    )
    adapter = BlueskyCommentsAdapter(provider, IdentityPseudonymizer(b"b" * 32))
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(
            adapter.crawl(
                "https://bsky.app/profile/player.bsky.social/post/post1",
                BlueskyCommentBudgets(),
            )
        )
    assert raised.value.code is CrawlerErrorCode.PARSE_CHANGED


@pytest.mark.parametrize(
    ("thread", "code"),
    [
        ({"uri": "at://did:plc:x/app.bsky.feed.post/x", "notFound": True}, CrawlerErrorCode.NOT_FOUND),
        (
            {
                "uri": "at://did:plc:x/app.bsky.feed.post/x",
                "blocked": True,
                "author": {"did": "did:plc:x"},
            },
            CrawlerErrorCode.PERMISSION_REQUIRED,
        ),
    ],
)
def test_bluesky_root_unavailable_is_typed(thread: dict, code: CrawlerErrorCode) -> None:
    provider = FakeThreadProvider(thread)
    adapter = BlueskyCommentsAdapter(provider, IdentityPseudonymizer(b"b" * 32))
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(
            adapter.crawl(
                "https://bsky.app/profile/player.bsky.social/post/post1",
                BlueskyCommentBudgets(),
            )
        )
    assert raised.value.code is code


def test_bluesky_transport_counts_each_retry_against_request_budget(monkeypatch) -> None:
    class RetryClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, endpoint, params=None):
            del endpoint, params
            self.calls += 1
            return httpx.Response(503, json={"error": "UpstreamError"})

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("app.services.bluesky_api.asyncio.sleep", no_sleep)
    client = RetryClient()
    provider = BlueskyApiCommentProvider(client, max_requests=1, attempts=3)
    with pytest.raises(CrawlerFailure) as raised:
        asyncio.run(
            provider.post_thread(
                "at://did:plc:content/app.bsky.feed.post/post1",
                depth=2,
                cancellation=CancellationToken(),
            )
        )
    assert raised.value.code is CrawlerErrorCode.BUDGET_EXHAUSTED
    assert provider.request_count == 1
    assert client.calls == 1


def test_bluesky_transport_uses_only_documented_public_xrpcs() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = []

        async def get(self, endpoint, params=None):
            self.calls.append((endpoint, dict(params or {})))
            if endpoint.endswith("resolveHandle"):
                return httpx.Response(200, json={"did": "did:plc:content"})
            return httpx.Response(200, json={"thread": {"notFound": True}})

    client = FakeClient()
    provider = BlueskyApiCommentProvider(client, max_requests=2, attempts=1)

    async def invoke() -> None:
        did = await provider.resolve_handle(
            "player.bsky.social", cancellation=CancellationToken()
        )
        await provider.post_thread(
            f"at://{did}/app.bsky.feed.post/post1",
            depth=4,
            cancellation=CancellationToken(),
        )

    asyncio.run(invoke())
    assert client.calls == [
        (
            "/xrpc/com.atproto.identity.resolveHandle",
            {"handle": "player.bsky.social"},
        ),
        (
            "/xrpc/app.bsky.feed.getPostThread",
            {
                "uri": "at://did:plc:content/app.bsky.feed.post/post1",
                "depth": 4,
                "parentHeight": 0,
            },
        ),
    ]
