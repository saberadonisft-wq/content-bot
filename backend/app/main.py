from __future__ import annotations

import asyncio
import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .config import settings
from .mongo import store
from .schemas import (
    BatchOutput,
    InsightSummaryOutput,
    ItemOutput,
    KeywordInput,
    KeywordOutput,
    PagedItems,
    RunRequest,
    SourceOutput,
    SourceRunOutput,
    TrendClustersOutput,
    TrendOutput,
    TrendPoint,
)
from .services.clusters import cluster_items
from .services.connectors import (
    UnconfiguredConnector,
    YouTubeConnector,
    default_connectors,
)
from .services.insights import summarize_items
from .services.runs import EventBus, RunManager, utcnow
from .services.text import content_insights, insights_match, normalized

connectors = default_connectors()
events = EventBus()
run_manager = RunManager(connectors, events)


async def scheduler_loop() -> None:
    while True:
        await run_manager.scheduler_tick()
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(store.initialize)
    await asyncio.to_thread(run_manager.cleanup_interrupted)
    await asyncio.to_thread(run_manager.cleanup_irrelevant)
    task = asyncio.create_task(scheduler_loop(), name="content-bot-scheduler")
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Content Bot API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in settings.content_bot_cors_origins.split(",")
        if origin.strip()
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)


def keyword_output(keyword: dict) -> KeywordOutput:
    return KeywordOutput(
        id=keyword["id"],
        name=keyword["name"],
        include_terms=keyword.get("include_terms", []),
        exclude_terms=keyword.get("exclude_terms", []),
        source_ids=keyword.get("source_ids", []),
        enabled=keyword.get("enabled", True),
        interval_minutes=keyword.get("interval_minutes", 360),
        max_items_per_source=keyword.get("max_items_per_source", 500),
        next_run_at=keyword.get("next_run_at"),
        created_at=keyword["created_at"],
        updated_at=keyword["updated_at"],
    )


def keyword_name_exists(name: str, exclude_id: int | None = None) -> bool:
    return store.keyword_name_exists(normalized(name), exclude_id)


def batch_output(batch: dict) -> BatchOutput:
    return BatchOutput(
        id=batch["id"],
        keyword_id=batch["keyword_id"],
        trigger=batch["trigger"],
        state=batch["state"],
        started_at=batch.get("started_at"),
        finished_at=batch.get("finished_at"),
        error_message=batch.get("error_message"),
        source_runs=[
            SourceRunOutput(
                id=row["id"],
                source_id=row["source_id"],
                state=row["state"],
                phase=row.get("phase", row["state"]),
                progress_mode=row.get("progress_mode", "determinate"),
                progress_current=row.get("progress_current", row.get("fetched_count", 0)),
                progress_total=row.get("progress_total"),
                progress_percent=(
                    100.0
                    if row.get("state") == "succeeded"
                    else (
                        round(
                            min(
                                row.get("fetched_count", 0)
                                / row["progress_total"]
                                * 100,
                                99,
                            ),
                            1,
                        )
                        if row.get("progress_mode", "determinate") == "determinate"
                        and row.get("progress_total")
                        else None
                    )
                ),
                message=row.get("message"),
                browser_state=row.get("browser_state"),
                fetched_count=row.get("fetched_count", 0),
                ingested_count=row.get("ingested_count", 0),
                started_at=row.get("started_at"),
                finished_at=row.get("finished_at"),
                heartbeat_at=row.get("heartbeat_at"),
                error_message=row.get("error_message"),
            )
            for row in batch.get("source_runs", [])
        ],
    )


def item_output(item: dict, match: dict) -> ItemOutput:
    hashtags = item.get("hashtags", [])
    return ItemOutput(
        id=item["id"],
        source_id=item["source_id"],
        canonical_url=item["canonical_url"],
        title=item.get("title", ""),
        body_snippet=item.get("body_snippet", ""),
        author=item.get("author", ""),
        hashtags=hashtags,
        locale=item.get("locale"),
        published_at=item.get("published_at"),
        metrics=item.get("metrics", {}),
        relevance_score=match.get("relevance_score", 0),
        trend_score=match.get("trend_score", 0),
        match_reasons=match.get("match_reasons", []),
        insights=content_insights(item.get("title", ""), item.get("body_snippet", ""), hashtags, item.get("locale")),
        first_seen_at=item["first_seen_at"],
        last_seen_at=item["last_seen_at"],
    )


def filtered_item_outputs(
    keyword_id: int,
    source_id: str | None = None,
    query: str | None = None,
    language: str | None = None,
    sentiment: str | None = None,
    topic: str | None = None,
    min_relevance: float = 0,
) -> list[ItemOutput]:
    rows = store.item_matches(keyword_id, positive_only=True)
    rows = [(item, match) for item, match in rows if match.get("relevance_score", 0) >= min_relevance]
    if source_id:
        rows = [(item, match) for item, match in rows if item["source_id"] == source_id]
    if query:
        needle = query.strip().casefold()
        rows = [
            (item, match)
            for item, match in rows
            if needle in " ".join((item.get("title", ""), item.get("body_snippet", ""), item.get("author", ""))).casefold()
        ]
    rows.sort(
        key=lambda row: (row[1].get("trend_score", 0), row[0].get("published_at") or row[0]["first_seen_at"]),
        reverse=True,
    )
    outputs = [item_output(item, match) for item, match in rows]
    return [
        output
        for output in outputs
        if insights_match(output.insights.model_dump(), language, sentiment, topic)
    ]


def default_keyword_sources() -> list[str]:
    """Return sources that can run immediately without opening a login flow."""
    return [
        source_id
        for source_id, connector in connectors.items()
        if not connector.capabilities.requires_login
        and not isinstance(connector, UnconfiguredConnector)
        and (not isinstance(connector, YouTubeConnector) or bool(settings.youtube_api_key))
    ]


@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "time": utcnow().isoformat(), "sources": len(connectors)}


@app.get("/api/v1/ready")
async def ready():
    try:
        await asyncio.to_thread(store.client.admin.command, "ping")
    except Exception as exc:
        raise HTTPException(503, "MongoDB is not ready") from exc
    return {"status": "ready", "time": utcnow().isoformat()}


@app.get("/api/v1/sources", response_model=list[SourceOutput])
async def list_sources():
    outputs: list[SourceOutput] = []
    for connector in connectors.values():
        status = await connector.healthcheck()
        outputs.append(
            SourceOutput(
                id=connector.source_id,
                label=connector.label,
                group=connector.group,
                state=status.state,
                detail=status.detail,
                global_search=connector.capabilities.global_search,
                watchlist_filter=connector.capabilities.watchlist_filter,
                requires_login=connector.capabilities.requires_login,
                interaction_fields=list(connector.capabilities.interaction_fields),
            )
        )
    return outputs


@app.get("/api/v1/keywords", response_model=list[KeywordOutput])
def list_keywords():
    return [keyword_output(row) for row in store.keywords()]


@app.post("/api/v1/keywords", response_model=KeywordOutput, status_code=201)
def create_keyword(payload: KeywordInput):
    if keyword_name_exists(payload.name):
        raise HTTPException(409, "A keyword with that name already exists")
    source_ids = payload.source_ids or default_keyword_sources()
    invalid = set(source_ids).difference(connectors)
    if invalid:
        raise HTTPException(422, f"Unknown sources: {', '.join(sorted(invalid))}")
    now = utcnow()
    row = store.create_keyword({
        "name": payload.name.strip(),
        "normalized_name": normalized(payload.name),
        "include_terms": payload.include_terms,
        "exclude_terms": payload.exclude_terms,
        "source_ids": source_ids,
        "enabled": payload.enabled,
        "interval_minutes": payload.interval_minutes,
        "max_items_per_source": payload.max_items_per_source,
        "next_run_at": now + timedelta(minutes=payload.interval_minutes) if payload.enabled else None,
        "created_at": now,
        "updated_at": now,
    })
    return keyword_output(row)


@app.patch("/api/v1/keywords/{keyword_id}", response_model=KeywordOutput)
def update_keyword(keyword_id: int, payload: KeywordInput):
    row = store.keyword(keyword_id)
    if not row:
        raise HTTPException(404, "Keyword not found")
    if keyword_name_exists(payload.name, exclude_id=keyword_id):
        raise HTTPException(409, "A keyword with that name already exists")
    now = utcnow()
    row = store.update_keyword(keyword_id, {
        "name": payload.name.strip(),
        "normalized_name": normalized(payload.name),
        "include_terms": payload.include_terms,
        "exclude_terms": payload.exclude_terms,
        "source_ids": payload.source_ids or default_keyword_sources(),
        "enabled": payload.enabled,
        "interval_minutes": payload.interval_minutes,
        "max_items_per_source": payload.max_items_per_source,
        "next_run_at": now + timedelta(minutes=payload.interval_minutes) if payload.enabled else None,
        "updated_at": now,
    })
    run_manager.rescore_keyword(keyword_id)
    return keyword_output(row)


@app.delete("/api/v1/keywords/{keyword_id}", status_code=204)
def delete_keyword(keyword_id: int):
    if not store.delete_keyword(keyword_id):
        raise HTTPException(404, "Keyword not found")


@app.post("/api/v1/runs", response_model=BatchOutput, status_code=202)
async def create_run(payload: RunRequest):
    if not await asyncio.to_thread(store.keyword, payload.keyword_id):
        raise HTTPException(404, "Keyword not found")
    try:
        batch_id = await run_manager.start_batch(payload.keyword_id, payload.trigger, payload.source_ids)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    batch = await asyncio.to_thread(store.batch, batch_id)
    return batch_output(batch)


@app.get("/api/v1/runs", response_model=list[BatchOutput])
def list_runs(
    keyword_id: int | None = None,
    limit: int = Query(default=10, ge=1, le=100),
):
    rows = store.batches(keyword_id, limit)
    return [batch_output(row) for row in rows]


@app.get("/api/v1/runs/{batch_id}", response_model=BatchOutput)
def get_run(batch_id: str):
    batch = store.batch(batch_id)
    if not batch:
        raise HTTPException(404, "Run not found")
    return batch_output(batch)


@app.post("/api/v1/runs/{batch_id}/cancel", status_code=202)
async def cancel_run(batch_id: str):
    if not await run_manager.cancel_batch(batch_id):
        raise HTTPException(409, "Run is not active")
    return {"id": batch_id, "state": "cancelling"}


@app.get("/api/v1/items", response_model=PagedItems)
def list_items(
    keyword_id: int,
    source_id: str | None = None,
    query: str | None = None,
    language: str | None = None,
    sentiment: Literal["positive", "negative", "mixed", "neutral"] | None = None,
    topic: str | None = None,
    min_relevance: float = Query(default=0, ge=0, le=100),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    outputs = filtered_item_outputs(
        keyword_id,
        source_id,
        query,
        language,
        sentiment,
        topic,
        min_relevance,
    )
    return PagedItems(
        total=len(outputs),
        items=outputs[offset : offset + limit],
    )


@app.get("/api/v1/insights/summary", response_model=InsightSummaryOutput)
def insight_summary(
    keyword_id: int,
    source_id: str | None = None,
    query: str | None = None,
    language: str | None = None,
    sentiment: Literal["positive", "negative", "mixed", "neutral"] | None = None,
    topic: str | None = None,
    min_relevance: float = Query(default=0, ge=0, le=100),
    top_limit: int = Query(default=5, ge=1, le=20),
):
    outputs = filtered_item_outputs(
        keyword_id,
        source_id,
        query,
        language,
        sentiment,
        topic,
        min_relevance,
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
        }.items()
        if value is not None
    }
    return InsightSummaryOutput(
        keyword_id=keyword_id,
        generated_at=utcnow(),
        filters=filters,
        **summarize_items([output.model_dump(mode="json") for output in outputs], top_limit),
    )


@app.get("/api/v1/insights/clusters", response_model=TrendClustersOutput)
def insight_clusters(
    keyword_id: int,
    source_id: str | None = None,
    query: str | None = None,
    language: str | None = None,
    sentiment: Literal["positive", "negative", "mixed", "neutral"] | None = None,
    topic: str | None = None,
    min_relevance: float = Query(default=0, ge=0, le=100),
    min_items: int = Query(default=2, ge=2, le=10),
    limit: int = Query(default=20, ge=1, le=50),
):
    keyword = store.keyword(keyword_id)
    if not keyword:
        raise HTTPException(404, "Keyword not found")
    outputs = filtered_item_outputs(
        keyword_id,
        source_id,
        query,
        language,
        sentiment,
        topic,
        min_relevance,
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
            "min_items": min_items if min_items != 2 else None,
        }.items()
        if value is not None
    }
    ignore_terms = [keyword["name"], *keyword.get("include_terms", [])]
    return TrendClustersOutput(
        keyword_id=keyword_id,
        generated_at=utcnow(),
        filters=filters,
        **cluster_items(
            [output.model_dump(mode="json") for output in outputs],
            ignore_terms,
            min_items,
            limit,
        ),
    )


@app.get("/api/v1/trends", response_model=list[TrendOutput])
def trends(keyword_id: int, limit: int = Query(default=10, ge=1, le=50)):
    rows = store.item_matches(keyword_id, positive_only=True)
    rows.sort(key=lambda row: row[1].get("trend_score", 0), reverse=True)
    rows = rows[:limit]
    output: list[TrendOutput] = []
    for item, match in rows:
        snapshots = store.snapshots(item["id"])
        output.append(
            TrendOutput(
                item_id=item["id"],
                title=item.get("title", ""),
                source_id=item["source_id"],
                trend_score=match.get("trend_score", 0),
                points=[TrendPoint(captured_at=row["captured_at"], engagement=row.get("like_count", 0) + 2 * row.get("comment_count", 0) + 3 * row.get("share_count", 0) + 2 * row.get("favorite_count", 0)) for row in snapshots],
            )
        )
    return output


@app.get("/api/v1/export.csv")
def export_csv(
    keyword_id: int,
    source_id: str | None = None,
    language: str | None = None,
    sentiment: Literal["positive", "negative", "mixed", "neutral"] | None = None,
    topic: str | None = None,
):
    outputs = filtered_item_outputs(
        keyword_id,
        source_id=source_id,
        language=language,
        sentiment=sentiment,
        topic=topic,
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
            *(f"{topic['label']}: {', '.join(topic['reasons'])}" for topic in insights["topics"]),
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
    return StreamingResponse(iter([stream.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=content-bot-{keyword_id}.csv"})


@app.get("/api/v1/export.json")
def export_json(
    keyword_id: int,
    source_id: str | None = None,
    language: str | None = None,
    sentiment: Literal["positive", "negative", "mixed", "neutral"] | None = None,
    topic: str | None = None,
):
    outputs = filtered_item_outputs(
        keyword_id,
        source_id=source_id,
        language=language,
        sentiment=sentiment,
        topic=topic,
    )
    return JSONResponse(
        [
            {
                "source_id": item.source_id,
                "title": item.title,
                "author": item.author,
                "url": str(item.canonical_url),
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "metrics": item.metrics,
                "relevance_score": item.relevance_score,
                "trend_score": item.trend_score,
                "match_reasons": item.match_reasons,
                "insights": item.insights.model_dump(),
            }
            for item in outputs
        ]
    )


@app.get("/api/v1/events")
async def event_stream():
    async def stream():
        async for event in events.subscribe():
            yield f"data: {json.dumps(event, default=str)}\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/v1/runs/{batch_id}/events")
async def run_event_stream(batch_id: str):
    async def stream():
        async for event in events.subscribe():
            if event.get("type") == "connected" or event.get("batch_id") == batch_id:
                yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
