from __future__ import annotations

import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, SecretStr

from ..application_services import AppServices, get_services
from ..config import settings
from ..schemas import (
    VideoLibraryItem,
)
from ..services.runs import utcnow
from ..services.thumbnail_selector import select_best_thumbnail
from ..services.video_thumbnails import (
    ThumbnailBusy,
    ThumbnailError,
    ThumbnailTimeout,
)
from .media_paths import (
    SUPPORTED_VIDEO_EXTENSIONS,
    _subtitled_video_path,
    _uploaded_video_path,
    _validate_local_video_id,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/v1/videos/directory")
def video_directory():
    return {"path": str(settings.data_dir / "videos" / "upload")}


class VideoDownloadRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=20)
    quality: Literal["best", "1080", "720", "480"] = "1080"
    cookie_text: SecretStr | None = Field(default=None, max_length=1_000_000)
    connection_id: str | None = Field(default=None, max_length=128)


class VideoDownloadRetryRequest(BaseModel):
    cookie_text: SecretStr | None = Field(default=None, max_length=1_000_000)
    connection_id: str | None = Field(default=None, max_length=128)


@router.get("/api/v1/videos/downloads")
def list_downloads(services: AppServices = Depends(get_services)):
    return services.video_downloads.list()


@router.post("/api/v1/videos/downloads", status_code=202)
def create_downloads(request: VideoDownloadRequest, services: AppServices = Depends(get_services)):
    try:
        return services.video_downloads.submit(
            request.urls, request.quality,
            request.cookie_text.get_secret_value() if request.cookie_text else None,
            request.connection_id,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.post("/api/v1/videos/downloads/{job_id}/cancel")
def cancel_download(job_id: str, services: AppServices = Depends(get_services)):
    record = services.video_downloads.cancel(job_id)
    if record is None:
        raise HTTPException(404, "Không tìm thấy lượt tải.")
    return record


@router.post("/api/v1/videos/downloads/{job_id}/pause")
def pause_download(job_id: str, services: AppServices = Depends(get_services)):
    record = services.video_downloads.pause(job_id)
    if record is None:
        raise HTTPException(404, "Không tìm thấy lượt tải.")
    return record


@router.post("/api/v1/videos/downloads/{job_id}/retry", status_code=202)
def retry_download(job_id: str, request: VideoDownloadRetryRequest, services: AppServices = Depends(get_services)):
    try:
        record = services.video_downloads.retry(
            job_id, request.cookie_text.get_secret_value() if request.cookie_text else None,
            request.connection_id,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if record is None:
        raise HTTPException(404, "Không tìm thấy lượt tải.")
    return record


@router.post("/api/v1/videos/downloads/{job_id}/resume", status_code=202)
def resume_download(job_id: str, request: VideoDownloadRetryRequest, services: AppServices = Depends(get_services)):
    try:
        record = services.video_downloads.retry(
            job_id, request.cookie_text.get_secret_value() if request.cookie_text else None,
            request.connection_id,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if record is None:
        raise HTTPException(404, "Không tìm thấy lượt tải.")
    return record


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


def _metadata_text(value: object, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    result = value.get(key)
    return result if isinstance(result, str) and result else None


def _metadata_int(value: object, key: str) -> int | None:
    if not isinstance(value, dict):
        return None
    result = value.get(key)
    return result if isinstance(result, int) and not isinstance(result, bool) else None


@router.get("/api/v1/videos", response_model=list[VideoLibraryItem])
def list_videos(services: AppServices = Depends(get_services)):
    upload_dir = settings.data_dir / "videos" / "upload"
    output_dir = settings.data_dir / "videos" / "output"

    videos: list[VideoLibraryItem] = []
    downloads = services.video_downloads.metadata()

    if upload_dir.exists():
        for path in upload_dir.iterdir():
            if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS:
                stat = path.stat()
                video_id = path.stem
                metadata = downloads.get(video_id, {})
                provenance = metadata.get("provenance") if isinstance(metadata, dict) else None
                source_url = _metadata_text(provenance, "source_url") or _metadata_text(metadata, "url")
                selected_thumbnail = (
                    settings.data_dir / "videos" / "thumbnails" / f"{video_id}_selected.jpg"
                )
                thumbnail_type = "selected" if selected_thumbnail.is_file() else "original"
                videos.append(
                    VideoLibraryItem(
                        id=video_id,
                        filename=path.name,
                        type="original",
                        size_bytes=stat.st_size,
                        created_at=datetime.fromtimestamp(stat.st_ctime, tz=UTC),
                        thumbnail_url=f"/api/v1/videos/{video_id}/thumbnail?type={thumbnail_type}",
                        video_url=f"/api/v1/videos/{video_id}/file?type=original",
                        title=metadata.get("title"),
                        source_url=source_url,
                        source_id=_metadata_text(provenance, "source_id"),
                        provider_id=_metadata_text(provenance, "provider_id"),
                        external_id=_metadata_text(provenance, "external_id"),
                        media_id=_metadata_text(provenance, "media_id"),
                        part_index=_metadata_int(provenance, "part_index"),
                        creator_id=_metadata_text(provenance, "creator_id"),
                        platform=metadata.get("platform"),
                        duration=metadata.get("duration"),
                        downloaded=bool(metadata),
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
                # Preserve the render suffix so each exported version is distinct.
                video_id = path.stem.removeprefix("subtitled_")
                videos.append(
                    VideoLibraryItem(
                        id=video_id,
                        filename=path.name,
                        type="subtitled",
                        size_bytes=stat.st_size,
                        created_at=datetime.fromtimestamp(stat.st_ctime, tz=UTC),
                        thumbnail_url=f"/api/v1/videos/{video_id}/thumbnail?type=subtitled",
                        video_url=f"/api/v1/videos/{video_id}/file?type=subtitled",
                    )
                )

    # Add scraped videos from DB

    try:
        # We only look for known video sources
        scraped = services.store.recent_items(
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
                    platform=str(source or ""),
                    source_url=_safe_external_url(item.get("canonical_url")),
                )
            )
    except Exception:
        # Ignore DB errors if not initialized properly
        logger.exception("Failed to list scraped videos")

    # Sort by created_at descending
    videos.sort(key=lambda x: x.created_at, reverse=True)
    return videos


@router.get("/api/v1/videos/{video_id}/file")
def get_library_video_file(
    video_id: str,
    type: Literal["original", "subtitled"] = "original",
):
    """Serve the exact file represented by a library row.

    Library rows can contain renders written by older versions of the app,
    before publication records were introduced.  The subtitle download route
    remains source/publication-bound; this narrow, exact-path endpoint keeps
    those historical library rows viewable while preserving their filename
    identity.
    """
    target_path = (
        _subtitled_video_path(video_id)
        if type == "subtitled"
        else _uploaded_video_path(video_id)
    )
    if not target_path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found")
    media_type = "video/mp4"
    if target_path.suffix.lower() == ".webm":
        media_type = "video/webm"
    elif target_path.suffix.lower() == ".mkv":
        media_type = "video/x-matroska"
    return FileResponse(target_path, media_type=media_type, filename=target_path.name)


@router.post("/api/v1/videos/{video_id}/thumbnail/select")
def select_video_thumbnail(
    video_id: str,
    n_candidates: int = Query(default=15, ge=3, le=60),
    anti_duplicate: bool = Query(default=False),
    *,
    services: AppServices = Depends(get_services),
):
    del services
    video_path = _uploaded_video_path(video_id)
    thumbnail_dir = settings.data_dir / "videos" / "thumbnails"
    selected_path = thumbnail_dir / f"{video_id}_selected.jpg"
    try:
        result = select_best_thumbnail(
            video_path,
            selected_path,
            n_candidates=n_candidates,
            anti_duplicate=anti_duplicate,
        )
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Không thể chọn thumbnail tự động; kiểm tra runtime hình ảnh rồi thử lại.",
        ) from exc
    return {
        **result,
        "thumbnail_url": f"/api/v1/videos/{video_id}/thumbnail?type=selected",
    }


@router.get("/api/v1/videos/{video_id}/thumbnail")
def get_video_thumbnail(
    video_id: str,
    type: str = "original",
    *,
    services: AppServices = Depends(get_services),
):
    if type not in {"original", "subtitled", "selected"}:
        raise HTTPException(status_code=422, detail="Invalid video type")
    if type == "selected":
        _validate_local_video_id(video_id)
        thumbnail_path = settings.data_dir / "videos" / "thumbnails" / f"{video_id}_selected.jpg"
        if not thumbnail_path.is_file():
            raise HTTPException(status_code=404, detail="Selected thumbnail not found")
        return FileResponse(
            thumbnail_path, media_type="image/jpeg", filename=thumbnail_path.name
        )
    if type == "subtitled":
        video_path = _subtitled_video_path(video_id)
    else:
        # Find the video file with any supported extension
        video_path = _uploaded_video_path(video_id)

    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video file not found")

    thumbnail_dir = settings.data_dir / "videos" / "thumbnails"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_path = thumbnail_dir / f"{video_id}_{type}.jpg"
    try:
        services.video_thumbnails.get(video_path, thumbnail_path)
    except ThumbnailBusy as exc:
        raise HTTPException(
            status_code=503, detail=str(exc), headers={"Retry-After": "2"}
        ) from exc
    except ThumbnailTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except ThumbnailError as exc:
        raise HTTPException(
            status_code=500, detail="Failed to generate thumbnail"
        ) from exc

    return FileResponse(
        thumbnail_path, media_type="image/jpeg", filename=thumbnail_path.name
    )


@router.delete("/api/v1/videos/{video_id}", status_code=204)
def delete_video(
    video_id: str,
    type: str = Query(...),
    *,
    services: AppServices = Depends(get_services),
):
    if type == "scraped":
        try:
            scraped_id = int(video_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Invalid scraped video id"
            ) from exc
        if services.store.item(scraped_id) is None:
            raise HTTPException(status_code=404, detail="Video not found")
        services.store.delete_item(scraped_id)
        return

    if type == "subtitled":
        video_path = _subtitled_video_path(video_id)
    elif type == "original":
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
    thumbnail_path.with_suffix(".fingerprint").unlink(missing_ok=True)
    selected = thumbnail_dir / f"{video_id}_selected.jpg"
    selected.unlink(missing_ok=True)
