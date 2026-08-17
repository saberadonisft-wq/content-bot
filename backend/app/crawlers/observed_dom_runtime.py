"""Static safety policy for reviewed browser DOM search contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .adapters.douyin import parse_douyin_target
from .adapters.kuaishou import parse_kuaishou_target
from .adapters.weibo import parse_weibo_target
from .adapters.xhs import parse_xhs_target
from .adapters.zhihu import parse_zhihu_target

Canonicalizer = Callable[[str], Any]
IdentityBuilder = Callable[[Any], str]


@dataclass(frozen=True, slots=True)
class ObservedDomRuntimeSpec:
    source_id: str
    label: str
    provider_id: str
    mode: str
    allowed_hosts: frozenset[str]
    login_hosts: frozenset[str]
    media_hosts: tuple[str, ...]
    content_kinds: frozenset[str]
    metric_ids: frozenset[str]
    canonicalize: Canonicalizer
    identity_builder: IdentityBuilder
    page_size: int = 20


def _default_identity(target: Any) -> str:
    return str(getattr(target, "external_id", ""))


def _typed_identity(target: Any) -> str:
    kind = str(getattr(getattr(target, "kind", None), "value", ""))
    external_id = str(getattr(target, "external_id", ""))
    return f"{kind}:{external_id}" if kind and external_id else ""


OBSERVED_DOM_RUNTIME_SPECS: dict[str, ObservedDomRuntimeSpec] = {
    "xhs": ObservedDomRuntimeSpec(
        "xhs",
        "Xiaohongshu",
        "cbce_xhs",
        "browser_video_v1",
        frozenset({"xiaohongshu.com", "rednote.com"}),
        frozenset({"passport.xiaohongshu.com"}),
        ("xiaohongshu.com", "rednote.com", "xhscdn.com"),
        frozenset({"note"}),
        frozenset({"like_count", "comment_count", "favorite_count"}),
        parse_xhs_target,
        _default_identity,
    ),
    "douyin": ObservedDomRuntimeSpec(
        "douyin",
        "Douyin",
        "cbce_douyin",
        "browser_video_v1",
        frozenset({"douyin.com"}),
        frozenset({"sso.douyin.com"}),
        ("douyin.com", "douyinpic.com", "douyinvod.com"),
        frozenset({"video"}),
        frozenset({"like_count", "comment_count", "share_count", "view_count"}),
        parse_douyin_target,
        _default_identity,
    ),
    "kuaishou": ObservedDomRuntimeSpec(
        "kuaishou",
        "Kuaishou",
        "cbce_kuaishou",
        "browser_video_v1",
        frozenset({"kuaishou.com"}),
        frozenset({"passport.kuaishou.com"}),
        ("kuaishou.com", "kwaicdn.com"),
        frozenset({"video"}),
        frozenset({"like_count", "comment_count", "share_count", "view_count"}),
        parse_kuaishou_target,
        _default_identity,
    ),
    "weibo": ObservedDomRuntimeSpec(
        "weibo",
        "Weibo",
        "cbce_weibo",
        "weibo_post_v1",
        frozenset({"weibo.com", "weibo.cn", "m.weibo.cn"}),
        frozenset({"passport.weibo.com"}),
        ("weibo.com", "weibo.cn", "sinaimg.cn", "sinaimg.com"),
        frozenset({"post"}),
        frozenset({"like_count", "comment_count", "share_count"}),
        parse_weibo_target,
        _default_identity,
    ),
    "zhihu": ObservedDomRuntimeSpec(
        "zhihu",
        "Zhihu",
        "cbce_zhihu",
        "browser_video_v1",
        frozenset({"zhihu.com"}),
        frozenset(),
        ("zhihu.com", "zhimg.com"),
        frozenset({"answer", "article", "video"}),
        frozenset({"like_count", "comment_count", "favorite_count"}),
        parse_zhihu_target,
        _typed_identity,
    ),
}
