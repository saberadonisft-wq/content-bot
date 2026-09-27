from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import uuid
import wave
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ..application_services import AppServices, get_services
from ..config import settings
from ..middleware.auth import get_current_user
from ..schemas import (
    GeminiSubtitleRequest,
    MediaMetadata,
    SubtitleAlignmentRequest,
    SubtitleAsrRequest,
    SubtitleAssPreviewRequestV2,
    SubtitleAssPreviewResponse,
    SubtitleBurnRequest,
    SubtitleBurnResponse,
    SubtitleDocumentV2,
    SubtitleExportSourceRequest,
    SubtitleItem,
    SubtitleJobResponse,
    SubtitleOcrRequest,
    SubtitleOverlayUploadResponse,
    SubtitleParseRequest,
    SubtitleParseRequestV2,
    SubtitleParseResponse,
    SubtitleParseResponseV2,
    SubtitleRenderRequestV2,
    SubtitleSceneDetectRequest,
    SubtitleSceneExportRequest,
    SubtitleTransformRequestV2,
    SubtitleTransformResponseV2,
    SubtitleTranslateRequest,
    SubtitleUploadResponse,
    VoiceSyncApplyRequest,
    VoiceSyncAuditRequest,
)
from ..services.gemini_media import atomic_json, digest_json
from ..services.gemini_review import GeminiReviewStore, ReviewScope, run_review
from ..services.gemini_subtitles import (
    DEFAULT_GEMINI_MODEL,
    GeminiSubtitleCanceled,
    GeminiSubtitleError,
    GeminiSubtitleService,
    gemini_generation_cache_key,
)
from ..services.media_probe import MediaProbeError, probe_media_cached
from ..services.scene_splitter import (
    detect_scene_cuts,
    export_scene_chunks,
    group_scenes_into_chunks,
)
from ..services.subtitle_alignment import (
    AlignmentSettings,
    SubtitleAlignmentCanceled,
    align_subtitle_document,
    alignment_cache_key,
)
from ..services.subtitle_asr import (
    SubtitleAsrCanceled,
    SubtitleAsrError,
    asr_cache_key,
    asr_model_signature,
    asr_runtime_status,
    normalize_source_language,
)
from ..services.subtitle_asr_supervisor import (
    run_subtitle_asr_job as extract_subtitles_asr,
)
from ..services.subtitle_jobs import (
    SubtitleJobCanceled,
)
from ..services.subtitle_ocr import (
    OcrAcceleration,
    SubtitleOcrCanceled,
    SubtitleOcrError,
    ocr_cache_key,
)
from ..services.subtitle_ocr_supervisor import (
    run_subtitle_ocr_job as extract_subtitles_ocr,
)
from ..services.subtitle_ocr_supervisor import (
    subtitle_ocr_runtime_signature,
)
from ..services.subtitle_output_bindings import (
    publish_render,
    publish_shorts,
    render_video_id,
    verify_render,
    verify_short,
)
from ..services.subtitle_overlay import (
    SUPPORTED_OVERLAY_EXTENSIONS,
    SubtitleOverlayError,
    overlay_directory,
    overlay_media_type,
    resolve_subtitle_overlay,
    validate_subtitle_overlay,
)
from ..services.subtitle_render import (
    SubtitleRenderCanceled,
    SubtitleRenderError,
    precision_render_cache_key,
    render_precision_video,
    subtitle_play_resolution,
)
from ..services.subtitle_thumbnail import (
    ThumbnailSpriteError,
    generate_thumbnail_sprite,
)
from ..services.subtitle_timing import transform_project_cues, validate_cues
from ..services.subtitle_timing_review import timing_regions
from ..services.subtitle_translate import (
    TRANSLATION_VERSION,
    SubtitleTranslateCanceled,
    SubtitleTranslateError,
    translate_source_document,
)
from ..services.subtitle_versions import (
    SubtitleVersionMediaMismatch,
    SubtitleVersionStore,
    regeneration_options,
)
from ..services.subtitles import (
    burn_subtitles_to_video,
    parse_subtitles_text,
    parse_subtitles_v2,
    subtitles_to_ass,
    subtitles_to_srt,
)
from ..services.voiceover.audio import audio_metadata
from ..services.voiceover.audio_cache import AudioCache
from ..services.voiceover.mix import (
    finalize_voiced_render,
    retained_voice_document,
    verify_document,
    verify_voice_cuts,
)
from ..services.voiceover.store import read_json, write_json
from ..services.voiceover.sync_apply import apply_sync_candidates
from ..services.voiceover.sync_audit import audit_path, snapshot_binding
from ..services.voiceover.sync_worker import run_sync_audit_worker
from ..services.voiceover.timing import verify_source_bindings
from .media_paths import (
    SUPPORTED_VIDEO_EXTENSIONS,
    _subtitled_video_path,
    _uploaded_video_path,
)

SUPPORTED_OVERLAY_CONTENT_TYPES = {
    "application/octet-stream",
    "image/jpeg",
    "image/png",
    "image/webp",
}


logger = logging.getLogger(__name__)
router = APIRouter()


class GeminiReviewRequest(BaseModel):
    video_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    document: SubtitleDocumentV2
    scope: ReviewScope = Field(default_factory=ReviewScope)
    model: str | None = Field(default=None, max_length=128)


class GeminiReviewApplyRequest(BaseModel):
    document: SubtitleDocumentV2
    proposal_ids: list[str] = Field(min_length=1, max_length=20_000)
    skip: bool = False


class GeminiReviewUndoRequest(BaseModel):
    document: SubtitleDocumentV2


def _review_store():
    return GeminiReviewStore(settings.data_dir / "gemini-reviews")


class SubtitleVersionRequest(BaseModel):
    document: SubtitleDocumentV2
    name: str = Field(default="Bản đang chỉnh", min_length=1, max_length=100)
    preserve_source: bool = False


def _version_store():
    return SubtitleVersionStore(settings.data_dir / "subtitle-versions")


def _version_media(video_id: str):
    video = _uploaded_video_path(video_id)
    try:
        media = probe_media_cached(video, settings.data_dir / "cache" / "media-probes",
                                   timeout_seconds=settings.content_bot_media_probe_timeout_seconds)
    except (MediaProbeError, OSError) as exc:
        raise HTTPException(422, "Không xác minh được video nguồn; hãy kiểm tra file video.") from exc
    return video, media


def _require_document_media(document, media):
    expected = document.media_fingerprint if isinstance(document, SubtitleDocumentV2) else document.get("media_fingerprint")
    if expected and expected != media.get("fingerprint"):
        raise HTTPException(409, "Phụ đề thuộc video nguồn khác. Video có thể đã được thay; hãy chọn bản phù hợp hoặc trích xuất lại.")


def _save_media_version(store, video_id, document, video, media, **metadata):
    _require_document_media(document, media)
    _assert_media_unchanged(video, media)
    return store.save(video_id, document, media_fingerprint=media.get("fingerprint"), **metadata)


def _version_binding(record, media):
    fingerprint = record.get("media_fingerprint")
    binding = "unverified" if not fingerprint else "match" if fingerprint == media.get("fingerprint") else "mismatch"
    return {**record, "media_binding": binding}


def _job_source(video_id, media):
    return {"video_id": video_id, "media_fingerprint": media["fingerprint"]} if media and media.get("fingerprint") else {}


def _public_job(job):
    """Revalidate completed results before a restored editor can apply them.

    Keep the durable history unchanged: this is availability for the current
    source, not a rewrite of the successful run's historical outcome.
    """
    if job["state"] != "succeeded":
        return job
    binding = job.get("source_binding") or {}
    result = job.get("result") or {}
    if not binding and result.get("video_id"):
        fingerprint = result.get("source_fingerprint") or result.get("document", {}).get("media_fingerprint")
        binding = _job_source(result["video_id"], {"fingerprint": fingerprint})
    if not binding and job["kind"] == "generation":
        try:
            recipe = json.loads((settings.data_dir / "gemini-runs" / f"{job['dedupe_key']}.json").read_text(encoding="utf-8"))
            binding = _job_source(recipe["video_id"], {"fingerprint": recipe.get("media_fingerprint")})
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if not binding:
        return job  # Legacy results with no durable source identity.
    try:
        _, media = _version_media(binding["video_id"])
        changed = media.get("fingerprint") != binding["media_fingerprint"]
    except HTTPException:
        message = "Không xác minh được video nguồn của kết quả đã lưu. Kiểm tra file video rồi tải lại."
        phase = "source_unavailable"
    else:
        if not changed:
            if job["kind"] == "render" and result.get("output_filename"):
                try:
                    verify_render(settings.data_dir / "videos" / "output", result["output_filename"], media.get("fingerprint"))
                except ValueError:
                    message = "Không xác minh được file render đã lưu; hãy xuất video lại."
                    phase = "output_unavailable"
                else:
                    return job
            else:
                return job
        else:
            message = "Video nguồn đã thay đổi; kết quả job cũ không được áp dụng. Hãy chạy lại trên video hiện tại."
            phase = "source_changed"
    return {**job, "state": "failed", "phase": phase, "message": message, "error": message, "result": None,
            "details": {**job.get("details", {}), "source_validation_failed": True}}


def _submit_bound_job(manager, kind, key, runner, source_binding):
    """Submit a source-bound job while tolerating small legacy test doubles."""
    try:
        parameters = inspect.signature(manager.submit).parameters.values()
        accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)
        accepts_binding = accepts_kwargs or "source_binding" in inspect.signature(manager.submit).parameters
    except (TypeError, ValueError):
        accepts_binding = True
    if accepts_binding:
        return manager.submit(kind, key, runner, source_binding=source_binding)
    return manager.submit(kind, key, runner)


def _assert_media_unchanged(input_path: Path, expected_media: dict) -> None:
    """Reject a job result when its source video changed while it was running.

    A job captures the media fingerprint before it is queued. Re-probing at the
    commit boundary prevents a slow OCR/ASR/render worker from publishing a
    result for a replacement file that reuses the same video id.
    """
    expected = expected_media.get("fingerprint")
    if not expected:
        # Test doubles and older callers may not have a fingerprint. The
        # endpoint still validates the source before submitting the job.
        return
    try:
        current = probe_media_cached(
            input_path,
            settings.data_dir / "cache" / "media-probes",
            timeout_seconds=settings.content_bot_media_probe_timeout_seconds,
        )
    except MediaProbeError as exc:
        raise RuntimeError(
            "Không thể xác minh video nguồn sau khi xử lý; kết quả chưa được lưu."
        ) from exc
    if current.get("fingerprint") != expected:
        raise RuntimeError(
            "Video nguồn đã thay đổi trong khi job đang chạy; kết quả chưa được lưu. "
            "Hãy chạy lại trên video hiện tại."
        )


@router.get("/api/v1/subtitles/videos/{video_id}/versions")
def list_subtitle_versions(video_id: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100)):
    try:
        result = _version_store().list(video_id, offset=offset, limit=limit)
        _, media = _version_media(video_id) if any(row.get("media_fingerprint") for row in result["versions"]) else (None, {})
        result["versions"] = [_version_binding(record, media) for record in result["versions"]]
        return result
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Không đọc được phiên bản phụ đề.") from exc


@router.get("/api/v1/subtitles/videos/{video_id}/versions/{version_id}")
def get_subtitle_version(video_id: str, version_id: str):
    try:
        record = _version_store().load(video_id, version_id)
        _, media = _version_media(video_id) if record.get("media_fingerprint") else (None, {})
        if record.get("media_fingerprint") and record["media_fingerprint"] != media.get("fingerprint"):
            raise SubtitleVersionMediaMismatch("Phiên bản phụ đề thuộc video nguồn khác; hãy chọn bản phù hợp với video hiện tại.")
        return _version_binding(record, media)
    except SubtitleVersionMediaMismatch as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Không tìm thấy phiên bản phụ đề.") from exc


@router.post("/api/v1/subtitles/videos/{video_id}/versions")
def save_subtitle_version(video_id: str, req: SubtitleVersionRequest):
    video, media = _version_media(video_id)
    try:
        document = req.document.model_dump(mode="json")
        if req.preserve_source and req.document.media_fingerprint:
            # A backup before replacement keeps the old identity. It must never
            # relabel an old document as belonging to the replacement video.
            record = _version_store().save(video_id, document, name=req.name, source="snapshot")
        else:
            record = _save_media_version(_version_store(), video_id, document, video, media, name=req.name)
        return _version_binding(record, media)
    except (SubtitleVersionMediaMismatch, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/v1/subtitles/v2/review/timing-scan")
def scan_subtitle_timing(req: GeminiReviewRequest):
    """Local heuristics only; no Gemini requests or job creation."""
    video = _uploaded_video_path(req.video_id)
    try:
        media = probe_media_cached(video, settings.data_dir / "cache" / "media-probes",
                                   timeout_seconds=settings.content_bot_media_probe_timeout_seconds)
        _require_document_media(req.document, media)
        return {"regions": timing_regions(req.document.model_dump(mode="json"), int(media["duration_ms"]))}
    except (ValueError, MediaProbeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/v1/subtitles/v2/review/gemini")
def create_gemini_review(req: GeminiReviewRequest, services: AppServices = Depends(get_services)):
    if not settings.content_bot_gemini_enabled:
        raise HTTPException(503, "Gemini đã bị tắt.")
    video = _uploaded_video_path(req.video_id)
    try:
        media = probe_media_cached(video, settings.data_dir / "cache" / "media-probes",
                                   timeout_seconds=settings.content_bot_media_probe_timeout_seconds)
        model = services.gemini_subtitle_service.resolve_model(req.model)
        _require_document_media(req.document, media)
        store = _review_store()
        record = store.create(req.document.model_dump(mode="json"), req.scope, media, model, req.video_id)
    except (ValueError, MediaProbeError, GeminiSubtitleError) as exc:
        raise HTTPException(422, str(exc)) from exc
    def run_review_job(context):
        result = run_review(
            services.gemini_subtitle_service, store, record["id"], video, context
        )
        _assert_media_unchanged(video, media)
        return result

    job = services.gemini_subtitle_jobs.submit(
        "review", digest_json({"review_id": record["id"]}), run_review_job, source_binding=_job_source(req.video_id, media)
    )
    return {"review": record, "job": job}


@router.get("/api/v1/subtitles/gemini/reviews/{review_id}")
def get_gemini_review(review_id: str):
    try:
        return _review_store().public(_review_store().load(review_id))
    except (ValueError, OSError) as exc:
        raise HTTPException(404, "Không tìm thấy review.") from exc


@router.post("/api/v1/subtitles/gemini/reviews/{review_id}/resume")
def resume_gemini_review(review_id: str, services: AppServices = Depends(get_services)):
    if not settings.content_bot_gemini_enabled:
        raise HTTPException(503, "Gemini đã bị tắt.")
    try:
        store = _review_store()
        record = store.load(review_id)
        video = _uploaded_video_path(record["video_id"])
        media = probe_media_cached(video, settings.data_dir / "cache" / "media-probes")
        if media.get("fingerprint") != record["media"].get("fingerprint"):
            raise ValueError("Media đã thay đổi; hãy tạo lượt review mới.")
    except (ValueError, OSError, MediaProbeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    def run_review_job(context):
        result = run_review(
            services.gemini_subtitle_service, store, review_id, video, context
        )
        _assert_media_unchanged(video, media)
        return result

    return services.gemini_subtitle_jobs.submit(
        "review", digest_json({"review_id": review_id}), run_review_job, source_binding=_job_source(record["video_id"], media)
    )


@router.post("/api/v1/subtitles/gemini/reviews/{review_id}/apply")
def apply_gemini_review(review_id: str, req: GeminiReviewApplyRequest):
    try:
        record = _review_store().load(review_id)
        _, media = _version_media(record["video_id"])
        _require_document_media(req.document, media)
        if record["media"].get("fingerprint") != media.get("fingerprint"):
            raise ValueError("Video nguồn đã thay đổi; hãy tạo lượt review mới.")
        return _review_store().apply(review_id, req.document.model_dump(mode="json"), req.proposal_ids, skip=req.skip)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/v1/subtitles/gemini/reviews/{review_id}/undo")
def undo_gemini_review(review_id: str, req: GeminiReviewUndoRequest):
    try:
        record = _review_store().load(review_id)
        _, media = _version_media(record["video_id"])
        _require_document_media(req.document, media)
        if record["media"].get("fingerprint") != media.get("fingerprint"):
            raise ValueError("Video nguồn đã thay đổi; không áp lịch sử review cũ.")
        return _review_store().undo(review_id, req.document.model_dump(mode="json"))
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/v1/subtitles/upload", response_model=SubtitleUploadResponse)
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


@router.post(
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


@router.get("/api/v1/subtitles/overlays/{overlay_id}")
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


@router.get(
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


@router.post(
    "/api/v1/subtitles/v2/scene-detect",
    response_model=SubtitleJobResponse,
)
def detect_subtitle_scenes(
    req: SubtitleSceneDetectRequest,
    *,
    services: AppServices = Depends(get_services),
):
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

    parameters = req.model_dump(mode="json")
    dedupe_key = digest_json({
        "video_id": req.video_id,
        "fingerprint": media["fingerprint"],
        "parameters": parameters,
    })

    def run_scene_detection(context):
        context.raise_if_canceled()

        def report(progress: int, phase: str, message: str):
            context.update(progress, phase, message)

        cuts = detect_scene_cuts(
            input_path,
            threshold=req.threshold,
            min_scene_len_s=req.min_scene_len_s,
            duration_s=media["duration_ms"] / 1000,
            check_cancel=context.raise_if_canceled,
            progress=report,
        )
        chunks = group_scenes_into_chunks(
            cuts,
            target_duration_s=req.target_duration_s,
            min_duration_s=req.min_duration_s,
            max_duration_s=req.max_duration_s,
        )
        _assert_media_unchanged(input_path, media)
        context.update(96, "result", "Đang chuẩn bị các đoạn theo cảnh")
        result_chunks = [
            {
                "id": f"scene-{index:03d}",
                "start_s": start,
                "end_s": end,
                "start_ms": round(start * 1000),
                "end_ms": round(end * 1000),
                "duration_s": round(end - start, 2),
            }
            for index, (start, end) in enumerate(chunks, start=1)
        ]
        context.update(100, "complete", f"Đã tìm thấy {len(result_chunks)} đoạn theo cảnh")
        return {
            "video_id": req.video_id,
            "source_fingerprint": media["fingerprint"],
            "duration_s": round(media["duration_ms"] / 1000, 3),
            "scene_cuts_s": cuts,
            "chunks": result_chunks,
            "parameters": parameters,
        }

    return services.subtitle_jobs.submit("scene", dedupe_key, run_scene_detection, source_binding=_job_source(req.video_id, media))


@router.post(
    "/api/v1/subtitles/v2/scene-export",
    response_model=SubtitleJobResponse,
)
def export_subtitle_scenes(
    req: SubtitleSceneExportRequest,
    *,
    services: AppServices = Depends(get_services),
):
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
    if media["fingerprint"] != req.source_fingerprint:
        raise HTTPException(
            status_code=409,
            detail="Video đã thay đổi; hãy dò cảnh lại trước khi xuất.",
        )
    duration_ms = int(media["duration_ms"])
    if any(chunk.end_ms > duration_ms for chunk in req.chunks):
        raise HTTPException(status_code=422, detail="Chunk vượt quá thời lượng video")

    if req.document:
        _require_document_media(req.document, media)
    parameters = req.model_dump(mode="json")
    dedupe_key = digest_json({
        "video_id": req.video_id,
        "fingerprint": media["fingerprint"],
        "parameters": parameters,
    })

    def run_scene_export(context):
        context.raise_if_canceled()
        output_dir = settings.data_dir / "videos" / "shorts" / req.video_id
        chunks = [(chunk.start_ms / 1000, chunk.end_ms / 1000) for chunk in req.chunks]

        def report(progress: int, phase: str, message: str):
            context.update(progress, phase, message)

        files = export_scene_chunks(
            input_path,
            output_dir,
            chunks,
            subtitles_doc=req.document.model_dump(mode="json") if req.document else None,
            source_fingerprint=media["fingerprint"],
            check_cancel=context.raise_if_canceled,
            progress=report,
        )
        _assert_media_unchanged(input_path, media)
        context.update(96, "manifest", "Đang hoàn tất manifest Shorts")
        public_files = []
        for item in files:
            row = {
                "chunk_index": item["chunk_index"],
                "start_ms": round(item["start_s"] * 1000),
                "end_ms": round(item["end_s"] * 1000),
                "video_url": f"/api/v1/subtitles/video/{req.video_id}/shorts/{item['video_file']}",
            }
            if "srt_file" in item:
                row["srt_url"] = f"/api/v1/subtitles/video/{req.video_id}/shorts/{item['srt_file']}"
                row["json_url"] = f"/api/v1/subtitles/video/{req.video_id}/shorts/{item['subtitles_file']}"
            public_files.append(row)
        manifest_file = Path(files[0]["manifest_path"]).name
        published = [manifest_file]
        for item in files:
            published.append(item["video_file"])
            published.extend([item[key] for key in ("srt_file", "subtitles_file") if key in item])
        publish_shorts(output_dir, req.video_id, manifest_file, published, media["fingerprint"])
        context.update(100, "complete", f"Đã xuất {len(public_files)} Shorts")
        return {
            "video_id": req.video_id,
            "source_fingerprint": media["fingerprint"],
            "manifest_url": f"/api/v1/subtitles/video/{req.video_id}/shorts/{manifest_file}",
            "files": public_files,
        }

    return services.subtitle_jobs.submit("scene", dedupe_key, run_scene_export, source_binding=_job_source(req.video_id, media))


@router.get("/api/v1/subtitles/video/{video_id}/shorts/{file_name}")
def get_subtitle_short_file(video_id: str, file_name: str):
    _uploaded_video_path(video_id)
    if Path(file_name).name != file_name or not re.fullmatch(
        r"[a-f0-9]{12,32}(?:_short_\d{2})?(?:_manifest)?\.(?:mp4|json|srt)",
        file_name,
    ):
        raise HTTPException(status_code=422, detail="Invalid Shorts file name")
    path = settings.data_dir / "videos" / "shorts" / video_id / file_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Shorts file not found")
    _, media = _version_media(video_id)
    try:
        verify_short(path.parent, video_id, file_name, media.get("fingerprint"))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    media_type = (
        "video/mp4" if path.suffix == ".mp4" else
        "application/json" if path.suffix == ".json" else "application/x-subrip"
    )
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get("/api/v1/subtitles/video/{video_id}/thumbnail-sprite")
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


@router.post("/api/v1/subtitles/parse", response_model=SubtitleParseResponse)
def parse_subtitle_text_endpoint(req: SubtitleParseRequest):
    sub_dicts = parse_subtitles_text(req.text)[:500]
    items = [SubtitleItem(**sub) for sub in sub_dicts]
    srt_str = subtitles_to_srt(sub_dicts)
    return SubtitleParseResponse(subtitles=items, srt=srt_str, count=len(items))


@router.post("/api/v1/subtitles/v2/parse", response_model=SubtitleParseResponseV2)
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


@router.post(
    "/api/v1/subtitles/v2/transform", response_model=SubtitleTransformResponseV2
)
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


@router.post(
    "/api/v1/subtitles/v2/generate/gemini",
    response_model=SubtitleJobResponse,
)
def generate_subtitles_with_gemini_endpoint(
    req: GeminiSubtitleRequest, *, services: AppServices = Depends(get_services)
):
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

    options = req.options.model_dump(mode="json")
    selected_model = GeminiSubtitleService.normalize_model(
        req.options.model or settings.content_bot_gemini_model or DEFAULT_GEMINI_MODEL
    )
    options["model"] = selected_model
    options["pipeline_policy"] = services.gemini_subtitle_service.cache_policy()
    if req.current_document is not None:
        if req.current_document.media_fingerprint:
            _version_store().save(req.video_id, req.current_document.model_dump(mode="json"), name="Trước khi tạo lại", source="snapshot")
        else:
            _save_media_version(_version_store(), req.video_id, req.current_document.model_dump(mode="json"), input_path, media,
                                name="Trước khi tạo lại", source="snapshot")
    if req.regenerate:
        options = regeneration_options(options)
    return _submit_generation(req.video_id, input_path, media, options, services)


def _submit_generation(video_id, input_path, media, options, services):
    selected_model = options["model"]
    content_key = gemini_generation_cache_key(
        media,
        options,
        model=selected_model,
    )
    dedupe_key = digest_json({"video_id": video_id, "binding": 1, "generation": content_key})
    atomic_json(settings.data_dir / "gemini-runs" / f"{dedupe_key}.json",
                {"version": 2, "video_id": video_id, "media_fingerprint": media.get("fingerprint"), "options": options})

    return services.gemini_subtitle_jobs.submit(
        "generation", dedupe_key, _generation_runner(video_id, input_path, media, options, services), source_binding=_job_source(video_id, media)
    )


def _generation_runner(video_id, input_path, media, options, services):
    selected_model = options["model"]

    def run_generation_job(context):
        try:
            result = services.gemini_subtitle_service.generate(
                input_path, media, options, context
            )
            context.raise_if_canceled()
            _assert_media_unchanged(input_path, media)
            version = _save_media_version(_version_store(), video_id, result["document"], input_path, media,
                                         name="Gemini · " + selected_model, source="generation", model=selected_model)
            result["document"] = version["document"]
            result["version_id"] = version["id"]
            return result
        except GeminiSubtitleCanceled as exc:
            raise SubtitleJobCanceled(str(exc)) from exc

    return run_generation_job


def _saved_generation(job, services):
    record = json.loads((settings.data_dir / "gemini-runs" / f"{job['dedupe_key']}.json").read_text(encoding="utf-8"))
    if record.get("version") not in (1, 2):
        raise ValueError("Không hỗ trợ phiên bản tác vụ này.")
    video = _uploaded_video_path(record["video_id"])
    media = probe_media_cached(video, settings.data_dir / "cache" / "media-probes",
                               timeout_seconds=settings.content_bot_media_probe_timeout_seconds)
    if media.get("fingerprint") != record["media_fingerprint"]:
        raise ValueError("Media đã thay đổi; hãy tạo tác vụ mới.")
    if record["options"].get("pipeline_policy") != services.gemini_subtitle_service.cache_policy():
        raise ValueError("Thiết lập pipeline đã thay đổi; hãy tạo tác vụ mới.")
    expected_key = gemini_generation_cache_key(media, record["options"], model=record["options"]["model"])
    if record["version"] == 2:
        expected_key = digest_json({"video_id": record["video_id"], "binding": 1, "generation": expected_key})
    if expected_key != job["dedupe_key"]:
        raise ValueError("Thiết lập tác vụ đã thay đổi; hãy tạo tác vụ mới.")
    return record, video, media


def recover_gemini_generation_jobs(services):
    """Called by application startup, independently of any open editor or browser."""
    if not settings.content_bot_gemini_enabled:
        return

    def restore(job):
        def run(context):
            context.raise_if_canceled()
            try:
                record, video, media = _saved_generation(job, services)
            except HTTPException as exc:
                raise ValueError("Không thể tiếp tục tác vụ đã lưu. " + str(exc.detail)) from exc
            return _generation_runner(record["video_id"], video, media, record["options"], services)(context)
        return run

    services.gemini_subtitle_jobs.recover_interrupted(restore)


@router.post("/api/v1/subtitles/gemini/jobs/{job_id}/resume", response_model=SubtitleJobResponse)
def resume_gemini_generation(job_id: str, services: AppServices = Depends(get_services)):
    if not settings.content_bot_gemini_enabled:
        raise HTTPException(503, "Gemini đã bị tắt.")
    job = services.gemini_subtitle_jobs.get(job_id)
    if not job or job["kind"] != "generation":
        raise HTTPException(404, "Không tìm thấy tác vụ tạo phụ đề.")
    if job["state"] in {"queued", "running", "succeeded"}:
        return _public_job(job)
    try:
        record, video, media = _saved_generation(job, services)
    except (ValueError, OSError, KeyError, MediaProbeError) as exc:
        raise HTTPException(409, "Không thể tiếp tục tác vụ đã lưu. " + str(exc)) from exc
    return _submit_generation(record["video_id"], video, media, record["options"], services)


@router.get("/api/v1/subtitles/gemini/status")
def get_gemini_api_status(services: AppServices = Depends(get_services)):
    if not settings.content_bot_gemini_enabled:
        return {"installed": False, "authenticated": False}
    return services.gemini_subtitle_service.status()


@router.get("/api/v1/subtitles/gemini/models")
def get_gemini_models(services: AppServices = Depends(get_services)):
    if not settings.content_bot_gemini_enabled:
        return {"models": [], "selected_model": DEFAULT_GEMINI_MODEL}
    try:
        return {
            "models": services.gemini_subtitle_service.list_models(),
            "selected_model": GeminiSubtitleService.normalize_model(
                settings.content_bot_gemini_model or DEFAULT_GEMINI_MODEL
            ),
        }
    except GeminiSubtitleError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/api/v1/subtitles/gemini/jobs/{job_id}",
    response_model=SubtitleJobResponse,
)
def get_gemini_subtitle_job(
    job_id: str, *, services: AppServices = Depends(get_services)
):
    if not re.fullmatch(r"[a-f0-9]{20}", job_id):
        raise HTTPException(status_code=422, detail="Invalid subtitle job id")
    job = services.gemini_subtitle_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Gemini subtitle job not found")
    return _public_job(job)


@router.post(
    "/api/v1/subtitles/gemini/jobs/{job_id}/cancel",
    response_model=SubtitleJobResponse,
)
def cancel_gemini_subtitle_job(
    job_id: str, *, services: AppServices = Depends(get_services)
):
    if not re.fullmatch(r"[a-f0-9]{20}", job_id):
        raise HTTPException(status_code=422, detail="Invalid subtitle job id")
    job = services.gemini_subtitle_jobs.cancel(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Gemini subtitle job not found")
    return _public_job(job)


@router.post("/api/v1/subtitles/v2/voice-sync")
def start_voice_sync_audit(req: VoiceSyncAuditRequest, user=Depends(get_current_user),
                          *, services: AppServices = Depends(get_services)):
    owner = str(user['sub'])
    store = services.voiceover_manager.store
    try:
        voice = store.get_document(owner, req.video_id)
        video = _uploaded_video_path(req.video_id)
        media = probe_media_cached(video, settings.data_dir / 'cache' / 'media-probes')
        if voice.revision != req.voice_revision or voice.video_fingerprint != media['fingerprint']:
            raise ValueError('Dự án hoặc video vừa thay đổi. Lưu lại trước khi kiểm tra.')
        if len(set(req.clip_ids)) != len(req.clip_ids) or not set(req.clip_ids) <= {c.id for c in voice.clips}:
            raise ValueError('Danh sách đoạn giọng không hợp lệ.')
    except FileNotFoundError as exc:
        raise HTTPException(404, 'Không tìm thấy dự án giọng.') from exc
    except (ValueError, MediaProbeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    _require_document_media(req.document, media)
    document = req.document.model_dump(mode='json')
    identifier = uuid.uuid4().hex

    def run(context):
        result = run_sync_audit_worker(
            store, owner, identifier, video, media, voice, document, req.clip_ids,
            align_source=req.align_source, model_dir=settings.data_dir / 'models' / 'faster-whisper',
            whisper_model=settings.content_bot_alignment_whisper_model,
            cancel=context.cancel_event, progress=context.update,
        )
        _assert_media_unchanged(video, media)
        return result

    # Unique audit id prevents a canceled/restarted request from sharing an old record.
    job = services.subtitle_jobs.submit('alignment', digest_json({
        'voice_sync': identifier, 'owner': owner, 'input': snapshot_binding(voice, document)}), run,
        source_binding=_job_source(req.video_id, media))
    write_json(audit_path(store, owner, identifier).with_suffix('.job.json'),
               {'job_id': job['id'], 'project_id': req.video_id})
    return {'audit_id': identifier, 'job': job}


@router.get("/api/v1/subtitles/v2/voice-sync/{identifier}")
def get_voice_sync_audit(identifier: str, user=Depends(get_current_user),
                        *, services: AppServices = Depends(get_services)):
    store = services.voiceover_manager.store
    try:
        path = audit_path(store, str(user['sub']), identifier)
        registry = read_json(path.with_suffix('.job.json'))
        record = read_json(path) if path.exists() else None
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, 'Không tìm thấy lượt kiểm tra đồng bộ.') from exc
    try:
        _, media = _version_media(record['project_id']) if record else (None, None)
        if record and record.get('source_fingerprint') != media.get('fingerprint'):
            raise ValueError('Video nguồn đã thay đổi; kết quả kiểm tra cũ không thể sử dụng.')
    except (ValueError, HTTPException) as exc:
        raise HTTPException(409, str(exc)) from exc
    job = services.subtitle_jobs.get(registry['job_id']) if registry.get('job_id') else None
    if job:
        job = _public_job(job)
    if registry.get('voice_job_id'):
        try:
            voice_job = services.voiceover_manager.get(str(user['sub']), registry['voice_job_id'])
            job = {'state': voice_job['state'], 'progress': voice_job.get('sync_progress', 100),
                   'message': voice_job['message'], 'error': voice_job.get('sync_result', {}).get('message')}
        except FileNotFoundError:
            pass
    public = {k: v for k, v in record.items() if k != 'source_document'} if record else None
    return {'audit_id': identifier, 'job': job, 'audit': public}


@router.post("/api/v1/subtitles/v2/voice-sync/{identifier}/cancel")
def cancel_voice_sync_audit(identifier: str, user=Depends(get_current_user),
                           *, services: AppServices = Depends(get_services)):
    try:
        path = audit_path(services.voiceover_manager.store, str(user['sub']), identifier)
        registry = read_json(path.with_suffix('.job.json'))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, 'Không tìm thấy lượt kiểm tra đồng bộ.') from exc
    if registry.get('voice_job_id'):
        return services.voiceover_manager.control(str(user['sub']), registry['voice_job_id'], 'cancel')
    return services.subtitle_jobs.cancel(registry['job_id'])


@router.get("/api/v1/subtitles/v2/voice-sync/{identifier}/clips/{clip_id}/audio")
def get_voice_sync_candidate_audio(identifier: str, clip_id: str, user=Depends(get_current_user),
                                   *, services: AppServices = Depends(get_services)):
    store = services.voiceover_manager.store
    owner = str(user['sub'])
    lease = None
    try:
        record = read_json(audit_path(store, owner, identifier))
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(404, 'Không tìm thấy audio kiểm tra hợp lệ.') from exc
    try:
        # Audits created before source binding was introduced have no project
        # identity.  Keep their owner-row/checksum protections working; new
        # audits carry both fields and are checked against the current media.
        if record.get('project_id') and record.get('source_fingerprint'):
            _, media = _version_media(record['project_id'])
            if record['source_fingerprint'] != media.get('fingerprint'):
                raise HTTPException(409, 'Video nguồn đã thay đổi; audio kiểm tra cũ không còn hợp lệ.')
    except HTTPException:
        raise
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, 'Không xác minh được video nguồn của audio kiểm tra.') from exc
    try:
        row = next(row for row in record['rows'] if row['clip_id'] == clip_id and row.get('completed'))
        prepared = row['processed']['audio']
        key = prepared['id']
        if not re.fullmatch(r'[a-f0-9]{64}', key):
            raise ValueError('ID audio không hợp lệ.')
        path = store.owner_root(owner) / 'sync-candidates' / 'audio' / f'{key}.wav'
        lease = AudioCache(path.parent).pin(key)
        if audio_metadata(path)['checksum'] != prepared['checksum']:
            raise ValueError('Audio đã thay đổi; cần kiểm tra lại.')
    except (OSError, ValueError, KeyError, StopIteration, EOFError, wave.Error) as exc:
        if lease:
            lease.release()
        raise HTTPException(404, 'Không tìm thấy audio kiểm tra hợp lệ.') from exc
    return FileResponse(path, media_type='audio/wav', headers={'Cache-Control': 'private, no-store'},
                        background=BackgroundTask(lease.release))


@router.post('/api/v1/subtitles/v2/voice-sync/{identifier}/apply')
def apply_voice_sync_candidates(identifier: str, req: VoiceSyncApplyRequest, user=Depends(get_current_user),
                                *, services: AppServices = Depends(get_services)):
    try:
        # Resolve the owner-scoped record before touching the video or project.
        store = services.voiceover_manager.store
        audit = read_json(audit_path(store, str(user['sub']), identifier))
        media = probe_media_cached(_uploaded_video_path(req.video_id), settings.data_dir / 'cache' / 'media-probes')
        if audit.get('project_id') != req.video_id or audit.get('source_fingerprint') != media.get('fingerprint'):
            raise ValueError('Video nguồn đã thay đổi; hãy chạy kiểm tra đồng bộ mới.')
        _require_document_media(req.document, media)
        return apply_sync_candidates(store, str(user['sub']), identifier, project_id=req.video_id,
            revision=req.voice_revision, subtitles=req.document.model_dump(mode='json'),
            clip_ids=req.clip_ids, media=media)
    except FileNotFoundError as exc:
        raise HTTPException(404, 'Không tìm thấy lượt kiểm tra hoặc audio cần áp dụng.') from exc
    except (ValueError, KeyError, EOFError, wave.Error, MediaProbeError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/v1/subtitles/v2/align", response_model=SubtitleJobResponse)
def align_subtitle_timeline_v2_endpoint(
    req: SubtitleAlignmentRequest, *, services: AppServices = Depends(get_services)
):
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
        preserve_display=req.options.preserve_display,
        source_language=req.options.source_language,
        max_shift_ms=req.options.max_shift_ms,
        whisper_model=settings.content_bot_alignment_whisper_model,
        whisper_device=settings.content_bot_alignment_whisper_device,
        whisper_compute_type=settings.content_bot_alignment_whisper_compute_type,
        whisper_model_dir=settings.data_dir / "models" / "faster-whisper",
        whisper_allow_download=settings.content_bot_alignment_whisper_allow_download,
        cpu_threads=settings.content_bot_alignment_cpu_threads,
    )
    document_data = req.document.model_dump(mode="json")
    selected_ids = set(req.cue_ids) if req.cue_ids is not None else None
    versions = SubtitleVersionStore(settings.data_dir / "subtitle-versions")
    _save_media_version(versions, req.video_id, document_data, input_path, media, name="Trước khi căn thời gian", source="snapshot")
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
        _assert_media_unchanged(input_path, media)
        result["document"] = SubtitleDocumentV2(**result["document"]).model_dump(
            mode="json"
        )
        context.raise_if_canceled()
        version = _save_media_version(versions, req.video_id, result["document"], input_path, media, name="Căn thời gian", source="alignment")
        result["document"] = version["document"]
        result["version_id"] = version["id"]
        return result

    return services.subtitle_jobs.submit("alignment", digest_json({"video_id": req.video_id, "binding": 1, "alignment": dedupe_key}), run_alignment_job, source_binding=_job_source(req.video_id, media))


@router.post("/api/v1/subtitles/v2/extract/ocr", response_model=SubtitleJobResponse)
def extract_subtitles_ocr_endpoint(
    req: SubtitleOcrRequest, *, services: AppServices = Depends(get_services)
):
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

    # Freeze effective options once for dedupe, CPU retry and the GPU worker.
    acceleration = OcrAcceleration(
        selective_refinement=settings.content_bot_subtitle_ocr_selective_refinement,
        recognition_reuse=settings.content_bot_subtitle_ocr_recognition_reuse,
        refinement_batch_size=settings.content_bot_subtitle_ocr_refinement_batch_size,
        verify_interval_ms=settings.content_bot_subtitle_ocr_verify_interval_ms,
        min_reuse_confidence=settings.content_bot_subtitle_ocr_min_reuse_confidence,
        decode_threads=settings.content_bot_subtitle_ocr_decode_threads,
        crop_before_bgr=settings.content_bot_subtitle_ocr_crop_before_bgr,
    )
    dedupe_key = ocr_cache_key(
        media,
        req.region,
        source_language=req.source_language,
        sample_fps=req.sample_fps,
        min_duration_ms=req.min_duration_ms,
        max_gap_ms=req.max_gap_ms, auto_probe=req.auto_probe,
        prefetch_frames=settings.content_bot_subtitle_ocr_prefetch_frames,
        glyph_cache=settings.content_bot_subtitle_ocr_glyph_cache,
        acceleration=acceleration,
    )
    versions = SubtitleVersionStore(settings.data_dir / "subtitle-versions")

    def run_ocr_job(context):
        try:
            result = extract_subtitles_ocr(
                input_path,
                req.region,
                media,
                source_language=req.source_language,
                sample_fps=req.sample_fps,
                min_duration_ms=req.min_duration_ms,
                max_gap_ms=req.max_gap_ms,
                auto_probe=req.auto_probe,
                device_policy=settings.content_bot_subtitle_ocr_device_policy,
                prefetch_frames=settings.content_bot_subtitle_ocr_prefetch_frames,
                glyph_cache=settings.content_bot_subtitle_ocr_glyph_cache,
                acceleration=acceleration,
                gpu_site_packages=settings.subtitle_ocr_gpu_site_packages,
                worker_timeout_seconds=settings.content_bot_subtitle_ocr_worker_timeout_seconds,
                cache_dir=settings.data_dir / "cache" / "subtitle-ocr",
                context=context,
            )
        except SubtitleOcrCanceled as exc:
            raise SubtitleJobCanceled(str(exc)) from exc
        except SubtitleOcrError as exc:
            raise RuntimeError(str(exc)) from exc
        _assert_media_unchanged(input_path, media)
        context.raise_if_canceled()
        version = _save_media_version(
            versions, req.video_id, result["document"], input_path, media, name="Trích xuất OCR", source="ocr"
        )
        result["document"] = version["document"]
        result["version_id"] = version["id"]
        return result

    return services.subtitle_jobs.submit(
        "ocr",
        digest_json(
            {
                "video_id": req.video_id,
                "binding": 1,
                "ocr": dedupe_key,
                "device_policy": settings.content_bot_subtitle_ocr_device_policy,
                "runtime": subtitle_ocr_runtime_signature(
                    settings.content_bot_subtitle_ocr_device_policy,
                    settings.subtitle_ocr_gpu_site_packages,
                ),
            }
        ),
        run_ocr_job,
        source_binding=_job_source(req.video_id, media),
    )


@router.get("/api/v1/subtitles/v2/extract/asr/status")
def subtitle_asr_runtime_status():
    return asr_runtime_status(settings.data_dir / 'models' / 'faster-whisper',
        settings.content_bot_alignment_whisper_allow_download)


@router.post("/api/v1/subtitles/v2/extract/asr", response_model=SubtitleJobResponse)
def extract_subtitles_asr_endpoint(
    req: SubtitleAsrRequest, *, services: AppServices = Depends(get_services)
):
    try:
        req.source_language = normalize_source_language(req.source_language)
    except SubtitleAsrError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc
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
            detail="Video không có âm thanh; vui lòng chọn Đọc phụ đề trên hình (OCR) hoặc Phân tích bằng Gemini.",
        )

    dedupe_key = asr_cache_key(
        {**media,'asr_model_signature':asr_model_signature(req.model,settings.data_dir / 'models' / 'faster-whisper')},
        source_language=req.source_language,
        model_name=req.model,
        device=req.device,
        compute_type=req.compute_type,
    )
    versions = SubtitleVersionStore(settings.data_dir / "subtitle-versions")

    def run_asr_job(context):
        try:
            result = extract_subtitles_asr(
                input_path,
                media,
                source_language=req.source_language,
                model_name=req.model,
                device=req.device,
                compute_type=req.compute_type,
                model_dir=settings.data_dir / "models" / "faster-whisper",
                allow_download=settings.content_bot_alignment_whisper_allow_download,
                cache_dir=settings.data_dir / "cache" / "subtitle-asr",
                context=context,
            )
        except SubtitleAsrCanceled as exc:
            raise SubtitleJobCanceled(str(exc)) from exc
        except SubtitleAsrError as exc:
            raise RuntimeError(str(exc)) from exc
        _assert_media_unchanged(input_path, media)
        context.raise_if_canceled()
        version = _save_media_version(
            versions, req.video_id, result["document"], input_path, media, name="Trích xuất ASR", source="asr"
        )
        result["document"] = version["document"]
        result["version_id"] = version["id"]
        return result

    return services.subtitle_jobs.submit("asr", digest_json({"video_id": req.video_id, "binding": 1, "asr": dedupe_key}), run_asr_job, source_binding=_job_source(req.video_id, media))


@router.post("/api/v1/subtitles/v2/translate/gemini", response_model=SubtitleJobResponse)
def translate_subtitles_gemini_endpoint(
    req: SubtitleTranslateRequest, *, services: AppServices = Depends(get_services)
):
    if not settings.content_bot_gemini_enabled:
        raise HTTPException(status_code=503, detail="Gemini đã bị tắt trong cấu hình.")

    doc_data = req.document.model_dump(mode="json")
    # Legacy text-only documents remain translatable without a media file.
    # Bound documents must retain and verify the source through every batch.
    input_path, media = _version_media(req.video_id) if req.document.media_fingerprint else (None, None)
    if media:
        _require_document_media(req.document, media)
    dedupe_payload = {
        "video_id": req.video_id,
        "version": TRANSLATION_VERSION,
        "batch_size": req.batch_size,
        "doc": doc_data,
        "target_language": req.target_language,
        "bilingual": req.bilingual,
        "model": services.gemini_subtitle_service.resolve_model(req.model),
    }
    dedupe_key = digest_json(dedupe_payload)
    versions = SubtitleVersionStore(settings.data_dir / "subtitle-versions")

    def run_translate_job(context):
        try:
            result = translate_source_document(
                services.gemini_subtitle_service,
                req.document,
                video_id=req.video_id,
                target_language=req.target_language,
                bilingual=req.bilingual,
                model=req.model,
                batch_size=req.batch_size,
                cache_dir=settings.data_dir / 'cache' / 'subtitle-translation',
                context=context,
            )
        except SubtitleTranslateCanceled as exc:
            raise SubtitleJobCanceled(str(exc)) from exc
        except SubtitleTranslateError as exc:
            if exc.partial_result and exc.partial_result['translated_count']:
                try:
                    if media:
                        _assert_media_unchanged(input_path, media)
                    partial_version = versions.save(req.video_id,exc.partial_result['document'],
                        name=f"Bản dịch chưa xong ({exc.partial_result['translated_count']}/{exc.partial_result['total_count']})",source='translation',
                        media_fingerprint=media.get("fingerprint") if media else None)
                except OSError as save_error:
                    context.update_details({'partial_version_save_failed':True})
                    raise RuntimeError(f'{exc} Không lưu được phiên bản dịch dở; kiểm tra dung lượng và quyền ghi thư mục dữ liệu.') from save_error
                context.update_details({'partial_version_id':partial_version['id'],
                    'resume_available':bool(exc.partial_result.get('checkpointed_count',exc.partial_result['translated_count']))})
            raise RuntimeError(str(exc)) from exc
        context.raise_if_canceled()
        if media:
            _assert_media_unchanged(input_path, media)
        version = versions.save(
            req.video_id,
            result["document"],
            name="Dịch tiếng Việt (Gemini)",
            source="translation",
            media_fingerprint=media.get("fingerprint") if media else None,
        )
        result["document"] = version["document"]
        result["version_id"] = version["id"]
        return result

    return services.subtitle_jobs.submit("translation", dedupe_key, run_translate_job, source_binding=_job_source(req.video_id, media))


@router.post("/api/v1/subtitles/v2/export/source-srt")
def export_source_srt_endpoint(req: SubtitleExportSourceRequest):
    cues = [segment.model_dump(mode="json") for segment in req.document.segments]
    srt_content = subtitles_to_srt(cues, use_source=True)
    return {"srt": srt_content, "count": len(cues)}


@router.post(
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

    _require_document_media(req.document, media)
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


@router.post("/api/v1/subtitles/v2/render", response_model=SubtitleJobResponse)
def render_subtitle_timeline_v2_endpoint(
    req: SubtitleRenderRequestV2,
    user=Depends(get_current_user),
    *,
    services: AppServices = Depends(get_services),
):
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

    _require_document_media(req.document, media)
    document_data = req.document.model_dump(mode="json")
    options_data = req.options.model_dump(mode="json")
    voice_document = None
    source_gain = float(options_data.get("volume", 1))
    if req.voice_project_id:
        if req.voice_project_id != req.video_id:
            raise HTTPException(422, "Giọng đọc không thuộc video này.")
        try:
            voice_document = services.voiceover_manager.store.get_document(
                str(user["sub"]), req.voice_project_id
            )
            if (
                voice_document.revision != req.voice_revision
                or voice_document.video_fingerprint != media["fingerprint"]
            ):
                raise ValueError("Dự án giọng vừa thay đổi. Lưu lại trước khi xuất.")
            if not voice_document.mix.enabled:
                voice_document = None
            else:
                voice_document = retained_voice_document(
                    voice_document, options_data, media["duration_ms"]
                )
                verify_source_bindings(voice_document, document_data['segments'])
                verify_document(
                    services.voiceover_manager.store,
                    str(user["sub"]),
                    voice_document,
                    media["duration_ms"],
                )
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
                context.update(
                    round(percent * 0.9),
                    "video" if phase == "complete" else phase,
                    "Đã dựng hình, chuẩn bị ghép giọng"
                    if phase == "complete"
                    else message,
                )
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
                context.update(
                    92, "voiceover", "Đang ghép giọng đọc theo timeline đã cắt"
                )
                result = finalize_voiced_render(
                    services.voiceover_manager.store,
                    str(user["sub"]),
                    voice_document,
                    result,
                    settings.data_dir / "videos" / "output",
                    media,
                    options_data,
                    source_gain,
                    context.cancel_event,
                )
                context.update(100, "complete", "Đã xuất video có giọng đọc")
            _assert_media_unchanged(input_path, media)
            publish_render(settings.data_dir / "videos" / "output", result["output_filename"], req.video_id, media["fingerprint"])
            return result
        except SubtitleRenderCanceled as exc:
            raise SubtitleJobCanceled(str(exc)) from exc

    job_key = digest_json({"video_id": req.video_id, "publication": 1, "render": dedupe_key})
    job = _submit_bound_job(
        services.subtitle_jobs,
        "render",
        job_key,
        run_render_job,
        _job_source(req.video_id, media),
    )
    if not isinstance(job, dict) or "state" not in job:
        return job
    public = _public_job(job)
    if public.get("phase") == "output_unavailable":
        # A completed historical job must not make "export again" a no-op
        # after its file/publication metadata has disappeared.
        retry = _submit_bound_job(
            services.subtitle_jobs,
            "render",
            digest_json({"retry": uuid.uuid4().hex, "render": job_key}),
            run_render_job,
            _job_source(req.video_id, media),
        )
        return _public_job(retry) if isinstance(retry, dict) and "state" in retry else retry
    return public


@router.get("/api/v1/subtitles/jobs/{job_id}", response_model=SubtitleJobResponse)
def get_subtitle_job(job_id: str, *, services: AppServices = Depends(get_services)):
    if not re.fullmatch(r"[a-f0-9]{20}", job_id):
        raise HTTPException(status_code=422, detail="Invalid subtitle job id")
    job = services.subtitle_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Subtitle job not found")
    return _public_job(job)


@router.post(
    "/api/v1/subtitles/jobs/{job_id}/cancel",
    response_model=SubtitleJobResponse,
)
def cancel_subtitle_job(job_id: str, *, services: AppServices = Depends(get_services)):
    if not re.fullmatch(r"[a-f0-9]{20}", job_id):
        raise HTTPException(status_code=422, detail="Invalid subtitle job id")
    job = services.subtitle_jobs.cancel(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Subtitle job not found")
    return _public_job(job)


@router.get("/api/v1/subtitles/renders/{filename}")
def get_precision_subtitle_render(filename: str):
    if not re.fullmatch(
        r"subtitled_[a-f0-9]{12,32}_[a-f0-9]{12}\.mp4",
        filename,
    ):
        raise HTTPException(status_code=422, detail="Invalid subtitle render filename")
    output_path = settings.data_dir / "videos" / "output" / filename
    if not output_path.is_file():
        raise HTTPException(status_code=404, detail="Subtitle render not found")
    _, media = _version_media(render_video_id(filename))
    try:
        verify_render(output_path.parent, filename, media.get("fingerprint"))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return FileResponse(output_path, media_type="video/mp4", filename=filename, headers={"Cache-Control": "private, no-store"})


@router.post("/api/v1/subtitles/burn", response_model=SubtitleBurnResponse)
def burn_subtitle_video_endpoint(req: SubtitleBurnRequest):
    input_path = _uploaded_video_path(req.video_id)
    try:
        media = probe_media_cached(input_path, settings.data_dir / "cache" / "media-probes",
                                   timeout_seconds=settings.content_bot_media_probe_timeout_seconds)
    except MediaProbeError as exc:
        raise HTTPException(422, "Không xác minh được video nguồn trước khi xuất.") from exc
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
        _assert_media_unchanged(input_path, media)
        publish_render(output_dir, out_filename, req.video_id, media["fingerprint"])
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


@router.get("/api/v1/subtitles/video/{video_id}")
def get_subtitle_video(
    video_id: str, type: Literal["original", "subtitled"] = "original"
):
    if type == "subtitled":
        target_path = _subtitled_video_path(video_id)
        if not target_path.exists():
            raise HTTPException(status_code=404, detail="Subtitled video not ready")
        _, media = _version_media(video_id)
        try:
            verify_render(target_path.parent, target_path.name, media.get("fingerprint"))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return FileResponse(target_path, media_type="video/mp4", filename=target_path.name,
                            headers={"Cache-Control": "private, no-store"})

    target_path = _uploaded_video_path(video_id)
    media_type = "video/mp4"
    if target_path.suffix.lower() == ".webm":
        media_type = "video/webm"
    elif target_path.suffix.lower() == ".mkv":
        media_type = "video/x-matroska"

    return FileResponse(target_path, media_type=media_type, filename=target_path.name)
