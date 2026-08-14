from __future__ import annotations

import asyncio

import httpx
import pytest

from app.crawlers.adapters.youtube import (
    YouTubeApiCommentProvider,
    YouTubeCommentBudgets,
    YouTubeCommentsAdapter,
    parse_youtube_video_target,
)
from app.crawlers.runtime import (
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
)

VIDEO_ID = "a1B2c3D4e5F"


def comment(
    comment_id: str,
    *,
    parent_id: str | None = None,
    author_id: str = "raw-channel-id",
    like_count: int = 1,
) -> dict:
    snippet = {
        "textOriginal": f"body-{comment_id}",
        "authorChannelId": {"value": author_id},
        "publishedAt": "2026-08-13T01:02:03Z",
        "likeCount": like_count,
    }
    if parent_id:
        snippet["parentId"] = parent_id
    return {"id": comment_id, "snippet": snippet}


def thread(comment_id: str, replies: int) -> dict:
    return {
        "id": f"thread-{comment_id}",
        "snippet": {
            "topLevelComment": comment(comment_id),
            "totalReplyCount": replies,
        },
    }


class FakeProvider:
    def __init__(self) -> None:
        self.request_count = 0
        self.root_calls: list[tuple[str | None, int]] = []
        self.reply_calls: list[tuple[str, str | None, int]] = []

    async def root_page(self, video_id, *, page_token, limit, **kwargs):
        self.request_count += 1
        self.root_calls.append((page_token, limit))
        assert video_id == VIDEO_ID
        if page_token is None:
            return {
                "items": [thread("root01", 3), thread("root02", 0)],
                "nextPageToken": "roots-next",
            }
        return {"items": [thread("root03", 0)]}

    async def reply_page(self, parent_id, *, page_token, limit, **kwargs):
        self.request_count += 1
        self.reply_calls.append((parent_id, page_token, limit))
        if page_token is None:
            return {
                "items": [
                    comment("child01", parent_id=parent_id),
                    comment("child02", parent_id=parent_id),
                ],
                "nextPageToken": "replies-next",
            }
        return {"items": [comment("child03", parent_id=parent_id)]}


def test_youtube_video_target_parser_is_strict_and_canonical() -> None:
    values = (
        VIDEO_ID,
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}",
        f"https://m.youtube.com/shorts/{VIDEO_ID}",
        f"https://youtube.com/live/{VIDEO_ID}",
    )
    assert {
        parse_youtube_video_target(value).canonical_url for value in values
    } == {f"https://www.youtube.com/watch?v={VIDEO_ID}"}
    for invalid in (
        f"http://youtube.com/watch?v={VIDEO_ID}",
        f"https://evilyoutube.com/watch?v={VIDEO_ID}",
        f"https://user@youtube.com/watch?v={VIDEO_ID}",
        "https://youtube.com/@channel",
    ):
        with pytest.raises(ValueError):
            parse_youtube_video_target(invalid)


def test_youtube_comment_adapter_uses_full_reply_listing_and_exact_budgets() -> None:
    provider = FakeProvider()
    scan = asyncio.run(
        YouTubeCommentsAdapter(
            provider, IdentityPseudonymizer(b"y" * 32)
        ).crawl(
            VIDEO_ID,
            YouTubeCommentBudgets(
                max_root_comments=2,
                max_children_per_root=2,
                max_total_comments=4,
                max_requests=10,
            ),
        )
    )

    assert [item.external_id for item in scan.records] == [
        "root01",
        "child01",
        "child02",
        "root02",
    ]
    assert scan.root_count == 2
    assert scan.child_count == 2
    assert scan.request_count == 2
    assert scan.truncated is True
    assert provider.root_calls == [(None, 2)]
    assert provider.reply_calls == [("root01", None, 2)]
    root, child = scan.records[:2]
    assert root.root_external_id == "root01"
    assert root.child_count == 3
    assert child.parent_external_id == "root01"
    assert child.root_external_id == "root01"
    assert "raw-channel-id" not in repr(scan.records)


def test_youtube_comment_adapter_rejects_reply_from_wrong_parent() -> None:
    provider = FakeProvider()

    async def wrong_reply(parent_id, *, page_token, limit, **kwargs):
        provider.request_count += 1
        return {"items": [comment("child01", parent_id="different-root")]}

    provider.reply_page = wrong_reply  # type: ignore[method-assign]
    with pytest.raises(CrawlerFailure) as caught:
        asyncio.run(
            YouTubeCommentsAdapter(
                provider, IdentityPseudonymizer(b"y" * 32)
            ).crawl(
                VIDEO_ID,
                YouTubeCommentBudgets(
                    max_root_comments=1,
                    max_children_per_root=1,
                    max_total_comments=2,
                    max_requests=2,
                ),
            )
        )
    assert caught.value.code is CrawlerErrorCode.PARSE_CHANGED


def test_youtube_api_comment_provider_uses_documented_endpoints() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/commentThreads"):
            return httpx.Response(200, json={"items": [thread("root01", 1)]})
        return httpx.Response(
            200, json={"items": [comment("child01", parent_id="root01")]}
        )

    async def run():
        async with httpx.AsyncClient(
            base_url="https://www.googleapis.com/youtube/v3",
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            provider = YouTubeApiCommentProvider(
                client, api_key="hidden-key", max_requests=2
            )
            roots = await provider.root_page(
                VIDEO_ID,
                page_token=None,
                limit=10,
                order="time",
                cancellation=CancellationToken(),
            )
            replies = await provider.reply_page(
                "root01",
                page_token=None,
                limit=10,
                cancellation=CancellationToken(),
            )
            return provider, roots, replies

    provider, roots, replies = asyncio.run(run())
    assert roots["items"][0]["snippet"]["totalReplyCount"] == 1
    assert replies["items"][0]["id"] == "child01"
    assert provider.request_count == 2
    assert requests[0].url.params["videoId"] == VIDEO_ID
    assert requests[0].url.params["textFormat"] == "plainText"
    assert requests[1].url.params["parentId"] == "root01"


def test_youtube_comments_disabled_is_typed_unsupported() -> None:
    async def run():
        async with httpx.AsyncClient(
            base_url="https://www.googleapis.com/youtube/v3",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    403,
                    json={
                        "error": {
                            "errors": [{"reason": "commentsDisabled"}]
                        }
                    },
                )
            ),
        ) as client:
            provider = YouTubeApiCommentProvider(
                client, api_key="hidden-key", max_requests=1
            )
            await provider.root_page(
                VIDEO_ID,
                page_token=None,
                limit=10,
                order="time",
                cancellation=CancellationToken(),
            )

    with pytest.raises(CrawlerFailure) as caught:
        asyncio.run(run())
    assert caught.value.code is CrawlerErrorCode.UNSUPPORTED
