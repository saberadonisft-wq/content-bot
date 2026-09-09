from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
import tempfile
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import imageio_ffmpeg
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .api.catalog import build_catalog_router
from .api.comments import build_comments_router
from .api.crawler_data import build_crawler_data_router
from .api.crawler_profiles import build_crawler_profiles_router
from .api.credentials import router as credentials_router
from .api.health import build_health_router
from .api.live_wall import build_live_wall_router
from .api.tiktok_auth import build_tiktok_auth_router
from .api.voiceover import build_voiceover_router
from .services.voiceover.manager import VoiceManager
from .services.voiceover.store import VoiceStore
from .services.voiceover.mix import finalize_voiced_render, verify_document, verify_voice_cuts, retained_voice_document
from .services.subtitle_render import SubtitleRenderError
from .middleware.auth import get_current_user
from .config import settings
from .crawlers.adapters.tiktok import TikTokOAuthConfig, TikTokTokenVault
from .mongo import store
from .schemas import (
    BatchOutput,
    GeminiSubtitleRequest,
    InsightSummaryOutput,
    ItemOutput,
    MediaMetadata,
    PagedItems,
    RunRequest,
    SourceRunOutput,
    SubtitleAlignmentRequest,
    SubtitleAssPreviewRequestV2,
    SubtitleAssPreviewResponse,
    SubtitleBurnRequest,
    SubtitleBurnResponse,
    SubtitleDocumentV2,
    SubtitleItem,
    SubtitleJobResponse,
    SubtitleOverlayUploadResponse,
    SubtitleParseRequest,
    SubtitleParseRequestV2,
    SubtitleParseResponse,
    SubtitleParseResponseV2,
    SubtitleRenderRequestV2,
    SubtitleTransformRequestV2,
    SubtitleTransformResponseV2,
    SubtitleUploadResponse,
    TrendClustersOutput,
    TrendOutput,
    TrendPoint,
    VideoLibraryItem,
)
from .services.clusters import cluster_items
from .services.connectors import default_connectors
from .services.crawler_login import CrawlerLoginManager
from .services.credential_resolver import credential
from .services.http_pool import close_http_pools
from .services.gemini_subtitles import (
    DEFAULT_GEMINI_MODEL,
    GeminiSubtitleCanceled,
    GeminiSubtitleError,
    GeminiSubtitleService,
    GeminiSubtitleSettings,
    gemini_generation_cache_key,
)
from .services.insights import summarize_items
from .services.live_wall import LiveWallManager
from .services.media_probe import MediaProbeError, probe_media_cached
from .services.runs import EventBus, RunManager, utcnow
from .services.subtitle_alignment import (
    AlignmentSettings,
    SubtitleAlignmentCanceled,
    align_subtitle_document,
    alignment_cache_key,
)
from .services.subtitle_jobs import (
    SubtitleJobCanceled,
    SubtitleJobManager,
)
from .services.subtitle_overlay import (
    SUPPORTED_OVERLAY_EXTENSIONS,
    SubtitleOverlayError,
    overlay_directory,
    overlay_media_type,
    resolve_subtitle_overlay,
    validate_subtitle_overlay,
)
from .services.subtitle_render import (
    SubtitleRenderCanceled,
    precision_render_cache_key,
    render_precision_video,
    subtitle_play_resolution,
)
from .services.subtitle_thumbnail import ThumbnailSpriteError, generate_thumbnail_sprite
from .services.subtitle_timing import transform_project_cues, validate_cues
from .services.subtitles import (
    burn_subtitles_to_video,
    parse_subtitles_text,
    parse_subtitles_v2,
    subtitles_to_ass,
    subtitles_to_srt,
)
from .services.text import content_insights, insights_match
from .services.tiktok_oauth import TikTokOAuthService

logger = logging.getLogger(__name__)
voiceover_manager = VoiceManager(VoiceStore(settings.data_dir / "voiceover"))
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv"}
SUPPORTED_OVERLAY_CONTENT_TYPES = {
    "application/octet-stream",
    "image/jpeg",
    "image/png",
    "image/webp",
}
VIDEO_ID_PATTERN = re.compile(r"^[a-f0-9]{12,32}$")
connectors = default_connectors()
events = EventBus()
run_manager = RunManager(connectors, events)
crawler_login_manager = CrawlerLoginManager()
live_wall_manager = LiveWallManager(
    profile_root=settings.data_dir / "live-wall-profiles",
    browser_executable=settings.content_bot_coccoc_executable_path,
)
tiktok_oauth_service = TikTokOAuthService(
    TikTokTokenVault(settings.data_dir / "crawler-secrets" / "tiktok"),
    lambda: TikTokOAuthConfig(
        credential("tiktok_client_key", ""),
        credential("tiktok_client_secret", "") or "",
        credential("tiktok_redirect_uri", ""),
    ),
    settings.content_bot_frontend_url,
)
subtitle_jobs = SubtitleJobManager(
    settings.data_dir / "subtitle-jobs",
    max_workers=settings.content_bot_subtitle_job_concurrency,
)
gemini_subtitle_jobs = SubtitleJobManager(
    settings.data_dir / "gemini-subtitle-jobs",
    max_workers=1,
)
gemini_subtitle_service = GeminiSubtitleService(
    GeminiSubtitleSettings(
        job_root=Path(tempfile.gettempdir()) / "content-bot-gemini-jobs",
        model=settings.content_bot_gemini_model,
        timeout_seconds=settings.content_bot_gemini_timeout_seconds,
        chunk_seconds=settings.content_bot_gemini_chunk_seconds,
        max_input_mb=settings.content_bot_gemini_max_input_mb,
        max_retries=settings.content_bot_gemini_max_retries,
        retry_base_seconds=settings.content_bot_gemini_retry_base_seconds,
    ),
    api_key_provider=lambda: credential("gemini_api_key"),
)


async def scheduler_loop() -> None:
    retention_interval = timedelta(
        hours=max(1, settings.content_bot_crawler_retention_interval_hours)
    )
    next_retention_at = utcnow()
    while True:
        now = utcnow()
        if now >= next_retention_at:
            deleted = await asyncio.to_thread(
                run_manager.cleanup_retention,
                max(1, settings.content_bot_crawler_retention_days),
            )
            if deleted:
                logger.info("Crawler retention removed %s expired items", deleted)
            next_retention_at = now + retention_interval
        await run_manager.scheduler_tick()
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(store.initialize)
    task = None
    if store.is_available:
        await asyncio.to_thread(run_manager.cleanup_interrupted)
        cleanup_migration = store.metadata("cleanup-irrelevant-v1")
        if cleanup_migration is None:
            await asyncio.to_thread(run_manager.cleanup_irrelevant)
            store.set_metadata(
                "cleanup-irrelevant-v1",
                {"completed": True, "completed_at": utcnow()},
            )
        task = asyncio.create_task(scheduler_loop(), name="content-bot-scheduler")
    else:
        logger.warning(
            "%s storage is unavailable; scheduler is disabled",
            store.storage_name,
        )
    try:
        yield
    finally:
        await asyncio.to_thread(voiceover_manager.shutdown)
        await live_wall_manager.shutdown()
        await crawler_login_manager.shutdown()
        await close_http_pools()
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Content Bot API", version="0.1.0", lifespan=lifespan)
app.include_router(build_health_router(lambda: store, lambda: len(connectors)))
app.include_router(build_catalog_router(lambda: store, connectors, run_manager))
app.include_router(build_crawler_data_router(lambda: store))
app.include_router(build_comments_router(lambda: store, lambda: connectors))
app.include_router(build_crawler_profiles_router(crawler_login_manager))
app.include_router(build_tiktok_auth_router(tiktok_oauth_service))
app.include_router(build_live_wall_router(live_wall_manager, lambda: store))
app.include_router(credentials_router)
app.include_router(build_voiceover_router(voiceover_manager))


@app.middleware("http")
async def verify_auth_token_middleware(request, call_next):
    if (
        settings.content_bot_auth_enabled
        and request.method != "OPTIONS"
        and not any(
            request.url.path.startswith(prefix)
            for prefix in (
                "/health",
                "/api/v1/health",
                "/api/v1/ready",
                "/api/v1/version",
                "/api/v1/update",
                "/docs",
                "/openapi.json",
                "/redoc",
            )
        )
    ):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Yêu cầu đăng nhập để sử dụng tính năng này."},
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = auth_header.split(" ", 1)[1].strip()
        try:
            from .middleware.auth import verify_token

            payload = verify_token(token)
            if payload.get("status") != "approved":
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "Tài khoản của bạn đang chờ phê duyệt từ quản trị viên."
                    },
                )
            request.state.user = payload
        except HTTPException as he:
            return JSONResponse(
                status_code=he.status_code,
                content={"detail": he.detail},
            )
        except Exception as e:
            return JSONResponse(
                status_code=401,
                content={"detail": f"Xác thực thất bại: {e!s}"},
            )

    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in settings.content_bot_cors_origins.split(",")
        if origin.strip()
    ],
    # Vite may move to the next local port when 5173 is occupied. Keep the
    # local-only API usable from that preview/dev port without allowing remote
    # browser origins.
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    expose_headers=[
        "X-Sprite-Frames",
        "X-Sprite-Frame-Width",
        "X-Sprite-Frame-Height",
        "X-Cache-Hit",
    ],
)


def batch_output(batch: dict) -> BatchOutput:
    return BatchOutput(
        id=batch["id"],
        keyword_id=batch["keyword_id"],
        trigger=batch["trigger"],
        session_number=batch.get("session_number", 1),
        new_item_count=batch.get(
            "new_item_count",
            sum(row.get("ingested_count", 0) for row in batch.get("source_runs", [])),
        ),
        state=batch["state"],
        started_at=batch.get("started_at"),
        finished_at=batch.get("finished_at"),
        error_message=batch.get("error_message"),
        source_runs=[
            SourceRunOutput(
                id=row["id"],
                source_id=row["source_id"],
                channel_id=row.get("channel_id"),
                channel_url=row.get("channel_url"),
                channel_label=row.get("channel_label"),
                state=row["state"],
                phase=row.get("phase", row["state"]),
                progress_mode=row.get("progress_mode", "determinate"),
                progress_current=row.get(
                    "progress_current", row.get("fetched_count", 0)
                ),
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
        insights=content_insights(
            item.get("title", ""),
            item.get("body_snippet", ""),
            hashtags,
            item.get("locale"),
        ),
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
    session_id: str | None = None,
) -> list[ItemOutput]:
    rows = store.item_matches(
        keyword_id,
        positive_only=True,
        source_id=source_id,
        min_relevance=min_relevance,
        session_id=session_id,
    )
    rows = [
        (item, match)
        for item, match in rows
        if match.get("relevance_score", 0) >= min_relevance
    ]
    if source_id:
        rows = [(item, match) for item, match in rows if item["source_id"] == source_id]
    if query:
        needle = query.strip().casefold()
        rows = [
            (item, match)
            for item, match in rows
            if needle
            in " ".join(
                (
                    item.get("title", ""),
                    item.get("body_snippet", ""),
                    item.get("author", ""),
                )
            ).casefold()
        ]
    rows.sort(
        key=lambda row: (
            row[1].get("trend_score", 0),
            row[0].get("published_at") or row[0]["first_seen_at"],
        ),
        reverse=True,
    )
    outputs = [item_output(item, match) for item, match in rows]
    return [
        output
        for output in outputs
        if insights_match(output.insights.model_dump(), language, sentiment, topic)
    ]


def _validate_local_video_id(video_id: str) -> None:
    if not VIDEO_ID_PATTERN.fullmatch(video_id):
        raise HTTPException(status_code=422, detail="Invalid video id")


def _uploaded_video_path(video_id: str) -> Path:
    _validate_local_video_id(video_id)
    upload_dir = settings.data_dir / "videos" / "upload"
    matches = sorted(
        path
        for path in upload_dir.glob(f"{video_id}.*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
    )
    if not matches:
        raise HTTPException(status_code=404, detail="Video file not found")
    return matches[0]


def _safe_external_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return value


def _as_utc_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        with suppress(ValueError):
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return utcnow()


@app.post("/api/v1/runs", response_model=BatchOutput, status_code=202)
async def create_run(payload: RunRequest):
    if not await asyncio.to_thread(store.keyword, payload.keyword_id):
        raise HTTPException(404, "Keyword not found")
    try:
        batch_id = await run_manager.start_batch(
            payload.keyword_id,
            payload.trigger,
            payload.source_ids,
            payload.channel_ids,
        )
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
    session_id: str | None = None,
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
        session_id,
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
    session_id: str | None = None,
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
        **summarize_items(
            [output.model_dump(mode="json") for output in outputs], top_limit
        ),
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
    session_id: str | None = None,
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


@app.get("/api/v1/export.csv")
def export_csv(
    keyword_id: int,
    source_id: str | None = None,
    session_id: str | None = None,
    language: str | None = None,
    sentiment: Literal["positive", "negative", "mixed", "neutral"] | None = None,
    topic: str | None = None,
):
    outputs = filtered_item_outputs(
        keyword_id,
        source_id=source_id,
        session_id=session_id,
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


@app.get("/api/v1/export.json")
def export_json(
    keyword_id: int,
    source_id: str | None = None,
    session_id: str | None = None,
    language: str | None = None,
    sentiment: Literal["positive", "negative", "mixed", "neutral"] | None = None,
    topic: str | None = None,
):
    outputs = filtered_item_outputs(
        keyword_id,
        source_id=source_id,
        session_id=session_id,
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


@app.get("/api/v1/events")
async def event_stream():
    async def stream():
        async for event in events.subscribe():
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


@app.post("/api/v1/subtitles/upload", response_model=SubtitleUploadResponse)
async def upload_subtitle_video(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file selected")

    filename = Path(file.filename).name
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=415, detail="Only MP4, WebM, and MKV videos are supported"
        )
    allowed_content_types = {
        "application/octet-stream",
        "video/mp4",
        "video/webm",
        "video/x-matroska",
    }
    if file.content_type and file.content_type not in allowed_content_types:
        raise HTTPException(status_code=415, detail="Unsupported video content type")

    video_id = uuid.uuid4().hex
    upload_dir = settings.data_dir / "videos" / "upload"
    upload_dir.mkdir(parents=True, exist_ok=True)

    dest_path = upload_dir / f"{video_id}{ext}"
    total_bytes = 0
    try:
        with dest_path.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > settings.content_bot_max_video_size_bytes:
                    raise HTTPException(
                        status_code=413, detail="Video file is too large"
                    )
                destination.write(chunk)
        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="Video file is empty")
        media = await asyncio.to_thread(
            probe_media_cached,
            dest_path,
            settings.data_dir / "cache" / "media-probes",
            timeout_seconds=settings.content_bot_media_probe_timeout_seconds,
        )
    except MediaProbeError as exc:
        dest_path.unlink(missing_ok=True)
        logger.info("Uploaded video failed validation: %s", exc)
        raise HTTPException(
            status_code=422, detail="The uploaded file is not a valid video"
        ) from exc
    except HTTPException:
        dest_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest_path.unlink(missing_ok=True)
        logger.exception("Failed to store uploaded video")
        raise HTTPException(
            status_code=500, detail="Could not store uploaded video"
        ) from exc
    finally:
        await file.close()

    return {
        "video_id": video_id,
        "filename": filename,
        "video_url": f"/api/v1/subtitles/video/{video_id}",
        "media": media,
    }


@app.post(
    "/api/v1/subtitles/overlays",
    response_model=SubtitleOverlayUploadResponse,
)
async def upload_subtitle_overlay(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="No overlay image selected")
    filename = Path(file.filename).name
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_OVERLAY_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail="Only PNG, JPEG, and WebP overlay images are supported",
        )
    if file.content_type and file.content_type not in SUPPORTED_OVERLAY_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported overlay content type")

    destination_dir = overlay_directory(settings.data_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_dir / f".{uuid.uuid4().hex}.upload{extension}"
    digest = hashlib.sha256()
    total_bytes = 0
    try:
        with temporary_path.open("wb") as destination:
            while chunk := await file.read(256 * 1024):
                total_bytes += len(chunk)
                if total_bytes > settings.content_bot_max_overlay_size_bytes:
                    raise HTTPException(
                        status_code=413, detail="Overlay image is too large"
                    )
                digest.update(chunk)
                destination.write(chunk)
        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="Overlay image is empty")
        await asyncio.to_thread(validate_subtitle_overlay, temporary_path)
        overlay_id = digest.hexdigest()
        try:
            stored_path = resolve_subtitle_overlay(settings.data_dir, overlay_id)
            temporary_path.unlink(missing_ok=True)
        except SubtitleOverlayError:
            stored_path = destination_dir / f"{overlay_id}{extension}"
            temporary_path.replace(stored_path)
    except SubtitleOverlayError as exc:
        temporary_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        temporary_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temporary_path.unlink(missing_ok=True)
        logger.exception("Failed to store subtitle overlay")
        raise HTTPException(
            status_code=500, detail="Could not store overlay image"
        ) from exc
    finally:
        await file.close()

    return {
        "overlay_id": overlay_id,
        "filename": filename,
        "overlay_url": f"/api/v1/subtitles/overlays/{overlay_id}",
        "size_bytes": stored_path.stat().st_size,
    }


@app.get("/api/v1/subtitles/overlays/{overlay_id}")
def get_subtitle_overlay(overlay_id: str):
    try:
        path = resolve_subtitle_overlay(settings.data_dir, overlay_id)
    except SubtitleOverlayError as exc:
        status_code = 422 if str(exc) == "Invalid overlay id" else 404
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type=overlay_media_type(path),
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.get(
    "/api/v1/subtitles/video/{video_id}/metadata",
    response_model=MediaMetadata,
)
async def get_subtitle_video_metadata(video_id: str):
    input_path = _uploaded_video_path(video_id)
    try:
        return await asyncio.to_thread(
            probe_media_cached,
            input_path,
            settings.data_dir / "cache" / "media-probes",
            timeout_seconds=settings.content_bot_media_probe_timeout_seconds,
        )
    except MediaProbeError as exc:
        raise HTTPException(
            status_code=422, detail="Could not probe video metadata"
        ) from exc


@app.get("/api/v1/subtitles/video/{video_id}/thumbnail-sprite")
def get_subtitle_thumbnail_sprite(video_id: str):
    input_path = _uploaded_video_path(video_id)
    try:
        media = probe_media_cached(
            input_path,
            settings.data_dir / "cache" / "media-probes",
            timeout_seconds=settings.content_bot_media_probe_timeout_seconds,
        )
        sprite = generate_thumbnail_sprite(
            input_path,
            media,
            settings.data_dir / "cache" / "thumbnail-sprites",
            timeout_seconds=settings.content_bot_thumbnail_timeout_seconds,
        )
    except MediaProbeError as exc:
        raise HTTPException(
            status_code=422, detail="Could not probe video metadata"
        ) from exc
    except ThumbnailSpriteError as exc:
        raise HTTPException(
            status_code=500, detail="Could not create thumbnail sprite"
        ) from exc
    return FileResponse(
        sprite["path"],
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Sprite-Frames": str(sprite["frame_count"]),
            "X-Sprite-Frame-Width": str(sprite["frame_width"]),
            "X-Sprite-Frame-Height": str(sprite["frame_height"]),
            "X-Cache-Hit": "1" if sprite["cache_hit"] else "0",
        },
    )


@app.post("/api/v1/subtitles/parse", response_model=SubtitleParseResponse)
def parse_subtitle_text_endpoint(req: SubtitleParseRequest):
    sub_dicts = parse_subtitles_text(req.text)[:500]
    items = [SubtitleItem(**sub) for sub in sub_dicts]
    srt_str = subtitles_to_srt(sub_dicts)
    return SubtitleParseResponse(subtitles=items, srt=srt_str, count=len(items))


@app.post("/api/v1/subtitles/v2/parse", response_model=SubtitleParseResponseV2)
def parse_subtitle_text_v2_endpoint(req: SubtitleParseRequestV2):
    document_data, warning_data = parse_subtitles_v2(
        req.text,
        media_duration_ms=req.media_duration_ms,
    )
    document = SubtitleDocumentV2(**document_data)
    return SubtitleParseResponseV2(
        document=document,
        warnings=warning_data,
        srt=subtitles_to_srt(document_data["segments"]),
        count=len(document.segments),
    )


@app.post("/api/v1/subtitles/v2/transform", response_model=SubtitleTransformResponseV2)
def transform_subtitle_timeline_v2_endpoint(req: SubtitleTransformRequestV2):
    document_data = req.document.model_dump(mode="json")
    time_map = req.time_map
    transformed = transform_project_cues(
        document_data["segments"],
        trim_start_ms=time_map.trim_start_ms,
        trim_end_ms=time_map.trim_end_ms,
        video_speed=time_map.video_speed,
    )
    document_data["segments"] = transformed
    document = SubtitleDocumentV2(**document_data)
    return SubtitleTransformResponseV2(
        document=document,
        warnings=validate_cues(transformed),
    )


@app.post(
    "/api/v1/subtitles/v2/generate/gemini",
    response_model=SubtitleJobResponse,
)
def generate_subtitles_with_gemini_endpoint(req: GeminiSubtitleRequest):
    if not settings.content_bot_gemini_enabled:
        raise HTTPException(
            status_code=503, detail="Gemini subtitle generation is disabled"
        )
    input_path = _uploaded_video_path(req.video_id)
    try:
        media = probe_media_cached(
            input_path,
            settings.data_dir / "cache" / "media-probes",
            timeout_seconds=settings.content_bot_media_probe_timeout_seconds,
        )
    except MediaProbeError as exc:
        raise HTTPException(
            status_code=422, detail="Could not probe video metadata"
        ) from exc
    if not media.get("has_audio"):
        raise HTTPException(status_code=422, detail="Video has no audio track")

    options = req.options.model_dump(mode="json")
    selected_model = GeminiSubtitleService.normalize_model(
        req.options.model or settings.content_bot_gemini_model or DEFAULT_GEMINI_MODEL
    )
    options["model"] = selected_model
    dedupe_key = gemini_generation_cache_key(
        media,
        options,
        model=selected_model,
    )

    def run_generation_job(context):
        try:
            return gemini_subtitle_service.generate(input_path, media, options, context)
        except GeminiSubtitleCanceled as exc:
            raise SubtitleJobCanceled(str(exc)) from exc

    return gemini_subtitle_jobs.submit("generation", dedupe_key, run_generation_job)


@app.get("/api/v1/subtitles/gemini/status")
def get_gemini_api_status():
    if not settings.content_bot_gemini_enabled:
        return {"installed": False, "authenticated": False}
    return gemini_subtitle_service.status()


@app.get("/api/v1/subtitles/gemini/models")
def get_gemini_models():
    if not settings.content_bot_gemini_enabled:
        return {"models": [], "selected_model": DEFAULT_GEMINI_MODEL}
    try:
        return {
            "models": gemini_subtitle_service.list_models(),
            "selected_model": GeminiSubtitleService.normalize_model(
                settings.content_bot_gemini_model or DEFAULT_GEMINI_MODEL
            ),
        }
    except GeminiSubtitleError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get(
    "/api/v1/subtitles/gemini/jobs/{job_id}",
    response_model=SubtitleJobResponse,
)
def get_gemini_subtitle_job(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{20}", job_id):
        raise HTTPException(status_code=422, detail="Invalid subtitle job id")
    job = gemini_subtitle_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Gemini subtitle job not found")
    return job


@app.post(
    "/api/v1/subtitles/gemini/jobs/{job_id}/cancel",
    response_model=SubtitleJobResponse,
)
def cancel_gemini_subtitle_job(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{20}", job_id):
        raise HTTPException(status_code=422, detail="Invalid subtitle job id")
    job = gemini_subtitle_jobs.cancel(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Gemini subtitle job not found")
    return job


@app.post("/api/v1/subtitles/v2/align", response_model=SubtitleJobResponse)
def align_subtitle_timeline_v2_endpoint(req: SubtitleAlignmentRequest):
    input_path = _uploaded_video_path(req.video_id)
    try:
        media = probe_media_cached(
            input_path,
            settings.data_dir / "cache" / "media-probes",
            timeout_seconds=settings.content_bot_media_probe_timeout_seconds,
        )
    except MediaProbeError as exc:
        raise HTTPException(
            status_code=422, detail="Could not probe video metadata"
        ) from exc
    if not media.get("has_audio"):
        raise HTTPException(
            status_code=422,
            detail="Video has no audio track; use manual subtitle timing",
        )

    configured_engine = settings.content_bot_alignment_engine
    if configured_engine not in {"auto", "energy", "faster_whisper"}:
        configured_engine = "energy"
    requested_engine = req.options.engine
    resolved_request_engine = (
        configured_engine if requested_engine == "auto" else requested_engine
    )
    alignment_settings = AlignmentSettings(
        engine=resolved_request_engine,
        lead_in_ms=req.options.lead_in_ms,
        tail_ms=req.options.tail_ms,
        window_padding_ms=req.options.window_padding_ms,
        max_window_ms=req.options.max_window_ms,
        force_manual=req.options.force_manual,
        whisper_model=settings.content_bot_alignment_whisper_model,
        whisper_device=settings.content_bot_alignment_whisper_device,
        whisper_compute_type=settings.content_bot_alignment_whisper_compute_type,
        whisper_model_dir=settings.data_dir / "models" / "faster-whisper",
        whisper_allow_download=settings.content_bot_alignment_whisper_allow_download,
        cpu_threads=settings.content_bot_alignment_cpu_threads,
    )
    document_data = req.document.model_dump(mode="json")
    selected_ids = set(req.cue_ids) if req.cue_ids else None
    dedupe_key = alignment_cache_key(
        document_data,
        media,
        alignment_settings,
        selected_ids,
    )

    def run_alignment_job(context):
        try:
            result = align_subtitle_document(
                input_path,
                document_data,
                media,
                settings=alignment_settings,
                cue_ids=selected_ids,
                cache_dir=settings.data_dir / "cache" / "subtitle-alignment",
                cancel_event=context.cancel_event,
                progress=context.update,
                extraction_timeout_seconds=(
                    settings.content_bot_alignment_extract_timeout_seconds
                ),
            )
        except SubtitleAlignmentCanceled as exc:
            raise SubtitleJobCanceled(str(exc)) from exc
        result["document"] = SubtitleDocumentV2(**result["document"]).model_dump(
            mode="json"
        )
        return result

    return subtitle_jobs.submit("alignment", dedupe_key, run_alignment_job)


@app.post(
    "/api/v1/subtitles/v2/preview-ass",
    response_model=SubtitleAssPreviewResponse,
)
def preview_subtitle_timeline_v2_endpoint(req: SubtitleAssPreviewRequestV2):
    input_path = _uploaded_video_path(req.video_id)
    try:
        media = probe_media_cached(
            input_path,
            settings.data_dir / "cache" / "media-probes",
            timeout_seconds=settings.content_bot_media_probe_timeout_seconds,
        )
    except MediaProbeError as exc:
        raise HTTPException(
            status_code=422, detail="Could not probe video metadata"
        ) from exc

    options = req.options.model_dump(mode="json")
    cues = [segment.model_dump(mode="json") for segment in req.document.segments]
    if options.get("uppercase"):
        for cue in cues:
            cue["text"] = str(cue["text"]).upper()
            if cue.get("secondary_text"):
                cue["secondary_text"] = str(cue["secondary_text"]).upper()
    play_res_x, play_res_y = subtitle_play_resolution(media, options)
    return SubtitleAssPreviewResponse(
        ass=subtitles_to_ass(
            cues,
            options,
            play_res_x=play_res_x,
            play_res_y=play_res_y,
        ),
        play_res_x=play_res_x,
        play_res_y=play_res_y,
    )


@app.post("/api/v1/subtitles/v2/render", response_model=SubtitleJobResponse)
def render_subtitle_timeline_v2_endpoint(req: SubtitleRenderRequestV2, user=Depends(get_current_user)):
    input_path = _uploaded_video_path(req.video_id)
    try:
        media = probe_media_cached(
            input_path,
            settings.data_dir / "cache" / "media-probes",
            timeout_seconds=settings.content_bot_media_probe_timeout_seconds,
        )
    except MediaProbeError as exc:
        raise HTTPException(
            status_code=422, detail="Could not probe video metadata"
        ) from exc

    document_data = req.document.model_dump(mode="json")
    options_data = req.options.model_dump(mode="json")
    voice_document = None
    source_gain = float(options_data.get("volume", 1))
    if req.voice_project_id:
        if req.voice_project_id != req.video_id:
            raise HTTPException(422, "Giọng đọc không thuộc video này.")
        try:
            voice_document = voiceover_manager.store.get_document(str(user["sub"]), req.voice_project_id)
            if voice_document.revision != req.voice_revision or voice_document.video_fingerprint != media["fingerprint"]:
                raise ValueError("Dự án giọng vừa thay đổi. Lưu lại trước khi xuất.")
            if not voice_document.mix.enabled:
                voice_document = None
            else:
                voice_document = retained_voice_document(voice_document, options_data, media['duration_ms'])
                verify_document(voiceover_manager.store, str(user["sub"]), voice_document, media["duration_ms"])
                verify_voice_cuts(voice_document, options_data, media["duration_ms"])
                options_data["voice_document"] = voice_document.model_dump()
                options_data["voice_pipeline"] = 3
                options_data["voice_source_gain"] = source_gain
                options_data["volume"] = 1
        except (ValueError, FileNotFoundError, SubtitleRenderError) as exc:
            raise HTTPException(422, str(exc)) from exc
    overlay_data = req.overlay.model_dump(mode="json") if req.overlay else None
    masks_data = [mask.model_dump(mode="json") for mask in req.masks]
    overlay_path = None
    if req.overlay:
        try:
            overlay_path = resolve_subtitle_overlay(
                settings.data_dir,
                req.overlay.overlay_id,
            )
        except SubtitleOverlayError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    dedupe_key = precision_render_cache_key(
        document_data,
        media,
        options_data,
        overlay_data,
        masks_data,
    )
    fonts_dir = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "arimo"

    def run_render_job(context):
        def report_render_progress(percent, phase, message):
            if voice_document:
                context.update(round(percent * 0.9), "video" if phase == "complete" else phase,
                               "Đã dựng hình, chuẩn bị ghép giọng" if phase == "complete" else message)
            else:
                context.update(percent, phase, message)

        try:
            render_input = input_path
            render_media = media
            result = render_precision_video(
                render_input,
                document_data,
                render_media,
                options_data,
                settings.data_dir / "videos" / "output",
                video_id=req.video_id,
                fonts_dir=fonts_dir,
                overlay_path=overlay_path,
                overlay=overlay_data,
                masks=masks_data,
                cancel_event=context.cancel_event,
                progress=report_render_progress,
                timeout_seconds=settings.content_bot_subtitle_render_timeout_seconds,
            )
            if voice_document:
                context.update(92, "voiceover", "Đang ghép giọng đọc theo timeline đã cắt")
                result = finalize_voiced_render(voiceover_manager.store, str(user["sub"]), voice_document,
                    result, settings.data_dir / "videos" / "output", media, options_data,
                    source_gain, context.cancel_event)
                context.update(100, "complete", "Đã xuất video có giọng đọc")
            return result
        except SubtitleRenderCanceled as exc:
            raise SubtitleJobCanceled(str(exc)) from exc

    return subtitle_jobs.submit("render", dedupe_key, run_render_job)


@app.get("/api/v1/subtitles/jobs/{job_id}", response_model=SubtitleJobResponse)
def get_subtitle_job(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{20}", job_id):
        raise HTTPException(status_code=422, detail="Invalid subtitle job id")
    job = subtitle_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Subtitle job not found")
    return job


@app.post(
    "/api/v1/subtitles/jobs/{job_id}/cancel",
    response_model=SubtitleJobResponse,
)
def cancel_subtitle_job(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{20}", job_id):
        raise HTTPException(status_code=422, detail="Invalid subtitle job id")
    job = subtitle_jobs.cancel(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Subtitle job not found")
    return job


@app.get("/api/v1/subtitles/renders/{filename}")
def get_precision_subtitle_render(filename: str):
    if not re.fullmatch(
        r"subtitled_[a-f0-9]{12,32}_[a-f0-9]{12}\.mp4",
        filename,
    ):
        raise HTTPException(status_code=422, detail="Invalid subtitle render filename")
    output_path = settings.data_dir / "videos" / "output" / filename
    if not output_path.is_file():
        raise HTTPException(status_code=404, detail="Subtitle render not found")
    return FileResponse(output_path, media_type="video/mp4", filename=filename)


@app.post("/api/v1/subtitles/burn", response_model=SubtitleBurnResponse)
def burn_subtitle_video_endpoint(req: SubtitleBurnRequest):
    input_path = _uploaded_video_path(req.video_id)
    output_dir = settings.data_dir / "videos" / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    out_filename = f"subtitled_{req.video_id}.mp4"
    output_path = output_dir / out_filename

    sub_dicts = [sub.model_dump() for sub in req.subtitles]
    options_dict = req.options.model_dump()

    try:
        burn_subtitles_to_video(
            input_path, sub_dicts, options_dict, output_path, video_id=req.video_id
        )
    except Exception as err:
        output_path.unlink(missing_ok=True)
        logger.exception("Subtitle render failed for video %s", req.video_id)
        raise HTTPException(status_code=500, detail="Failed to burn subtitles") from err

    return SubtitleBurnResponse(
        video_id=req.video_id,
        output_filename=out_filename,
        video_url=f"/api/v1/subtitles/video/{req.video_id}",
        subtitled_video_url=f"/api/v1/subtitles/video/{req.video_id}?type=subtitled",
    )


@app.get("/api/v1/subtitles/video/{video_id}")
def get_subtitle_video(
    video_id: str, type: Literal["original", "subtitled"] = "original"
):
    _validate_local_video_id(video_id)
    if type == "subtitled":
        output_dir = settings.data_dir / "videos" / "output"
        target_path = output_dir / f"subtitled_{video_id}.mp4"
        if not target_path.exists():
            raise HTTPException(status_code=404, detail="Subtitled video not ready")
        return FileResponse(
            target_path, media_type="video/mp4", filename=target_path.name
        )

    target_path = _uploaded_video_path(video_id)
    media_type = "video/mp4"
    if target_path.suffix.lower() == ".webm":
        media_type = "video/webm"
    elif target_path.suffix.lower() == ".mkv":
        media_type = "video/x-matroska"

    return FileResponse(target_path, media_type=media_type, filename=target_path.name)


@app.get("/api/v1/videos", response_model=list[VideoLibraryItem])
def list_videos():
    upload_dir = settings.data_dir / "videos" / "upload"
    output_dir = settings.data_dir / "videos" / "output"

    videos: list[VideoLibraryItem] = []

    if upload_dir.exists():
        for path in upload_dir.iterdir():
            if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS:
                stat = path.stat()
                video_id = path.stem
                videos.append(
                    VideoLibraryItem(
                        id=video_id,
                        filename=path.name,
                        type="original",
                        size_bytes=stat.st_size,
                        created_at=datetime.fromtimestamp(stat.st_ctime, tz=UTC),
                        thumbnail_url=f"/api/v1/videos/{video_id}/thumbnail?type=original",
                        video_url=f"/api/v1/subtitles/video/{video_id}",
                    )
                )

    if output_dir.exists():
        for path in output_dir.iterdir():
            if (
                path.is_file()
                and path.name.startswith("subtitled_")
                and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
            ):
                stat = path.stat()
                # Extract original video_id by stripping "subtitled_" prefix
                video_id = path.stem.removeprefix("subtitled_")
                videos.append(
                    VideoLibraryItem(
                        id=video_id,
                        filename=path.name,
                        type="subtitled",
                        size_bytes=stat.st_size,
                        created_at=datetime.fromtimestamp(stat.st_ctime, tz=UTC),
                        thumbnail_url=f"/api/v1/videos/{video_id}/thumbnail?type=subtitled",
                        video_url=f"/api/v1/subtitles/video/{video_id}?type=subtitled",
                    )
                )

    # Add scraped videos from DB
    from .mongo import store

    try:
        # We only look for known video sources
        scraped = store.recent_items(
            {
                "youtube",
                "douyin",
                "dy",
                "xhs",
                "kuaishou",
                "ks",
                "bilibili",
                "bili",
            },
            limit=50,
        )

        for item in scraped:
            source = item.get("source_id")
            ext_id = item.get("external_id")

            raw_payload = item.get("raw_payload", {})
            if not isinstance(raw_payload, dict):
                raw_payload = {}
            thumb_url = ""
            if source == "youtube":
                snippet = raw_payload.get("snippet")
                thumbnails = (
                    snippet.get("thumbnails") if isinstance(snippet, dict) else {}
                )
                medium = (
                    thumbnails.get("medium") if isinstance(thumbnails, dict) else {}
                )
                thumb_url = medium.get("url", "") if isinstance(medium, dict) else ""
                if not thumb_url:
                    thumb_url = f"https://i.ytimg.com/vi/{ext_id}/hqdefault.jpg"
            elif source in {"bilibili", "bili"}:
                thumb_url = raw_payload.get("pic", "")
            elif source in {"douyin", "dy", "kuaishou", "ks", "xhs"}:
                # MediaCrawler saves cover URLs in raw_payload
                video_payload = raw_payload.get("video")
                cover_payload = (
                    video_payload.get("cover")
                    if isinstance(video_payload, dict)
                    else {}
                )
                cover_urls = (
                    cover_payload.get("url_list", [])
                    if isinstance(cover_payload, dict)
                    else []
                )
                if not isinstance(cover_urls, list):
                    cover_urls = []
                thumb_url = raw_payload.get("cover") or (
                    cover_urls[0] if cover_urls else ""
                )

            videos.append(
                VideoLibraryItem(
                    id=str(item.get("id")),
                    filename=item.get("title", f"Scraped from {source}")[:50],
                    type="scraped",
                    size_bytes=0,
                    created_at=_as_utc_datetime(
                        item.get("published_at") or item.get("first_seen_at")
                    ),
                    thumbnail_url=_safe_external_url(thumb_url),
                    video_url=_safe_external_url(item.get("canonical_url")),
                    metrics=item.get("metrics", {}),
                )
            )
    except Exception:
        # Ignore DB errors if not initialized properly
        logger.exception("Failed to list scraped videos")

    # Sort by created_at descending
    videos.sort(key=lambda x: x.created_at, reverse=True)
    return videos


@app.get("/api/v1/videos/{video_id}/thumbnail")
def get_video_thumbnail(video_id: str, type: str = "original"):
    _validate_local_video_id(video_id)
    if type not in {"original", "subtitled"}:
        raise HTTPException(status_code=422, detail="Invalid video type")
    if type == "subtitled":
        video_dir = settings.data_dir / "videos" / "output"
        video_path = video_dir / f"subtitled_{video_id}.mp4"
    else:
        video_dir = settings.data_dir / "videos" / "upload"
        # Find the video file with any supported extension
        video_path = _uploaded_video_path(video_id)

    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")

    thumbnail_dir = settings.data_dir / "videos" / "thumbnails"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_path = thumbnail_dir / f"{video_id}_{type}.jpg"

    if not thumbnail_path.exists():
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_exe,
            "-y",
            "-i",
            str(video_path.resolve()),
            "-ss",
            "00:00:01",
            "-vframes",
            "1",
            "-q:v",
            "2",
            str(thumbnail_path.resolve()),
        ]
        import subprocess

        try:
            subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )
        except Exception:
            # If thumbnail generation fails, return 404 or a fallback image
            raise HTTPException(status_code=500, detail="Failed to generate thumbnail")

    return FileResponse(
        thumbnail_path, media_type="image/jpeg", filename=thumbnail_path.name
    )


@app.delete("/api/v1/videos/{video_id}", status_code=204)
def delete_video(video_id: str, type: str = Query(...)):
    if type == "scraped":
        from .mongo import store

        try:
            scraped_id = int(video_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Invalid scraped video id"
            ) from exc
        if store.item(scraped_id) is None:
            raise HTTPException(status_code=404, detail="Video not found")
        store.delete_item(scraped_id)
        return

    if type == "subtitled":
        _validate_local_video_id(video_id)
        video_dir = settings.data_dir / "videos" / "output"
        video_path = video_dir / f"subtitled_{video_id}.mp4"
    elif type == "original":
        video_dir = settings.data_dir / "videos" / "upload"
        video_path = _uploaded_video_path(video_id)
    else:
        raise HTTPException(status_code=422, detail="Invalid video type")

    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    video_path.unlink()

    # Also attempt to delete thumbnail
    thumbnail_dir = settings.data_dir / "videos" / "thumbnails"
    thumbnail_path = thumbnail_dir / f"{video_id}_{type}.jpg"
    if thumbnail_path.exists():
        thumbnail_path.unlink()
