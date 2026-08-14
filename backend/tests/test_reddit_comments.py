from __future__ import annotations

import asyncio

import httpx
import pytest

from app.crawlers.adapters.reddit import (
    RedditApiCommentProvider,
    RedditCommentBudgets,
    RedditCommentsAdapter,
    parse_reddit_post_target,
)
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
)


def comment(
    comment_id: str,
    parent_id: str,
    *,
    author: str = "raw-author",
    replies: list[dict] | None = None,
    score: int = 1,
) -> dict:
    data = {
        "id": comment_id,
        "name": f"t1_{comment_id}",
        "parent_id": parent_id,
        "body": f"body-{comment_id}",
        "author": author,
        "created_utc": 1_700_000_000,
        "score": score,
    }
    if replies is not None:
        data["replies"] = {"data": {"children": replies}}
    return {"kind": "t1", "data": data}


def more(parent_id: str, *children: str) -> dict:
    return {
        "kind": "more",
        "data": {"parent_id": parent_id, "children": list(children)},
    }


class FakeProvider:
    def __init__(self) -> None:
        self.request_count = 0
        self.more_calls: list[tuple[str, ...]] = []

    async def comment_tree(self, post_id, **kwargs):
        self.request_count += 1
        assert post_id == "abc123"
        return (
            comment(
                "root01",
                "t3_abc123",
                replies=[
                    comment("child01", "t1_root01", score=-2),
                    more("t1_root01", "child02", "child03"),
                ],
            ),
            comment("root02", "t3_abc123"),
            more("t3_abc123", "root03"),
        )

    async def more_children(self, post_id, child_ids, **kwargs):
        self.request_count += 1
        self.more_calls.append(tuple(child_ids))
        mapping = {
            "child02": comment("child02", "t1_root01"),
            "child03": comment("child03", "t1_root01"),
            "root03": comment("root03", "t3_abc123"),
        }
        return tuple(mapping[value] for value in child_ids)


def test_reddit_target_parser_is_strict_and_canonical() -> None:
    values = (
        "abc123",
        "https://www.reddit.com/r/gaming/comments/abc123/title/",
        "https://old.reddit.com/comments/abc123/title/",
        "https://redd.it/abc123",
    )
    assert {
        parse_reddit_post_target(value).canonical_url for value in values
    } == {"https://www.reddit.com/comments/abc123"}
    for invalid in (
        "http://reddit.com/comments/abc123",
        "https://evilreddit.com/comments/abc123",
        "https://user@reddit.com/comments/abc123",
        "https://reddit.com/r/gaming",
    ):
        with pytest.raises(ValueError):
            parse_reddit_post_target(invalid)


def test_reddit_comment_tree_obeys_separate_root_child_total_budgets() -> None:
    provider = FakeProvider()
    adapter = RedditCommentsAdapter(
        provider, IdentityPseudonymizer(b"r" * 32)
    )

    scan = asyncio.run(
        adapter.crawl(
            "https://www.reddit.com/comments/abc123",
            RedditCommentBudgets(
                max_root_comments=2,
                max_children_per_root=2,
                max_total_comments=4,
                max_requests=5,
                max_depth=5,
            ),
        )
    )

    assert [row.external_id for row in scan.records] == [
        "root01",
        "child01",
        "root02",
        "child02",
    ]
    assert scan.root_count == 2
    assert scan.child_count == 2
    assert scan.request_count == 2
    assert scan.truncated is True
    child = scan.records[1]
    assert child.parent_external_id == "root01"
    assert child.root_external_id == "root01"
    assert child.like_count == 0
    assert child.provenance["reddit_score"] == -2
    assert "raw-author" not in repr(scan.records)
    assert provider.more_calls == [("child02",)]


def test_reddit_child_budget_zero_does_not_emit_orphan_descendants() -> None:
    provider = FakeProvider()
    scan = asyncio.run(
        RedditCommentsAdapter(
            provider, IdentityPseudonymizer(b"r" * 32)
        ).crawl(
            "abc123",
            RedditCommentBudgets(
                max_root_comments=3,
                max_children_per_root=0,
                max_total_comments=3,
                max_requests=3,
            ),
        )
    )
    assert [row.external_id for row in scan.records] == ["root01", "root02", "root03"]
    assert all(row.parent_external_id is None for row in scan.records)
    assert scan.child_count == 0


def test_reddit_api_provider_parses_documented_tree_and_morechildren() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/comments/abc123":
            return httpx.Response(
                200,
                json=[
                    {"data": {"children": []}},
                    {"data": {"children": [comment("root01", "t3_abc123")]}},
                ],
            )
        assert request.url.path == "/api/morechildren"
        return httpx.Response(
            200,
            json={
                "json": {
                    "data": {
                        "things": [comment("child01", "t1_root01")]
                    }
                }
            },
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer hidden"},
        ) as client:
            provider = RedditApiCommentProvider(client, max_requests=2)
            tree = await provider.comment_tree(
                "abc123",
                limit=20,
                depth=4,
                sort="new",
                cancellation=CancellationToken(),
            )
            expanded = await provider.more_children(
                "abc123",
                ["child01"],
                depth=3,
                sort="new",
                cancellation=CancellationToken(),
            )
            return provider, tree, expanded

    provider, tree, expanded = asyncio.run(run())
    assert tree[0]["data"]["id"] == "root01"
    assert expanded[0]["data"]["id"] == "child01"
    assert provider.request_count == 2
    assert calls[1].url.params["children"] == "child01"
    assert calls[1].url.params["limit_children"] == "true"
    assert calls[1].url.params["link_id"] == "t3_abc123"


def test_reddit_api_provider_classifies_permission_error() -> None:
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(403, json={"message": "private"})
            )
        ) as client:
            provider = RedditApiCommentProvider(client, max_requests=1)
            await provider.comment_tree(
                "abc123",
                limit=10,
                depth=2,
                sort="new",
                cancellation=CancellationToken(),
            )

    with pytest.raises(CrawlerFailure) as caught:
        asyncio.run(run())
    assert caught.value.code is CrawlerErrorCode.PERMISSION_REQUIRED
