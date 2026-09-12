from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..application_services import AppServices, get_services
from ..config import settings
from ..middleware.auth import get_current_user
from ..schemas import (
    GeminiSubtitleRequest,
    MediaMetadata,
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
)
from ..services.gemini_subtitles import (
    DEFAULT_GEMINI_MODEL,
    GeminiSubtitleCanceled,
    GeminiSubtitleError,
    GeminiSubtitleService,
    gemini_generation_cache_key,
)
from ..services.media_probe import MediaProbeError, probe_media_cached
from ..services.subtitle_alignment import (
    AlignmentSettings,
    SubtitleAlignmentCanceled,
    align_subtitle_document,
    alignment_cache_key,
)
from ..services.subtitle_jobs import (
    SubtitleJobCanceled,
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
from ..services.subtitles import (
    burn_subtitles_to_video,
    parse_subtitles_text,
    parse_subtitles_v2,
    subtitles_to_ass,
    subtitles_to_srt,
)
from ..services.voiceover.mix import (
    finalize_voiced_render,
    retained_voice_document,
    verify_document,
    verify_voice_cuts,
)
from .media_paths import (
    SUPPORTED_VIDEO_EXTENSIONS,
    _uploaded_video_path,
    _validate_local_video_id,
)

SUPPORTED_OVERLAY_CONTENT_TYPES = {
    "application/octet-stream",
    "image/jpeg",
    "image/png",
    "image/webp",
}


logger = logging.getLogger(__name__)
router = APIRouter()


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
            return services.gemini_subtitle_service.generate(
                input_path, media, options, context
            )
        except GeminiSubtitleCanceled as exc:
            raise SubtitleJobCanceled(str(exc)) from exc

    return services.gemini_subtitle_jobs.submit(
        "generation", dedupe_key, run_generation_job
    )


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
    return job


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
    return job


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

    return services.subtitle_jobs.submit("alignment", dedupe_key, run_alignment_job)


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
            return result
        except SubtitleRenderCanceled as exc:
            raise SubtitleJobCanceled(str(exc)) from exc

    return services.subtitle_jobs.submit("render", dedupe_key, run_render_job)


@router.get("/api/v1/subtitles/jobs/{job_id}", response_model=SubtitleJobResponse)
def get_subtitle_job(job_id: str, *, services: AppServices = Depends(get_services)):
    if not re.fullmatch(r"[a-f0-9]{20}", job_id):
        raise HTTPException(status_code=422, detail="Invalid subtitle job id")
    job = services.subtitle_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Subtitle job not found")
    return job


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
    return job


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
    return FileResponse(output_path, media_type="video/mp4", filename=filename)


@router.post("/api/v1/subtitles/burn", response_model=SubtitleBurnResponse)
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


@router.get("/api/v1/subtitles/video/{video_id}")
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
