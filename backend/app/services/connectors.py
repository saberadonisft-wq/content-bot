"""Connector registry and compatible public imports; implementations live per provider."""

from __future__ import annotations

from ..config import settings
from .connector_bluesky import (
    BlueskyConnector,
)
from .connector_command import (
    JsonlCommandConnector,
)
from .connector_contracts import (
    ConnectorCapabilities,
    ConnectorStatus,
    RawContentItem,
    SearchQuery,
    SourceConnector,
)
from .connector_facebook import (
    FacebookPageConnector,
)
from .connector_feed import (
    FeedConnector,
)
from .connector_instagram import (
    InstagramHashtagConnector,
)
from .connector_mastodon import (
    MastodonConnector,
)
from .connector_reddit import (
    RedditConnector,
)
from .connector_steam import (
    SteamReviewsConnector,
)
from .connector_support import (
    DEFAULT_WEB_FEED_URLS,
    RETRYABLE_HTTP_STATUSES,
    allocate_limits,
    get_with_retries,
    logger,
    login_progress_from_stderr,
    parse_feed_datetime,
    plain_text,
    pooled_client,
    stable_external_id,
)
from .connector_tiktok import (
    TikTokDisplayConnector,
)
from .connector_x import (
    XCommentScan,
    XConnector,
    _order_x_comments,
    _x_content_id,
)
from .connector_youtube import (
    YouTubeConnector,
)

__all__ = [
    "DEFAULT_WEB_FEED_URLS",
    "RETRYABLE_HTTP_STATUSES",
    "BlueskyConnector",
    "ConnectorCapabilities",
    "ConnectorStatus",
    "FacebookPageConnector",
    "FeedConnector",
    "InstagramHashtagConnector",
    "JsonlCommandConnector",
    "MastodonConnector",
    "RawContentItem",
    "RedditConnector",
    "SearchQuery",
    "SourceConnector",
    "SteamReviewsConnector",
    "TikTokDisplayConnector",
    "XCommentScan",
    "XConnector",
    "YouTubeConnector",
    "_order_x_comments",
    "_x_content_id",
    "allocate_limits",
    "default_connectors",
    "get_with_retries",
    "logger",
    "login_progress_from_stderr",
    "parse_feed_datetime",
    "plain_text",
    "pooled_client",
    "stable_external_id",
]


def default_connectors() -> dict[str, SourceConnector]:
    connectors: list[SourceConnector] = [
        YouTubeConnector(),
        FeedConnector(
            "web",
            "Game news",
            "Public keyword news feed is available by default; override it with WEB_FEED_URLS.",
            settings.web_feed_urls or DEFAULT_WEB_FEED_URLS,
        ),
        SteamReviewsConnector(),
        BlueskyConnector(),
        MastodonConnector(),
        RedditConnector(),
        XConnector(),
    ]
    media_sources = [
        (
            "xhs",
            "Xiaohongshu",
            "xhs",
            ("like_count", "comment_count", "favorite_count"),
        ),
        (
            "douyin",
            "Douyin",
            "dy",
            ("like_count", "comment_count", "share_count", "view_count"),
        ),
        (
            "kuaishou",
            "Kuaishou",
            "ks",
            ("like_count", "comment_count", "share_count", "view_count"),
        ),
        (
            "bilibili",
            "Bilibili",
            "bili",
            ("like_count", "comment_count", "favorite_count", "view_count"),
        ),
        ("weibo", "Weibo", "wb", ("like_count", "comment_count", "share_count")),
        ("tieba", "Baidu Tieba", "tieba", ("comment_count",)),
        ("zhihu", "Zhihu", "zhihu", ("like_count", "comment_count", "favorite_count")),
    ]
    connectors.extend(
        JsonlCommandConnector(
            source_id,
            label,
            "Set MEDIACRAWLER_COMMAND to an explicit local adapter after you have logged in visibly.",
            ConnectorCapabilities(True, requires_login=True, interaction_fields=fields),
            platform,
        )
        for source_id, label, platform, fields in media_sources
    )
    connectors.extend(
        [
            TikTokDisplayConnector(),
            FacebookPageConnector(),
            InstagramHashtagConnector(),
        ]
    )
    if settings.content_bot_cbce_enabled:
        from .cbce_runtime import provider_overrides

        overrides = provider_overrides()
        from .cbce_connectors import (
            CbceBilibiliConnector,
            CbceLicensedWeiboConnector,
            CbceObservedDomConnector,
            CbceTiebaConnector,
        )

        selected_search_providers = {
            source_id: operations.get("search")
            for source_id, operations in overrides.items()
            if operations.get("search")
        }
        connectors = [
            (
                CbceLicensedWeiboConnector()
                if selected_search_providers.get(connector.source_id)
                == "licensed_weibo"
                else (
                    CbceTiebaConnector()
                    if connector.source_id == "tieba"
                    and selected_search_providers.get(connector.source_id)
                    == "cbce_tieba"
                    else (
                        CbceBilibiliConnector()
                        if connector.source_id == "bilibili"
                        and selected_search_providers.get(connector.source_id)
                        == "cbce_bilibili"
                        else CbceObservedDomConnector(connector.source_id)
                        if selected_search_providers.get(connector.source_id)
                        == f"cbce_{connector.source_id}"
                        else connector
                    )
                )
            )
            for connector in connectors
        ]
    result = {connector.source_id: connector for connector in connectors}
    if len(result) != len(connectors):
        raise ValueError("Duplicate connector source ID")
    from ..crawlers.registry import CANONICAL_SOURCE_IDS

    if tuple(result) != CANONICAL_SOURCE_IDS:
        raise ValueError(
            "Connector registrations must match the canonical source registry"
        )
    return result
