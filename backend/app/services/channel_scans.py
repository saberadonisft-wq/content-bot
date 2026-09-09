from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from ..config import settings
from ..crawlers import SOURCE_REGISTRY
from ..crawlers.contracts import ImplementationState, Operation
from ..crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from ..crawlers.target_detection import InvalidTargetUrl, normalize_target_url
from .bluesky_api import bluesky_json
from .bluesky_api import is_invalid_cursor as bluesky_invalid_cursor
from .bluesky_api import post_identity as bluesky_post_identity
from .connectors import (
    DEFAULT_WEB_FEED_URLS,
    BlueskyConnector,
    FeedConnector,
    RawContentItem,
    SearchQuery,
    SteamReviewsConnector,
    get_with_retries,
    stable_external_id,
)
from .credential_resolver import credential
from .feed_ingestion import parse_feed_templates
from .mastodon_api import (
    MastodonRequestBudget,
    mastodon_instances,
    mastodon_json,
)
from .mastodon_api import (
    next_max_id as mastodon_next_max_id,
)
from .mastodon_api import (
    normalize_status as normalize_mastodon_status,
)
from .reddit_oauth import reddit_token_cache
from .steam_reviews import (
    SteamRequestBudget,
    canonical_steam_app_url,
    parse_steam_app_id,
    steam_review_external_id,
)
from .youtube_api import YouTubeQuotaBudget, is_invalid_page_token, youtube_json


class ChannelUnavailable(RuntimeError):
    """The channel is saved, but cannot be scanned with the configured access."""


def _configured_target_hosts() -> dict[str, list[str]]:
    feed_values = settings.web_feed_urls or DEFAULT_WEB_FEED_URLS
    try:
        feed_urls = [template.value for template in parse_feed_templates(feed_values)]
    except ValueError:
        feed_urls = []
    try:
        mastodon_hosts = list(mastodon_instances(settings.mastodon_instances))
    except ValueError:
        mastodon_hosts = []
    return {
        "web": feed_urls,
        "mastodon": mastodon_hosts,
    }


def detect_source_id(url: str) -> str:
    match = SOURCE_REGISTRY.match_target(
        url,
        configured_hosts=_configured_target_hosts(),
    )
    return match.source_id if match else "web"


def channel_mode(source_id: str) -> str:
    canonical_id = SOURCE_REGISTRY.resolve_id(source_id)
    if SOURCE_REGISTRY.executable(canonical_id, Operation.RENDER_EMBED) and not SOURCE_REGISTRY.executable(
        canonical_id, Operation.SCAN_CHANNEL
    ):
        return "embed_only"
    specs = SOURCE_REGISTRY.operation_specs(canonical_id, Operation.SCAN_CHANNEL)
    if not any(
        spec.implementation is ImplementationState.IMPLEMENTED and spec.handler_key
        for _, spec in specs
    ):
        return "manual"
    if canonical_id == "x":
        return "api" if credential("x_bearer_token") else "embed_only"
    if canonical_id == "youtube":
        return "api" if credential("youtube_api_key") else "setup_required"
    if canonical_id == "reddit":
        return (
            "api"
            if credential("reddit_client_id") and credential("reddit_client_secret")
            else "setup_required"
        )
    if canonical_id in {"web", "steam", "bluesky", "mastodon"}:
        return "public"
    if canonical_id == "tieba":
        from .cbce_runtime import provider_overrides

        return (
            "browser"
            if settings.content_bot_cbce_enabled
            and provider_overrides().get("tieba", {}).get("search") == "cbce_tieba"
            else "manual"
        )
    if canonical_id == "tiktok":
        return "api" if _tiktok_display_configured() else "setup_required"
    if canonical_id == "facebook":
        return "api" if _facebook_page_configured() else "setup_required"
    return "manual"


def _tiktok_display_configured() -> bool:
    from .connectors import TikTokDisplayConnector

    return TikTokDisplayConnector().configured


def _facebook_page_configured() -> bool:
    from .connectors import FacebookPageConnector

    return FacebookPageConnector().configured


def _auto_label(source_id: str, normalized_url: str) -> str:
    """Extract a human-readable channel name from a normalized URL."""
    try:
        parsed = urlparse(normalized_url)
    except Exception:
        return ""
    parts = [p for p in parsed.path.split("/") if p]
    if source_id in {"x", "tiktok", "instagram"}:
        # e.g. https://x.com/Vaniixdom → @Vaniixdom
        if parts:
            handle = parts[-1].lstrip("@")
            return f"@{handle}" if handle else ""
    if source_id == "youtube":
        # e.g. https://www.youtube.com/@phongvanreview07 → @phongvanreview07
        if parts:
            segment = parts[-1]
            return segment if segment.startswith("@") else segment
    if source_id == "bluesky":
        # e.g. https://bsky.app/profile/handle.bsky.social → @handle.bsky.social
        if len(parts) >= 2 and parts[0] == "profile":
            return f"@{parts[1]}"
    if source_id == "mastodon":
        # e.g. https://mastodon.social/@user → @user
        if parts and parts[-1].startswith("@"):
            return parts[-1]
        if parts:
            return f"@{parts[-1]}"
    if source_id == "bilibili":
        # e.g. https://space.bilibili.com/12345 → Bilibili 12345
        if parts:
            return f"Bilibili {parts[-1]}"
    if source_id == "reddit":
        # e.g. https://www.reddit.com/r/gaming or /user/name
        if len(parts) >= 2:
            if parts[0] == "r":
                return f"r/{parts[1]}"
            if parts[0] in {"u", "user"}:
                return f"u/{parts[1]}"
    if source_id == "facebook":
        # e.g. https://www.facebook.com/PageName → PageName
        if parts:
            return parts[-1]
    if source_id == "steam":
        # e.g. https://store.steampowered.com/app/12345 → Steam App 12345
        if len(parts) >= 2 and parts[0] == "app":
            return f"Steam App {parts[1]}"
    if source_id in {"douyin", "kuaishou", "weibo", "xhs", "tieba", "zhihu"}:
        if parts:
            return parts[-1]
    if source_id == "web":
        return parsed.netloc or ""
    # Generic fallback: use the last path segment
    if parts:
        return parts[-1]
    return parsed.netloc or ""


def normalize_channel(values: dict[str, Any]) -> dict[str, Any]:
    raw_url = str(values.get("url") or "").strip()
    try:
        safe_url, _ = normalize_target_url(raw_url)
    except InvalidTargetUrl:
        raise ValueError(f"Invalid channel URL: {raw_url or '(empty)'}")
    match = SOURCE_REGISTRY.match_target(
        safe_url,
        configured_hosts=_configured_target_hosts(),
    )
    source_id = match.source_id if match else "web"
    parsed = urlparse(match.normalized_url if match else safe_url)
    # Provider query parameters rarely identify the channel and often contain
    # tracking tokens. RSS/Atom URLs are the exception and keep their query.
    query = parsed.query if source_id == "web" and match is not None else ""
    if source_id == "x":
        from ..crawlers.adapters.x import XTargetKind, parse_x_target

        try:
            x_target = parse_x_target(safe_url)
        except ValueError as exc:
            raise ValueError("X channel URL must identify a creator") from exc
        if x_target.kind is not XTargetKind.CREATOR:
            raise ValueError("X post URLs are content targets, not channels")
        normalized_url = x_target.canonical_url
    elif source_id == "tieba":
        from ..crawlers.adapters.tieba import TiebaTargetKind, parse_tieba_target

        try:
            tieba_target = parse_tieba_target(safe_url)
        except ValueError as exc:
            raise ValueError("Tieba channel URL must identify a forum or creator") from exc
        if tieba_target.kind not in {TiebaTargetKind.FORUM, TiebaTargetKind.CREATOR}:
            raise ValueError("Tieba thread URLs are content targets, not channels")
        normalized_url = tieba_target.canonical_url
    elif source_id == "tiktok":
        from ..crawlers.adapters.tiktok import TikTokTargetKind, parse_tiktok_target

        try:
            tiktok_target = parse_tiktok_target(safe_url)
        except ValueError as exc:
            raise ValueError("TikTok channel URL must identify a creator") from exc
        if tiktok_target.kind is not TikTokTargetKind.CREATOR:
            raise ValueError("TikTok video or short URLs are content targets, not channels")
        normalized_url = tiktok_target.canonical_url
    elif source_id == "facebook":
        from ..crawlers.adapters.facebook import (
            FacebookTargetKind,
            parse_facebook_target,
        )

        try:
            facebook_target = parse_facebook_target(safe_url)
        except ValueError as exc:
            raise ValueError("Facebook channel URL must identify a Page") from exc
        if facebook_target.kind is not FacebookTargetKind.PAGE:
            raise ValueError("Facebook content or short URLs are not Page channels")
        normalized_url = facebook_target.canonical_url
    elif source_id == "steam":
        try:
            normalized_url = canonical_steam_app_url(parse_steam_app_id(safe_url))
        except ValueError as exc:
            raise ValueError("Steam channel URL must identify one numeric App ID") from exc
    elif source_id == "bluesky":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0].casefold() != "profile":
            raise ValueError("Bluesky channel URL must identify one profile")
        actor = parts[1].strip().casefold()
        if not actor or len(actor) > 253:
            raise ValueError("Bluesky profile identifier is invalid")
        normalized_url = f"https://bsky.app/profile/{actor}"
    elif source_id == "mastodon":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1 or not parts[0].startswith("@"):
            raise ValueError("Mastodon channel URL must identify one account profile")
        account = parts[0][1:].strip().casefold()
        if not account or len(account) > 253 or any(char in account for char in "/?#@"):
            raise ValueError("Mastodon account identifier is invalid")
        normalized_url = f"https://{parsed.netloc.lower()}/@{account}"
    else:
        normalized_url = urlunparse(
            (
                "https",
                parsed.netloc.lower(),
                parsed.path.rstrip("/") or "/",
                "",
                query,
                "",
            )
        )
    mode = channel_mode(source_id) if match is not None else "manual"
    return {
        "id": str(
            values.get("id")
            or uuid.uuid5(uuid.NAMESPACE_URL, normalized_url).hex[:16]
        ),
        "url": raw_url,
        "normalized_url": normalized_url,
        "label": (str(values.get("label") or "").strip()[:120]
                  or _auto_label(source_id, normalized_url)),
        "source_id": source_id,
        "mode": mode,
        "enabled": bool(values.get("enabled", True)),
        "include_replies": bool(values.get("include_replies", False)),
        "include_reposts": bool(values.get("include_reposts", False)),
        "checkpoint": dict(values.get("checkpoint") or {}),
        "last_scanned_at": values.get("last_scanned_at"),
        "last_status": values.get("last_status"),
        "last_error": values.get("last_error"),
    }


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _title(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), text)[:180]


async def scan_channel(
    channel: dict[str, Any], query: SearchQuery
) -> AsyncIterator[RawContentItem]:
    source_id = channel["source_id"]
    current_mode = channel_mode(source_id)
    if current_mode == "embed_only":
        raise ChannelUnavailable(
            "Kênh X đã được lưu để xem, nhưng muốn tự tải bài mới cần X API User Timeline."
        )
    if current_mode == "setup_required":
        raise ChannelUnavailable(
            f"{source_id} chưa có thông tin API cần thiết; kênh vẫn được lưu."
        )
    scanner = CHANNEL_SCANNERS.get(source_id)
    if scanner is None:
        raise ChannelUnavailable(
            f"{source_id} hiện chỉ hỗ trợ lưu/mở kênh; chưa có cách quét kênh ổn định."
        )
    async for item in scanner(channel, query):
        yield item


async def _scan_tieba(
    channel: dict[str, Any], query: SearchQuery
) -> AsyncIterator[RawContentItem]:
    from .cbce_connectors import CbceTiebaConnector

    async for item in CbceTiebaConnector().scan_channel(channel, query):
        yield item


async def _scan_tiktok(
    channel: dict[str, Any], query: SearchQuery
) -> AsyncIterator[RawContentItem]:
    from .connectors import TikTokDisplayConnector

    async for item in TikTokDisplayConnector().scan_channel(channel, query):
        yield item


async def _scan_x(
    channel: dict[str, Any], query: SearchQuery
) -> AsyncIterator[RawContentItem]:
    from .connectors import XConnector

    async for item in XConnector().scan_channel(channel, query):
        yield item


async def _scan_facebook(
    channel: dict[str, Any], query: SearchQuery
) -> AsyncIterator[RawContentItem]:
    from .connectors import FacebookPageConnector

    async for item in FacebookPageConnector().scan_channel(channel, query):
        yield item


async def _scan_feed(
    channel: dict[str, Any], query: SearchQuery
) -> AsyncIterator[RawContentItem]:
    connector = FeedConnector("web", "Saved feed", "", channel["url"])
    if connector.configuration_error:
        raise ChannelUnavailable(connector.configuration_error)
    feed_query = SearchQuery(
        keyword_id=query.keyword_id,
        name=query.name,
        include_terms=[],
        max_items=query.max_items,
        request_budget=query.request_budget,
        deadline_seconds=query.deadline_seconds,
        warning_callback=query.warning_callback,
        checkpoint_tracker=query.checkpoint_tracker,
        legacy_checkpoint=query.legacy_checkpoint,
    )
    async for item in connector.search(feed_query):
        yield item


async def _youtube_channel_id(client: httpx.AsyncClient, url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    params: dict[str, Any] = {
        "part": "snippet,contentDetails",
        "key": credential("youtube_api_key"),
    }
    if parts and parts[0] == "channel" and len(parts) > 1:
        params["id"] = parts[1]
    elif parts and parts[0].startswith("@"):
        params["forHandle"] = parts[0][1:]
    elif parts and parts[0] == "user" and len(parts) > 1:
        params["forUsername"] = parts[1]
    else:
        raise ChannelUnavailable(
            "Link YouTube cần có dạng /@handle, /channel/UC... hoặc /user/..."
        )
    payload = await youtube_json(client, "/channels", params=params)
    rows = payload.get("items") or []
    if not rows:
        raise ChannelUnavailable("Không tìm thấy kênh YouTube từ link đã lưu.")
    return str(rows[0].get("id") or "")


async def _scan_youtube(
    channel: dict[str, Any], query: SearchQuery
) -> AsyncIterator[RawContentItem]:
    if not credential("youtube_api_key"):
        raise ChannelUnavailable("Thiếu YOUTUBE_API_KEY.")
    quota = YouTubeQuotaBudget(
        settings.youtube_search_request_budget,
        settings.youtube_general_request_budget,
        max_total_requests=query.request_limit(
            settings.youtube_search_request_budget
            + settings.youtube_general_request_budget,
            maximum=10_000,
        ),
    )
    async with httpx.AsyncClient(
        base_url="https://www.googleapis.com/youtube/v3",
        timeout=30,
        follow_redirects=False,
    ) as client:
        if not quota.spend("/channels"):
            raise ChannelUnavailable("YouTube request budget is too small to resolve the channel.")
        channel_id = await _youtube_channel_id(client, channel["url"])
        if not quota.spend("/channels"):
            raise ChannelUnavailable("YouTube request budget is too small to inspect the channel.")
        channel_payload = await youtube_json(
            client,
            "/channels",
            params={
                "part": "contentDetails",
                "id": channel_id,
                "key": credential("youtube_api_key"),
            },
        )
        rows = channel_payload.get("items") or []
        uploads = (
            (((rows[0] if rows else {}).get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        )
        if not uploads:
            raise ChannelUnavailable("Kênh YouTube không có uploads playlist công khai.")
        yielded = 0
        backlog_token = query.resume_cursor(
            "channel", channel_id=channel_id, default=None
        )
        page_token: str | None = None
        used_backlog = False
        known_ids = set(query.recent_ids)
        while yielded < query.max_items:
            if not quota.spend("/playlistItems"):
                query.report_cursor(
                    "channel", page_token, channel_id=channel_id
                )
                break
            params: dict[str, Any] = {
                "part": "snippet,contentDetails",
                "playlistId": uploads,
                "maxResults": min(50, query.max_items - yielded),
                "key": credential("youtube_api_key"),
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                payload = await youtube_json(client, "/playlistItems", params=params)
            except CrawlerFailure as exc:
                if page_token and is_invalid_page_token(exc):
                    query.report_cursor("channel", None, channel_id=channel_id)
                    break
                raise
            items = payload.get("items") or []
            video_ids = [
                str((row.get("contentDetails") or {}).get("videoId") or "")
                for row in items
            ]
            video_ids = [video_id for video_id in video_ids if video_id]
            if not video_ids:
                query.report_cursor("channel", None, channel_id=channel_id)
                break
            page_has_unseen = any(video_id not in known_ids for video_id in video_ids)
            if not page_has_unseen:
                if backlog_token and not used_backlog:
                    page_token = str(backlog_token)
                    used_backlog = True
                    continue
                query.report_cursor("channel", None, channel_id=channel_id)
                break
            if not quota.spend("/videos"):
                query.report_cursor(
                    "channel", page_token, channel_id=channel_id
                )
                break
            details_payload = await youtube_json(
                client,
                "/videos",
                params={
                    "part": "snippet,statistics",
                    "id": ",".join(video_ids),
                    "key": credential("youtube_api_key"),
                },
            )
            details = {
                str(row.get("id")): row
                for row in details_payload.get("items") or []
            }
            for video_id in video_ids:
                if video_id in known_ids or yielded >= query.max_items:
                    continue
                row = details.get(video_id) or {}
                if not row:
                    continue
                snippet = row.get("snippet") or {}
                statistics = row.get("statistics") or {}
                yield RawContentItem(
                    external_id=video_id,
                    canonical_url=f"https://www.youtube.com/watch?v={video_id}",
                    title=str(snippet.get("title") or ""),
                    body_snippet=str(snippet.get("description") or "")[:4000],
                    author=str(snippet.get("channelTitle") or ""),
                    hashtags=[
                        str(tag) if str(tag).startswith("#") else f"#{tag}"
                        for tag in snippet.get("tags") or []
                        if str(tag).strip()
                    ],
                    locale=snippet.get("defaultLanguage"),
                    published_at=_datetime(snippet.get("publishedAt")),
                    metrics={
                        "view_count": int(statistics.get("viewCount", 0) or 0),
                        "like_count": int(statistics.get("likeCount", 0) or 0),
                        "comment_count": int(statistics.get("commentCount", 0) or 0),
                    },
                    raw_payload={
                        "provider_id": "youtube_data_v3",
                        "video_id": video_id,
                        "channel_id": str(snippet.get("channelId") or ""),
                        "category_id": str(snippet.get("categoryId") or ""),
                        "live_broadcast_content": str(
                            snippet.get("liveBroadcastContent") or ""
                        ),
                    },
                )
                yielded += 1
                known_ids.add(video_id)
            next_page_token = payload.get("nextPageToken")
            if yielded >= query.max_items:
                query.report_cursor(
                    "channel", next_page_token, channel_id=channel_id
                )
                break
            if not next_page_token or next_page_token == page_token:
                query.report_cursor("channel", None, channel_id=channel_id)
                break
            page_token = next_page_token


def _steam_app_id(url: str) -> str:
    try:
        return parse_steam_app_id(url)
    except ValueError:
        return ""


async def _scan_steam(
    channel: dict[str, Any], query: SearchQuery
) -> AsyncIterator[RawContentItem]:
    app_id = _steam_app_id(channel["url"])
    if not app_id:
        raise ChannelUnavailable("Link Steam cần chứa app id của game.")
    budget = SteamRequestBudget(
        max(1, min(settings.steam_discovery_request_budget, 20)),
        max(1, min(settings.steam_review_request_budget, 100)),
        total_limit=query.request_limit(
            settings.steam_discovery_request_budget
            + settings.steam_review_request_budget,
            maximum=120,
        ),
    )
    scope = {
        "app_id": app_id,
        "filter": settings.steam_review_filter,
        "language": settings.steam_review_language,
        "purchase_type": settings.steam_purchase_type,
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        backlog_cursor = str(
            query.resume_cursor("channel", default="", **scope) or ""
        )
        cursor = "*"
        used_backlog = False
        yielded = 0
        known_ids = set(query.recent_ids)
        while yielded < query.max_items:
            if not budget.spend_review():
                query.report_cursor(
                    "channel",
                    backlog_cursor or (cursor if cursor != "*" else None),
                    **scope,
                )
                if query.warning_callback:
                    await query.warning_callback(
                        "BUDGET_EXHAUSTED",
                        "Steam review request budget was exhausted.",
                    )
                break
            response = await get_with_retries(
                client,
                f"https://store.steampowered.com/appreviews/{app_id}",
                params={
                    "json": 1,
                    "filter": settings.steam_review_filter,
                    "language": settings.steam_review_language,
                    "purchase_type": settings.steam_purchase_type,
                    "num_per_page": min(100, query.max_items - yielded),
                    "cursor": cursor,
                },
            )
            payload = response.json()
            reviews = payload.get("reviews") or []
            if payload.get("success") != 1 or not reviews:
                query.report_cursor("channel", None, **scope)
                break
            page_has_unseen = any(
                steam_review_external_id(app_id, review) not in known_ids
                for review in reviews
                if review.get("recommendationid")
            )
            if not page_has_unseen:
                if backlog_cursor and not used_backlog:
                    cursor = backlog_cursor
                    used_backlog = True
                    continue
                query.report_cursor("channel", None, **scope)
                break
            for review in reviews:
                if yielded >= query.max_items:
                    break
                review_id = str(review.get("recommendationid") or "")
                external_id = f"{app_id}:{review_id}"
                if not review_id or external_id in known_ids:
                    continue
                yield SteamReviewsConnector.raw_review(
                    review,
                    app_id=app_id,
                    game_name=str(channel.get("label") or f"Steam App {app_id}"),
                )
                known_ids.add(external_id)
                yielded += 1
            next_cursor = str(payload.get("cursor") or "")
            if yielded >= query.max_items:
                query.report_cursor("channel", next_cursor or None, **scope)
                break
            if not next_cursor or next_cursor == cursor:
                query.report_cursor("channel", None, **scope)
                break
            cursor = next_cursor


def _bluesky_actor(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    return parts[1] if len(parts) >= 2 and parts[0] == "profile" else ""


async def _scan_bluesky(
    channel: dict[str, Any], query: SearchQuery
) -> AsyncIterator[RawContentItem]:
    actor = _bluesky_actor(
        str(channel.get("normalized_url") or channel["url"])
    )
    if not actor:
        raise ChannelUnavailable("Link Bluesky cần có dạng bsky.app/profile/handle.")
    request_budget = query.request_limit(
        settings.bluesky_request_budget, maximum=100
    )
    async with httpx.AsyncClient(
        base_url="https://public.api.bsky.app",
        timeout=30,
        follow_redirects=False,
        headers={"User-Agent": "ContentBot/0.1 (public AppView reader)"},
    ) as client:
        backlog_cursor = str(
            query.resume_cursor("channel", actor=actor, default="") or ""
        )
        cursor = ""
        used_backlog = False
        yielded = 0
        known_ids = set(query.recent_ids)
        requests = 0
        while yielded < query.max_items:
            if requests >= request_budget:
                query.report_cursor(
                    "channel", backlog_cursor or cursor or None, actor=actor
                )
                if query.warning_callback:
                    await query.warning_callback(
                        "BUDGET_EXHAUSTED",
                        "Bluesky AppView request budget was exhausted.",
                    )
                break
            params = {
                "actor": actor,
                "limit": min(100, query.max_items - yielded),
                "filter": "posts_with_replies" if channel.get("include_replies") else "posts_no_replies",
            }
            if cursor:
                params["cursor"] = cursor
            requests += 1
            try:
                payload = await bluesky_json(
                    client,
                    "/xrpc/app.bsky.feed.getAuthorFeed",
                    params=params,
                )
            except CrawlerFailure as exc:
                if cursor and bluesky_invalid_cursor(exc):
                    query.report_cursor("channel", None, actor=actor)
                    break
                raise
            entries = payload.get("feed") or []
            if not isinstance(entries, list):
                raise CrawlerFailure(
                    CrawlerErrorCode.PARSE_CHANGED,
                    "Bluesky author feed has an invalid collection.",
                )
            if not entries:
                query.report_cursor("channel", None, actor=actor)
                break
            candidates = [
                (entry.get("post") or {}, bool(entry.get("reason")))
                for entry in entries
                if isinstance(entry, dict)
                and (channel.get("include_reposts") or not entry.get("reason"))
            ]
            identities = [
                (post, is_repost, bluesky_post_identity(post))
                for post, is_repost in candidates
                if isinstance(post, dict)
            ]
            valid_identities = [
                (post, is_repost, identity)
                for post, is_repost, identity in identities
                if identity is not None
            ]
            next_cursor = str(payload.get("cursor") or "")
            if candidates and not valid_identities:
                raise CrawlerFailure(
                    CrawlerErrorCode.PARSE_CHANGED,
                    "Bluesky author feed records lack valid AT URI identities.",
                )
            if not valid_identities:
                if not next_cursor or next_cursor == cursor:
                    query.report_cursor("channel", None, actor=actor)
                    break
                cursor = next_cursor
                continue
            page_has_unseen = any(
                stable_external_id("bsky", identity[0]) not in known_ids
                for _post, _is_repost, identity in valid_identities
            )
            if not page_has_unseen:
                if backlog_cursor and not used_backlog:
                    cursor = backlog_cursor
                    used_backlog = True
                    continue
                query.report_cursor("channel", None, actor=actor)
                break
            for post, is_repost, identity in valid_identities:
                if yielded >= query.max_items:
                    break
                uri, record_key = identity
                external_id = stable_external_id("bsky", uri)
                if external_id in known_ids:
                    continue
                normalized = BlueskyConnector.raw_post(
                    post,
                    record_key=record_key,
                    external_id=external_id,
                    discovery={
                        "channel_actor": actor,
                        "is_repost": is_repost,
                    },
                )
                if normalized is None:
                    continue
                yield normalized
                known_ids.add(external_id)
                yielded += 1
            if yielded >= query.max_items:
                query.report_cursor("channel", next_cursor or None, actor=actor)
                break
            if not next_cursor or next_cursor == cursor:
                query.report_cursor("channel", None, actor=actor)
                break
            cursor = next_cursor


async def _reddit_token(client: httpx.AsyncClient) -> str:
    if not credential("reddit_client_id") or not credential("reddit_client_secret"):
        raise ChannelUnavailable("Thiếu Reddit OAuth configuration.")
    return await reddit_token_cache.get(
        client,
        client_id=credential("reddit_client_id"),
        client_secret=credential("reddit_client_secret"),
    )


async def _scan_reddit(
    channel: dict[str, Any], query: SearchQuery
) -> AsyncIterator[RawContentItem]:
    parts = [part for part in urlparse(channel["url"]).path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() not in {"r", "user", "u"}:
        raise ChannelUnavailable("Link Reddit cần trỏ tới /r/subreddit hoặc /user/account.")
    scope = "r" if parts[0].lower() == "r" else "user"
    name = parts[1]
    from .connectors import RedditConnector

    pseudonymizer = RedditConnector._pseudonymizer()
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=False,
        headers={"User-Agent": credential("reddit_user_agent", "ContentBot/0.1 (local research tool)")},
    ) as client:
        token = await _reddit_token(client)
        client.headers["Authorization"] = f"Bearer {token}"
        after = str(
            query.resume_cursor("channel", scope=scope, name=name, default="") or ""
        )
        yielded = 0
        seen: set[str] = set()
        request_budget = query.request_limit(100, maximum=100)
        requests = 0
        while yielded < query.max_items:
            if requests >= request_budget:
                query.report_cursor(
                    "channel", after or None, scope=scope, name=name
                )
                if query.warning_callback:
                    await query.warning_callback(
                        "BUDGET_EXHAUSTED",
                        "Reddit channel request budget was exhausted.",
                    )
                break
            params: dict[str, Any] = {
                "limit": min(100, query.max_items - yielded),
                "raw_json": 1,
            }
            if after:
                params["after"] = after
            requests += 1
            response = await get_with_retries(
                client,
                f"https://oauth.reddit.com/{'r/' if scope == 'r' else 'user/'}{name}/new",
                params=params,
            )
            listing = response.json().get("data") or {}
            children = listing.get("children") or []
            if not children:
                query.report_cursor("channel", None, scope=scope, name=name)
                break
            page_yielded = 0
            for child in children:
                if yielded >= query.max_items:
                    break
                post = child.get("data") or {}
                post_id = str(post.get("id") or "")
                permalink = str(post.get("permalink") or "")
                if not post_id or post_id in seen or not permalink:
                    continue
                seen.add(post_id)
                yield RawContentItem(
                    external_id=post_id,
                    canonical_url=f"https://www.reddit.com{permalink}",
                    title=str(post.get("title") or "")[:180],
                    body_snippet=str(post.get("selftext") or "")[:4000],
                    author=pseudonymizer.pseudonym(
                        "reddit", str(post.get("author") or "")
                    ),
                    published_at=datetime.fromtimestamp(float(post.get("created_utc")), tz=UTC)
                    if post.get("created_utc")
                    else None,
                    metrics={
                        "like_count": int(post.get("score", 0) or 0),
                        "comment_count": int(post.get("num_comments", 0) or 0),
                    },
                    raw_payload={
                        "post_id": post_id,
                        "subreddit": str(post.get("subreddit") or ""),
                        "score": int(post.get("score", 0) or 0),
                        "is_self": bool(post.get("is_self")),
                        "outbound_url": str(post.get("url") or ""),
                        "flair": str(post.get("link_flair_text") or ""),
                    },
                )
                yielded += 1
                page_yielded += 1
            next_after = str(listing.get("after") or "")
            query.report_cursor(
                "channel", next_after or None, scope=scope, name=name
            )
            if not next_after or next_after == after or page_yielded == 0:
                break
            after = next_after


async def _scan_mastodon(
    channel: dict[str, Any], query: SearchQuery
) -> AsyncIterator[RawContentItem]:
    parsed = urlparse(channel.get("normalized_url") or channel["url"])
    account = next(
        (part[1:] for part in parsed.path.split("/") if part.startswith("@")), ""
    )
    if not account:
        raise ChannelUnavailable("Link Mastodon cần chứa /@account.")
    instance = parsed.netloc.lower()
    if instance not in mastodon_instances(settings.mastodon_instances):
        raise ChannelUnavailable("Mastodon instance is not in MASTODON_INSTANCES.")
    budget = MastodonRequestBudget(
        query.request_limit(settings.mastodon_request_budget, maximum=500)
    )
    async with httpx.AsyncClient(
        base_url=f"https://{instance}",
        timeout=30,
        follow_redirects=False,
        headers={"User-Agent": "ContentBot/0.1 (Mastodon public API)"},
    ) as client:
        lookup, _lookup_response = await mastodon_json(
            client,
            "/api/v1/accounts/lookup",
            params={"acct": account},
            budget=budget,
        )
        account_id = str((lookup if isinstance(lookup, dict) else {}).get("id") or "")
        if not account_id:
            raise ChannelUnavailable("Không tìm thấy tài khoản Mastodon.")
        scope = {
            "instance": instance,
            "account_id": account_id,
            "include_replies": bool(channel.get("include_replies")),
            "include_reposts": bool(channel.get("include_reposts")),
        }
        backlog = str(
            query.resume_cursor("channel", default="", **scope) or ""
        )
        max_id = ""
        frontier = True
        yielded = 0
        known_ids: set[str] = set(query.recent_ids)
        seen_cursors: set[str] = set()
        while yielded < query.max_items:
            params: dict[str, Any] = {
                "limit": min(40, query.max_items - yielded),
                "exclude_replies": str(not channel.get("include_replies")).lower(),
                "exclude_reblogs": str(not channel.get("include_reposts")).lower(),
            }
            if max_id:
                params["max_id"] = max_id
            payload, response = await mastodon_json(
                client,
                f"/api/v1/accounts/{account_id}/statuses",
                params=params,
                budget=budget,
            )
            if not isinstance(payload, list):
                raise CrawlerFailure(
                    CrawlerErrorCode.PARSE_CHANGED,
                    "Mastodon account timeline response was invalid.",
                )
            posts = [post for post in payload if isinstance(post, dict)]
            if not posts:
                query.report_cursor("channel", None, **scope)
                break
            normalized = [
                normalize_mastodon_status(
                    post,
                    fetching_instance=instance,
                    discovery={"account_scan": True},
                )
                for post in posts
            ]
            valid = [item for item in normalized if item is not None]
            overlap = any(item["external_id"] in known_ids for item in valid)
            next_cursor = mastodon_next_max_id(response, posts)
            if frontier and overlap and backlog:
                max_id = backlog
                frontier = False
                continue
            if frontier and overlap:
                query.report_cursor("channel", None, **scope)
                break
            for item in valid:
                if yielded >= query.max_items:
                    break
                external_id = str(item["external_id"])
                if external_id in known_ids:
                    continue
                known_ids.add(external_id)
                yield RawContentItem(
                    external_id=external_id,
                    canonical_url=str(item["canonical_url"]),
                    title=str(item["title"]),
                    body_snippet=str(item["body"]),
                    author=str(item["author"]),
                    hashtags=list(item["hashtags"]),
                    locale=item["locale"],
                    published_at=item["published_at"],
                    metrics=dict(item["metrics"]),
                    raw_payload=dict(item["raw_payload"]),
                )
                yielded += 1
            if not next_cursor:
                query.report_cursor("channel", None, **scope)
                break
            if next_cursor in seen_cursors or next_cursor == max_id:
                if query.warning_callback:
                    await query.warning_callback(
                        "MASTODON_CURSOR_STALLED",
                        f"Mastodon instance {instance} repeated its pagination cursor.",
                    )
                break
            seen_cursors.add(next_cursor)
            query.report_cursor("channel", next_cursor, **scope)
            max_id = next_cursor
            frontier = False


CHANNEL_SCANNERS = {
    "youtube": _scan_youtube,
    "web": _scan_feed,
    "steam": _scan_steam,
    "bluesky": _scan_bluesky,
    "reddit": _scan_reddit,
    "mastodon": _scan_mastodon,
    "tieba": _scan_tieba,
    "x": _scan_x,
    "tiktok": _scan_tiktok,
    "facebook": _scan_facebook,
}
