"""Bounded YouTube comment threads through Data API v3."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

import httpx

from ....services.youtube_api import youtube_json
from ...runtime import (
    CancellationToken,
    CommentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
)

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ORDERS = frozenset({"time", "relevance"})


@dataclass(frozen=True, slots=True)
class YouTubeVideoTarget:
    video_id: str
    canonical_url: str


def parse_youtube_video_target(value: str) -> YouTubeVideoTarget:
    raw = str(value or "").strip()
    if _VIDEO_ID.fullmatch(raw):
        return YouTubeVideoTarget(raw, f"https://www.youtube.com/watch?v={raw}")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("YouTube video target is invalid") from exc
    if parsed.scheme.casefold() != "https" or parsed.username or parsed.password:
        raise ValueError("YouTube video target must be a public HTTPS URL")
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    video_id = ""
    if host == "youtu.be" and len(parts) == 1:
        video_id = parts[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path.rstrip("/") == "/watch":
            values = parse_qs(parsed.query).get("v") or []
            video_id = values[0] if len(values) == 1 else ""
        elif len(parts) == 2 and parts[0].casefold() in {
            "shorts",
            "live",
            "embed",
        }:
            video_id = parts[1]
    else:
        raise ValueError("YouTube video host is not supported")
    if not _VIDEO_ID.fullmatch(video_id):
        raise ValueError("YouTube video ID is invalid")
    return YouTubeVideoTarget(
        video_id, f"https://www.youtube.com/watch?v={video_id}"
    )


@dataclass(frozen=True, slots=True)
class YouTubeCommentBudgets:
    max_root_comments: int = 100
    max_children_per_root: int = 100
    max_total_comments: int = 300
    max_requests: int = 25

    def __post_init__(self) -> None:
        if not 1 <= self.max_root_comments <= 500:
            raise ValueError("Root comment budget must be between 1 and 500")
        if not 0 <= self.max_children_per_root <= 500:
            raise ValueError("Reply budget must be between 0 and 500")
        if not 1 <= self.max_total_comments <= 1_000:
            raise ValueError("Total comment budget must be between 1 and 1000")
        if not 1 <= self.max_requests <= 100:
            raise ValueError("YouTube comment request budget must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class YouTubeCommentScan:
    target: YouTubeVideoTarget
    records: tuple[CommentRecord, ...]
    request_count: int
    root_count: int
    child_count: int
    truncated: bool
    provider_id: str = "youtube_public"


class YouTubeCommentProvider(Protocol):
    request_count: int

    async def root_page(
        self,
        video_id: str,
        *,
        page_token: str | None,
        limit: int,
        order: str,
        cancellation: CancellationToken,
    ) -> Mapping[str, Any]: ...

    async def reply_page(
        self,
        parent_id: str,
        *,
        page_token: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> Mapping[str, Any]: ...


class YouTubeApiCommentProvider:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        max_requests: int,
    ) -> None:
        key = api_key.strip()
        if not key or len(key) > 512 or not 1 <= max_requests <= 100:
            raise ValueError("YouTube comment provider configuration is invalid")
        self.client = client
        self._api_key = key
        self.max_requests = max_requests
        self.request_count = 0

    def _spend(self) -> bool:
        if self.request_count >= self.max_requests:
            return False
        self.request_count += 1
        return True

    async def root_page(
        self,
        video_id: str,
        *,
        page_token: str | None,
        limit: int,
        order: str,
        cancellation: CancellationToken,
    ) -> Mapping[str, Any]:
        cancellation.raise_if_cancelled()
        params: dict[str, Any] = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": min(max(limit, 1), 100),
            "order": order,
            "textFormat": "plainText",
            "key": self._api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        return await youtube_json(
            self.client,
            "/commentThreads",
            params=params,
            before_request=self._spend,
        )

    async def reply_page(
        self,
        parent_id: str,
        *,
        page_token: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> Mapping[str, Any]:
        cancellation.raise_if_cancelled()
        params: dict[str, Any] = {
            "part": "snippet",
            "parentId": parent_id,
            "maxResults": min(max(limit, 1), 100),
            "textFormat": "plainText",
            "key": self._api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        return await youtube_json(
            self.client,
            "/comments",
            params=params,
            before_request=self._spend,
        )


class YouTubeCommentsAdapter:
    source_id = "youtube"
    provider_id = "youtube_public"

    def __init__(
        self,
        provider: YouTubeCommentProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer

    async def crawl(
        self,
        target_value: str,
        budgets: YouTubeCommentBudgets,
        *,
        order: str = "time",
        cancellation: CancellationToken | None = None,
    ) -> YouTubeCommentScan:
        if order == "new":
            order = "time"
        if order not in _ORDERS:
            raise ValueError("Unsupported YouTube comment order")
        token = cancellation or CancellationToken()
        target = parse_youtube_video_target(target_value)
        records: list[CommentRecord] = []
        seen: set[str] = set()
        root_count = 0
        child_count = 0
        page_token: str | None = None
        seen_root_cursors: set[str] = set()
        truncated = False

        while (
            root_count < budgets.max_root_comments
            and len(records) < budgets.max_total_comments
        ):
            remaining_roots = budgets.max_root_comments - root_count
            remaining_total = budgets.max_total_comments - len(records)
            try:
                page = await self.provider.root_page(
                    target.video_id,
                    page_token=page_token,
                    limit=min(100, remaining_roots, remaining_total),
                    order=order,
                    cancellation=token,
                )
            except CrawlerFailure as exc:
                if records and exc.code is CrawlerErrorCode.BUDGET_EXHAUSTED:
                    truncated = True
                    break
                raise
            items = _items(page, "YouTube returned invalid comment threads.")
            if not items:
                break
            for thread in items:
                token.raise_if_cancelled()
                if (
                    root_count >= budgets.max_root_comments
                    or len(records) >= budgets.max_total_comments
                ):
                    truncated = True
                    break
                root, declared_replies = _root_comment(thread)
                record = _normalize_comment(
                    root,
                    content_external_id=target.video_id,
                    root_external_id=None,
                    declared_children=declared_replies,
                    pseudonymizer=self.pseudonymizer,
                )
                if record.external_id in seen:
                    continue
                record = CommentRecord(
                    source_id=record.source_id,
                    external_id=record.external_id,
                    content_external_id=record.content_external_id,
                    body=record.body,
                    author_pseudonym=record.author_pseudonym,
                    published_at=record.published_at,
                    like_count=record.like_count,
                    child_count=record.child_count,
                    parent_external_id=None,
                    root_external_id=record.external_id,
                    provenance=record.provenance,
                )
                seen.add(record.external_id)
                records.append(record)
                root_count += 1
                if declared_replies <= 0 or budgets.max_children_per_root == 0:
                    truncated = truncated or declared_replies > 0
                    continue
                reply_cursor: str | None = None
                seen_reply_cursors: set[str] = set()
                replies_for_root = 0
                while (
                    replies_for_root < budgets.max_children_per_root
                    and replies_for_root < declared_replies
                    and len(records) < budgets.max_total_comments
                ):
                    remaining_replies = min(
                        budgets.max_children_per_root - replies_for_root,
                        declared_replies - replies_for_root,
                        budgets.max_total_comments - len(records),
                        100,
                    )
                    try:
                        reply_page = await self.provider.reply_page(
                            record.external_id,
                            page_token=reply_cursor,
                            limit=remaining_replies,
                            cancellation=token,
                        )
                    except CrawlerFailure as exc:
                        if exc.code is CrawlerErrorCode.BUDGET_EXHAUSTED:
                            truncated = True
                            break
                        raise
                    replies = _items(
                        reply_page, "YouTube returned invalid comment replies."
                    )
                    if not replies:
                        break
                    for reply in replies:
                        child = _normalize_comment(
                            reply,
                            content_external_id=target.video_id,
                            root_external_id=record.external_id,
                            declared_children=0,
                            pseudonymizer=self.pseudonymizer,
                        )
                        if child.parent_external_id != record.external_id:
                            raise _parse_changed(
                                "YouTube reply hierarchy does not match its thread."
                            )
                        if child.external_id in seen:
                            continue
                        seen.add(child.external_id)
                        records.append(child)
                        replies_for_root += 1
                        child_count += 1
                        if (
                            replies_for_root >= budgets.max_children_per_root
                            or len(records) >= budgets.max_total_comments
                        ):
                            break
                    next_reply = _next_token(reply_page)
                    if not next_reply:
                        break
                    if next_reply == reply_cursor or next_reply in seen_reply_cursors:
                        truncated = True
                        break
                    seen_reply_cursors.add(next_reply)
                    reply_cursor = next_reply
                if replies_for_root < declared_replies:
                    truncated = True
            next_root = _next_token(page)
            if not next_root:
                break
            if next_root == page_token or next_root in seen_root_cursors:
                truncated = True
                break
            seen_root_cursors.add(next_root)
            page_token = next_root

        if root_count >= budgets.max_root_comments or len(records) >= budgets.max_total_comments:
            truncated = True
        return YouTubeCommentScan(
            target=target,
            records=tuple(records),
            request_count=self.provider.request_count,
            root_count=root_count,
            child_count=child_count,
            truncated=truncated,
        )


def _root_comment(thread: Mapping[str, Any]) -> tuple[Mapping[str, Any], int]:
    snippet = thread.get("snippet")
    if not isinstance(snippet, Mapping):
        raise _parse_changed("YouTube comment thread snippet is missing.")
    root = snippet.get("topLevelComment")
    if not isinstance(root, Mapping):
        raise _parse_changed("YouTube top-level comment is missing.")
    return root, max(0, _integer(snippet.get("totalReplyCount")))


def _normalize_comment(
    value: Mapping[str, Any],
    *,
    content_external_id: str,
    root_external_id: str | None,
    declared_children: int,
    pseudonymizer: IdentityPseudonymizer,
) -> CommentRecord:
    external_id = str(value.get("id") or "").strip()
    snippet = value.get("snippet")
    if not external_id or len(external_id) > 256 or not isinstance(snippet, Mapping):
        raise _parse_changed("YouTube comment identity or snippet is invalid.")
    author_channel = snippet.get("authorChannelId")
    author_id = (
        str(author_channel.get("value") or "")
        if isinstance(author_channel, Mapping)
        else ""
    )
    parent = str(snippet.get("parentId") or "").strip() or None
    return CommentRecord(
        source_id="youtube",
        external_id=external_id,
        content_external_id=content_external_id,
        body=str(snippet.get("textOriginal") or "")[:4_000],
        author_pseudonym=pseudonymizer.pseudonym("youtube", author_id),
        published_at=_timestamp(snippet.get("publishedAt")),
        like_count=max(0, _integer(snippet.get("likeCount"))),
        child_count=declared_children,
        parent_external_id=parent,
        root_external_id=root_external_id,
        provenance={
            "provider_id": "youtube_public",
            "contract_version": "cbce.youtube.comments.v1",
            "coverage": "bounded_official_comment_tree",
        },
    )


def _items(payload: Mapping[str, Any], message: str) -> tuple[Mapping[str, Any], ...]:
    items = payload.get("items")
    if items is None:
        return ()
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise _parse_changed(message)
    if any(not isinstance(item, Mapping) for item in items):
        raise _parse_changed(message)
    return tuple(items)  # type: ignore[arg-type]


def _next_token(payload: Mapping[str, Any]) -> str | None:
    value = str(payload.get("nextPageToken") or "").strip()
    if len(value) > 4_096:
        raise _parse_changed("YouTube comment cursor is invalid.")
    return value or None


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _parse_changed("YouTube comment timestamp is invalid.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_changed(message: str) -> CrawlerFailure:
    return CrawlerFailure(CrawlerErrorCode.PARSE_CHANGED, message)
