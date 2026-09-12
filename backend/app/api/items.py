from __future__ import annotations

import csv
import io
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse

from ..application_services import AppServices, get_services
from ..config import settings
from ..schemas import (
    ItemOutput,
    PagedItems,
)
from ..services.item_query import ItemQuery, query_items

logger = logging.getLogger(__name__)
router = APIRouter()


def filtered_item_outputs(
    keyword_id: int,
    source_id: str | None = None,
    query: str | None = None,
    language: str | None = None,
    sentiment: str | None = None,
    topic: str | None = None,
    min_relevance: float = 0,
    session_id: str | None = None,
    *,
    services: AppServices,
) -> list[ItemOutput]:
    _, outputs = query_items(
        services.store,
        ItemQuery(
            keyword_id,
            source_id,
            query,
            language,
            sentiment,
            topic,
            min_relevance,
            session_id,
        ),
        indexed=settings.content_bot_indexed_item_queries,
    )
    return outputs


@router.get("/api/v1/items", response_model=PagedItems)
def list_items(
    keyword_id: int,
    source_id: str | None = None,
    query: str | None = None,
    language: str | None = None,
    sentiment: Literal["positive", "negative", "mixed", "neutral"] | None = None,
    topic: str | None = None,
    min_relevance: float = Query(default=0, ge=0, le=100),
    session_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    *,
    services: AppServices = Depends(get_services),
):
    total, outputs = query_items(
        services.store,
        ItemQuery(
            keyword_id,
            source_id,
            query,
            language,
            sentiment,
            topic,
            min_relevance,
            session_id,
        ),
        limit=limit,
        offset=offset,
        indexed=settings.content_bot_indexed_item_queries,
    )
    return PagedItems(total=total, items=outputs)


@router.get("/api/v1/export.csv")
def export_csv(
    keyword_id: int,
    source_id: str | None = None,
    session_id: str | None = None,
    language: str | None = None,
    sentiment: Literal["positive", "negative", "mixed", "neutral"] | None = None,
    topic: str | None = None,
    query: str | None = None,
    min_relevance: float = Query(default=0, ge=0, le=100),
    *,
    services: AppServices = Depends(get_services),
):
    outputs = filtered_item_outputs(
        keyword_id,
        source_id=source_id,
        session_id=session_id,
        language=language,
        sentiment=sentiment,
        topic=topic,
        query=query,
        min_relevance=min_relevance,
        services=services,
    )
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(
        [
            "source",
            "title",
            "author",
            "url",
            "published_at",
            "language",
            "sentiment",
            "topics",
            "insight_reasons",
            "relevance",
            "trend",
            "views",
            "likes",
            "comments",
            "shares",
            "favorites",
        ]
    )
    for item in outputs:
        insights = item.insights.model_dump()
        reasons = [
            *insights["sentiment"]["reasons"],
            *(
                f"{topic['label']}: {', '.join(topic['reasons'])}"
                for topic in insights["topics"]
            ),
        ]
        writer.writerow(
            [
                item.source_id,
                item.title,
                item.author,
                item.canonical_url,
                item.published_at,
                insights["language"]["code"],
                insights["sentiment"]["label"],
                "; ".join(topic["label"] for topic in insights["topics"]),
                "; ".join(reasons),
                item.relevance_score,
                item.trend_score,
                item.metrics.get("view_count", 0),
                item.metrics.get("like_count", item.metrics.get("reaction_count", 0)),
                item.metrics.get("comment_count", 0),
                item.metrics.get("share_count", 0),
                item.metrics.get("favorite_count", 0),
            ]
        )
    return StreamingResponse(
        iter([stream.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=content-bot-{keyword_id}.csv"
        },
    )


@router.get("/api/v1/export.json")
def export_json(
    keyword_id: int,
    source_id: str | None = None,
    session_id: str | None = None,
    language: str | None = None,
    sentiment: Literal["positive", "negative", "mixed", "neutral"] | None = None,
    topic: str | None = None,
    query: str | None = None,
    min_relevance: float = Query(default=0, ge=0, le=100),
    *,
    services: AppServices = Depends(get_services),
):
    outputs = filtered_item_outputs(
        keyword_id,
        source_id=source_id,
        session_id=session_id,
        language=language,
        sentiment=sentiment,
        topic=topic,
        query=query,
        min_relevance=min_relevance,
        services=services,
    )
    return JSONResponse(
        [
            {
                "source_id": item.source_id,
                "title": item.title,
                "author": item.author,
                "url": str(item.canonical_url),
                "published_at": item.published_at.isoformat()
                if item.published_at
                else None,
                "metrics": item.metrics,
                "relevance_score": item.relevance_score,
                "trend_score": item.trend_score,
                "match_reasons": item.match_reasons,
                "insights": item.insights.model_dump(),
            }
            for item in outputs
        ]
    )
