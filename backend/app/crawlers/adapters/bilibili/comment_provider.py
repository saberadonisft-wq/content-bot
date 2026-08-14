"""Bilibili root comments from the public page-owned Web Component state."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from ...runtime import (
    BrowserSession,
    CancellationToken,
    CommentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    Page,
    PlaywrightBrowserHandle,
    RunContext,
)
from .targets import BilibiliTargetKind, parse_bilibili_target


@dataclass(frozen=True, slots=True)
class BilibiliComment:
    comment_id: str
    content_id: str
    body: str
    author_id: str = ""
    created_at: datetime | None = None
    like_count: int = 0
    child_count: int = 0
    parent_id: str | None = None
    root_id: str | None = None


@dataclass(frozen=True, slots=True)
class BilibiliCommentPage:
    items: tuple[BilibiliComment, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class BilibiliCommentCursor:
    offset: int = 0

    def __post_init__(self) -> None:
        if self.offset < 0:
            raise ValueError("Invalid Bilibili comment cursor")

    def encode(self) -> str:
        return f"v1m:{self.offset}"

    @classmethod
    def decode(cls, value: str | None) -> BilibiliCommentCursor:
        if value is None:
            return cls()
        match = re.fullmatch(r"v1m:([0-9]+)", value)
        if not match:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili comment checkpoint cursor is invalid.",
            )
        return cls(int(match.group(1)))


@dataclass(frozen=True, slots=True)
class BilibiliChildCommentCursor:
    root_id: str
    page_number: int = 1
    offset: int = 0

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[1-9][0-9]*", self.root_id)
            or self.page_number < 1
            or self.offset < 0
        ):
            raise ValueError("Invalid Bilibili child-comment cursor")

    def encode(self) -> str:
        return f"v1s:{self.root_id}:{self.page_number}:{self.offset}"

    @classmethod
    def decode(
        cls,
        value: str | None,
        *,
        root_id: str,
    ) -> BilibiliChildCommentCursor:
        if value is None:
            return cls(root_id)
        match = re.fullmatch(
            r"v1s:([1-9][0-9]*):([1-9][0-9]*):([0-9]+)", value
        )
        if not match or match.group(1) != root_id:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili child-comment checkpoint cursor is invalid.",
            )
        return cls(match.group(1), int(match.group(2)), int(match.group(3)))


class BilibiliCommentsProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def root_comments(
        self,
        target: str,
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> BilibiliCommentPage: ...

    async def child_comments(
        self,
        target: str,
        root_id: str,
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> BilibiliCommentPage: ...

    async def close(self) -> None: ...


class BilibiliDomCommentsProvider:
    def __init__(
        self,
        browser: BrowserSession,
        *,
        on_auth_required: Callable[[], None] | None = None,
        on_authenticated: Callable[[], None] | None = None,
        auth_timeout_seconds: float = 600,
    ) -> None:
        if auth_timeout_seconds <= 0:
            raise ValueError("Bilibili auth timeout must be positive")
        self.browser = browser
        self.on_auth_required = on_auth_required
        self.on_authenticated = on_authenticated
        self.auth_timeout_seconds = auth_timeout_seconds
        self._page: Any | None = None
        self._target: str | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        handle = await self.browser.open(cancellation)
        if not isinstance(handle, PlaywrightBrowserHandle):
            raise TypeError(
                "Bilibili DOM provider requires the Playwright browser driver"
            )
        self._page = (
            handle.context.pages[0]
            if handle.context.pages
            else await handle.context.new_page()
        )

    async def root_comments(
        self,
        target: str,
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> BilibiliCommentPage:
        if self._page is None:
            raise RuntimeError("Bilibili comments provider is not open")
        parsed = _video_target(target)
        state = BilibiliCommentCursor.decode(cursor)
        cancellation.raise_if_cancelled()
        if self._target != parsed.canonical_url:
            await self._navigate(parsed.canonical_url)
            self._target = parsed.canonical_url
        desired = state.offset + limit
        snapshot = await self._load_until(desired, cancellation)
        if snapshot["auth_required"]:
            await self._wait_for_auth(cancellation)
            snapshot = await self._load_until(desired, cancellation)
        raw_items = snapshot["items"][state.offset : desired]
        items = tuple(
            parse_bilibili_comment(item, parsed.external_id or "") for item in raw_items
        )
        next_offset = state.offset + len(items)
        has_more = next_offset < snapshot["loaded"] or not snapshot["end"]
        next_cursor = BilibiliCommentCursor(next_offset).encode() if has_more else None
        return BilibiliCommentPage(items, next_cursor, has_more)

    async def child_comments(
        self,
        target: str,
        root_id: str,
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> BilibiliCommentPage:
        if self._page is None:
            raise RuntimeError("Bilibili comments provider is not open")
        if not re.fullmatch(r"[1-9][0-9]*", root_id):
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili child-comment root identity is invalid.",
            )
        parsed = _video_target(target)
        state = BilibiliChildCommentCursor.decode(cursor, root_id=root_id)
        cancellation.raise_if_cancelled()
        if self._target != parsed.canonical_url:
            await self._navigate(parsed.canonical_url)
            self._target = parsed.canonical_url
        snapshot = await self._load_child_page(
            root_id,
            state.page_number,
            cancellation,
        )
        if snapshot["auth_required"]:
            await self._wait_for_auth(cancellation)
            snapshot = await self._load_child_page(
                root_id,
                state.page_number,
                cancellation,
            )
        desired = state.offset + limit
        raw_items = snapshot["items"][state.offset : desired]
        items = tuple(
            parse_bilibili_comment(item, parsed.external_id or "") for item in raw_items
        )
        next_offset = state.offset + len(items)
        if next_offset < snapshot["loaded"]:
            next_state = BilibiliChildCommentCursor(
                root_id,
                state.page_number,
                next_offset,
            )
        elif state.page_number < snapshot["total_pages"]:
            next_state = BilibiliChildCommentCursor(
                root_id,
                state.page_number + 1,
                0,
            )
        else:
            next_state = None
        if (
            next_state is None
            and snapshot["declared"] > snapshot["loaded"]
            and snapshot["total_pages"] <= state.page_number
        ):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili child-comment component reported incomplete pagination.",
            )
        return BilibiliCommentPage(
            items,
            next_state.encode() if next_state else None,
            next_state is not None,
        )

    async def close(self) -> None:
        page, self._page = self._page, None
        self._target = None
        if page is not None and not page.is_closed():
            await page.close()
        await self.browser.close()

    async def _navigate(self, target: str) -> None:
        assert self._page is not None
        response = await self._page.goto(
            target,
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        status = response.status if response is not None else 0
        if status in {429, 412}:
            raise CrawlerFailure(
                CrawlerErrorCode.RATE_LIMITED,
                "Bilibili temporarily limited the public comment session.",
                retryable=True,
            )
        if status in {401, 403}:
            raise CrawlerFailure(
                CrawlerErrorCode.CHALLENGE_REQUIRED,
                "Bilibili requires user interaction in the visible browser.",
            )
        if status == 404:
            raise CrawlerFailure(
                CrawlerErrorCode.NOT_FOUND,
                "Bilibili public video was not found.",
            )
        if status >= 500 or status == 0:
            raise CrawlerFailure(
                CrawlerErrorCode.TRANSPORT_ERROR,
                "Bilibili public comment navigation failed.",
                retryable=True,
            )
        await self._page.locator("bili-comments").scroll_into_view_if_needed(
            timeout=20_000
        )
        await self._page.wait_for_timeout(3_000)

    async def _load_until(
        self,
        desired: int,
        cancellation: CancellationToken,
    ) -> dict[str, Any]:
        assert self._page is not None
        previous_loaded = -1
        stalled = 0
        for _ in range(100):
            cancellation.raise_if_cancelled()
            snapshot = await self._snapshot()
            if snapshot["auth_required"]:
                return snapshot
            if snapshot["loaded"] >= desired or snapshot["end"]:
                return snapshot
            if snapshot["loaded"] == previous_loaded:
                stalled += 1
            else:
                stalled = 0
            if stalled >= 3:
                raise CrawlerFailure(
                    CrawlerErrorCode.CURSOR_STALLED,
                    "Bilibili comment feed stopped advancing.",
                    retryable=True,
                )
            previous_loaded = snapshot["loaded"]
            await self._page.evaluate(
                "() => document.querySelector('bili-comments')?.shadowRoot?.querySelector('#end')?.scrollIntoView({block: 'center'})"
            )
            await self._page.wait_for_timeout(1_000)
        raise CrawlerFailure(
            CrawlerErrorCode.DEADLINE_EXCEEDED,
            "Bilibili comment feed exceeded its bounded load loop.",
            retryable=True,
        )

    async def _snapshot(self) -> dict[str, Any]:
        assert self._page is not None
        payload = await self._page.evaluate(
            """() => {
                const host = document.querySelector('bili-comments');
                if (!host || !host.shadowRoot || !Array.isArray(host.list)) {
                    return null;
                }
                const mask = host.shadowRoot.querySelector('#limit-mask');
                const maskVisible = Boolean(mask && mask.getBoundingClientRect().height > 0);
                const compact = (item) => ({
                    rpid_str: String(item?.rpid_str || item?.rpid || ''),
                    root_str: String(item?.root_str || item?.root || ''),
                    parent_str: String(item?.parent_str || item?.parent || ''),
                    mid_str: String(item?.mid_str || item?.member?.mid || item?.mid || ''),
                    ctime: Number(item?.ctime || 0),
                    like: Number(item?.like || 0),
                    count: Number(item?.count || item?.rcount || 0),
                    message: String(item?.content?.message || ''),
                });
                return {
                    items: host.list.map(compact),
                    loaded: host.list.length,
                    total: Number(host.count || 0),
                    end: Boolean(host.showEnd),
                    auth_required: maskVisible && Number(host.count || 0) > host.list.length,
                };
            }"""
        )
        if not isinstance(payload, dict):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili comments Web Component was not recognized.",
            )
        return payload

    async def _wait_for_auth(self, cancellation: CancellationToken) -> None:
        assert self._page is not None
        if self.on_auth_required is None:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "Log in in the visible Bilibili browser to load the full comment feed.",
            )
        if self.on_auth_required is not None:
            self.on_auth_required()
        deadline = asyncio.get_running_loop().time() + self.auth_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            cancellation.raise_if_cancelled()
            await self._page.wait_for_timeout(2_000)
            snapshot = await self._snapshot()
            if not snapshot["auth_required"]:
                if self.on_authenticated is not None:
                    self.on_authenticated()
                return
        raise CrawlerFailure(
            CrawlerErrorCode.AUTH_TIMEOUT,
            "Timed out while waiting for Bilibili login in the visible browser.",
            retryable=True,
        )

    async def _load_child_page(
        self,
        root_id: str,
        page_number: int,
        cancellation: CancellationToken,
    ) -> dict[str, Any]:
        thread = await self._find_root_thread(root_id, cancellation)
        replies = thread.locator("bili-comment-replies-renderer").first
        if not await replies.count():
            declared = await thread.evaluate("el => Number(el.data?.count || 0)")
            if declared:
                raise CrawlerFailure(
                    CrawlerErrorCode.PARSE_CHANGED,
                    "Bilibili child-comment component was not recognized.",
                )
            return {
                "items": [],
                "loaded": 0,
                "declared": 0,
                "current_page": 1,
                "total_pages": 1,
                "show_view_more": False,
                "show_pagination": False,
                "auth_required": False,
            }

        snapshot = await self._child_snapshot(thread, replies)
        if snapshot["auth_required"]:
            return snapshot
        if snapshot["show_view_more"]:
            button = replies.locator("#view-more bili-text-button").first
            if not await button.count():
                raise CrawlerFailure(
                    CrawlerErrorCode.PARSE_CHANGED,
                    "Bilibili child-comment expansion control was not recognized.",
                )
            previous_ids = tuple(item["rpid_str"] for item in snapshot["items"])
            await button.click(timeout=10_000)
            snapshot = await self._wait_for_child_change(
                thread,
                replies,
                previous_ids,
                cancellation,
            )
            if snapshot["auth_required"]:
                return snapshot

        while snapshot["current_page"] < page_number:
            cancellation.raise_if_cancelled()
            next_page = snapshot["current_page"] + 1
            pagination = replies.locator("bili-pagination, #pagination").first
            if not await pagination.count():
                raise CrawlerFailure(
                    CrawlerErrorCode.PARSE_CHANGED,
                    "Bilibili child-comment pagination control was not recognized.",
                )
            page_button = pagination.get_by_text(str(next_page), exact=True).first
            if not await page_button.count():
                raise CrawlerFailure(
                    CrawlerErrorCode.PARSE_CHANGED,
                    "Bilibili child-comment next-page control was not recognized.",
                )
            previous_ids = tuple(item["rpid_str"] for item in snapshot["items"])
            await page_button.click(timeout=10_000)
            snapshot = await self._wait_for_child_change(
                thread,
                replies,
                previous_ids,
                cancellation,
            )
            if snapshot["current_page"] != next_page:
                raise CrawlerFailure(
                    CrawlerErrorCode.CURSOR_STALLED,
                    "Bilibili child-comment page stopped advancing.",
                    retryable=True,
                )
        if snapshot["current_page"] != page_number:
            raise CrawlerFailure(
                CrawlerErrorCode.CURSOR_STALLED,
                "Bilibili child-comment cursor moved to an unexpected page.",
            )
        return snapshot

    async def _find_root_thread(
        self,
        root_id: str,
        cancellation: CancellationToken,
    ) -> Any:
        assert self._page is not None
        for _ in range(100):
            cancellation.raise_if_cancelled()
            threads = self._page.locator(
                "bili-comments bili-comment-thread-renderer"
            )
            for index in range(await threads.count()):
                thread = threads.nth(index)
                observed_id = await thread.evaluate(
                    "el => String(el.data?.rpid_str || el.data?.rpid || '')"
                )
                if observed_id == root_id:
                    return thread
            snapshot = await self._snapshot()
            if snapshot["auth_required"]:
                raise CrawlerFailure(
                    CrawlerErrorCode.AUTH_REQUIRED,
                    "Log in in the visible Bilibili browser to locate the root comment.",
                )
            if snapshot["end"]:
                raise CrawlerFailure(
                    CrawlerErrorCode.NOT_FOUND,
                    "Bilibili root comment was not found in the current feed.",
                )
            await self._page.evaluate(
                "() => document.querySelector('bili-comments')?.shadowRoot?.querySelector('#end')?.scrollIntoView({block: 'center'})"
            )
            await self._page.wait_for_timeout(1_000)
        raise CrawlerFailure(
            CrawlerErrorCode.DEADLINE_EXCEEDED,
            "Bilibili root-comment lookup exceeded its bounded load loop.",
            retryable=True,
        )

    async def _wait_for_child_change(
        self,
        thread: Any,
        replies: Any,
        previous_ids: tuple[str, ...],
        cancellation: CancellationToken,
    ) -> dict[str, Any]:
        snapshot: dict[str, Any] | None = None
        for _ in range(20):
            cancellation.raise_if_cancelled()
            snapshot = await self._child_snapshot(thread, replies)
            observed_ids = tuple(item["rpid_str"] for item in snapshot["items"])
            if (
                snapshot["auth_required"]
                or observed_ids != previous_ids
                or snapshot["show_pagination"]
                or not snapshot["show_view_more"]
            ):
                return snapshot
            assert self._page is not None
            await self._page.wait_for_timeout(500)
        raise CrawlerFailure(
            CrawlerErrorCode.CURSOR_STALLED,
            "Bilibili child-comment component stopped advancing.",
            retryable=True,
        )

    async def _child_snapshot(self, thread: Any, replies: Any) -> dict[str, Any]:
        assert self._page is not None
        components = replies.locator("bili-comment-reply-renderer")
        items: list[dict[str, Any]] = []
        for index in range(await components.count()):
            payload = await components.nth(index).evaluate(
                """el => {
                    const item = el.data || {};
                    return {
                        rpid_str: String(item.rpid_str || item.rpid || ''),
                        root_str: String(item.root_str || item.root || ''),
                        parent_str: String(item.parent_str || item.parent || ''),
                        mid_str: String(item.mid_str || item.member?.mid || item.mid || ''),
                        ctime: Number(item.ctime || 0),
                        like: Number(item.like || 0),
                        count: Number(item.count || item.rcount || 0),
                        message: String(item.content?.message || ''),
                    };
                }"""
            )
            if isinstance(payload, dict):
                items.append(payload)
        state = await replies.evaluate(
            """el => ({
                declared: Number(el.data?.count || 0),
                current_page: Number(el.currentPage || 1),
                total_pages: Math.max(1, Number(el.totalPage || 1)),
                show_view_more: Boolean(el.showViewMore),
                show_pagination: Boolean(el.showPagination),
            })"""
        )
        thread_count = await thread.evaluate("el => Number(el.data?.count || 0)")
        mask_visible = await self._page.evaluate(
            """() => {
                const mask = document.querySelector('bili-comments')?.shadowRoot?.querySelector('#limit-mask');
                return Boolean(mask && mask.getBoundingClientRect().height > 0);
            }"""
        )
        declared = max(_nonnegative_int(state.get("declared")), thread_count)
        return {
            "items": items,
            "loaded": len(items),
            "declared": declared,
            "current_page": max(1, _nonnegative_int(state.get("current_page"))),
            "total_pages": max(1, _nonnegative_int(state.get("total_pages"))),
            "show_view_more": bool(state.get("show_view_more")),
            "show_pagination": bool(state.get("show_pagination")),
            "auth_required": bool(mask_visible and declared > len(items)),
        }


class BilibiliCommentsAdapter:
    source_id = "bilibili"
    provider_id = "cbce_bilibili"
    owns_resources = True

    def __init__(
        self,
        provider: BilibiliCommentsProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._target: str | None = None
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        target = context.target.get("url")
        if not isinstance(target, str):
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili comments require a public video URL.",
            )
        parsed = _video_target(target)
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
            raise RuntimeError("Bilibili comments adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili comment checkpoint has an unsupported shape.",
            )
        provider_page = await self.provider.root_comments(
            self._target,
            cursor,
            limit,
            self._cancellation,
        )
        records = tuple(self._normalize(comment) for comment in provider_page.items)
        return Page(records, provider_page.next_cursor, provider_page.has_more)

    async def close(self) -> None:
        self._target = None
        self._cancellation = None
        await self.provider.close()

    def _normalize(self, comment: BilibiliComment) -> CommentRecord:
        return _normalize_comment(comment, self.pseudonymizer, "root_comments")


class BilibiliChildCommentsAdapter:
    source_id = "bilibili"
    provider_id = "cbce_bilibili"
    owns_resources = True

    def __init__(
        self,
        provider: BilibiliCommentsProvider,
        pseudonymizer: IdentityPseudonymizer,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self._target: str | None = None
        self._root_id: str | None = None
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        target = context.target.get("url")
        root_id = context.target.get("root_comment_id")
        if not isinstance(target, str) or not isinstance(root_id, str):
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili child comments require a video URL and root comment ID.",
            )
        parsed = _video_target(target)
        if not re.fullmatch(r"[1-9][0-9]*", root_id):
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili child-comment root identity is invalid.",
            )
        self._target = parsed.canonical_url
        self._root_id = root_id
        self._cancellation = cancellation
        await self.provider.open(context, cancellation)

    async def fetch_page(
        self,
        context: RunContext,
        cursor: object | None,
        limit: int,
    ) -> Page[CommentRecord, str]:
        if (
            self._target is None
            or self._root_id is None
            or self._cancellation is None
        ):
            raise RuntimeError("Bilibili child-comments adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili child-comment checkpoint has an unsupported shape.",
            )
        provider_page = await self.provider.child_comments(
            self._target,
            self._root_id,
            cursor,
            limit,
            self._cancellation,
        )
        records = tuple(
            _normalize_comment(comment, self.pseudonymizer, "child_comments")
            for comment in provider_page.items
        )
        return Page(records, provider_page.next_cursor, provider_page.has_more)

    async def close(self) -> None:
        self._target = None
        self._root_id = None
        self._cancellation = None
        await self.provider.close()


def _normalize_comment(
    comment: BilibiliComment,
    pseudonymizer: IdentityPseudonymizer,
    coverage: str,
) -> CommentRecord:
    return CommentRecord(
        source_id="bilibili",
        external_id=comment.comment_id,
        content_external_id=comment.content_id,
        body=comment.body[:4_000],
        author_pseudonym=pseudonymizer.pseudonym("bilibili", comment.author_id),
        published_at=comment.created_at,
        like_count=comment.like_count,
        child_count=comment.child_count,
        parent_external_id=comment.parent_id,
        root_external_id=comment.root_id,
        provenance={
            "provider_id": "cbce_bilibili",
            "contract_version": "cbce.bilibili.comment.v1",
            "coverage": coverage,
        },
    )


def parse_bilibili_comment(payload: Any, content_id: str) -> BilibiliComment:
    if not isinstance(payload, dict):
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Bilibili comment payload is invalid.",
        )
    comment_id = str(payload.get("rpid_str") or "").strip()
    body = str(payload.get("message") or "").strip()
    if not comment_id or not body or not content_id:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Bilibili comment identity or content is missing.",
        )
    timestamp = _nonnegative_int(payload.get("ctime"))
    created_at = datetime.fromtimestamp(timestamp, tz=UTC) if timestamp else None
    root = _optional_platform_id(payload.get("root_str"))
    parent = _optional_platform_id(payload.get("parent_str"))
    return BilibiliComment(
        comment_id=comment_id,
        content_id=content_id,
        body=body,
        author_id=str(payload.get("mid_str") or "").strip(),
        created_at=created_at,
        like_count=_nonnegative_int(payload.get("like")),
        child_count=_nonnegative_int(payload.get("count")),
        parent_id=parent,
        root_id=root,
    )


def _video_target(target: str):
    try:
        parsed = parse_bilibili_target(target)
    except ValueError as exc:
        raise CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "Bilibili comment target is invalid.",
        ) from exc
    if parsed.kind is not BilibiliTargetKind.VIDEO:
        raise CrawlerFailure(
            CrawlerErrorCode.UNSUPPORTED,
            "Bilibili comment target is not a video.",
        )
    return parsed


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _optional_platform_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text and text != "0" else None
