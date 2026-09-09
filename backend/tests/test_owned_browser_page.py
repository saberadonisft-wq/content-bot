import asyncio

import pytest

from app.crawlers.adapters import NavigationPolicy, OwnedBrowserPage
from app.crawlers.runtime import CancellationToken, CrawlerErrorCode, CrawlerFailure


class Response:
    def __init__(self, status):
        self.status = status


class Page:
    def __init__(
        self,
        status=200,
        final_url="https://s.weibo.com/weibo",
        login_return_url=None,
        same_host_auth_checks=0,
    ):
        self.status = status
        self.url = final_url
        self.login_return_url = login_return_url
        self.closed = False
        self.goto_calls = []
        self.same_host_auth_checks = same_host_auth_checks

    async def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        return Response(self.status)

    async def wait_for_timeout(self, value):
        if self.login_return_url:
            self.url = self.login_return_url
            self.login_return_url = None

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    def locator(self, selector):
        return Locator(self)


class Locator:
    def __init__(self, page):
        self.page = page
        self.first = self

    async def count(self):
        return int(self.page.same_host_auth_checks > 0)

    async def is_visible(self):
        if self.page.same_host_auth_checks <= 0:
            return False
        self.page.same_host_auth_checks -= 1
        return True


class Browser:
    closed = False

    async def close(self):
        self.closed = True


def provider(page):
    browser = Browser()
    owned = OwnedBrowserPage(
        browser,
        NavigationPolicy(
            "weibo",
            frozenset({"weibo.com", "weibo.cn"}),
            frozenset({"passport.weibo.com"}),
        ),
    )
    owned.page = page
    return owned, browser


def same_host_provider(page):
    browser = Browser()
    owned = OwnedBrowserPage(
        browser,
        NavigationPolicy(
            "xhs",
            frozenset({"xiaohongshu.com"}),
            login_selectors=("div.login-container img.qrcode-img",),
        ),
    )
    owned.page = page
    return owned


def test_owned_browser_navigation_accepts_dns_boundary_and_closes() -> None:
    owned, browser = provider(Page(final_url="https://s.weibo.com/weibo"))

    async def run():
        result = await owned.navigate(
            "https://s.weibo.com/weibo", CancellationToken(), wait_after_ms=1
        )
        await owned.close()
        return result

    result = asyncio.run(run())
    assert result.closed is True
    assert browser.closed is True


@pytest.mark.parametrize(
    ("status", "url", "code"),
    [
        (200, "https://passport.weibo.com/login", CrawlerErrorCode.AUTH_REQUIRED),
        (200, "https://evilweibo.com/", CrawlerErrorCode.CHALLENGE_REQUIRED),
        (401, "https://s.weibo.com/", CrawlerErrorCode.AUTH_REQUIRED),
        (403, "https://s.weibo.com/", CrawlerErrorCode.CHALLENGE_REQUIRED),
        (412, "https://s.weibo.com/", CrawlerErrorCode.CHALLENGE_REQUIRED),
        (429, "https://s.weibo.com/", CrawlerErrorCode.RATE_LIMITED),
        (404, "https://s.weibo.com/", CrawlerErrorCode.NOT_FOUND),
        (503, "https://s.weibo.com/", CrawlerErrorCode.TRANSPORT_ERROR),
    ],
)
def test_owned_browser_navigation_maps_auth_challenge_and_transport(
    status, url, code
) -> None:
    owned, _ = provider(Page(status, url))

    async def run():
        with pytest.raises(CrawlerFailure) as captured:
            await owned.navigate("https://s.weibo.com/weibo", CancellationToken())
        return captured.value

    assert asyncio.run(run()).code is code


def test_navigation_policy_rejects_invalid_host_configuration() -> None:
    with pytest.raises(ValueError):
        NavigationPolicy("weibo", frozenset({"https://weibo.com"}))
    with pytest.raises(ValueError):
        NavigationPolicy(
            "xhs",
            frozenset({"xiaohongshu.com"}),
            login_selectors=("\n",),
        )


def test_same_host_login_modal_requires_interactive_auth() -> None:
    page = Page(
        final_url="https://www.xiaohongshu.com/search_result",
        same_host_auth_checks=1,
    )
    owned = same_host_provider(page)

    async def run():
        with pytest.raises(CrawlerFailure) as captured:
            await owned.navigate(
                "https://www.xiaohongshu.com/search_result",
                CancellationToken(),
            )
        return captured.value

    assert asyncio.run(run()).code is CrawlerErrorCode.AUTH_REQUIRED


def test_same_host_login_modal_waits_until_the_gate_disappears() -> None:
    page = Page(
        final_url="https://www.xiaohongshu.com/search_result",
        same_host_auth_checks=1,
    )
    owned = same_host_provider(page)
    events = []

    async def run():
        return await owned.navigate(
            "https://www.xiaohongshu.com/search_result",
            CancellationToken(),
            auth_timeout_seconds=30,
            on_auth_required=lambda: events.append("required"),
            on_authenticated=lambda: events.append("authenticated"),
        )

    assert asyncio.run(run()) is page
    assert events == ["required", "authenticated"]


def test_interactive_navigation_waits_for_manual_login_and_resumes() -> None:
    page = Page(
        final_url="https://passport.weibo.com/login",
        login_return_url="https://s.weibo.com/weibo?q=game",
    )
    owned, _ = provider(page)
    events = []

    async def run():
        return await owned.navigate(
            "https://s.weibo.com/weibo?q=game",
            CancellationToken(),
            auth_timeout_seconds=30,
            on_auth_required=lambda: events.append("required"),
            on_authenticated=lambda: events.append("authenticated"),
        )

    assert asyncio.run(run()) is page
    assert events == ["required", "authenticated"]


def test_interactive_navigation_rejects_login_redirect_outside_policy() -> None:
    page = Page(
        final_url="https://passport.weibo.com/login",
        login_return_url="https://example.com/continue",
    )
    owned, _ = provider(page)

    async def run():
        with pytest.raises(CrawlerFailure) as captured:
            await owned.navigate(
                "https://s.weibo.com/weibo?q=game",
                CancellationToken(),
                auth_timeout_seconds=30,
            )
        return captured.value

    assert asyncio.run(run()).code is CrawlerErrorCode.CHALLENGE_REQUIRED


@pytest.mark.parametrize(
    "url",
    [
        "http://s.weibo.com/weibo",
        "https://evilweibo.com/weibo",
        "https://user@s.weibo.com/weibo",
        "https://s.weibo.com:444/weibo",
    ],
)
def test_owned_browser_rejects_unapproved_target_before_network(url) -> None:
    page = Page()
    owned, _ = provider(page)

    async def run():
        with pytest.raises(CrawlerFailure) as captured:
            await owned.navigate(url, CancellationToken())
        return captured.value

    assert asyncio.run(run()).code is CrawlerErrorCode.CHALLENGE_REQUIRED
    assert page.goto_calls == []


def test_owned_browser_route_blocking_filters_unneeded_resources() -> None:
    from app.crawlers.adapters.browser_session import _filter_browser_route

    class MockRequest:
        def __init__(self, resource_type: str, url: str):
            self.resource_type = resource_type
            self.url = url

    class MockRoute:
        def __init__(self, resource_type: str, url: str):
            self.request = MockRequest(resource_type, url)
            self.action = None

        async def abort(self):
            self.action = "aborted"

        async def continue_(self):
            self.action = "continued"

    test_cases = [
        # Media / font
        ("media", "https://bilibili.com/video.m4s", "aborted"),
        ("font", "https://bilibili.com/font.woff2", "aborted"),
        # Trackers
        ("script", "https://hm.baidu.com/hm.js", "aborted"),
        ("script", "https://www.google-analytics.com/analytics.js", "aborted"),
        # Media extensions
        ("other", "https://bilibili.com/stream.mp4?token=123", "aborted"),
        # Regular images (aborted to save bandwidth/RAM)
        ("image", "https://i0.hdslb.com/bfs/archive/cover.jpg", "aborted"),
        # Login / QR images (must be preserved for login flow!)
        ("image", "https://passport.bilibili.com/qrcode/getLoginUrl", "continued"),
        ("image", "https://s.weibo.com/captcha/image.png", "continued"),
        ("image", "https://xiaohongshu.com/login_qr.png", "continued"),
        # Main document and page scripts (must continue for crawler DOM parsing)
        ("document", "https://s.weibo.com/weibo?q=test", "continued"),
        ("script", "https://s.weibo.com/bundle.js", "continued"),
        ("xhr", "https://api.bilibili.com/x/web-interface/search", "continued"),
    ]

    for r_type, url, expected_action in test_cases:
        route = MockRoute(r_type, url)
        asyncio.run(_filter_browser_route(route))
        assert route.action == expected_action, f"Failed for {r_type} @ {url}: expected {expected_action}, got {route.action}"

