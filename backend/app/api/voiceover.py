from __future__ import annotations

import tempfile
import subprocess
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Query
from fastapi.responses import FileResponse

from ..middleware.auth import get_current_user
from ..services.subtitle_render import SubtitleRenderError
from ..services.voiceover.audio import convert_reference
from ..services.voiceover.manager import VoiceManager
from ..services.voiceover.mix import export_voice_audio
from ..services.voiceover.models import (
    AudioExportRequest,
    PreviewRequest,
    StartJob,
    VoiceClip,
    VoiceDocument,
    VoiceProfile,
)
from ..services.voiceover.packages import (
    MAX_PACKAGE_BYTES,
    export_package,
    import_package,
)
from ..services.voiceover.store import read_json, write_json


def build_voiceover_router(manager: VoiceManager) -> APIRouter:
    router = APIRouter(prefix="/api/v1/voiceover", tags=["voiceover"])
    store = manager.store

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
        return manager.status()

    @router.get("/profiles")
    def profiles(user=Depends(owner)):
        return [
            read_json(p) for p in (store.owner_root(user) / "profiles").glob("*.json")
        ]

    @router.post("/profiles")
    def save_profile(profile: VoiceProfile, user=Depends(owner)):
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

    @router.post("/references")
    async def reference(file: UploadFile = File(...), user=Depends(owner)):
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

                metadata = await asyncio.to_thread(convert_reference, source, output)
                rid = metadata["checksum"]
                dest = store.path(user, "references", rid, ".wav")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(output.read_bytes())
                return {"id": rid, "duration_ms": metadata["duration_ms"]}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(422, "Không đọc được mẫu giọng. Chọn file audio hợp lệ dài 3–8 giây.") from exc

    @router.get("/references/{reference_id}")
    def get_reference(reference_id: str, user=Depends(owner)):
        path = checked(lambda: store.path(user, "references", reference_id, ".wav"))
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="audio/wav")

    @router.get("/projects/{project}")
    def get_document(project: str, user=Depends(owner)):
        return checked(lambda: store.get_document(user, project))

    @router.put("/projects/{project}")
    def save_document(project: str, document: VoiceDocument, user=Depends(owner)):
        if project != document.project_id:
            raise HTTPException(422, "ID dự án không khớp.")
        return checked(lambda: store.save_document(user, document))

    @router.get("/projects/{project}/jobs")
    def project_jobs(project: str, user=Depends(owner)):
        checked(lambda: store.path(user, "projects", project))
        rows = [read_json(p) for p in (store.owner_root(user) / "jobs").glob("*.json")]
        return sorted(
            [r for r in rows if r.get("project_id") == project],
            key=lambda r: r.get("created_at", 0),
            reverse=True,
        )[:10]

    @router.post("/preview")
    def preview(request: PreviewRequest, user=Depends(owner)):
        project = uuid.uuid4().hex[:20]
        doc = VoiceDocument(
            project_id=project,
            video_fingerprint="preview",
            profile=request.profile,
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
        return checked(
            lambda: manager.start(
                user, request.project_id, request.device, request.clip_ids
            )
        )

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str, user=Depends(owner)):
        return checked(lambda: manager.get(user, job_id))

    @router.post("/jobs/{job_id}/{action}")
    def control(job_id: str, action: str, user=Depends(owner)):
        if action not in {"pause", "resume", "cancel"}:
            raise HTTPException(404)
        return checked(lambda: manager.control(user, job_id, action))

    @router.get("/assets/{asset_id}/peaks")
    def peaks(asset_id: str, max_points: int = Query(default=250, ge=16, le=2048), user=Depends(owner)):
        metadata = checked(lambda: read_json(store.path(user, "assets", asset_id)))
        levels = metadata.get("peaks", [])
        selected = next((level for level in levels if len(level) <= max_points), levels[-1] if levels else [])
        if len(selected) > max_points:
            stride = (len(selected) + max_points - 1) // max_points
            selected = [max(selected[i:i + stride]) for i in range(0, len(selected), stride)]
        return {"peaks": [selected], "duration_ms": metadata["duration_ms"]}

    @router.get("/assets/{asset_id}")
    def asset(asset_id: str, user=Depends(owner)):
        path = checked(lambda: store.path(user, "assets", asset_id, ".wav"))
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(
            path, media_type="audio/wav", filename=f"voice_{asset_id[:12]}.wav"
        )

    @router.post("/packages/export")
    def export(request: StartJob, user=Depends(owner)):
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
        def compose():
            doc = store.get_document(user, project)
            return export_voice_audio(store, user, doc, format)

        path = checked(compose)
        return FileResponse(path, media_type={"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg"}[format], filename=f"giong-doc.{format}")

    @router.post("/projects/{project}/audio")
    def export_edited_audio(project: str, request: AudioExportRequest, user=Depends(owner)):
        def compose():
            doc = store.get_document(user, project)
            if doc.revision != request.revision:
                raise ValueError("Lời đọc đã thay đổi. Lưu và thử xuất lại.")
            return export_voice_audio(store, user, doc, request.format,
                                      request.model_dump(), request.duration_ms)
        path = checked(compose)
        return FileResponse(path, media_type={"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg"}[request.format],
                            filename=f"giong-doc.{request.format}")

    return router
