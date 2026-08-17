"""Canonical source catalog for Content Bot.

This module is declarative. It intentionally contains no copied endpoint,
selector, signing, or anti-bot implementation from MediaCrawler.
"""

from __future__ import annotations

from .contracts import (
    AuthMode,
    Coverage,
    CrawlBudgets,
    DomainRule,
    ExecutionLane,
    ImplementationState,
    MetricSpec,
    Operation,
    PolicyState,
    ProviderManifest,
    ProviderOperationSpec,
    SchedulePolicy,
    SourceManifest,
    TargetKind,
)


def _operation(
    operation: Operation,
    *targets: TargetKind,
    coverage: Coverage = Coverage.FULL,
    implementation: ImplementationState = ImplementationState.IMPLEMENTED,
    auth: AuthMode = AuthMode.NONE,
    schedule: SchedulePolicy = SchedulePolicy.BACKGROUND_SAFE,
    handler: str | None = None,
    codec: str | None = "cbce.checkpoint.v1",
    metrics: tuple[str, ...] = (),
    cta: str | None = None,
    budget_limits: CrawlBudgets | None = None,
) -> ProviderOperationSpec:
    return ProviderOperationSpec(
        operation=operation,
        target_kinds=targets,
        coverage=coverage,
        implementation=implementation,
        auth_modes=(auth,),
        schedule_policy=schedule,
        checkpoint_codec=codec,
        handler_key=handler,
        metric_ids=metrics,
        cta=cta,
        budget_limits=budget_limits,
    )


def _provider(
    provider_id: str,
    label: str,
    operations: tuple[ProviderOperationSpec, ...],
    *,
    access: str,
    lane: ExecutionLane = ExecutionLane.IN_PROCESS,
    policy: PolicyState = PolicyState.ALLOWED,
) -> ProviderManifest:
    return ProviderManifest(provider_id, label, access, lane, policy, operations)


def _metric(metric_id: str, label: str, semantics: str, legacy: str | None = None) -> MetricSpec:
    return MetricSpec(metric_id, label, semantics, legacy)


def _source(
    source_id: str,
    label: str,
    order: int,
    domains: tuple[str, ...],
    providers: tuple[ProviderManifest, ...],
    *,
    group: str,
    aliases: tuple[str, ...] = (),
    metrics: tuple[MetricSpec, ...] = (),
    primary: Operation = Operation.SEARCH,
    disclaimer: str | None = None,
) -> SourceManifest:
    return SourceManifest(
        id=source_id,
        legacy_aliases=aliases,
        label=label,
        order=order,
        group_id=group.casefold().replace(" ", "_"),
        group_label=group,
        domain_rules=tuple(DomainRule(domain) for domain in domains),
        providers=providers,
        default_provider_id=providers[0].id,
        content_kinds=("post", "video", "article", "comment"),
        metrics=metrics,
        primary_operation=primary,
        coverage_disclaimer=disclaimer,
    )


def _public_provider(source_id: str, *, search_coverage: Coverage = Coverage.FULL) -> ProviderManifest:
    metrics = {
        "youtube": ("view_count", "like_count", "comment_count"),
        "steam": ("helpful_votes", "comment_count"),
        "bluesky": ("like_count", "reply_count", "repost_count", "quote_count"),
        "reddit": ("score", "comment_count"),
        "mastodon": ("favorite_count", "reply_count", "reblog_count"),
    }.get(source_id, ())
    auth_mode = {
        "youtube": AuthMode.API_KEY,
        "reddit": AuthMode.APP_TOKEN,
    }.get(source_id, AuthMode.NONE)
    operations = [
        _operation(Operation.SEARCH, TargetKind.KEYWORD, coverage=search_coverage, auth=auth_mode, handler=f"connector:{source_id}:search", metrics=metrics),
        _operation(Operation.SCAN_CHANNEL, TargetKind.CHANNEL, auth=auth_mode, handler=f"channel:{source_id}", metrics=metrics),
    ]
    if source_id in {"bluesky", "mastodon", "reddit", "youtube"}:
        comment_metrics = {
            "reddit": ("score",),
            "youtube": ("like_count",),
            "bluesky": ("like_count", "reply_count"),
            "mastodon": ("favorite_count", "reply_count"),
        }[source_id]
        operations.append(
            _operation(
                Operation.LIST_COMMENTS,
                TargetKind.CONTENT_ID,
                coverage=Coverage.PARTIAL,
                auth=auth_mode,
                schedule=SchedulePolicy.MANUAL_ONLY,
                handler=f"connector:{source_id}:list_comments",
                codec=None,
                metrics=comment_metrics,
                cta=f"Scan a stored {source_id.title()} item with explicit root, child, total, depth, and request budgets.",
            )
        )
    return _provider(
        f"{source_id}_public",
        "Official/public provider",
        tuple(operations),
        access="public_or_official_api",
    )


def _bridge_provider(source_id: str) -> ProviderManifest:
    return _provider(
        "legacy_bridge",
        "Legacy bridge (temporary)",
        (
            _operation(
                Operation.SEARCH,
                TargetKind.KEYWORD,
                auth=AuthMode.BROWSER_PROFILE,
                schedule=SchedulePolicy.MANUAL_ONLY,
                handler=f"connector:{source_id}:search",
            ),
        ),
        access="interactive_browser_session",
        lane=ExecutionLane.ISOLATED_BROWSER,
        policy=PolicyState.LEGACY_ONLY,
    )


def _planned_browser_provider(
    source_id: str, *, search_implemented: bool = False
) -> ProviderManifest:
    implemented_operations = (
        {
            Operation.SEARCH,
            Operation.SCAN_CHANNEL,
            Operation.FETCH_DETAIL,
            Operation.LIST_CREATOR,
            Operation.LIST_COMMENTS,
        }
        if source_id == "tieba" and search_implemented
        else (
            {Operation.SEARCH, Operation.LIST_COMMENTS}
            if source_id == "bilibili" and search_implemented
            else ({Operation.SEARCH} if search_implemented else set())
        )
    )
    handler_names = {
        Operation.SEARCH: "search",
        Operation.SCAN_CHANNEL: "scan_channel",
        Operation.FETCH_DETAIL: "fetch_detail",
        Operation.LIST_CREATOR: "list_creator",
        Operation.LIST_COMMENTS: "list_comments",
    }
    operations = tuple(
        _operation(
            operation,
            target,
            coverage=(
                Coverage.PARTIAL
                if operation in implemented_operations
                else Coverage.FULL
            ),
            implementation=(
                ImplementationState.IMPLEMENTED
                if operation in implemented_operations
                else ImplementationState.PLANNED
            ),
            auth=AuthMode.BROWSER_PROFILE,
            schedule=SchedulePolicy.MANUAL_ONLY,
            handler=(
                f"connector:{source_id}:{handler_names[operation]}"
                if operation in implemented_operations
                else None
            ),
            cta=(
                "Run the experimental clean-room DOM provider manually."
                if operation in implemented_operations
                else "Internal clean-room adapter is planned."
            ),
        )
        for operation, target in (
            (Operation.SEARCH, TargetKind.KEYWORD),
            (Operation.SCAN_CHANNEL, TargetKind.CHANNEL),
            (Operation.FETCH_DETAIL, TargetKind.CONTENT_URL),
            (Operation.LIST_CREATOR, TargetKind.CREATOR),
            (Operation.LIST_COMMENTS, TargetKind.CONTENT_ID),
            (Operation.MEDIA_METADATA, TargetKind.CONTENT_ID),
        )
    )
    return _provider(
        f"cbce_{source_id}",
        "Content Bot clean-room adapter",
        operations,
        access="interactive_browser_session",
        lane=ExecutionLane.ISOLATED_BROWSER,
    )


def _licensed_weibo_provider() -> ProviderManifest:
    return _provider(
        "licensed_weibo",
        "MediaCrawler licensed Weibo API facade",
        (
            _operation(
                Operation.SEARCH,
                TargetKind.KEYWORD,
                coverage=Coverage.PARTIAL,
                auth=AuthMode.BROWSER_PROFILE,
                schedule=SchedulePolicy.MANUAL_ONLY,
                handler="connector:weibo:search",
                codec="cbce.licensed.weibo.v1",
                metrics=("like_count", "comment_count", "share_count"),
                cta="Enable the non-commercial licensed provider and complete Weibo login in the visible profile.",
                budget_limits=CrawlBudgets(
                    max_items=100,
                    max_requests=100,
                    deadline_seconds=900,
                ),
            ),
        ),
        access="licensed_noncommercial_learning",
        lane=ExecutionLane.ISOLATED_BROWSER,
        policy=PolicyState.ALLOWED,
    )


def _bilibili_open_provider() -> ProviderManifest:
    return _provider(
        "bilibili_open_platform",
        "Bilibili Open Platform",
        tuple(
            _operation(
                operation,
                target,
                coverage=Coverage.PARTIAL,
                implementation=ImplementationState.PLANNED,
                auth=AuthMode.OAUTH,
                schedule=SchedulePolicy.DISABLED,
                handler=None,
                cta="Approved Bilibili Open Platform access and an authorized UP account are required.",
            )
            for operation, target in (
                (Operation.SCAN_CHANNEL, TargetKind.CREATOR),
                (Operation.FETCH_DETAIL, TargetKind.CONTENT_ID),
                (Operation.LIST_CREATOR, TargetKind.CREATOR),
                (Operation.MEDIA_METADATA, TargetKind.CONTENT_ID),
            )
        ),
        access="authorized_associated_up_account",
        policy=PolicyState.APPROVAL_REQUIRED,
    )


def _x_api_provider() -> ProviderManifest:
    implemented = tuple(
        _operation(
            operation,
            target,
            coverage=(
                Coverage.PARTIAL
                if operation is Operation.LIST_COMMENTS
                else Coverage.FULL
            ),
            auth=AuthMode.BEARER_TOKEN,
            schedule=SchedulePolicy.MANUAL_ONLY,
            handler=f"connector:x:{handler}",
            cta="Configure X API credits/access to run this read-only operation.",
        )
        for operation, target, handler in (
            (Operation.FETCH_DETAIL, TargetKind.CONTENT_ID, "fetch_detail"),
            (Operation.LIST_CREATOR, TargetKind.CREATOR, "list_creator"),
            (Operation.LIST_COMMENTS, TargetKind.CONTENT_ID, "list_comments"),
        )
    )
    planned_media = _operation(
        Operation.MEDIA_METADATA,
        TargetKind.CONTENT_ID,
        coverage=Coverage.FULL,
        implementation=ImplementationState.PLANNED,
        auth=AuthMode.BEARER_TOKEN,
        schedule=SchedulePolicy.DISABLED,
        handler=None,
        cta="X media metadata is not exposed as a standalone operation yet.",
    )
    return _provider(
        "x_api",
        "Official X API",
        (
            _operation(
                Operation.SEARCH,
                TargetKind.KEYWORD,
                coverage=Coverage.PARTIAL,
                auth=AuthMode.BEARER_TOKEN,
                schedule=SchedulePolicy.MANUAL_ONLY,
                handler="connector:x:search",
                codec="cbce.x.search.v1",
                metrics=(
                    "like_count",
                    "reply_count",
                    "repost_count",
                    "quote_count",
                ),
                cta="Configure X API credits/access before running read-only search.",
            ),
            _operation(
                Operation.SCAN_CHANNEL,
                TargetKind.ACCOUNT,
                coverage=Coverage.PARTIAL,
                auth=AuthMode.BEARER_TOKEN,
                schedule=SchedulePolicy.MANUAL_ONLY,
                handler="connector:x:scan_channel",
                codec="cbce.x.creator.v1",
                metrics=(
                    "like_count",
                    "reply_count",
                    "repost_count",
                    "quote_count",
                ),
                cta="Configure X API credits/access to scan a saved creator account.",
            ),
            *implemented,
            planned_media,
        ),
        access="approved_official_api_pay_per_use",
        policy=PolicyState.APPROVAL_REQUIRED,
    )


SOURCE_MANIFESTS: tuple[SourceManifest, ...] = (
    _source("youtube", "YouTube", 1, ("youtube.com", "youtu.be"), (_public_provider("youtube", search_coverage=Coverage.PARTIAL),), group="Official API", metrics=(_metric("view_count", "Views", "public view count"), _metric("like_count", "Likes", "public like count"), _metric("comment_count", "Comments", "public comment count")), disclaimer="Official newest-first search uses a rolling 90-day window and project quota; saved-channel scans follow the public uploads playlist."),
    _source("web", "Game news", 2, (), (_public_provider("web", search_coverage=Coverage.PARTIAL),), group="Public web", disclaimer="Discovery is limited to configured RSS/Atom feeds."),
    _source("steam", "Steam reviews", 3, ("store.steampowered.com", "steamcommunity.com"), (_public_provider("steam", search_coverage=Coverage.PARTIAL),), group="Public game community", metrics=(_metric("helpful_votes", "Helpful votes", "Steam votes_up", "like_count"), _metric("comment_count", "Comments", "review comments")), disclaimer="Keyword discovery fails closed on ambiguous Store matches. Save an exact /app/<id> URL to pin a game; public reviewer identities are not retained."),
    _source("bluesky", "Bluesky", 4, ("bsky.app",), (_public_provider("bluesky", search_coverage=Coverage.PARTIAL),), group="Public social API", metrics=(_metric("like_count", "Likes", "public likes"), _metric("reply_count", "Replies", "public replies", "comment_count"), _metric("repost_count", "Reposts", "public reposts", "share_count"), _metric("quote_count", "Quotes", "public quote posts", "share_count")), disclaimer="Public AppView search and author feeds are best-effort surfaces. Stable identity is derived from the post AT URI while CID is retained only as the content version."),
    _source("mastodon", "Mastodon", 5, (), (_public_provider("mastodon", search_coverage=Coverage.PARTIAL),), group="Public social API", metrics=(_metric("favorite_count", "Favorites", "public favourites", "like_count"), _metric("reply_count", "Replies", "public replies", "comment_count"), _metric("reblog_count", "Reblogs", "public reblogs", "share_count")), disclaimer="Discovery is hashtag-only and limited to explicitly configured instances; bounded status-context replies are available only for stored posts on those instances. Public preview can be disabled and coverage is not Fediverse-wide."),
    _source("reddit", "Reddit", 6, ("reddit.com", "redd.it"), (_public_provider("reddit"),), group="Official API", metrics=(_metric("score", "Score", "net Reddit vote score", "like_count"), _metric("comment_count", "Comments", "public comment count"))),
    _source("x", "X", 7, ("x.com", "twitter.com"), (
        _provider("x_embed", "Public timeline embed", (_operation(Operation.RENDER_EMBED, TargetKind.ACCOUNT, handler="renderer:x", codec=None),), access="public_embed", lane=ExecutionLane.EMBED_ONLY),
        _x_api_provider(),
    ), group="Official API + public embed", primary=Operation.RENDER_EMBED, metrics=(_metric("like_count", "Likes", "public like count"), _metric("reply_count", "Replies", "public reply count", "comment_count"), _metric("repost_count", "Reposts", "public repost count", "share_count"), _metric("quote_count", "Quotes", "public quote count", "share_count")), disclaimer="Public timeline embed is view-only. Official recent search and saved-creator timeline scans are pay-per-use, provider-window limited, and manual by default."),
    *tuple(
        _source(
            source_id,
            label,
            order,
            domains,
            (
                _bridge_provider(source_id),
                *((_bilibili_open_provider(),) if source_id == "bilibili" else ()),
                *((_licensed_weibo_provider(),) if source_id == "weibo" else ()),
                _planned_browser_provider(
                    source_id,
                    search_implemented=source_id
                    in {
                        "xhs",
                        "douyin",
                        "kuaishou",
                        "bilibili",
                        "weibo",
                        "tieba",
                        "zhihu",
                    },
                ),
            ),
            group="Browser session",
            aliases=aliases,
            metrics=metrics,
        )
        for source_id, label, order, domains, aliases, metrics in (
            ("xhs", "Xiaohongshu", 8, ("xiaohongshu.com", "rednote.com", "xhslink.com"), (), (_metric("like_count", "Likes", "public likes"), _metric("comment_count", "Comments", "public comments"), _metric("favorite_count", "Favorites", "public favorites"))),
            ("douyin", "Douyin", 9, ("douyin.com",), ("dy",), (_metric("like_count", "Likes", "public likes"), _metric("comment_count", "Comments", "public comments"), _metric("share_count", "Shares", "public shares"), _metric("view_count", "Views", "public views"))),
            ("kuaishou", "Kuaishou", 10, ("kuaishou.com",), ("ks",), (_metric("like_count", "Likes", "public likes"), _metric("comment_count", "Comments", "public comments"), _metric("share_count", "Shares", "public shares"), _metric("view_count", "Views", "public views"))),
            ("bilibili", "Bilibili", 11, ("bilibili.com", "b23.tv"), ("bili",), (_metric("like_count", "Likes", "public likes"), _metric("comment_count", "Comments", "public comments"), _metric("favorite_count", "Favorites", "public favorites"), _metric("view_count", "Views", "public views"))),
            ("weibo", "Weibo", 12, ("weibo.com", "weibo.cn"), ("wb",), (_metric("like_count", "Likes", "public likes"), _metric("comment_count", "Comments", "public comments"), _metric("share_count", "Shares", "public reposts"))),
            ("tieba", "Baidu Tieba", 13, ("tieba.baidu.com",), (), (_metric("comment_count", "Comments", "public replies"),)),
            ("zhihu", "Zhihu", 14, ("zhihu.com",), (), (_metric("like_count", "Likes", "public votes"), _metric("comment_count", "Comments", "public comments"), _metric("favorite_count", "Favorites", "public favorites"))),
        )
    ),
    _source("tiktok", "TikTok", 15, ("tiktok.com",), (
        _provider(
            "tiktok_display",
            "TikTok Display API",
            tuple(
                _operation(
                    operation,
                    target,
                    coverage=Coverage.PARTIAL,
                    implementation=ImplementationState.IMPLEMENTED,
                    auth=AuthMode.OAUTH,
                    schedule=SchedulePolicy.MANUAL_ONLY,
                    handler=f"connector:tiktok:{handler}",
                    metrics=(
                        "view_count",
                        "like_count",
                        "comment_count",
                        "share_count",
                    ),
                    cta="Connect the creator through approved TikTok Login Kit OAuth with video.list.",
                )
                for operation, target, handler in (
                    (Operation.SCAN_CHANNEL, TargetKind.ACCOUNT, "scan_channel"),
                    (Operation.FETCH_DETAIL, TargetKind.CONTENT_URL, "fetch_detail"),
                )
            ),
            access="authorized_creator",
            policy=PolicyState.APPROVAL_REQUIRED,
        ),
        _provider("tiktok_research", "TikTok Research API", (_operation(Operation.SEARCH, TargetKind.KEYWORD, coverage=Coverage.PARTIAL, implementation=ImplementationState.PLANNED, auth=AuthMode.BEARER_TOKEN, schedule=SchedulePolicy.DISABLED, handler=None), _operation(Operation.LIST_COMMENTS, TargetKind.CONTENT_ID, coverage=Coverage.PARTIAL, implementation=ImplementationState.PLANNED, auth=AuthMode.BEARER_TOKEN, schedule=SchedulePolicy.DISABLED, handler=None)), access="approved_research", policy=PolicyState.APPROVAL_REQUIRED),
    ), group="Approved API", primary=Operation.SCAN_CHANNEL, metrics=(_metric("view_count", "Views", "public video views"), _metric("like_count", "Likes", "public video likes"), _metric("comment_count", "Comments", "public video comments"), _metric("share_count", "Shares", "public video shares")), disclaimer="Display API coverage is limited to the creator account that explicitly authorized video.list. Public keyword discovery requires separately approved Research API access and is not executable."),
    _source("facebook", "Facebook", 16, ("facebook.com", "fb.com", "fb.watch"), (
        _provider(
            "meta_pages",
            "Meta Pages API",
            (
                _operation(
                    Operation.SCAN_CHANNEL,
                    TargetKind.PAGE,
                    coverage=Coverage.PARTIAL,
                    auth=AuthMode.OAUTH,
                    schedule=SchedulePolicy.BACKGROUND_SAFE,
                    handler="connector:facebook:scan_channel",
                    codec="cbce.facebook.page-feed.v1",
                    metrics=("reaction_count", "comment_count", "share_count"),
                    cta="Configure an approved Page access token for the authorized Page.",
                ),
                _operation(Operation.FETCH_DETAIL, TargetKind.CONTENT_ID, coverage=Coverage.PARTIAL, implementation=ImplementationState.PLANNED, auth=AuthMode.OAUTH, schedule=SchedulePolicy.DISABLED, handler=None),
                _operation(Operation.LIST_COMMENTS, TargetKind.CONTENT_ID, coverage=Coverage.PARTIAL, implementation=ImplementationState.PLANNED, auth=AuthMode.OAUTH, schedule=SchedulePolicy.DISABLED, handler=None),
            ),
            access="authorized_page",
            policy=PolicyState.APPROVAL_REQUIRED,
        ),
    ), group="Approved API", primary=Operation.SCAN_CHANNEL, metrics=(_metric("reaction_count", "Reactions", "public Page post reactions"), _metric("comment_count", "Comments", "public Page post comments"), _metric("share_count", "Shares", "public Page post shares")), disclaimer="Only the explicitly authorized Page feed is implemented. Public Page access without authorization requires separate PPCA approval; profiles, Groups, and global keyword search are unsupported."),
    _source("instagram", "Instagram", 17, ("instagram.com",), (
        _provider(
            "instagram_hashtag",
            "Instagram Hashtag Search",
            (
                _operation(
                    Operation.SEARCH,
                    TargetKind.HASHTAG,
                    coverage=Coverage.PARTIAL,
                    auth=AuthMode.OAUTH,
                    schedule=SchedulePolicy.BACKGROUND_SAFE,
                    handler="connector:instagram:search",
                    codec="cbce.instagram.hashtag.v1",
                    metrics=("like_count", "comment_count"),
                    cta="Configure approved Instagram hashtag access for a Professional account.",
                ),
            ),
            access="approved_hashtag_discovery",
            policy=PolicyState.APPROVAL_REQUIRED,
        ),
        _provider("instagram_owned", "Instagram Professional API", tuple(_operation(op, target, coverage=Coverage.PARTIAL, implementation=ImplementationState.PLANNED, auth=AuthMode.OAUTH, schedule=SchedulePolicy.DISABLED, handler=None) for op, target in ((Operation.SCAN_CHANNEL, TargetKind.ACCOUNT), (Operation.FETCH_DETAIL, TargetKind.CONTENT_ID), (Operation.LIST_COMMENTS, TargetKind.CONTENT_ID))), access="authorized_professional_account", policy=PolicyState.APPROVAL_REQUIRED),
    ), group="Approved API", primary=Operation.SEARCH, metrics=(_metric("like_count", "Likes", "public like count"), _metric("comment_count", "Comments", "public comment count")), disclaimer="Hashtag discovery is permission-gated, quota-limited, and best effort. Owned Professional-account operations remain planned."),
)
