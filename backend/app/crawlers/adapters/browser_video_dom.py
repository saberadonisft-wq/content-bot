"""Contract-driven DOM provider shared by browser-session video platforms."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..runtime import CancellationToken, CrawlerErrorCode, CrawlerFailure, RunContext
from .browser_session import OwnedBrowserPage
from .browser_video import BrowserVideo, BrowserVideoPage

BrowserEventHandler = Callable[[], Awaitable[None] | None]
SearchUrlBuilder = Callable[[str, int], str]
Canonicalizer = Callable[[str], Any]
IdentityBuilder = Callable[[Any], str]
_SELECTOR_LIMIT = 300
_ATTRIBUTE = re.compile(r"[A-Za-z_:][A-Za-z0-9_.:-]{0,79}")
_SOURCE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_COUNT = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(万|亿|[kKmM])?")

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
    const metrics = {};
    for (const [name, selector] of Object.entries(contract.metric_selectors)) {
      const node = first(selector);
      metrics[name] = node ? node.innerText : '';
    }
    const media = contract.image_selector
      ? Array.from(card.querySelectorAll(contract.image_selector)).slice(0, 20)
          .map((node) => node.currentSrc || node.src || '')
      : [];
    const cover = first(contract.cover_selector);
    return {
      href: link ? link.href : '',
      title: title ? title.innerText : '',
      body: body ? body.innerText : '',
      author: author
        ? (author.getAttribute(contract.author_attribute) || author.href || '')
        : '',
      timestamp: timestamp
        ? (timestamp.getAttribute('datetime') ||
           timestamp.getAttribute('date') ||
           timestamp.getAttribute('data-time') || '')
        : '',
      metrics,
      media,
      cover: cover ? (cover.currentSrc || cover.src || '') : '',
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
class BrowserVideoDomContract:
    root_selector: str
    card_selector: str
    link_selector: str
    title_selector: str
    next_selector: str
    body_selector: str = ""
    author_selector: str = ""
    author_attribute: str = "data-user-id"
    timestamp_selector: str = ""
    metric_selectors: Mapping[str, str] = field(default_factory=dict)
    image_selector: str = ""
    cover_selector: str = ""

    def __post_init__(self) -> None:
        required = (
            self.root_selector,
            self.card_selector,
            self.link_selector,
            self.title_selector,
            self.next_selector,
        )
        optional = (
            self.body_selector,
            self.author_selector,
            self.timestamp_selector,
            self.image_selector,
            self.cover_selector,
        )
        if any(not _valid_selector(value) for value in required):
            raise ValueError("Browser video DOM contract has an invalid required selector")
        if any(value and not _valid_selector(value) for value in optional):
            raise ValueError("Browser video DOM contract has an invalid optional selector")
        if not _ATTRIBUTE.fullmatch(self.author_attribute):
            raise ValueError("Browser video DOM author attribute is invalid")
        metrics = dict(self.metric_selectors)
        if len(metrics) > 20 or any(
            not _SOURCE.fullmatch(key) or not _valid_selector(value)
            for key, value in metrics.items()
        ):
            raise ValueError("Browser video DOM metric selector is invalid")
        object.__setattr__(self, "metric_selectors", metrics)

    def as_browser_payload(self) -> dict[str, object]:
        return {
            "root_selector": self.root_selector,
            "card_selector": self.card_selector,
            "link_selector": self.link_selector,
            "title_selector": self.title_selector,
            "next_selector": self.next_selector,
            "body_selector": self.body_selector,
            "author_selector": self.author_selector,
            "author_attribute": self.author_attribute,
            "timestamp_selector": self.timestamp_selector,
            "metric_selectors": dict(self.metric_selectors),
            "image_selector": self.image_selector,
            "cover_selector": self.cover_selector,
        }


@dataclass(frozen=True, slots=True)
class BrowserVideoDomCursor:
    source_id: str
    term_index: int = 0
    page_number: int = 1
    offset: int = 0

    def __post_init__(self) -> None:
        if (
            not _SOURCE.fullmatch(self.source_id)
            or self.term_index < 0
            or self.page_number < 1
            or self.offset < 0
        ):
            raise ValueError("Browser video DOM cursor is invalid")

    def encode(self) -> str:
        return f"v1v:{self.source_id}:{self.term_index}:{self.page_number}:{self.offset}"

    @classmethod
    def decode(cls, source_id: str, value: str | None) -> BrowserVideoDomCursor:
        if value is None:
            return cls(source_id)
        match = re.fullmatch(
            r"v1v:([a-z][a-z0-9_]{0,39}):([0-9]+):([1-9][0-9]*):([0-9]+)",
            value,
        )
        if not match or match.group(1) != source_id:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                f"{source_id} DOM checkpoint is invalid.",
            )
        return cls(
            source_id,
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
        )


class BrowserVideoDomSearchProvider:
    def __init__(
        self,
        *,
        source_id: str,
        provider_id: str,
        content_kinds: frozenset[str],
        browser_page: OwnedBrowserPage,
        contract: BrowserVideoDomContract,
        build_search_url: SearchUrlBuilder,
        canonicalize: Canonicalizer,
        media_hosts: tuple[str, ...],
        metric_ids: frozenset[str],
        identity_builder: IdentityBuilder | None = None,
        auth_timeout_seconds: float = 600,
        on_auth_required: BrowserEventHandler | None = None,
        on_authenticated: BrowserEventHandler | None = None,
    ) -> None:
        if (
            not _SOURCE.fullmatch(source_id)
            or not _SOURCE.fullmatch(provider_id)
            or not content_kinds
            or any(not _SOURCE.fullmatch(kind) for kind in content_kinds)
        ):
            raise ValueError("Browser video DOM provider identity is invalid")
        if not media_hosts or any(
            not host or host != host.casefold().rstrip(".") for host in media_hosts
        ):
            raise ValueError("Browser video DOM media hosts are invalid")
        if not metric_ids or not metric_ids.issubset(contract.metric_selectors):
            raise ValueError("Browser video DOM metric contract is inconsistent")
        if not 1 <= auth_timeout_seconds <= 7_200:
            raise ValueError("Browser video auth timeout must be between 1 and 7200 seconds")
        self.source_id = source_id
        self.provider_id = provider_id
        self.content_kinds = content_kinds
        self.browser_page = browser_page
        self.contract = contract
        self.build_search_url = build_search_url
        self.canonicalize = canonicalize
        self.media_hosts = media_hosts
        self.metric_ids = metric_ids
        self.identity_builder = identity_builder or _target_identity
        self.auth_timeout_seconds = auth_timeout_seconds
        self.on_auth_required = on_auth_required
        self.on_authenticated = on_authenticated
        self._context: RunContext | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if (
            context.source_id != self.source_id
            or context.provider_id != self.provider_id
            or context.operation != "search"
        ):
            raise ValueError("Browser video DOM provider context is invalid")
        self._context = context
        await self.browser_page.open(context, cancellation)

    async def search_page(
        self,
        terms: tuple[str, ...],
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> BrowserVideoPage:
        if self._context is None:
            raise RuntimeError("Browser video DOM provider is not open")
        if not 1 <= limit <= 500:
            raise ValueError("Browser video DOM page limit is invalid")
        state = BrowserVideoDomCursor.decode(self.source_id, cursor)
        if state.term_index >= len(terms):
            return BrowserVideoPage((), None, False)
        url = self.build_search_url(terms[state.term_index], state.page_number)
        page = await self.browser_page.navigate(
            url,
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
                f"{self.source_id} search DOM layout was not recognized.",
            )
        rows = raw.get("cards")
        if not isinstance(rows, list):
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                f"{self.source_id} search DOM card collection changed.",
            )
        start = min(state.offset, len(rows))
        stop = min(len(rows), start + limit)
        items: list[BrowserVideo] = []
        seen: set[str] = set()
        for row in rows[start:stop]:
            cancellation.raise_if_cancelled()
            item = self._parse_item(row)
            if item is not None and item.external_id not in seen:
                seen.add(item.external_id)
                items.append(item)
        if stop < len(rows):
            next_state = BrowserVideoDomCursor(
                self.source_id, state.term_index, state.page_number, stop
            )
        elif raw.get("has_more") is True:
            next_state = BrowserVideoDomCursor(
                self.source_id, state.term_index, state.page_number + 1, 0
            )
        elif state.term_index + 1 < len(terms):
            next_state = BrowserVideoDomCursor(
                self.source_id, state.term_index + 1, 1, 0
            )
        else:
            next_state = None
        return BrowserVideoPage(
            tuple(items),
            next_state.encode() if next_state else None,
            next_state is not None,
        )

    async def close(self) -> None:
        self._context = None
        await self.browser_page.close()

    def _parse_item(self, value: Any) -> BrowserVideo | None:
        if not isinstance(value, Mapping):
            return None
        try:
            target = self.canonicalize(str(value.get("href") or ""))
        except ValueError:
            return None
        if (
            str(getattr(getattr(target, "kind", None), "value", ""))
            not in self.content_kinds
        ):
            return None
        external_id = self.identity_builder(target)
        title = str(value.get("title") or "").strip()
        if not external_id or not title:
            return None
        metrics_value = value.get("metrics")
        metrics = {
            key: count
            for key, raw in (
                metrics_value.items() if isinstance(metrics_value, Mapping) else ()
            )
            if key in self.metric_ids and (count := _parse_count(raw)) is not None
        }
        media_value = value.get("media")
        media = [
            {"kind": "image", "url": url}
            for raw in (media_value if isinstance(media_value, list) else [])[:20]
            if (url := _safe_media_url(raw, self.media_hosts)) is not None
        ]
        cover = _safe_media_url(value.get("cover"), self.media_hosts)
        if cover:
            media.append({"kind": "video_cover", "url": cover})
        return BrowserVideo(
            external_id=external_id,
            canonical_url=str(target.canonical_url),
            title=title[:500],
            body=str(value.get("body") or "").strip()[:10_000],
            author_id=_bounded_author(value.get("author")),
            published_at=_parse_timestamp(value.get("timestamp")),
            metrics=metrics,
            media=tuple(media),
        )


def _bounded_author(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 500:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme:
        return parsed.path.strip("/")[:300]
    return text[:300]


def _target_identity(target: Any) -> str:
    return str(getattr(target, "external_id", ""))


def _safe_media_url(value: Any, roots: tuple[str, ...]) -> str | None:
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme != "https" or not any(
        host == root or host.endswith(f".{root}") for root in roots
    ):
        return None
    return urlunsplit(("https", host, parsed.path[:2_000], "", ""))


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
