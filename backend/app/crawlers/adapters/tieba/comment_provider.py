"""Bounded Tieba root comments from project-observed public DOM."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ...runtime import (
    CancellationToken,
    CommentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    Page,
    RunContext,
)
from ..browser_session import OwnedBrowserPage
from .targets import TiebaTargetKind, parse_tieba_target

_EXTRACT_ROOT_COMMENTS = r"""
() => {
  const root = document.querySelector('.pc-pb-reply-list');
  if (!root) return {recognized: false, items: []};
  const items = Array.from(root.querySelectorAll('.pb-comment-item'))
  .slice(0, 500).map((card) => {
    const holder = card.closest('[data-id]');
    const body = card.querySelector('.comment-content .pb-rich-text');
    const author = card.querySelector('a.head-name, a[href*="/home/main"]');
    const likes = card.querySelector('.zan-container .action-number');
    return {
      id: holder ? holder.getAttribute('data-id') || '' : '',
      body: body ? body.innerText : '',
      author: author ? (author.href || author.getAttribute('data-user-id') || '') : '',
      like_count: likes ? likes.innerText : '',
      child_count: card.querySelectorAll('.pb-lzl-item[data-id]').length,
    };
  });
  return {recognized: true, items};
}
"""

@dataclass(frozen=True, slots=True)
class TiebaComment:
    comment_id: str
    thread_id: str
    body: str
    author_id: str = ""
    like_count: int = 0
    child_count: int = 0


@dataclass(frozen=True, slots=True)
class TiebaCommentPage:
    items: tuple[TiebaComment, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class TiebaCommentCursor:
    thread_id: str
    offset: int = 0

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[1-9][0-9]{0,19}", self.thread_id) or self.offset < 0:
            raise ValueError("Invalid Tieba comment cursor")

    def encode(self) -> str:
        return f"v1m:{self.thread_id}:{self.offset}"

    @classmethod
    def decode(cls, thread_id: str, value: str | None) -> TiebaCommentCursor:
        if value is None:
            return cls(thread_id)
        match = re.fullmatch(r"v1m:([1-9][0-9]{0,19}):([0-9]+)", value)
        if not match or match.group(1) != thread_id:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Tieba comment checkpoint is invalid.",
            )
        return cls(match.group(1), int(match.group(2)))


class TiebaCommentsProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def root_comments(
        self,
        target: str,
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> TiebaCommentPage: ...

    async def close(self) -> None: ...


class TiebaDomCommentsProvider:
    def __init__(self, browser_page: OwnedBrowserPage) -> None:
        self.browser_page = browser_page
        self._context: RunContext | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if context.source_id != "tieba" or context.operation != "list_comments":
            raise ValueError("Tieba comments provider context is invalid")
        self._context = context
        await self.browser_page.open(context, cancellation)

    async def root_comments(
        self,
        target: str,
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> TiebaCommentPage:
        if self._context is None:
            raise RuntimeError("Tieba comments provider is not open")
        if not 1 <= limit <= 500:
            raise ValueError("Tieba comment limit is invalid")
        parsed = _thread_target(target)
        state = TiebaCommentCursor.decode(parsed.external_id, cursor)
        page = await self.browser_page.navigate(
            parsed.canonical_url,
            cancellation,
            wait_after_ms=4_000,
        )
        raw = await page.evaluate(_EXTRACT_ROOT_COMMENTS)
        if not isinstance(raw, Mapping) or raw.get("recognized") is not True:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Tieba root-comment DOM layout was not recognized.",
            )
        values = raw.get("items")
        if not isinstance(values, list):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Tieba root-comment collection changed.",
            )
        items: list[TiebaComment] = []
        seen: set[str] = set()
        for value in values:
            cancellation.raise_if_cancelled()
            item = _parse_comment(value, parsed.external_id)
            if item is not None and item.comment_id not in seen:
                seen.add(item.comment_id)
                items.append(item)
        start = min(state.offset, len(items))
        stop = min(len(items), start + limit)
        next_state = (
            TiebaCommentCursor(parsed.external_id, stop) if stop < len(items) else None
        )
        return TiebaCommentPage(
            tuple(items[start:stop]),
            next_state.encode() if next_state else None,
            next_state is not None,
        )

    async def close(self) -> None:
        self._context = None
        await self.browser_page.close()


class TiebaCommentsAdapter:
    source_id = "tieba"
    provider_id = "cbce_tieba"
    owns_resources = True

    def __init__(
        self,
        provider: TiebaCommentsProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._target: str | None = None
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        target = context.target.get("url")
        if context.operation != "list_comments" or not isinstance(target, str):
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Tieba comments require a public thread URL.",
            )
        parsed = _thread_target(target)
        self._target = parsed.canonical_url
        self._cancellation = cancellation
        await self.provider.open(context, cancellation)

    async def fetch_page(
        self,
        context: RunContext,
        cursor: object | None,
        limit: int,
    ) -> Page[CommentRecord, str]:
        if self._target is None or self._cancellation is None:
            raise RuntimeError("Tieba comments adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Tieba comment checkpoint has an unsupported shape.",
            )
        page = await self.provider.root_comments(
            self._target,
            cursor,
            limit,
            self._cancellation,
        )
        return Page(
            tuple(_normalize(item, self.pseudonymizer) for item in page.items),
            page.next_cursor,
            page.has_more,
        )

    async def close(self) -> None:
        self._target = None
        self._cancellation = None
        await self.provider.close()


def _thread_target(value: str):
    try:
        parsed = parse_tieba_target(value)
    except ValueError as exc:
        raise CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "Tieba comment target is invalid.",
        ) from exc
    if parsed.kind is not TiebaTargetKind.THREAD:
        raise CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "Tieba comment target is not a thread.",
        )
    return parsed


def _parse_comment(value: Any, thread_id: str) -> TiebaComment | None:
    if not isinstance(value, Mapping):
        return None
    comment_id = str(value.get("id") or "").strip()
    body = str(value.get("body") or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,19}", comment_id) or not body:
        return None
    return TiebaComment(
        comment_id,
        thread_id,
        body[:4_000],
        author_id=str(value.get("author") or "")[:500],
        like_count=_count(value.get("like_count")),
        child_count=_count(value.get("child_count")),
    )


def _count(value: Any) -> int:
    match = re.search(r"[0-9]+", str(value or "").replace(",", ""))
    return int(match.group()) if match else 0


def _normalize(
    comment: TiebaComment,
    pseudonymizer: IdentityPseudonymizer,
) -> CommentRecord:
    return CommentRecord(
        source_id="tieba",
        external_id=comment.comment_id,
        content_external_id=comment.thread_id,
        body=comment.body,
        author_pseudonym=pseudonymizer.pseudonym("tieba", comment.author_id),
        like_count=comment.like_count,
        child_count=comment.child_count,
        provenance={
            "provider_id": "cbce_tieba",
            "contract_version": "cbce.tieba.comment.v1",
            "coverage": "rendered_root_comments_only",
        },
    )
