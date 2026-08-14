"""Contract-driven Tieba keyword search over the owned visible browser DOM."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from ...runtime import CancellationToken, CrawlerErrorCode, CrawlerFailure, RunContext
from ..browser_session import OwnedBrowserPage
from .provider import TiebaSearchCursor, TiebaThread, TiebaThreadPage
from .targets import TiebaTargetKind, parse_tieba_target

BrowserEventHandler = Callable[[], Awaitable[None] | None]
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
    const title = first(contract.title_selector);
    const body = first(contract.body_selector);
    const author = first(contract.author_selector);
    const timestamp = first(contract.timestamp_selector);
    const replies = first(contract.comment_count_selector);
    return {
      href: link ? link.href : '',
      title: title ? title.innerText : '',
      body: body ? body.innerText : '',
      author: author
        ? (author.getAttribute('data-user-id') || author.href || '')
        : '',
      timestamp: timestamp
        ? (timestamp.getAttribute('datetime') ||
           timestamp.getAttribute('date') ||
           timestamp.getAttribute('data-time') || '')
        : '',
      comment_count: replies ? replies.innerText : '',
    };
  });
  const next = contract.next_selector
    ? root.querySelector(contract.next_selector)
    : null;
  const disabled = next && (
    next.getAttribute('aria-disabled') === 'true' ||
    next.hasAttribute('disabled') ||
    /(?:^|\s)(?:disabled|is-disabled)(?:\s|$)/.test(next.className || '')
  );
  return {recognized: true, cards: values, has_more: Boolean(next && !disabled)};
}
"""


@dataclass(frozen=True, slots=True)
class TiebaDomContract:
    root_selector: str
    card_selector: str
    link_selector: str
    title_selector: str
    next_selector: str = ""
    body_selector: str = ""
    author_selector: str = ""
    timestamp_selector: str = ""
    comment_count_selector: str = ""

    def __post_init__(self) -> None:
        required = (
            self.root_selector,
            self.card_selector,
            self.link_selector,
            self.title_selector,
        )
        optional = (
            self.next_selector,
            self.body_selector,
            self.author_selector,
            self.timestamp_selector,
            self.comment_count_selector,
        )
        if any(not _valid_selector(value) for value in required):
            raise ValueError("Tieba DOM contract has an invalid required selector")
        if any(value and not _valid_selector(value) for value in optional):
            raise ValueError("Tieba DOM contract has an invalid optional selector")

    def as_browser_payload(self) -> dict[str, str]:
        return {
            "root_selector": self.root_selector,
            "card_selector": self.card_selector,
            "link_selector": self.link_selector,
            "title_selector": self.title_selector,
            "next_selector": self.next_selector,
            "body_selector": self.body_selector,
            "author_selector": self.author_selector,
            "timestamp_selector": self.timestamp_selector,
            "comment_count_selector": self.comment_count_selector,
        }


class TiebaDomSearchProvider:
    def __init__(
        self,
        browser_page: OwnedBrowserPage,
        contract: TiebaDomContract,
        *,
        auth_timeout_seconds: float = 600,
        on_auth_required: BrowserEventHandler | None = None,
        on_authenticated: BrowserEventHandler | None = None,
    ) -> None:
        if not 1 <= auth_timeout_seconds <= 7_200:
            raise ValueError("Tieba auth timeout must be between 1 and 7200 seconds")
        self.browser_page = browser_page
        self.contract = contract
        self.auth_timeout_seconds = auth_timeout_seconds
        self.on_auth_required = on_auth_required
        self.on_authenticated = on_authenticated
        self._context: RunContext | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if context.source_id != "tieba" or context.operation != "search":
            raise ValueError("Tieba DOM provider context is invalid")
        self._context = context
        await self.browser_page.open(context, cancellation)

    async def search_page(
        self,
        terms: tuple[str, ...],
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> TiebaThreadPage:
        if self._context is None:
            raise RuntimeError("Tieba DOM provider is not open")
        if not 1 <= limit <= 500:
            raise ValueError("Tieba DOM page limit is invalid")
        state = TiebaSearchCursor.decode(cursor)
        if state.term_index >= len(terms):
            return TiebaThreadPage((), None, False)
        parameters: dict[str, str | int] = {
            "ie": "utf-8",
            "qw": terms[state.term_index],
        }
        page = await self.browser_page.navigate(
            f"https://tieba.baidu.com/f/search/res?{urlencode(parameters)}",
            cancellation,
            wait_after_ms=4_000,
            auth_timeout_seconds=self.auth_timeout_seconds,
            on_auth_required=self.on_auth_required,
            on_authenticated=self.on_authenticated,
        )
        for _load_index in range(1, state.page_number):
            cancellation.raise_if_cancelled()
            load_more = page.locator(self.contract.next_selector).first
            if not await load_more.count():
                return TiebaThreadPage((), None, False)
            await load_more.click(timeout=15_000)
            await page.wait_for_timeout(2_000)
        rows, has_next_page = await extract_tieba_dom_threads(
            page,
            self.contract,
            cancellation,
            layout_label="search",
        )
        start = min(state.offset, len(rows))
        stop = min(len(rows), start + limit)
        items = list(rows[start:stop])
        if stop < len(rows):
            next_state = TiebaSearchCursor(
                state.term_index, state.page_number, stop
            )
        elif has_next_page:
            next_state = TiebaSearchCursor(
                state.term_index, state.page_number + 1, stop
            )
        elif state.term_index + 1 < len(terms):
            next_state = TiebaSearchCursor(state.term_index + 1, 1, 0)
        else:
            next_state = None
        return TiebaThreadPage(
            tuple(items),
            next_state.encode() if next_state else None,
            next_state is not None,
        )

    async def close(self) -> None:
        self._context = None
        await self.browser_page.close()


async def extract_tieba_dom_threads(
    page: Any,
    contract: TiebaDomContract,
    cancellation: CancellationToken,
    *,
    layout_label: str,
) -> tuple[tuple[TiebaThread, ...], bool]:
    raw = await page.evaluate(
        _EXTRACT_SCRIPT,
        {"contract": contract.as_browser_payload()},
    )
    if not isinstance(raw, Mapping) or raw.get("recognized") is not True:
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            f"Tieba {layout_label} DOM layout was not recognized.",
        )
    rows = raw.get("cards")
    if not isinstance(rows, list):
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            f"Tieba {layout_label} DOM card collection changed.",
        )
    items: list[TiebaThread] = []
    seen: set[str] = set()
    for row in rows:
        cancellation.raise_if_cancelled()
        thread = _parse_dom_thread(row)
        if thread is not None and thread.thread_id not in seen:
            seen.add(thread.thread_id)
            items.append(thread)
    return tuple(items), raw.get("has_more") is True


def _parse_dom_thread(value: Any) -> TiebaThread | None:
    if not isinstance(value, Mapping):
        return None
    try:
        target = parse_tieba_target(str(value.get("href") or ""))
    except ValueError:
        return None
    if target.kind is not TiebaTargetKind.THREAD:
        return None
    title = str(value.get("title") or "").strip()
    if not title:
        return None
    author_id = _author_id(str(value.get("author") or ""))
    comment_count = _parse_count(value.get("comment_count"))
    return TiebaThread(
        thread_id=target.external_id,
        canonical_url=target.canonical_url,
        title=title[:500],
        body=str(value.get("body") or "").strip()[:10_000],
        author_id=author_id,
        published_at=_parse_timestamp(value.get("timestamp")),
        metrics=(
            {"comment_count": comment_count}
            if comment_count is not None
            else {}
        ),
    )


def _author_id(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        target = parse_tieba_target(text)
    except ValueError:
        parsed = urlsplit(text)
        return parsed.path.strip("/")[:256] if not parsed.scheme else ""
    return target.external_id if target.kind is TiebaTargetKind.CREATOR else ""


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
    match = _COUNT.search(str(value or "").strip().replace(",", ""))
    if not match:
        return None
    multiplier = {
        "万": 10_000,
        "亿": 100_000_000,
        "k": 1_000,
        "m": 1_000_000,
    }.get((match.group(2) or "").casefold(), 1)
    return max(0, int(float(match.group(1)) * multiplier))


def _valid_selector(value: str) -> bool:
    text = str(value).strip()
    return bool(text and len(text) <= _SELECTOR_LIMIT and "\x00" not in text)


TIEBA_SEARCH_DOM_CONTRACT = TiebaDomContract(
    root_selector=".search-tab-content",
    card_selector=".threadcardclass",
    link_selector="a.action-link-bg",
    title_selector=".top-title",
    body_selector=".abstract-wrap",
    comment_count_selector="a.comment-link-zone .action-number",
)
