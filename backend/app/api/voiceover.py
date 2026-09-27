from __future__ import annotations

import subprocess
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..middleware.auth import get_current_user
from ..services.subtitle_jobs import SubtitleJobManager
from ..services.subtitle_render import SubtitleRenderError
from ..services.vocal_audio import ffmpeg as separation_ffmpeg
from ..services.vocal_separator import SEPARATOR_VERSION
from ..services.vocal_separator_supervisor import run_vocal_separation_job
from ..services.voiceover.audio import convert_reference
from ..services.voiceover.manager import VoiceManager
from ..services.voiceover.mix import export_voice_audio
from ..services.voiceover.models import (
    AudioExportRequest,
    PreviewRequest,
    StartJob,
    TextPreviewRequest,
    VoiceClip,
    VoiceDocument,
    VoiceProfile,
)
from ..services.voiceover.packages import (
    MAX_PACKAGE_BYTES,
    export_package,
    import_package,
)
from ..services.voiceover.quiet_analysis import analyze_quiet_edges
from ..services.voiceover.separation_store import (
    SeparationStore,
    SeparationStoreError,
    sha256_file,
)
from ..services.voiceover.store import digest, normalized_text, read_json, write_json
from .media_paths import _uploaded_video_path


class SeparationRequest(BaseModel):
    method: Literal["demucs", "center_reduction"] = "demucs"
    device: Literal["auto", "cpu", "cuda"] = "auto"


class BackgroundStemSelection(BaseModel):
    gain: float = Field(default=1, ge=0, le=2)


def build_voiceover_router(
    manager: VoiceManager | Callable[[], VoiceManager],
    *, sync_service_provider=None,
    separation_jobs_provider: Callable[[], SubtitleJobManager] | None = None,
) -> APIRouter:
    manager_provider = manager if callable(manager) else lambda: manager
    router = APIRouter(prefix="/api/v1/voiceover", tags=["voiceover"])

    def configure_sync(manager):
        if sync_service_provider is not None:
            from ..config import settings
            from ..services.voiceover.sync_generation import finish_generation_sync
            manager.sync_runner = lambda active_manager, owner_id, job, persist: finish_generation_sync(
                active_manager, sync_service_provider(), owner_id, job, persist, settings=settings)

    def separation_store(manager) -> SeparationStore:
        return SeparationStore(
            manager.store.root / "separation-cache",
            max_entries=settings.content_bot_separation_cache_max_entries,
            max_bytes=settings.content_bot_separation_cache_max_bytes,
        )

    def separation_public(project: str, manifest: dict) -> dict:
        return {
            "stem_id": manifest["id"],
            "method": manifest.get("method"),
            "model": manifest.get("model"),
            "device": manifest.get("device"),
            "source_fingerprint": manifest.get("source_fingerprint"),
            "source_audio_checksum": manifest.get("source_audio_checksum"),
            "warnings": manifest.get("warnings", []),
            "stems": {
                name: {
                    "checksum": value["checksum"],
                    "duration_ms": value["metadata"]["duration_ms"],
                    "sample_rate": value["metadata"]["sample_rate"],
                    "channels": value["metadata"]["channels"],
                    "url": f"/projects/{project}/separations/stems/{manifest['id']}/{name}",
                }
                for name, value in manifest.get("stems", {}).items()
            },
        }

    def _separation_job_runner(owner: str, project: str, request: SeparationRequest,
                               manager: VoiceManager):
        document = manager.store.get_document(owner, project)
        source_fingerprint = document.video_fingerprint
        model_name = settings.content_bot_demucs_model if request.method == "demucs" else None
        cache_key = digest({
            "version": SEPARATOR_VERSION,
            "source_fingerprint": source_fingerprint,
            "method": request.method,
            "device": request.device,
            "model": model_name,
        })

        def run(context):
            store = separation_store(manager)
            cached = store.find_cached(owner, cache_key=cache_key)
            if cached:
                context.update(100, "cache", "Đã dùng lại stem tách nền đã kiểm tra")
                return separation_public(project, cached)
            video = _uploaded_video_path(project)
            owner_root = manager.store.owner_root(owner)
            owner_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="separation-source-", dir=owner_root) as temp:
                audio = Path(temp) / "source.wav"
                separation_ffmpeg(
                    ["-i", video, "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
                     "-c:a", "pcm_s16le", audio],
                    context=context,
                    timeout_seconds=settings.content_bot_separation_timeout_seconds,
                )
                source_audio_checksum = sha256_file(audio)
                cached = store.find_cached(owner, cache_key=cache_key)
                if cached:
                    context.update(100, "cache", "Đã dùng lại stem tách nền đã kiểm tra")
                    return separation_public(project, cached)
                with tempfile.TemporaryDirectory(prefix="separation-output-", dir=owner_root) as output:
                    result = run_vocal_separation_job(
                        audio,
                        Path(output),
                        context=context,
                        timeout_seconds=settings.content_bot_separation_timeout_seconds,
                        prefer_demucs=request.method == "demucs",
                        device=request.device,
                        model_name=model_name or "htdemucs",
                        model_repo=settings.content_bot_demucs_model_repo,
                    )
                    manifest = store.publish(
                        owner,
                        source_fingerprint=source_fingerprint,
                        source_audio_checksum=source_audio_checksum,
                        cache_key=cache_key,
                        result=result,
                    )
                    context.update_details({"stem_id": manifest["id"], "method": manifest["method"]})
                    return separation_public(project, manifest)

        return run

    def owner(user=Depends(get_current_user)):
        return str(user["sub"])

    def checked(fn):
        try:
            return fn()
        except FileNotFoundError as exc:
            raise HTTPException(404, "Không tìm thấy dữ liệu giọng đọc.") from exc
        except (ValueError, SubtitleRenderError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/status")
    def status(_=Depends(owner)):
        manager = manager_provider()
        return manager.status()

    @router.get("/profiles")
    def profiles(user=Depends(owner)):
        manager = manager_provider()
        store = manager.store
        return [
            read_json(p) for p in (store.owner_root(user) / "profiles").glob("*.json")
        ]

    @router.post("/profiles")
    def save_profile(profile: VoiceProfile, user=Depends(owner)):
        manager = manager_provider()
        store = manager.store

        def save():
            with store.lock:
                path = store.path(user, "profiles", profile.id)
                if path.exists():
                    profile.revision = read_json(path)["revision"] + 1
                if (
                    profile.reference_id
                    and not store.path(
                        user, "references", profile.reference_id, ".wav"
                    ).exists()
                ):
                    raise ValueError("Mẫu giọng không tồn tại.")
                write_json(path, profile.model_dump())
                return profile

        return checked(save)

    @router.delete("/profiles/{profile_id}")
    def delete_profile(profile_id: str, user=Depends(owner)):
        manager = manager_provider()
        store = manager.store

        def delete():
            with store.lock:
                path = store.path(user, "profiles", profile_id)
                if path.exists():
                    path.unlink()
                return {"id": profile_id, "deleted": True}

        return checked(delete)

    @router.post("/references")
    async def reference(
        file: UploadFile = File(...),
        start_seconds: float = Query(default=0, ge=0, le=86400, allow_inf_nan=False),
        duration_seconds: float = Query(default=8, ge=3, le=8, allow_inf_nan=False),
        user=Depends(owner),
    ):
        manager = manager_provider()
        store = manager.store
        try:
            with tempfile.TemporaryDirectory(prefix="voice-reference-") as tmp:
                source = Path(tmp) / "input"
                size = 0
                with source.open("wb") as handle:
                    while block := await file.read(1024 * 1024):
                        size += len(block)
                        if size > 20 * 1024 * 1024:
                            raise HTTPException(413, "Mẫu âm thanh tối đa 20 MB.")
                        handle.write(block)
                output = Path(tmp) / "reference.wav"
                import asyncio

                metadata = await asyncio.to_thread(
                    convert_reference,
                    source,
                    output,
                    start_seconds=start_seconds,
                    duration_seconds=duration_seconds,
                )
                rid = metadata["checksum"]
                dest = store.path(user, "references", rid, ".wav")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(output.read_bytes())
                return {"id": rid, "duration_ms": metadata["duration_ms"]}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(
                422, "Không đọc được mẫu giọng. Chọn file audio hợp lệ dài 3–8 giây."
            ) from exc

    @router.get("/references/{reference_id}")
    def get_reference(reference_id: str, user=Depends(owner)):
        manager = manager_provider()
        store = manager.store
        path = checked(lambda: store.path(user, "references", reference_id, ".wav"))
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="audio/wav")

    @router.get("/projects/{project}")
    def get_document(project: str, user=Depends(owner)):
        manager = manager_provider()
        store = manager.store
        return checked(lambda: store.get_document(user, project))

    @router.put("/projects/{project}")
    def save_document(project: str, document: VoiceDocument, user=Depends(owner)):
        manager = manager_provider()
        store = manager.store
        if project != document.project_id:
            raise HTTPException(422, "ID dự án không khớp.")
        return checked(lambda: store.save_document(user, document))

    @router.get("/projects/{project}/jobs")
    def project_jobs(project: str, user=Depends(owner)):
        manager = manager_provider()
        store = manager.store
        checked(lambda: store.path(user, "projects", project))
        rows = [read_json(p) for p in (store.owner_root(user) / "jobs").glob("*.json")]
        return sorted(
            [r for r in rows if r.get("project_id") == project],
            key=lambda r: r.get("created_at", 0),
            reverse=True,
        )[:10]

    @router.get("/projects/{project}/separations")
    def list_separations(project: str, user=Depends(owner)):
        manager = manager_provider()
        store = manager.store
        document = checked(lambda: store.get_document(user, project))
        separations = separation_store(manager)
        root = separations._directory(user)
        rows = []
        for path in root.glob("[a-f0-9]*.json"):
            try:
                manifest = separations.get(user, path.stem)
            except (OSError, ValueError, SeparationStoreError):
                continue
            if manifest.get("source_fingerprint") == document.video_fingerprint:
                rows.append(separation_public(project, manifest))
        return sorted(rows, key=lambda row: row["stem_id"], reverse=True)[:20]

    @router.post("/projects/{project}/separations")
    def start_separation(project: str, request: SeparationRequest, user=Depends(owner)):
        if separation_jobs_provider is None:
            raise HTTPException(503, "Bộ xử lý tách nền chưa được khởi động.")
        manager = manager_provider()
        checked(lambda: manager.store.get_document(user, project))
        model = settings.content_bot_demucs_model if request.method == "demucs" else None
        key = digest({
            "owner": user,
            "project": project,
            "version": SEPARATOR_VERSION,
            "source": manager.store.get_document(user, project).video_fingerprint,
            "method": request.method,
            "device": request.device,
            "model": model,
        })
        jobs = separation_jobs_provider()
        return checked(lambda: jobs.submit(
            "separation",
            key,
            _separation_job_runner(user, project, request, manager),
            details={"project_id": project, "owner": user},
        ))

    @router.get("/projects/{project}/separations/jobs/{job_id}")
    def get_separation_job(project: str, job_id: str, user=Depends(owner)):
        if separation_jobs_provider is None:
            raise HTTPException(503, "Bộ xử lý tách nền chưa được khởi động.")
        manager = manager_provider()
        checked(lambda: manager.store.get_document(user, project))
        job = separation_jobs_provider().get(job_id)
        if (not job or job.get("kind") != "separation"
                or job.get("details", {}).get("project_id") != project
                or job.get("details", {}).get("owner") != user):
            raise HTTPException(404, "Không tìm thấy tác vụ tách nền.")
        return job

    @router.post("/projects/{project}/separations/jobs/{job_id}/cancel")
    def cancel_separation_job(project: str, job_id: str, user=Depends(owner)):
        if separation_jobs_provider is None:
            raise HTTPException(503, "Bộ xử lý tách nền chưa được khởi động.")
        manager = manager_provider()
        checked(lambda: manager.store.get_document(user, project))
        job = separation_jobs_provider().get(job_id)
        if (not job or job.get("kind") != "separation"
                or job.get("details", {}).get("project_id") != project
                or job.get("details", {}).get("owner") != user):
            raise HTTPException(404, "Không tìm thấy tác vụ tách nền.")
        return separation_jobs_provider().cancel(job_id)

    @router.get("/projects/{project}/separations/stems/{stem_id}/{kind}")
    def get_separation_stem(project: str, stem_id: str, kind: str, user=Depends(owner)):
        manager = manager_provider()
        document = checked(lambda: manager.store.get_document(user, project))
        try:
            separation = separation_store(manager).get(user, stem_id)
            if separation.get("source_fingerprint") != document.video_fingerprint:
                raise SeparationStoreError("Stem không thuộc video hiện tại.")
            path = separation_store(manager).file(user, stem_id, kind)
        except (OSError, ValueError, SeparationStoreError) as exc:
            raise HTTPException(404, "Không tìm thấy stem tách nền.") from exc
        return FileResponse(path, media_type="audio/wav", filename=f"{kind}.wav")

    @router.post("/projects/{project}/background-stems/{stem_id}/select")
    def select_background_stem(project: str, stem_id: str, request: BackgroundStemSelection,
                               user=Depends(owner)):
        manager = manager_provider()
        store = manager.store
        document = checked(lambda: store.get_document(user, project))
        try:
            manifest = separation_store(manager).get(user, stem_id)
            value = manifest["stems"]["background"]
            if manifest.get("source_fingerprint") != document.video_fingerprint:
                raise SeparationStoreError("Stem không thuộc video hiện tại.")
        except (OSError, ValueError, SeparationStoreError, KeyError) as exc:
            raise HTTPException(409, "Stem tách nền đã cũ hoặc không hợp lệ.") from exc
        document.mix.background_stem_id = stem_id
        document.mix.background_stem_checksum = value["checksum"]
        document.mix.background_source_fingerprint = manifest["source_fingerprint"]
        document.mix.background_gain = request.gain
        return checked(lambda: store.save_document(user, document))

    @router.delete("/projects/{project}/background-stems/selection")
    def clear_background_stem(project: str, user=Depends(owner)):
        manager = manager_provider()
        store = manager.store
        document = checked(lambda: store.get_document(user, project))
        document.mix.background_stem_id = None
        document.mix.background_stem_checksum = None
        document.mix.background_source_fingerprint = None
        document.mix.background_gain = 1
        return checked(lambda: store.save_document(user, document))

    @router.post('/text-preview')
    def text_preview(request: TextPreviewRequest, user=Depends(owner)):
        text = normalized_text(request.text,request.pronunciation,request.text_normalization)
        return {'text':text,'normalizer':request.text_normalization,'changed':text != request.text}

    @router.post("/preview")
    def preview(request: PreviewRequest, user=Depends(owner)):
        manager = manager_provider()
        store = manager.store
        project = uuid.uuid4().hex[:20]
        doc = VoiceDocument(
            project_id=project,
            video_fingerprint="preview",
            profile=request.profile,
            pronunciation=request.pronunciation,
            text_normalization=request.text_normalization,
            clips=[
                VoiceClip(
                    id="preview",
                    spoken_text=request.text,
                    start_ms=0,
                    end_ms=600000,
                    rate=1,
                )
            ],
        )
        checked(lambda: store.save_document(user, doc))
        return checked(lambda: manager.start(user, project, request.device))

    @router.post("/jobs")
    def start_job(request: StartJob, user=Depends(owner)):
        manager = manager_provider()
        if request.subtitle_document is not None:
            configure_sync(manager)
        return checked(
            lambda: manager.start(
                user, request.project_id, request.device, request.clip_ids,
                subtitle_document=request.subtitle_document.model_dump(mode='json') if request.subtitle_document else None,
            )
        )

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str, user=Depends(owner)):
        manager = manager_provider()
        return checked(lambda: manager.get(user, job_id))

    @router.post("/jobs/{job_id}/{action}")
    def control(job_id: str, action: str, user=Depends(owner)):
        manager = manager_provider()
        if action not in {"pause", "resume", "cancel"}:
            raise HTTPException(404)
        if action == 'resume':
            configure_sync(manager)
        return checked(lambda: manager.control(user, job_id, action))

    @router.get("/assets/{asset_id}/quiet-analysis")
    def quiet_analysis(asset_id: str, user=Depends(owner)):
        store = manager_provider().store

        def analyze():
            meta = read_json(store.path(user, "assets", asset_id))
            return analyze_quiet_edges(store.path(user, "assets", asset_id, ".wav"),
                expected_checksum=meta["checksum"], cache_dir=store.owner_root(user) / "quiet-analysis")

        return checked(analyze)

    @router.get("/assets/{asset_id}/peaks")
    def peaks(
        asset_id: str,
        max_points: int = Query(default=250, ge=16, le=2048),
        user=Depends(owner),
    ):
        manager = manager_provider()
        store = manager.store
        metadata = checked(lambda: read_json(store.path(user, "assets", asset_id)))
        levels = metadata.get("peaks", [])
        selected = next(
            (level for level in levels if len(level) <= max_points),
            levels[-1] if levels else [],
        )
        if len(selected) > max_points:
            stride = (len(selected) + max_points - 1) // max_points
            selected = [
                max(selected[i : i + stride]) for i in range(0, len(selected), stride)
            ]
        return {"peaks": [selected], "duration_ms": metadata["duration_ms"]}

    @router.get("/assets/{asset_id}")
    def asset(asset_id: str, user=Depends(owner)):
        manager = manager_provider()
        store = manager.store
        path = checked(lambda: store.path(user, "assets", asset_id, ".wav"))
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(
            path, media_type="audio/wav", filename=f"voice_{asset_id[:12]}.wav"
        )

    @router.post("/packages/export")
    def export(request: StartJob, user=Depends(owner)):
        manager = manager_provider()
        path = checked(
            lambda: export_package(manager, user, request.project_id, request.device)
        )
        return FileResponse(
            path, media_type="application/zip", filename="voiceover-kaggle.zip"
        )

    @router.post("/packages/import/{project}")
    async def import_result(
        project: str, file: UploadFile = File(...), user=Depends(owner)
    ):
        manager = manager_provider()
        import asyncio
        import zipfile

        with tempfile.TemporaryDirectory(prefix="voice-upload-") as tmp:
            path = Path(tmp) / "result.zip"
            size = 0
            with path.open("wb") as output:
                while block := await file.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_PACKAGE_BYTES:
                        raise HTTPException(413, "Gói tối đa 4 GB.")
                    output.write(block)
            try:
                return await asyncio.to_thread(
                    import_package, manager, user, project, path
                )
            except (ValueError, FileNotFoundError, zipfile.BadZipFile, KeyError) as exc:
                raise HTTPException(422, str(exc)) from exc

    @router.get("/projects/{project}/audio")
    def export_audio(project: str, format: str = "wav", user=Depends(owner)):
        manager = manager_provider()
        store = manager.store

        def compose():
            doc = store.get_document(user, project)
            return export_voice_audio(store, user, doc, format)

        path = checked(compose)
        return FileResponse(
            path,
            media_type={"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg"}[
                format
            ],
            filename=f"giong-doc.{format}",
        )

    @router.post("/projects/{project}/audio")
    def export_edited_audio(
        project: str, request: AudioExportRequest, user=Depends(owner)
    ):
        manager = manager_provider()
        store = manager.store

        def compose():
            doc = store.get_document(user, project)
            if doc.revision != request.revision:
                raise ValueError("Lời đọc đã thay đổi. Lưu và thử xuất lại.")
            return export_voice_audio(
                store,
                user,
                doc,
                request.format,
                request.model_dump(),
                request.duration_ms,
            )

        path = checked(compose)
        return FileResponse(
            path,
            media_type={"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg"}[
                request.format
            ],
            filename=f"giong-doc.{request.format}",
        )

    return router
