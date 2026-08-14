"""Contract-driven Weibo DOM search using an owned visible browser session."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from ...runtime import CancellationToken, CrawlerErrorCode, CrawlerFailure, RunContext
from ..browser_session import OwnedBrowserPage
from .provider import WeiboPost, WeiboPostPage, WeiboSearchCursor
from .targets import WeiboTargetKind, parse_weibo_target

BrowserEventHandler = Callable[[], Awaitable[None] | None]
SearchUrlBuilder = Callable[[str, int], str]
_SAFE_METRICS = frozenset({"like_count", "comment_count", "share_count"})
_COUNT = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(万|亿|[kKmM])?")
_SELECTOR_LIMIT = 300

_EXTRACT_SCRIPT = r"""
({contract}) => {
  const root = document.querySelector(contract.root_selector);
  if (!root) return {recognized: false, cards: [], has_more: false};
  const cards = Array.from(root.querySelectorAll(contract.card_selector));
  const values = cards.slice(0, 500).map((card) => {
    const first = (selector) => selector ? card.querySelector(selector) : null;
    const link = first(contract.link_selector);
    const text = first(contract.text_selector);
    const author = first(contract.author_selector);
    const timestamp = first(contract.timestamp_selector);
    const metrics = {};
    for (const [name, selector] of Object.entries(contract.metric_selectors)) {
      const node = first(selector);
      metrics[name] = node ? node.innerText : '';
    }
    const images = contract.image_selector
      ? Array.from(card.querySelectorAll(contract.image_selector)).slice(0, 20)
          .map((node) => node.currentSrc || node.src || '')
      : [];
    return {
      href: link ? link.href : '',
      text: text ? text.innerText : '',
      author: author
        ? (author.getAttribute('data-user-id') || author.href || '')
        : '',
      timestamp: timestamp
        ? (timestamp.getAttribute('datetime') ||
           timestamp.getAttribute('date') ||
           timestamp.getAttribute('data-time') || '')
        : '',
      metrics,
      images,
    };
  });
  const next = document.querySelector(contract.next_selector);
  const disabled = next && (
    next.getAttribute('aria-disabled') === 'true' ||
    next.hasAttribute('disabled') ||
    /(?:^|\s)(?:disabled|is-disabled)(?:\s|$)/.test(next.className || '')
  );
  return {recognized: true, cards: values, has_more: Boolean(next && !disabled)};
}
"""


@dataclass(frozen=True, slots=True)
class WeiboDomContract:
    root_selector: str
    card_selector: str
    link_selector: str
    text_selector: str
    author_selector: str
    timestamp_selector: str
    next_selector: str
    metric_selectors: Mapping[str, str] = field(default_factory=dict)
    image_selector: str = ""

    def __post_init__(self) -> None:
        required = (
            self.root_selector,
            self.card_selector,
            self.link_selector,
            self.text_selector,
            self.next_selector,
        )
        optional = (
            self.author_selector,
            self.timestamp_selector,
            self.image_selector,
        )
        if any(not _valid_selector(value) for value in required):
            raise ValueError("Weibo DOM contract has an invalid required selector")
        if any(value and not _valid_selector(value) for value in optional):
            raise ValueError("Weibo DOM contract has an invalid optional selector")
        metrics = dict(self.metric_selectors)
        if any(
            key not in _SAFE_METRICS or not _valid_selector(value)
            for key, value in metrics.items()
        ):
            raise ValueError("Weibo DOM metric selector is invalid")
        object.__setattr__(self, "metric_selectors", metrics)

    def as_browser_payload(self) -> dict[str, object]:
        return {
            "root_selector": self.root_selector,
            "card_selector": self.card_selector,
            "link_selector": self.link_selector,
            "text_selector": self.text_selector,
            "author_selector": self.author_selector,
            "timestamp_selector": self.timestamp_selector,
            "next_selector": self.next_selector,
            "metric_selectors": dict(self.metric_selectors),
            "image_selector": self.image_selector,
        }


class WeiboDomSearchProvider:
    def __init__(
        self,
        browser_page: OwnedBrowserPage,
        contract: WeiboDomContract,
        *,
        build_search_url: SearchUrlBuilder | None = None,
        auth_timeout_seconds: float = 600,
        on_auth_required: BrowserEventHandler | None = None,
        on_authenticated: BrowserEventHandler | None = None,
    ) -> None:
        if not 1 <= auth_timeout_seconds <= 7_200:
            raise ValueError("Weibo auth timeout must be between 1 and 7200 seconds")
        self.browser_page = browser_page
        self.contract = contract
        self.build_search_url = build_search_url or _default_search_url
        self.auth_timeout_seconds = auth_timeout_seconds
        self.on_auth_required = on_auth_required
        self.on_authenticated = on_authenticated
        self._context: RunContext | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if context.source_id != "weibo" or context.operation != "search":
            raise ValueError("Weibo DOM provider context is invalid")
        self._context = context
        await self.browser_page.open(context, cancellation)

    async def search_page(
        self,
        terms: tuple[str, ...],
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> WeiboPostPage:
        if self._context is None:
            raise RuntimeError("Weibo DOM provider is not open")
        if not 1 <= limit <= 500:
            raise ValueError("Weibo DOM page limit is invalid")
        state = WeiboSearchCursor.decode(cursor)
        if state.term_index >= len(terms):
            return WeiboPostPage((), None, False)
        page = await self.browser_page.navigate(
            self.build_search_url(terms[state.term_index], state.page_number),
            cancellation,
            wait_after_ms=4_000,
            auth_timeout_seconds=self.auth_timeout_seconds,
            on_auth_required=self.on_auth_required,
            on_authenticated=self.on_authenticated,
        )
        raw = await page.evaluate(
            _EXTRACT_SCRIPT,
            {"contract": self.contract.as_browser_payload()},
        )
        if not isinstance(raw, Mapping) or raw.get("recognized") is not True:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Weibo search DOM layout was not recognized.",
            )
        rows = raw.get("cards")
        if not isinstance(rows, list):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Weibo search DOM card collection changed.",
            )
        start = min(state.offset, len(rows))
        stop = min(len(rows), start + limit)
        items: list[WeiboPost] = []
        seen: set[str] = set()
        for row in rows[start:stop]:
            cancellation.raise_if_cancelled()
            post = _parse_dom_post(row)
            if post is not None and post.post_id not in seen:
                seen.add(post.post_id)
                items.append(post)
        has_next_page = raw.get("has_more") is True
        if stop < len(rows):
            next_state = WeiboSearchCursor(
                state.term_index, state.page_number, stop
            )
        elif has_next_page:
            next_state = WeiboSearchCursor(
                state.term_index, state.page_number + 1, 0
            )
        elif state.term_index + 1 < len(terms):
            next_state = WeiboSearchCursor(state.term_index + 1, 1, 0)
        else:
            next_state = None
        return WeiboPostPage(
            tuple(items),
            next_state.encode() if next_state else None,
            next_state is not None,
        )

    async def close(self) -> None:
        self._context = None
        await self.browser_page.close()


def _parse_dom_post(value: Any) -> WeiboPost | None:
    if not isinstance(value, Mapping):
        return None
    try:
        target = parse_weibo_target(str(value.get("href") or ""))
    except ValueError:
        return None
    if target.kind is not WeiboTargetKind.POST:
        return None
    text = str(value.get("text") or "").strip()
    if not text:
        return None
    author_id = _author_id(str(value.get("author") or ""))
    metrics_value = value.get("metrics")
    metrics = {
        key: count
        for key, raw in (
            metrics_value.items() if isinstance(metrics_value, Mapping) else ()
        )
        if key in _SAFE_METRICS and (count := _parse_count(raw)) is not None
    }
    images_value = value.get("images")
    media = tuple(
        {"kind": "image", "url": url}
        for raw in (images_value if isinstance(images_value, list) else [])[:20]
        if (url := _safe_image_url(raw)) is not None
    )
    return WeiboPost(
        post_id=target.external_id,
        canonical_url=target.canonical_url,
        text=text[:10_000],
        author_id=author_id,
        published_at=_parse_timestamp(value.get("timestamp")),
        metrics=metrics,
        media=media,
    )


def _author_id(value: str) -> str:
    text = value.strip()
    if text.isdigit() and 1 <= len(text) <= 20:
        return text
    try:
        target = parse_weibo_target(text)
    except ValueError:
        return ""
    return target.external_id if target.kind is WeiboTargetKind.CREATOR else ""


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        numeric = int(text)
        if numeric > 10_000_000_000:
            numeric //= 1_000
        return datetime.fromtimestamp(numeric, tz=UTC)
    except (ValueError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_count(value: Any) -> int | None:
    text = str(value or "").strip().replace(",", "")
    match = _COUNT.search(text)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {
        "万": 10_000,
        "亿": 100_000_000,
        "k": 1_000,
        "m": 1_000_000,
    }.get((match.group(2) or "").casefold(), 1)
    return max(0, int(number * multiplier))


def _safe_image_url(value: Any) -> str | None:
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    allowed = ("sinaimg.cn", "sinaimg.com", "weibo.com", "weibo.cn")
    if parsed.scheme != "https" or not any(
        host == root or host.endswith(f".{root}") for root in allowed
    ):
        return None
    return urlunsplit(("https", host, parsed.path[:2_000], "", ""))


def _valid_selector(value: str) -> bool:
    text = str(value).strip()
    return bool(text and len(text) <= _SELECTOR_LIMIT and "\x00" not in text)


def _default_search_url(term: str, page: int) -> str:
    parameters: dict[str, str | int] = {"q": term}
    if page > 1:
        parameters["page"] = page
    return f"https://s.weibo.com/weibo?{urlencode(parameters)}"
