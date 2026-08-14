"""Weibo clean-room adapter contracts."""

from .dom_provider import WeiboDomContract, WeiboDomSearchProvider
from .provider import (
    WeiboPost,
    WeiboPostPage,
    WeiboSearchAdapter,
    WeiboSearchCursor,
    WeiboSearchProvider,
    normalize_weibo_post,
)
from .targets import WeiboTarget, WeiboTargetKind, parse_weibo_target

__all__ = [
    "WeiboDomContract",
    "WeiboDomSearchProvider",
    "WeiboPost",
    "WeiboPostPage",
    "WeiboSearchAdapter",
    "WeiboSearchCursor",
    "WeiboSearchProvider",
    "WeiboTarget",
    "WeiboTargetKind",
    "normalize_weibo_post",
    "parse_weibo_target",
]
