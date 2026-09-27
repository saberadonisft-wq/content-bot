"""Public Bilibili search through an owned visible browser DOM.

The selectors in this module were independently observed by this project on
2026-08-13 and are recorded in the Bilibili provenance ledger. No internal API
or request-signing implementation is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode, urlsplit

from ...runtime import (
    BrowserSession,
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    PlaywrightBrowserHandle,
    RunContext,
)
from .provider import (
    BilibiliVideo,
    BilibiliVideoPage,
    normalize_bilibili_cover_url,
)
from .targets import BilibiliTargetKind, parse_bilibili_target

_CARD = ".video-list-item .bili-video-card"
_TITLE = ".bili-video-card__info--tit"
_VIEW = ".bili-video-card__stats--item span"
_PAGE_SIDE = ".vui_pagenation--btn-side"
_SEARCH_ROOT = ".search-page"
_DETAIL_TITLE = "h1.video-title"
_DETAIL_DESCRIPTION = "meta[name='description']"
_DETAIL_RELEASE_DATE = "meta[property='video:release_date']"
_DETAIL_COVER = "meta[property='og:image']"
_DETAIL_DURATION = "meta[property='video:duration']"
_DETAIL_METRICS = {
    "view_count": ".view-text",
    "like_count": ".video-like-info",
    "favorite_count": ".video-fav-info",
}
_COUNT = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*(万|亿|[kKmM])?$")
_DATE_TOKEN = re.compile(
    r"\b20[0-9]{2}[-/.][0-9]{1,2}[-/.][0-9]{1,2}"
    r"(?:[ T][0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)?\b"
)


@dataclass(frozen=True, slots=True)
class BilibiliDomCursor:
    term_index: int = 0
    page_number: int = 1
    offset: int = 0

    def __post_init__(self) -> None:
        if self.term_index < 0 or self.page_number < 1 or self.offset < 0:
            raise ValueError("Invalid Bilibili DOM cursor")

    def encode(self) -> str:
        return f"v1:{self.term_index}:{self.page_number}:{self.offset}"

    @classmethod
    def decode(cls, value: str | None) -> BilibiliDomCursor:
        if value is None:
            return cls()
        match = re.fullmatch(r"v1:([0-9]+):([1-9][0-9]*):([0-9]+)", value)
        if not match:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili DOM checkpoint cursor is invalid.",
            )
        return cls(*(int(item) for item in match.groups()))


class BilibiliDomSearchProvider:
    def __init__(self, browser: BrowserSession) -> None:
        self.browser = browser
        self._page: Any | None = None
        self._cancellation: CancellationToken | None = None
        self._loaded_key: tuple[int, int] | None = None

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
        self._cancellation = cancellation

    async def search_page(
        self,
        terms: tuple[str, ...],
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> BilibiliVideoPage:
        if self._page is None or self._cancellation is None:
            raise RuntimeError("Bilibili DOM provider is not open")
        state = BilibiliDomCursor.decode(cursor)
        if state.term_index >= len(terms):
            return BilibiliVideoPage((), None, False)
        cancellation.raise_if_cancelled()
        if self._loaded_key != (state.term_index, state.page_number):
            await self._navigate(terms[state.term_index], state.page_number)
            self._loaded_key = (state.term_index, state.page_number)

        cards = self._page.locator(_CARD)
        card_count = await cards.count()
        if not card_count and not await self._page.locator(_SEARCH_ROOT).count():
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili public search layout was not recognized.",
            )
        start = min(state.offset, card_count)
        stop = min(card_count, start + limit)
        videos: list[BilibiliVideo] = []
        seen: set[str] = set()
        for index in range(start, stop):
            cancellation.raise_if_cancelled()
            video = await self._extract_card(cards.nth(index))
            if video is not None and video.video_id not in seen:
                seen.add(video.video_id)
                videos.append(video)

        has_next_page = await self._has_next_page()
        if stop < card_count:
            next_state = BilibiliDomCursor(state.term_index, state.page_number, stop)
        elif has_next_page:
            next_state = BilibiliDomCursor(state.term_index, state.page_number + 1, 0)
        elif state.term_index + 1 < len(terms):
            next_state = BilibiliDomCursor(state.term_index + 1, 1, 0)
        else:
            next_state = None
        return BilibiliVideoPage(
            tuple(videos),
            next_state.encode() if next_state else None,
            next_state is not None,
        )

    async def close(self) -> None:
        page, self._page = self._page, None
        self._cancellation = None
        self._loaded_key = None
        if page is not None and not page.is_closed():
            await page.close()
        await self.browser.close()

    async def _navigate(self, term: str, page_number: int) -> None:
        assert self._page is not None
        parameters: dict[str, str | int] = {"keyword": term}
        if page_number > 1:
            parameters["page"] = page_number
        query = urlencode(parameters)
        response = await self._page.goto(
            f"https://search.bilibili.com/all?{query}",
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        status = response.status if response is not None else 0
        if status in {429, 412}:
            raise CrawlerFailure(
                CrawlerErrorCode.RATE_LIMITED,
                "Bilibili temporarily limited the public search session.",
                retryable=True,
            )
        if status in {401, 403}:
            raise CrawlerFailure(
                CrawlerErrorCode.CHALLENGE_REQUIRED,
                "Bilibili requires user interaction in the visible browser.",
            )
        if status >= 500 or status == 0:
            raise CrawlerFailure(
                CrawlerErrorCode.TRANSPORT_ERROR,
                "Bilibili public search navigation failed.",
                retryable=True,
            )
        host = (urlsplit(self._page.url).hostname or "").casefold()
        if host != "search.bilibili.com":
            raise CrawlerFailure(
                CrawlerErrorCode.CHALLENGE_REQUIRED,
                "Bilibili redirected the session away from public search.",
            )
        await self._page.wait_for_timeout(5_000)

    async def _extract_card(self, card: Any) -> BilibiliVideo | None:
        title_node = card.locator(_TITLE).first
        if not await title_node.count():
            return None
        title = (
            await title_node.get_attribute("title") or await title_node.inner_text()
        ).strip()
        href = await title_node.locator("xpath=..").get_attribute("href")
        if not title or not href:
            return None
        if href.startswith("//"):
            href = f"https:{href}"
        try:
            target = parse_bilibili_target(href)
        except ValueError:
            return None
        if target.kind is not BilibiliTargetKind.VIDEO or target.external_id is None:
            return None
        metrics: dict[str, int] = {}
        view_node = card.locator(_VIEW).first
        if await view_node.count():
            view_count = parse_compact_count(await view_node.inner_text())
            if view_count is not None:
                metrics["view_count"] = view_count
        cover_url = await _extract_card_cover(card)
        published_at = await _extract_card_published_at(card)
        return BilibiliVideo(
            video_id=target.external_id,
            canonical_url=target.canonical_url,
            title=title,
            published_at=published_at,
            metrics=metrics,
            media=(
                ({"kind": "cover", "url": cover_url},)
                if cover_url
                else ()
            ),
        )

    async def _has_next_page(self) -> bool:
        assert self._page is not None
        sides = self._page.locator(_PAGE_SIDE)
        if not await sides.count():
            return False
        classes = (await sides.last.get_attribute("class") or "").split()
        return "vui_button--disabled" not in classes


class BilibiliDomDetailProvider:
    """Read one public video page through DOM and standard metadata only."""

    def __init__(self, browser: BrowserSession) -> None:
        self.browser = browser
        self._page: Any | None = None
        self._loaded_target: str | None = None

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

    async def fetch_detail(
        self,
        target: str,
        cancellation: CancellationToken,
    ) -> BilibiliVideo:
        if self._page is None:
            raise RuntimeError("Bilibili DOM detail provider is not open")
        try:
            parsed = parse_bilibili_target(target)
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili detail target is invalid.",
            ) from exc
        if parsed.kind is not BilibiliTargetKind.VIDEO or parsed.external_id is None:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili detail target is not a public video.",
            )
        cancellation.raise_if_cancelled()
        if self._loaded_target != parsed.media_url:
            await self._navigate_detail(parsed.media_url)
            self._loaded_target = parsed.media_url
        title_node = self._page.locator(_DETAIL_TITLE).first
        if not await title_node.count():
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili public video layout was not recognized.",
            )
        title = (
            await title_node.get_attribute("title") or await title_node.inner_text()
        ).strip()
        description_node = self._page.locator(_DETAIL_DESCRIPTION).first
        description = (
            (await description_node.get_attribute("content") or "").strip()
            if await description_node.count()
            else ""
        )
        release_node = self._page.locator(_DETAIL_RELEASE_DATE).first
        published_at = (
            parse_public_datetime(await release_node.get_attribute("content"))
            if await release_node.count()
            else None
        )
        metrics: dict[str, int] = {}
        for metric, selector in _DETAIL_METRICS.items():
            node = self._page.locator(selector).first
            if not await node.count():
                continue
            value = parse_compact_count(await node.inner_text())
            if value is not None:
                metrics[metric] = value
        media: list[dict[str, object]] = []
        cover_node = self._page.locator(_DETAIL_COVER).first
        if await cover_node.count():
            cover_url = (await cover_node.get_attribute("content") or "").strip()
            if cover_url:
                media.append({"kind": "cover", "url": cover_url})
        duration_node = self._page.locator(_DETAIL_DURATION).first
        if await duration_node.count():
            duration = _positive_int(await duration_node.get_attribute("content"))
            if duration is not None:
                media.append(
                    {"kind": "video_metadata", "duration_seconds": duration}
                )
        return BilibiliVideo(
            video_id=parsed.external_id,
            canonical_url=parsed.canonical_url,
            title=title,
            description=description[:4_000],
            published_at=published_at,
            metrics=metrics,
            media=tuple(media),
        )

    async def close(self) -> None:
        page, self._page = self._page, None
        self._loaded_target = None
        if page is not None and not page.is_closed():
            await page.close()
        await self.browser.close()

    async def _navigate_detail(self, target: str) -> None:
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
                "Bilibili temporarily limited the public video session.",
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
                "Bilibili public video navigation failed.",
                retryable=True,
            )
        try:
            observed = parse_bilibili_target(self._page.url)
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.CHALLENGE_REQUIRED,
                "Bilibili redirected the session away from the public video.",
            ) from exc
        expected = parse_bilibili_target(target)
        if observed.external_id != expected.external_id:
            raise CrawlerFailure(
                CrawlerErrorCode.CHALLENGE_REQUIRED,
                "Bilibili redirected the session to a different target.",
            )
        try:
            await self._page.locator(_DETAIL_TITLE).first.wait_for(
                state="attached", timeout=15_000
            )
        except Exception as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili public video title did not hydrate.",
            ) from exc


def parse_compact_count(value: str) -> int | None:
    normalized = value.strip().replace(",", "").replace(" ", "")
    match = _COUNT.fullmatch(normalized)
    if not match:
        return None
    multiplier = {
        None: Decimal(1),
        "k": Decimal(1_000),
        "K": Decimal(1_000),
        "m": Decimal(1_000_000),
        "M": Decimal(1_000_000),
        "万": Decimal(10_000),
        "亿": Decimal(100_000_000),
    }[match.group(2)]
    try:
        return int(Decimal(match.group(1)) * multiplier)
    except InvalidOperation:
        return None


async def _extract_card_cover(card: Any) -> str | None:
    image = card.locator("img").first
    if not await image.count():
        return None
    for attribute in ("src", "data-src", "data-lazy-src", "data-original"):
        cover = normalize_bilibili_cover_url(await image.get_attribute(attribute))
        if cover:
            return cover
    srcset = await image.get_attribute("srcset") or await image.get_attribute("data-srcset") or ""
    for raw_candidate in srcset.split(","):
        cover = normalize_bilibili_cover_url(raw_candidate.strip().split(" ", 1)[0])
        if cover:
            return cover
    return None


async def _extract_card_published_at(card: Any) -> datetime | None:
    for selector in ("time", "[datetime]", ".bili-video-card__info--date", ".bili-video-card__info--bottom"):
        node = card.locator(selector).first
        if not await node.count():
            continue
        raw_value = await node.get_attribute("datetime") or await node.inner_text()
        match = _DATE_TOKEN.search(raw_value or "")
        value = match.group(0) if match else raw_value
        published_at = parse_public_datetime(value)
        if published_at is not None:
            return published_at
    return None


def parse_public_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        timestamp = int(text)
        if timestamp > 10_000_000_000:
            timestamp //= 1_000
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _positive_int(value: str | None) -> int | None:
    try:
        parsed = int(str(value or "").strip())
    except ValueError:
        return None
    return parsed if 0 < parsed <= 86_400 else None
