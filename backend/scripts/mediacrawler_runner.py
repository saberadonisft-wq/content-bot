from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import runpy
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MEDIACRAWLER_ROOT = PROJECT_ROOT / "vendor" / "mediacrawler"
PROFILE_ENV = "CONTENT_BOT_MEDIACRAWLER_PROFILE_DIR"
COCCOC_ENV = "CONTENT_BOT_COCCOC_EXECUTABLE_PATH"
DEFAULT_COCCOC_PATH = Path(
    r"C:\Program Files\CocCoc\Browser\Application\browser.exe"
)
LOGIN_COOKIE_MARKERS: dict[str, dict[str, str | None]] = {
    "xhs": {"web_session": None},
    "dy": {"LOGIN_STATUS": "1"},
    "ks": {"passToken": None},
    "bili": {"DedeUserID": None, "SESSDATA": None, "bili_jct": None},
    "wb": {"SSOLoginState": None, "WBPSESS": None},
    "tieba": {"STOKEN": None, "PTOKEN": None, "BDUSS": None},
    "zhihu": {"z_c0": None},
}
_active_login_monitors: set[int] = set()


def has_login_cookie(platform: str, cookies: list[dict]) -> bool:
    markers = LOGIN_COOKIE_MARKERS.get(platform.casefold(), {})
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        expected = markers.get(name)
        if name in markers and (expected is None or str(cookie.get("value") or "") == expected):
            return True
    return False


def emit_authenticated_progress() -> None:
    print(
        "CONTENT_BOT_PROGRESS "
        + json.dumps(
            {
                "status": "authenticated",
                "message": "Login session detected in Cốc Cốc",
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
        flush=True,
    )


def emit_browser_recovery_progress() -> None:
    print(
        "CONTENT_BOT_PROGRESS "
        + json.dumps(
            {
                "status": "retrying",
                "message": "Bilibili page was closed; reopening the Cốc Cốc page",
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
        flush=True,
    )


async def monitor_login_state(browser_context, platform: str) -> None:
    """Detect an existing or newly-created session without exposing cookie values."""
    context_id = id(browser_context)
    try:
        while True:
            try:
                if has_login_cookie(platform, await browser_context.cookies()):
                    emit_authenticated_progress()
                    return
            except Exception as exc:
                if "closed" in str(exc).casefold():
                    return
            await asyncio.sleep(1)
    finally:
        _active_login_monitors.discard(context_id)


def start_login_monitor(browser_context, user_data_dir: str) -> None:
    platform = Path(user_data_dir).name.casefold()
    if platform not in LOGIN_COOKIE_MARKERS:
        return
    context_id = id(browser_context)
    if context_id in _active_login_monitors:
        return
    _active_login_monitors.add(context_id)
    asyncio.create_task(monitor_login_state(browser_context, platform))


def install_progress_logging() -> None:
    """Forward MediaCrawler's login confirmation as a machine-readable line."""

    class ContentBotProgressHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage()
            lowered = message.casefold()
            markers = (
                "login successful",
                "login status confirmed",
                "login state result: true",
                "use cache login state",
                "login state verified",
                "ping zhihu successfully",
            )
            if any(marker in lowered for marker in markers):
                emit_authenticated_progress()

    root = logging.getLogger()
    if not any(
        isinstance(handler, ContentBotProgressHandler)
        for handler in root.handlers
    ):
        root.addHandler(ContentBotProgressHandler())


def configure_media_crawler(config: ModuleType, profile_dir: Path) -> None:
    """Make unattended API launches open their own visible, persistent browser.

    MediaCrawler currently defaults to connecting to an already-running Chrome
    debugging port. Content Bot does not require users to configure that port;
    the regular Playwright persistent-context path is more predictable for the
    explicit, user-supervised login flow.
    """

    config.ENABLE_CDP_MODE = False
    config.CDP_CONNECT_EXISTING = False
    config.HEADLESS = False
    config.CDP_HEADLESS = False
    config.SAVE_LOGIN_STATE = True
    config.USER_DATA_DIR = str(profile_dir.resolve() / "%s")


def configure_coccoc(executable_path: Path) -> None:
    """Force MediaCrawler's Chromium launch calls to use the local Cốc Cốc binary."""
    from playwright.async_api import BrowserType

    if getattr(BrowserType, "_content_bot_coccoc_patched", False):
        return

    original_persistent = BrowserType.launch_persistent_context
    original_launch = BrowserType.launch

    async def launch_persistent(self, user_data_dir, **kwargs):
        kwargs.pop("channel", None)
        kwargs.setdefault("executable_path", str(executable_path))
        try:
            lock_path = Path(user_data_dir) / "SingletonLock"
            if lock_path.exists():
                lock_path.unlink(missing_ok=True)
        except Exception:
            pass
        browser_context = await original_persistent(self, user_data_dir, **kwargs)
        start_login_monitor(browser_context, str(user_data_dir))
        return browser_context

    async def launch(self, **kwargs):
        kwargs.pop("channel", None)
        kwargs.setdefault("executable_path", str(executable_path))
        return await original_launch(self, **kwargs)

    BrowserType.launch_persistent_context = launch_persistent
    BrowserType.launch = launch
    BrowserType._content_bot_coccoc_patched = True


def install_bilibili_page_recovery() -> None:
    """Rebind Bilibili's API client if its page target disappears mid-run."""
    try:
        from playwright._impl._errors import TargetClosedError
        from media_platform.bilibili.client import BilibiliClient
    except ImportError:
        return

    if getattr(BilibiliClient, "_content_bot_page_recovery_patched", False):
        return

    original_get_wbi_keys = BilibiliClient.get_wbi_keys

    async def get_wbi_keys_with_recovery(self):
        try:
            return await original_get_wbi_keys(self)
        except TargetClosedError as closed_error:
            emit_browser_recovery_progress()
            try:
                old_page = getattr(self, "playwright_page", None)
                context = old_page.context
                open_pages = [page for page in context.pages if not page.is_closed()]
                page = open_pages[0] if open_pages else await context.new_page()
                await page.goto(
                    "https://www.bilibili.com",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                self.playwright_page = page
            except Exception as recovery_error:
                # Keep the original marker so the bridge's bounded full-process
                # retry can run when the whole context disappeared.
                raise closed_error from recovery_error
            return await original_get_wbi_keys(self)

    BilibiliClient.get_wbi_keys = get_wbi_keys_with_recovery
    BilibiliClient._content_bot_page_recovery_patched = True


def main() -> None:
    if not MEDIACRAWLER_ROOT.joinpath("main.py").exists():
        raise SystemExit("MediaCrawler submodule is missing")

    profile_dir = Path(os.environ.get(PROFILE_ENV, PROJECT_ROOT / "data" / "browser-profile"))
    coccoc_path = Path(
        os.environ.get(COCCOC_ENV) or str(DEFAULT_COCCOC_PATH)
    ).expanduser()
    if not coccoc_path.is_file():
        raise SystemExit(f"Cốc Cốc browser executable was not found: {coccoc_path}")
    sys.path.insert(0, str(MEDIACRAWLER_ROOT))
    os.chdir(MEDIACRAWLER_ROOT)
    config = importlib.import_module("config")
    configure_media_crawler(config, profile_dir)
    configure_coccoc(coccoc_path)
    install_progress_logging()
    install_bilibili_page_recovery()
    runpy.run_path(str(MEDIACRAWLER_ROOT / "main.py"), run_name="__main__")


if __name__ == "__main__":
    main()
