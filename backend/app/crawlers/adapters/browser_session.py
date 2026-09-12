"""Shared owned-browser navigation boundary for platform DOM providers."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..runtime import (
    BrowserSession,
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    PlaywrightBrowserHandle,
    RunContext,
)

BrowserEventHandler = Callable[[], Awaitable[None] | None]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NavigationPolicy:
    source_id: str
    allowed_hosts: frozenset[str]
    login_hosts: frozenset[str] = frozenset()
    login_selectors: tuple[str, ...] = ()
    login_detection_grace_ms: int = 0

    def __post_init__(self) -> None:
        if not self.source_id or not self.allowed_hosts:
            raise ValueError("Navigation policy identity/hosts cannot be empty")
        hosts = self.allowed_hosts | self.login_hosts
        if any(
            not host
            or ":" in host
            or "/" in host
            or host != host.casefold().rstrip(".")
            for host in hosts
        ):
            raise ValueError("Navigation policy host is invalid")
        if len(self.login_selectors) > 10 or any(
            not selector.strip()
            or len(selector) > 300
            or any(ord(character) < 32 for character in selector)
            for selector in self.login_selectors
        ):
            raise ValueError("Navigation policy login selector is invalid")
        if not 0 <= self.login_detection_grace_ms <= 10_000:
            raise ValueError("Navigation policy login detection grace is invalid")


BLOCKED_RESOURCE_TYPES: frozenset[str] = frozenset({"media", "font"})
TRACKER_DOMAINS: tuple[str, ...] = (
    "hm.baidu.com",
    "google-analytics.com",
    "growingio.com",
    "sensorsdata.cn",
    "cnzz.com",
)
MEDIA_EXTENSIONS: tuple[str, ...] = (
    ".mp4",
    ".m4s",
    ".webm",
    ".flv",
    ".avi",
    ".mov",
    ".woff2",
    ".woff",
    ".ttf",
)
LOGIN_IMAGE_KEYWORDS: tuple[str, ...] = (
    "qr",
    "login",
    "captcha",
    "auth",
    "verify",
    "passport",
    "challenge",
)


async def _filter_browser_route(route: Any) -> None:
    try:
        req = getattr(route, "request", None)
        resource_type = getattr(req, "resource_type", "") if req else ""
        if resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
            return

        url = (getattr(req, "url", "") or "").casefold()
        if any(tracker in url for tracker in TRACKER_DOMAINS):
            await route.abort()
            return

        if any(url.endswith(ext) or f"{ext}?" in url for ext in MEDIA_EXTENSIONS):
            await route.abort()
            return

        if resource_type == "image":
            if any(kw in url for kw in LOGIN_IMAGE_KEYWORDS):
                await route.continue_()
                return
            await route.abort()
            return

        await route.continue_()
    except Exception:
        try:
            await route.continue_()
        except Exception:
            logger.debug("Browser route was already closed during fallback")


class OwnedBrowserPage:
    def __init__(self, browser: BrowserSession, policy: NavigationPolicy) -> None:
        self.browser = browser
        self.policy = policy
        self.page: Any | None = None

    async def _configure_route_blocking(self, page: Any) -> None:
        if hasattr(page, "route") and callable(page.route):
            try:
                await page.route("**/*", _filter_browser_route)
            except Exception:
                logger.debug("Browser page does not support resource filtering")

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        if context.source_id != self.policy.source_id:
            raise ValueError("Navigation policy does not match run source")
        handle = await self.browser.open(cancellation)
        if not isinstance(handle, PlaywrightBrowserHandle):
            raise TypeError("Owned browser page requires the Playwright driver")
        self.page = (
            handle.context.pages[0]
            if handle.context.pages
            else await handle.context.new_page()
        )
        await self._configure_route_blocking(self.page)

    async def navigate(
        self,
        url: str,
        cancellation: CancellationToken,
        *,
        wait_after_ms: int = 0,
        auth_timeout_seconds: float = 0,
        on_auth_required: BrowserEventHandler | None = None,
        on_authenticated: BrowserEventHandler | None = None,
    ) -> Any:
        if self.page is None:
            raise RuntimeError("Owned browser page is not open")
        cancellation.raise_if_cancelled()
        requested = urlsplit(str(url).strip())
        requested_host = (requested.hostname or "").casefold().rstrip(".")
        try:
            requested_port = requested.port
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.CHALLENGE_REQUIRED,
                f"{self.policy.source_id} navigation target is invalid.",
            ) from exc
        if (
            requested.scheme != "https"
            or requested.username is not None
            or requested.password is not None
            or requested_port not in {None, 443}
            or not _matches_any(
                requested_host, self.policy.allowed_hosts | self.policy.login_hosts
            )
        ):
            raise CrawlerFailure(
                CrawlerErrorCode.CHALLENGE_REQUIRED,
                f"{self.policy.source_id} navigation target is outside its approved domains.",
            )
        response = await self.page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        status = response.status if response is not None else 0
        if status == 401:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                f"{self.policy.source_id} browser login is required.",
            )
        if status in {403, 412}:
            raise CrawlerFailure(
                CrawlerErrorCode.CHALLENGE_REQUIRED,
                f"{self.policy.source_id} requires user interaction in the visible browser.",
            )
        if status == 429:
            raise CrawlerFailure(
                CrawlerErrorCode.RATE_LIMITED,
                f"{self.policy.source_id} temporarily rate-limited the browser session.",
                retryable=True,
            )
        if status == 404:
            raise CrawlerFailure(
                CrawlerErrorCode.NOT_FOUND,
                f"{self.policy.source_id} public target was not found.",
            )
        if status == 0 or status >= 500:
            raise CrawlerFailure(
                CrawlerErrorCode.TRANSPORT_ERROR,
                f"{self.policy.source_id} browser navigation failed.",
                retryable=True,
            )
        host = (urlsplit(self.page.url).hostname or "").casefold().rstrip(".")
        if _matches_any(host, self.policy.login_hosts):
            if auth_timeout_seconds <= 0:
                raise CrawlerFailure(
                    CrawlerErrorCode.AUTH_REQUIRED,
                    f"Log in to {self.policy.source_id} in its visible application profile.",
                )
            await self._wait_for_login_return(
                cancellation,
                timeout_seconds=auth_timeout_seconds,
                on_auth_required=on_auth_required,
                on_authenticated=on_authenticated,
            )
            host = (urlsplit(self.page.url).hostname or "").casefold().rstrip(".")
        if not _matches_any(host, self.policy.allowed_hosts):
            raise CrawlerFailure(
                CrawlerErrorCode.CHALLENGE_REQUIRED,
                f"{self.policy.source_id} redirected away from the expected public site.",
            )
        if wait_after_ms:
            await self.page.wait_for_timeout(min(max(wait_after_ms, 0), 30_000))
        if await self._detect_same_host_auth():
            if auth_timeout_seconds <= 0:
                raise CrawlerFailure(
                    CrawlerErrorCode.AUTH_REQUIRED,
                    f"Log in to {self.policy.source_id} in its visible application profile.",
                )
            await self._wait_for_same_host_auth(
                cancellation,
                timeout_seconds=auth_timeout_seconds,
                on_auth_required=on_auth_required,
                on_authenticated=on_authenticated,
            )
        cancellation.raise_if_cancelled()
        return self.page

    async def _detect_same_host_auth(self) -> bool:
        if not self.policy.login_selectors:
            return False
        if await self._same_host_auth_visible():
            return True
        remaining = self.policy.login_detection_grace_ms
        while remaining > 0:
            delay = min(500, remaining)
            await self.page.wait_for_timeout(delay)
            remaining -= delay
            if await self._same_host_auth_visible():
                return True
        return False

    async def _same_host_auth_visible(self) -> bool:
        if self.page is None:
            raise RuntimeError("Owned browser page is not open")
        for selector in self.policy.login_selectors:
            locator = self.page.locator(selector)
            if await locator.count() and await locator.first.is_visible():
                return True
        return False

    async def _wait_for_same_host_auth(
        self,
        cancellation: CancellationToken,
        *,
        timeout_seconds: float,
        on_auth_required: BrowserEventHandler | None,
        on_authenticated: BrowserEventHandler | None,
    ) -> None:
        if not 1 <= timeout_seconds <= 7_200:
            raise ValueError("Interactive auth timeout must be between 1 and 7200 seconds")
        await _invoke(on_auth_required)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while await self._same_host_auth_visible():
            cancellation.raise_if_cancelled()
            if self.page is None or self.page.is_closed():
                raise CrawlerFailure(
                    CrawlerErrorCode.AUTH_REQUIRED,
                    f"The {self.policy.source_id} login page was closed before authentication completed.",
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CrawlerFailure(
                    CrawlerErrorCode.AUTH_TIMEOUT,
                    f"Timed out waiting for {self.policy.source_id} interactive login.",
                    retryable=True,
                )
            await self.page.wait_for_timeout(
                min(1_000, max(1, int(remaining * 1_000)))
            )
        await _invoke(on_authenticated)

    async def _wait_for_login_return(
        self,
        cancellation: CancellationToken,
        *,
        timeout_seconds: float,
        on_auth_required: BrowserEventHandler | None,
        on_authenticated: BrowserEventHandler | None,
    ) -> None:
        if self.page is None:
            raise RuntimeError("Owned browser page is not open")
        if not 1 <= timeout_seconds <= 7_200:
            raise ValueError("Interactive auth timeout must be between 1 and 7200 seconds")
        await _invoke(on_auth_required)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            cancellation.raise_if_cancelled()
            if self.page.is_closed():
                raise CrawlerFailure(
                    CrawlerErrorCode.AUTH_REQUIRED,
                    f"The {self.policy.source_id} login page was closed before authentication completed.",
                )
            host = (urlsplit(self.page.url).hostname or "").casefold().rstrip(".")
            if _matches_any(host, self.policy.allowed_hosts) and not _matches_any(
                host, self.policy.login_hosts
            ):
                await _invoke(on_authenticated)
                return
            if not _matches_any(host, self.policy.login_hosts):
                raise CrawlerFailure(
                    CrawlerErrorCode.CHALLENGE_REQUIRED,
                    f"{self.policy.source_id} redirected away from its approved login flow.",
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CrawlerFailure(
                    CrawlerErrorCode.AUTH_TIMEOUT,
                    f"Timed out waiting for {self.policy.source_id} interactive login.",
                    retryable=True,
                )
            await self.page.wait_for_timeout(min(1_000, max(1, int(remaining * 1_000))))

    async def close(self) -> None:
        page, self.page = self.page, None
        if page is not None and not page.is_closed():
            await page.close()
        await self.browser.close()


def _matches_any(host: str, roots: frozenset[str]) -> bool:
    return any(host == root or host.endswith(f".{root}") for root in roots)


async def _invoke(handler: BrowserEventHandler | None) -> None:
    if handler is None:
        return
    result = handler()
    if inspect.isawaitable(result):
        await result
