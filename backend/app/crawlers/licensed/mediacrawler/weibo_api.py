"""Bounded Weibo mobile API adapter derived from the licensed upstream client.

Only the platform request shape and result-card vocabulary are reused here.
CBCE still owns the visible browser, profile, cancellation, budgets, privacy
normalization and persistence.  No upstream proxy pool, mutable config, store,
CDP manager or anti-detection code is imported.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from ...adapters.browser_session import BrowserEventHandler, OwnedBrowserPage
from ...adapters.weibo.provider import (
    WeiboPost,
    WeiboPostPage,
    WeiboSearchCursor,
)
from ...runtime import CancellationToken, CrawlerErrorCode, CrawlerFailure, RunContext
from .weibo_field import SearchType

_MAX_RESPONSE_BYTES = 2_000_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_SAFE_METRICS = frozenset({"like_count", "comment_count", "share_count"})


class LicensedWeiboApiSearchProvider:
    """Use the logged-in mobile API through the owned browser page."""

    def __init__(
        self,
        browser_page: OwnedBrowserPage,
        *,
        search_type: SearchType = SearchType.DEFAULT,
        auth_timeout_seconds: float = 600,
        on_auth_required: BrowserEventHandler | None = None,
        on_authenticated: BrowserEventHandler | None = None,
    ) -> None:
        if not 1 <= auth_timeout_seconds <= 7_200:
            raise ValueError("Weibo auth timeout must be between 1 and 7200 seconds")
        self.browser_page = browser_page
        self.search_type = search_type
        self.auth_timeout_seconds = auth_timeout_seconds
        self.on_auth_required = on_auth_required
        self.on_authenticated = on_authenticated
        self._context: RunContext | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if context.source_id != "weibo" or context.operation != "search":
            raise ValueError("Licensed Weibo API context is invalid")
        self.search_type = _parse_search_type(
            context.filters.get("search_type", self.search_type.value)
        )
        self._context = context
        await self.browser_page.open(context, cancellation)
        await self.browser_page.navigate(
            "https://passport.weibo.com",
            cancellation,
            wait_after_ms=300,
            auth_timeout_seconds=self.auth_timeout_seconds,
            on_auth_required=self.on_auth_required,
            on_authenticated=self.on_authenticated,
        )
        page = await self.browser_page.navigate(
            "https://m.weibo.cn",
            cancellation,
            wait_after_ms=500,
            auth_timeout_seconds=self.auth_timeout_seconds,
            on_auth_required=self.on_auth_required,
            on_authenticated=self.on_authenticated,
        )
        # The home page is only a session probe.  Do not persist its body.
        if page is None:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "Weibo mobile session did not open.",
            )

    async def search_page(
        self,
        terms: tuple[str, ...],
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> WeiboPostPage:
        if self._context is None:
            raise RuntimeError("Licensed Weibo API provider is not open")
        if not 1 <= limit <= 500:
            raise ValueError("Weibo API page limit is invalid")
        state = WeiboSearchCursor.decode(cursor)
        if state.term_index >= len(terms):
            return WeiboPostPage((), None, False)
        url = _search_url(terms[state.term_index], state.page_number, self.search_type)
        page = await self.browser_page.navigate(
            url,
            cancellation,
            wait_after_ms=300,
            auth_timeout_seconds=self.auth_timeout_seconds,
            on_auth_required=self.on_auth_required,
            on_authenticated=self.on_authenticated,
        )
        body = await page.locator("body").inner_text()
        if len(body.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Weibo API response exceeded the bounded parser size.",
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Weibo mobile API response is not JSON.",
            ) from exc
        rows, api_has_more = _result_cards(payload)
        posts: list[WeiboPost] = []
        seen: set[str] = set()
        for row in rows:
            cancellation.raise_if_cancelled()
            post = _post_from_card(row)
            if post is not None and post.post_id not in seen:
                seen.add(post.post_id)
                posts.append(post)
        start = min(state.offset, len(posts))
        stop = min(len(posts), start + limit)
        items = tuple(posts[start:stop])
        if stop < len(posts):
            next_state = WeiboSearchCursor(state.term_index, state.page_number, stop)
        elif api_has_more:
            next_state = WeiboSearchCursor(state.term_index, state.page_number + 1, 0)
        elif state.term_index + 1 < len(terms):
            next_state = WeiboSearchCursor(state.term_index + 1, 1, 0)
        else:
            next_state = None
        return WeiboPostPage(
            items,
            next_state.encode() if next_state else None,
            next_state is not None,
        )

    async def close(self) -> None:
        self._context = None
        await self.browser_page.close()


def _search_url(term: str, page: int, search_type: SearchType) -> str:
    containerid = f"100103type={search_type.value}&q={term}"
    query = urlencode(
        {
            "containerid": containerid,
            "page_type": "searchall",
            "page": page,
        }
    )
    return f"https://m.weibo.cn/api/container/getIndex?{query}"


def _result_cards(payload: Any) -> tuple[list[Mapping[str, Any]], bool]:
    if not isinstance(payload, Mapping) or payload.get("ok") != 1:
        raise CrawlerFailure(
            CrawlerErrorCode.AUTH_REQUIRED,
            "Weibo mobile API session is not authorized for search.",
        )
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Weibo mobile API result shape changed.",
        )
    cards = data.get("cards")
    if not isinstance(cards, list):
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Weibo mobile API cards are missing.",
        )
    rows: list[Mapping[str, Any]] = []
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        if card.get("card_type") == 9:
            rows.append(card)
        group = card.get("card_group")
        if isinstance(group, list):
            rows.extend(
                item
                for item in group
                if isinstance(item, Mapping) and item.get("card_type") == 9
            )
    page_info = data.get("cardlistInfo") or data.get("pageInfo")
    has_more = isinstance(page_info, Mapping) and (
        bool(str(page_info.get("page_url") or "").strip())
        or _truthy_flag(page_info.get("has_more"))
    )
    return rows, has_more


def _post_from_card(card: Mapping[str, Any]) -> WeiboPost | None:
    mblog = card.get("mblog")
    if not isinstance(mblog, Mapping):
        return None
    post_id = str(mblog.get("id") or "").strip()
    text = _plain_text(mblog.get("text") or mblog.get("raw_text") or "")
    if not post_id or not _SAFE_ID.fullmatch(post_id) or not text:
        return None
    metrics = {
        "like_count": _counter(mblog.get("attitudes_count")),
        "comment_count": _counter(mblog.get("comments_count")),
        "share_count": _counter(mblog.get("reposts_count")),
    }
    metrics = {key: value for key, value in metrics.items() if key in _SAFE_METRICS and value is not None}
    media: list[dict[str, str]] = []
    pics = mblog.get("pics")
    if isinstance(pics, list):
        for pic in pics[:20]:
            if not isinstance(pic, Mapping):
                continue
            candidate = str(
                ((pic.get("large") or {}).get("url") if isinstance(pic.get("large"), Mapping) else None)
                or pic.get("original_pic")
                or ""
            )
            if candidate.startswith("https://"):
                media.append({"kind": "image", "url": candidate[:2_000]})
    return WeiboPost(
        post_id=post_id,
        canonical_url=f"https://m.weibo.cn/detail/{post_id}",
        text=text[:10_000],
        author_id=str((mblog.get("user") or {}).get("id") or "") if isinstance(mblog.get("user"), Mapping) else "",
        metrics=metrics,
        media=tuple(media),
    )


def _plain_text(value: object) -> str:
    text = re.sub(r"<[^>]*>", " ", str(value))
    return re.sub(r"\s+", " ", text).strip()


def _counter(value: object) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 2_000_000_000 else None


def _parse_search_type(value: object) -> SearchType:
    normalized = str(value).strip().casefold()
    by_name = {
        "default": SearchType.DEFAULT,
        "real_time": SearchType.REAL_TIME,
        "popular": SearchType.POPULAR,
        "video": SearchType.VIDEO,
    }
    if normalized in by_name:
        return by_name[normalized]
    try:
        return SearchType(str(value).strip())
    except ValueError as exc:
        raise ValueError("Licensed Weibo search type is unsupported") from exc


def _truthy_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value or "").strip().casefold() in {"1", "true", "yes"}
