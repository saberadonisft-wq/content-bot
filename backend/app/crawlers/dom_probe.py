"""Reproducible, value-free DOM probe for clean-room adapter research."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from . import SOURCE_REGISTRY
from .adapters import NavigationPolicy, OwnedBrowserPage
from .runtime import (
    BrowserLaunchRequest,
    BrowserSession,
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    PlaywrightPersistentDriver,
    ProfileNamespace,
    observe_dom_structure,
    safe_diagnostic,
)


@dataclass(frozen=True, slots=True)
class DomProbeSpec:
    source_id: str
    provider_id: str
    url: str
    allowed_hosts: tuple[str, ...]
    login_hosts: tuple[str, ...]
    login_selectors: tuple[str, ...] = ()
    login_detection_grace_ms: int = 0
    challenge_selectors: tuple[str, ...] = ()
    link_path_prefixes: tuple[tuple[str, str], ...] = ()
    auth_landmarks: tuple[str, ...] = ()


DOM_PROBE_SPECS = {
    "xhs": DomProbeSpec(
        "xhs",
        "cbce_xhs",
        "https://www.xiaohongshu.com/search_result?keyword=game",
        ("xiaohongshu.com", "rednote.com"),
        (),
        ("button.login-btn", "div.login-container img.qrcode-img"),
        5_000,
        (),
        (("content", "/explore/"),),
        ("button.login-btn",),
    ),
    "douyin": DomProbeSpec(
        "douyin",
        "cbce_douyin",
        "https://www.douyin.com/search/game",
        ("douyin.com",),
        (),
        ('input[type="tel"]',),
        7_000,
        (),
        (("content", "/video/"),),
    ),
    "kuaishou": DomProbeSpec(
        "kuaishou",
        "cbce_kuaishou",
        "https://www.kuaishou.com/search/video?searchKey=game",
        ("kuaishou.com",),
        (),
        ("div.login div.detail",),
        5_000,
        (),
        (("content", "/short-video/"),),
    ),
    "weibo": DomProbeSpec(
        "weibo",
        "cbce_weibo",
        "https://s.weibo.com/weibo?q=game",
        ("weibo.com", "weibo.cn"),
        ("passport.weibo.com",),
        (),
        0,
        (),
        (("content", "/detail/"),),
    ),
    "tieba": DomProbeSpec(
        "tieba",
        "cbce_tieba",
        "https://tieba.baidu.com/f/search/res?ie=utf-8&qw=game",
        ("tieba.baidu.com",),
        ("passport.baidu.com",),
        (),
        0,
        (),
        (("thread", "/p/"),),
    ),
    "zhihu": DomProbeSpec(
        "zhihu",
        "cbce_zhihu",
        "https://www.zhihu.com/search?type=content&q=game",
        ("zhihu.com",),
        (),
        (),
        0,
        ("div.unhuman",),
        (
            ("answer", "/question/"),
            ("article", "/p/"),
            ("video", "/zvideo/"),
        ),
        ("div.signflowmodal",),
    ),
}


def dom_probe_spec(source_id: str) -> DomProbeSpec:
    canonical = SOURCE_REGISTRY.resolve_id(source_id)
    try:
        return DOM_PROBE_SPECS[canonical]
    except KeyError as exc:
        raise ValueError("Source does not have a clean-room DOM probe") from exc


async def probe_dom(
    source_id: str,
    *,
    executable: Path,
    profile_root: Path,
    auth_timeout_seconds: float = 600,
    account_ref: str = "default",
) -> dict[str, object]:
    spec = dom_probe_spec(source_id)
    profile = ProfileNamespace(profile_root, SOURCE_REGISTRY).profile(
        spec.source_id, account_ref
    )
    browser = BrowserSession(
        PlaywrightPersistentDriver(),
        BrowserLaunchRequest(executable, profile),
        owner_id=f"dom-probe-{spec.source_id}",
    )
    owned = OwnedBrowserPage(
        browser,
        NavigationPolicy(
            spec.source_id,
            frozenset(spec.allowed_hosts),
            frozenset(spec.login_hosts),
            spec.login_selectors,
            spec.login_detection_grace_ms,
        ),
    )
    cancellation = CancellationToken()
    await owned.open(
        _probe_context(spec.source_id, spec.provider_id), cancellation
    )
    try:
        page = await owned.navigate(
            spec.url,
            cancellation,
            wait_after_ms=5_000,
            auth_timeout_seconds=auth_timeout_seconds,
            on_auth_required=lambda: sys.stderr.write(
                f"CBCE_DOM_PROBE_AUTH_REQUIRED source={spec.source_id}\n"
            ),
            on_authenticated=lambda: sys.stderr.write(
                f"CBCE_DOM_PROBE_AUTHENTICATED source={spec.source_id}\n"
            ),
        )
        observation = await observe_dom_structure(
            page,
            source_id=spec.source_id,
            provider_id=spec.provider_id,
            operation="search",
            allowed_hosts=spec.allowed_hosts,
            link_path_prefixes=dict(spec.link_path_prefixes),
            sentinel_selectors={
                f"challenge_{index}": selector
                for index, selector in enumerate(spec.challenge_selectors, start=1)
            },
        )
        validate_probe_gate(spec, observation)
        return observation.as_dict()
    finally:
        await owned.close()


def _probe_context(source_id: str, provider_id: str):
    from .runtime import RunBudgets, RunContext

    return RunContext(
        run_id=f"dom-probe-{source_id}",
        keyword_id=0,
        source_id=source_id,
        provider_id=provider_id,
        operation="search",
        target={"kind": "keyword"},
        terms=("game",),
        filters={},
        budgets=RunBudgets(max_items=1, max_requests=1, deadline_seconds=900),
    )


def validate_probe_gate(spec: DomProbeSpec, observation: object) -> None:
    sentinel_counts = getattr(observation, "sentinel_counts", ())
    if any(getattr(item, "count", 0) for item in sentinel_counts):
        raise CrawlerFailure(
            CrawlerErrorCode.CHALLENGE_REQUIRED,
            f"{spec.source_id} requires manual challenge completion.",
        )
    landmarks = getattr(observation, "landmarks", ())
    landmark_counts = {
        str(getattr(item, "signature", "")): int(getattr(item, "count", 0))
        for item in landmarks
    }
    if any(
        landmark_counts.get(signature.casefold(), 0)
        for signature in spec.auth_landmarks
    ):
        raise CrawlerFailure(
            CrawlerErrorCode.AUTH_REQUIRED,
            f"Log in to {spec.source_id} in its visible application profile.",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect a value-free DOM structure observation."
    )
    parser.add_argument("--source", required=True, choices=tuple(DOM_PROBE_SPECS))
    parser.add_argument("--auth-timeout", type=float, default=600)
    args = parser.parse_args(argv)
    executable = (
        settings.content_bot_cbce_browser_executable_path
        or settings.content_bot_coccoc_executable_path
    )
    try:
        observation = asyncio.run(
            probe_dom(
                args.source,
                executable=executable,
                profile_root=settings.content_bot_cbce_profile_root,
                auth_timeout_seconds=args.auth_timeout,
            )
        )
    except CrawlerFailure as exc:
        sys.stderr.write(
            json.dumps(
                {"error_code": exc.code.value, "error": exc.safe_message},
                ensure_ascii=False,
            )
            + "\n"
        )
        return 2
    except Exception as exc:
        sys.stderr.write(
            json.dumps(
                {"error_code": "PROBE_FAILED", "error": safe_diagnostic(exc)},
                ensure_ascii=False,
            )
            + "\n"
        )
        return 2
    sys.stdout.write(json.dumps(observation, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
