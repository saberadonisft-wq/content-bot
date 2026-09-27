"""Public creator-video listing observed independently from Bilibili DOM."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from ...runtime import (
    BrowserSession,
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    PlaywrightBrowserHandle,
    RunContext,
)
from .dom_provider import _extract_card_cover, _extract_card_published_at
from .provider import BilibiliVideo, BilibiliVideoPage
from .targets import BilibiliTargetKind, parse_bilibili_target

_CARD = ".video-list .upload-video-card"
_TITLE = ".bili-video-card__title"
_VIDEO_LINK = "a[href*='/video/']"
_PAGE_ROOT = ".vui_pagenation.video-pagination"
_PAGE_SIDE = ".vui_pagenation--btn-side"
_PAGE_ACTIVE = ".vui_button--active.vui_pagenation--btn-num"
_LOGIN_GATE = (".login-tip-content", ".login-tip-content-item")


@dataclass(frozen=True, slots=True)
class BilibiliCreatorCursor:
    page_number: int = 1
    offset: int = 0

    def __post_init__(self) -> None:
        if self.page_number < 1 or self.offset < 0:
            raise ValueError("Invalid Bilibili creator cursor")

    def encode(self) -> str:
        return f"v1c:{self.page_number}:{self.offset}"

    @classmethod
    def decode(cls, value: str | None) -> BilibiliCreatorCursor:
        if value is None:
            return cls()
        match = re.fullmatch(r"v1c:([1-9][0-9]*):([0-9]+)", value)
        if not match:
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili creator checkpoint cursor is invalid.",
            )
        return cls(*(int(item) for item in match.groups()))


class BilibiliDomCreatorProvider:
    def __init__(self, browser: BrowserSession) -> None:
        self.browser = browser
        self._page: Any | None = None
        self._creator_url: str | None = None
        self._current_page = 0

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

    async def creator_page(
        self,
        target: str,
        cursor: str | None,
        limit: int,
        cancellation: CancellationToken,
    ) -> BilibiliVideoPage:
        if self._page is None:
            raise RuntimeError("Bilibili DOM creator provider is not open")
        try:
            parsed = parse_bilibili_target(target)
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili creator target is invalid.",
            ) from exc
        if parsed.kind is not BilibiliTargetKind.CREATOR:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "Bilibili creator target is not a public creator page.",
            )
        state = BilibiliCreatorCursor.decode(cursor)
        cancellation.raise_if_cancelled()
        creator_url = f"{parsed.canonical_url}/upload/video"
        if self._creator_url != creator_url or self._current_page > state.page_number:
            await self._navigate(creator_url)
            self._creator_url = creator_url
            self._current_page = 1
        await self._advance_to_page(state.page_number, cancellation)

        cards = self._page.locator(_CARD)
        card_count = await cards.count()
        if not card_count and not await self._page.locator(_PAGE_ROOT).count():
            raise CrawlerFailure(
                CrawlerErrorCode.PARSE_CHANGED,
                "Bilibili public creator video layout was not recognized.",
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
            next_state = BilibiliCreatorCursor(state.page_number, stop)
        elif has_next_page:
            next_state = BilibiliCreatorCursor(state.page_number + 1, 0)
        else:
            next_state = None
        return BilibiliVideoPage(
            tuple(videos),
            next_state.encode() if next_state else None,
            next_state is not None,
        )

    async def close(self) -> None:
        page, self._page = self._page, None
        self._creator_url = None
        self._current_page = 0
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
                "Bilibili temporarily limited the public creator session.",
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
                "Bilibili public creator was not found.",
            )
        if status >= 500 or status == 0:
            raise CrawlerFailure(
                CrawlerErrorCode.TRANSPORT_ERROR,
                "Bilibili public creator navigation failed.",
                retryable=True,
            )
        try:
            parsed = parse_bilibili_target(self._page.url)
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.CHALLENGE_REQUIRED,
                "Bilibili redirected away from the public creator page.",
            ) from exc
        if parsed.kind is not BilibiliTargetKind.CREATOR:
            raise CrawlerFailure(
                CrawlerErrorCode.CHALLENGE_REQUIRED,
                "Bilibili redirected to a different target.",
            )
        await self._wait_for_creator_layout()

    async def _wait_for_creator_layout(self) -> None:
        assert self._page is not None
        for attempt in range(80):
            if (
                await self._page.locator(_CARD).count()
                or await self._page.locator(_PAGE_ROOT).count()
            ):
                return
            if attempt % 4 == 0 and await self._login_gate_visible():
                raise CrawlerFailure(
                    CrawlerErrorCode.AUTH_REQUIRED,
                    "Bilibili creator listing requires login in the visible browser profile.",
                )
            await asyncio.sleep(0.25)
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Bilibili public creator video list did not hydrate.",
        )

    async def _login_gate_visible(self) -> bool:
        """Distinguish a login-gated profile from a changed creator layout."""
        assert self._page is not None
        for selector in _LOGIN_GATE:
            nodes = self._page.locator(selector)
            count = min(await nodes.count(), 5)
            for index in range(count):
                node = nodes.nth(index)
                is_visible = getattr(node, "is_visible", None)
                if callable(is_visible) and await is_visible():
                    return True

        body = self._page.locator("body")
        if not await body.count():
            return False
        text = " ".join((await body.inner_text()).split())
        return "登录后你可以" in text and "立即登录" in text

    async def _advance_to_page(
        self,
        target_page: int,
        cancellation: CancellationToken,
    ) -> None:
        assert self._page is not None
        while self._current_page < target_page:
            cancellation.raise_if_cancelled()
            sides = self._page.locator(_PAGE_SIDE)
            if not await sides.count():
                raise CrawlerFailure(
                    CrawlerErrorCode.PARSE_CHANGED,
                    "Bilibili creator pagination ended before the checkpoint.",
                )
            next_button = sides.last
            classes = (await next_button.get_attribute("class") or "").split()
            if "vui_button--disabled" in classes:
                raise CrawlerFailure(
                    CrawlerErrorCode.PARSE_CHANGED,
                    "Bilibili creator checkpoint exceeds available pages.",
                )
            await next_button.click()
            self._current_page += 1
            await self._wait_for_active_page(self._current_page)

    async def _wait_for_active_page(self, expected: int) -> None:
        assert self._page is not None
        for _ in range(80):
            active = self._page.locator(_PAGE_ACTIVE).first
            if await active.count() and (await active.inner_text()).strip() == str(
                expected
            ):
                return
            await asyncio.sleep(0.25)
        raise CrawlerFailure(
            CrawlerErrorCode.PARSE_CHANGED,
            "Bilibili creator pagination did not advance.",
        )

    @staticmethod
    async def _extract_card(card: Any) -> BilibiliVideo | None:
        title_node = card.locator(_TITLE).first
        link_node = card.locator(_VIDEO_LINK).first
        if not await title_node.count() or not await link_node.count():
            return None
        title = (
            await title_node.get_attribute("title") or await title_node.inner_text()
        ).strip()
        href = await link_node.get_attribute("href")
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
        cover_url = await _extract_card_cover(card)
        published_at = await _extract_card_published_at(card)
        return BilibiliVideo(
            target.external_id,
            target.canonical_url,
            title,
            published_at=published_at,
            media=({"kind": "cover", "url": cover_url},) if cover_url else (),
        )

    async def _has_next_page(self) -> bool:
        assert self._page is not None
        sides = self._page.locator(_PAGE_SIDE)
        if not await sides.count():
            return False
        classes = (await sides.last.get_attribute("class") or "").split()
        return "vui_button--disabled" not in classes
