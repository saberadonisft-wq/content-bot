from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ..application_services import AppServices, get_services
from ..config import settings
from ..schemas import (
    InsightSummaryOutput,
    TrendClustersOutput,
    TrendOutput,
    TrendPoint,
)
from ..services.clusters import cluster_items
from ..services.insight_cache import cached_aggregate
from ..services.insights import summarize_items
from ..services.item_query import ItemQuery, query_items
from ..services.runs import utcnow

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/v1/insights/summary", response_model=InsightSummaryOutput)
def insight_summary(
    keyword_id: int,
    source_id: str | None = None,
    query: str | None = None,
    language: str | None = None,
    sentiment: Literal["positive", "negative", "mixed", "neutral"] | None = None,
    topic: str | None = None,
    min_relevance: float = Query(default=0, ge=0, le=100),
    session_id: str | None = None,
    top_limit: int = Query(default=5, ge=1, le=20),
    *,
    services: AppServices = Depends(get_services),
):
    item_filter = ItemQuery(
        keyword_id,
        source_id,
        query,
        language,
        sentiment,
        topic,
        min_relevance,
        session_id,
    )
    filters = {
        key: str(value)
        for key, value in {
            "source_id": source_id,
            "query": query,
            "language": language,
            "sentiment": sentiment,
            "topic": topic,
            "min_relevance": min_relevance if min_relevance else None,
            "session_id": session_id,
        }.items()
        if value is not None
    }
    return InsightSummaryOutput(
        keyword_id=keyword_id,
        generated_at=utcnow(),
        filters=filters,
        **cached_aggregate(
            services.store,
            item_filter,
            ("summary", top_limit, settings.content_bot_indexed_item_queries),
            lambda: summarize_items(
                [
                    output.model_dump(mode="json")
                    for output in query_items(
                        services.store,
                        item_filter,
                        indexed=settings.content_bot_indexed_item_queries,
                    )[1]
                ],
                top_limit,
            ),
        ),
    )


@router.get("/api/v1/insights/clusters", response_model=TrendClustersOutput)
def insight_clusters(
    keyword_id: int,
    source_id: str | None = None,
    query: str | None = None,
    language: str | None = None,
    sentiment: Literal["positive", "negative", "mixed", "neutral"] | None = None,
    topic: str | None = None,
    min_relevance: float = Query(default=0, ge=0, le=100),
    session_id: str | None = None,
    min_items: int = Query(default=2, ge=2, le=10),
    limit: int = Query(default=20, ge=1, le=50),
    *,
    services: AppServices = Depends(get_services),
):
    keyword = services.store.keyword(keyword_id)
    if not keyword:
        raise HTTPException(404, "Keyword not found")
    item_filter = ItemQuery(
        keyword_id,
        source_id,
        query,
        language,
        sentiment,
        topic,
        min_relevance,
        session_id,
    )
    filters = {
        key: str(value)
        for key, value in {
            "source_id": source_id,
            "query": query,
            "language": language,
            "sentiment": sentiment,
            "topic": topic,
            "min_relevance": min_relevance if min_relevance else None,
            "session_id": session_id,
            "min_items": min_items if min_items != 2 else None,
        }.items()
        if value is not None
    }
    ignore_terms = [keyword["name"], *keyword.get("include_terms", [])]
    return TrendClustersOutput(
        keyword_id=keyword_id,
        generated_at=utcnow(),
        filters=filters,
        **cached_aggregate(
            services.store,
            item_filter,
            (
                "clusters",
                tuple(ignore_terms),
                min_items,
                limit,
                settings.content_bot_indexed_item_queries,
            ),
            lambda: cluster_items(
                [
                    output.model_dump(mode="json")
                    for output in query_items(
                        services.store,
                        item_filter,
                        indexed=settings.content_bot_indexed_item_queries,
                    )[1]
                ],
                ignore_terms,
                min_items,
                limit,
            ),
        ),
    )


@router.get("/api/v1/trends", response_model=list[TrendOutput])
def trends(
    keyword_id: int,
    limit: int = Query(default=10, ge=1, le=50),
    *,
    services: AppServices = Depends(get_services),
):
    rows = services.store.item_matches(keyword_id, positive_only=True)
    rows.sort(key=lambda row: row[1].get("trend_score", 0), reverse=True)
    rows = rows[:limit]
    output: list[TrendOutput] = []
    for item, match in rows:
        snapshots = services.store.snapshots(item["id"])
        output.append(
            TrendOutput(
                item_id=item["id"],
                title=item.get("title", ""),
                source_id=item["source_id"],
                trend_score=match.get("trend_score", 0),
                points=[
                    TrendPoint(
                        captured_at=row["captured_at"],
                        engagement=row.get("like_count", 0)
                        + 2 * row.get("comment_count", 0)
                        + 3 * row.get("share_count", 0)
                        + 2 * row.get("favorite_count", 0),
                    )
                    for row in snapshots
                ],
            )
        )
    return output
