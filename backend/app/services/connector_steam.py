from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from ..config import settings
from ..crawlers.runtime import (
    CrawlerErrorCode,
)
from .connector_contracts import (
    ConnectorCapabilities,
    ConnectorStatus,
    RawContentItem,
    SearchQuery,
    SourceConnector,
)
from .connector_support import (
    allocate_limits,
    get_with_retries,
    pooled_client,
)
from .steam_reviews import (
    SteamRequestBudget,
    select_discovered_apps,
    steam_review_external_id,
    steam_review_payload,
)


class SteamReviewsConnector(SourceConnector):
    source_id = "steam"
    label = "Steam reviews"
    group = "Public game community"
    capabilities = ConnectorCapabilities(
        True,
        watchlist_filter=True,
        interaction_fields=("like_count", "comment_count"),
    )

    def __init__(self) -> None:
        self._discovery_cache: dict[str, tuple[list[dict[str, Any]], bool]] = {}

    async def healthcheck(self) -> ConnectorStatus:
        return ConnectorStatus(
            "ready",
            "Public Steam game search and recent user reviews; no API key required.",
        )

    def checkpoint_fingerprint_fields(
        self,
        operation: str,
        channel: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del operation, channel
        return {
            "provider_contract": "steam-public-reviews-v2",
            "review_filter": settings.steam_review_filter.strip().casefold(),
            "language": settings.steam_review_language.strip().casefold(),
            "purchase_type": settings.steam_purchase_type.strip().casefold(),
            "max_discovered_apps": max(
                1,
                min(settings.steam_max_discovered_apps, 10),
            ),
        }

    async def search(
        self, query: SearchQuery, checkpoint: dict[str, Any] | None = None
    ) -> AsyncIterator[RawContentItem]:
        if checkpoint and not query.legacy_checkpoint:
            query.legacy_checkpoint = dict(checkpoint)
        budget = SteamRequestBudget(
            max(1, min(settings.steam_discovery_request_budget, 20)),
            max(1, min(settings.steam_review_request_budget, 100)),
            total_limit=query.request_limit(
                settings.steam_discovery_request_budget
                + settings.steam_review_request_budget,
                maximum=120,
            ),
        )
        max_apps = max(1, min(settings.steam_max_discovered_apps, 10))
        async with pooled_client(
            timeout=30,
            follow_redirects=False,
            headers={
                "User-Agent": "ContentBot/0.1 (local non-commercial game research)"
            },
        ) as client:
            games: list[dict[str, Any]] = []
            seen_apps: set[str] = set()
            game_limits = allocate_limits(max_apps, len(query.search_terms))
            for search_term, game_limit in zip(
                query.search_terms, game_limits, strict=True
            ):
                if game_limit == 0:
                    continue
                cache_key = search_term.strip().casefold()
                if cache_key in self._discovery_cache:
                    cached_selected, ambiguous = self._discovery_cache[cache_key]
                    selected = cached_selected[:game_limit]
                else:
                    if not budget.spend_discovery():
                        if query.warning_callback:
                            await query.warning_callback(
                                CrawlerErrorCode.BUDGET_EXHAUSTED.value,
                                "Steam app-discovery request budget was exhausted.",
                            )
                        break
                    search_response = await get_with_retries(
                        client,
                        "https://store.steampowered.com/api/storesearch/",
                        params={"term": search_term, "l": "english", "cc": "VN"},
                    )
                    selected, ambiguous = select_discovered_apps(
                        search_term,
                        list(search_response.json().get("items") or []),
                        limit=game_limit,
                    )
                    if selected and not ambiguous:
                        self._discovery_cache[cache_key] = (selected, ambiguous)
                if ambiguous:
                    if query.warning_callback:
                        await query.warning_callback(
                            "AMBIGUOUS_APP_MATCH",
                            f"Steam app discovery was ambiguous for {search_term!r}; save an exact /app/<id> URL instead.",
                        )
                    continue
                for game in selected:
                    app_id = str(game.get("id", ""))
                    if app_id and app_id not in seen_apps:
                        seen_apps.add(app_id)
                        games.append(game)
            review_limits = allocate_limits(query.max_items, len(games))
            for game, review_limit in zip(games, review_limits, strict=True):
                game_yielded = 0
                if review_limit == 0:
                    continue
                app_id = str(game.get("id", ""))
                game_name = str(game.get("name", query.name))
                if not app_id:
                    continue
                scope = {
                    "app_id": app_id,
                    "filter": settings.steam_review_filter,
                    "language": settings.steam_review_language,
                    "purchase_type": settings.steam_purchase_type,
                }
                backlog_cursor = str(
                    query.resume_cursor("app", default="", **scope) or ""
                )
                cursor = "*"
                used_backlog = False
                known_ids = set(query.recent_ids)
                while game_yielded < review_limit:
                    if not budget.spend_review():
                        query.report_cursor(
                            "app",
                            backlog_cursor or (cursor if cursor != "*" else None),
                            **scope,
                        )
                        if query.warning_callback:
                            await query.warning_callback(
                                CrawlerErrorCode.BUDGET_EXHAUSTED.value,
                                "Steam review request budget was exhausted.",
                            )
                        return
                    response = await get_with_retries(
                        client,
                        f"https://store.steampowered.com/appreviews/{app_id}",
                        params={
                            "json": 1,
                            "filter": settings.steam_review_filter,
                            "language": settings.steam_review_language,
                            "purchase_type": settings.steam_purchase_type,
                            "num_per_page": min(100, review_limit - game_yielded),
                            "cursor": cursor,
                        },
                    )
                    payload = response.json()
                    reviews = payload.get("reviews", [])
                    if payload.get("success") != 1 or not reviews:
                        query.report_cursor("app", None, **scope)
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
                        query.report_cursor("app", None, **scope)
                        break
                    for review in reviews:
                        if game_yielded >= review_limit:
                            break
                        review_id = str(review.get("recommendationid", ""))
                        external_id = f"{app_id}:{review_id}"
                        if not review_id or external_id in known_ids:
                            continue
                        yield self.raw_review(
                            review, app_id=app_id, game_name=game_name
                        )
                        known_ids.add(external_id)
                        game_yielded += 1
                    next_cursor = str(payload.get("cursor") or "")
                    if game_yielded >= review_limit:
                        query.report_cursor("app", next_cursor or None, **scope)
                        break
                    if not next_cursor or next_cursor == cursor:
                        query.report_cursor("app", None, **scope)
                        break
                    cursor = next_cursor
                    await asyncio.sleep(0.2)

    @staticmethod
    def raw_review(
        review: dict[str, Any],
        *,
        app_id: str,
        game_name: str,
    ) -> RawContentItem:
        timestamp = int(review.get("timestamp_created", 0) or 0)
        voted_up = bool(review.get("voted_up"))
        return RawContentItem(
            external_id=steam_review_external_id(app_id, review),
            canonical_url=f"https://steamcommunity.com/app/{app_id}/reviews/",
            title=f"{'Recommended' if voted_up else 'Not recommended'} — {game_name}",
            body_snippet=str(review.get("review") or "")[:4_000],
            author="Steam reviewer",
            locale=str(review.get("language") or "") or None,
            published_at=(
                datetime.fromtimestamp(timestamp, tz=UTC) if timestamp else None
            ),
            metrics={
                "like_count": int(review.get("votes_up", 0) or 0),
                "comment_count": int(review.get("comment_count", 0) or 0),
            },
            raw_payload=steam_review_payload(
                review,
                app_id=app_id,
                game_name=game_name,
            ),
        )
