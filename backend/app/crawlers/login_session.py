"""Visible, manual login bootstrap for application-owned crawler profiles."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ..config import settings
from . import SOURCE_REGISTRY
from .runtime import (
    BrowserLaunchRequest,
    BrowserSession,
    CancellationToken,
    PlaywrightBrowserHandle,
    PlaywrightPersistentDriver,
    ProfileNamespace,
)

LOGIN_HOMEPAGES = {
    "xhs": "https://www.xiaohongshu.com/",
    "douyin": "https://www.douyin.com/",
    "kuaishou": "https://www.kuaishou.com/?isHome=1",
    "bilibili": "https://www.bilibili.com/",
    # Search uses the desktop search origin. Bootstrapping on that origin lets
    # Weibo complete its own SSO redirect before the user closes the tab.
    "weibo": "https://s.weibo.com/",
    "tieba": "https://tieba.baidu.com/",
    "zhihu": "https://www.zhihu.com/",
}


def login_homepage(source_id: str) -> str:
    canonical = SOURCE_REGISTRY.resolve_id(source_id)
    try:
        return LOGIN_HOMEPAGES[canonical]
    except KeyError as exc:
        raise ValueError("Source does not use a CBCE browser login profile") from exc


async def open_manual_login(
    source_id: str,
    *,
    executable: Path,
    profile_root: Path,
    timeout_seconds: float = 600,
    account_ref: str = "default",
) -> None:
    if not 30 <= timeout_seconds <= 7_200:
        raise ValueError("Login timeout must be between 30 and 7200 seconds")
    canonical = SOURCE_REGISTRY.resolve_id(source_id)
    homepage = login_homepage(canonical)
    profile = ProfileNamespace(profile_root, SOURCE_REGISTRY).profile(
        canonical, account_ref
    )
    session = BrowserSession(
        PlaywrightPersistentDriver(),
        BrowserLaunchRequest(executable, profile),
        owner_id=f"manual-login-{canonical}",
    )
    cancellation = CancellationToken()
    handle = await session.open(cancellation)
    if not isinstance(handle, PlaywrightBrowserHandle):
        await session.close()
        raise TypeError("Manual login requires the Playwright browser driver")
    page = handle.context.pages[0] if handle.context.pages else await handle.context.new_page()
    try:
        await page.goto(homepage, wait_until="domcontentloaded", timeout=45_000)
        sys.stderr.write(
            f"CBCE_LOGIN_READY source={canonical} profile={profile.account_key}\n"
        )
        sys.stderr.write(
            "Complete login/challenges in the visible browser, then close that tab.\n"
        )
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while not page.is_closed():
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Manual browser login timed out")
            await asyncio.sleep(0.5)
    finally:
        await session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open a visible CBCE-owned browser profile for manual login."
    )
    parser.add_argument("--source", required=True, choices=tuple(LOGIN_HOMEPAGES))
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    executable = (
        settings.content_bot_cbce_browser_executable_path
        or settings.content_bot_coccoc_executable_path
    )
    try:
        asyncio.run(
            open_manual_login(
                args.source,
                executable=executable,
                profile_root=settings.content_bot_cbce_profile_root,
                timeout_seconds=args.timeout,
            )
        )
    except (KeyboardInterrupt, TimeoutError) as exc:
        sys.stderr.write(f"CBCE_LOGIN_ENDED {type(exc).__name__}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
