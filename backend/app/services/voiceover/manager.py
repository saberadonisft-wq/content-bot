from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .audio import audio_metadata
from .models import SDK_VERSION, MODEL_REVISION, VoiceDocument
from .store import VoiceStore, generation_hash, normalized_text, read_json, write_json

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNTIME = REPO_ROOT / "runtimes" / "voiceover"


class WorkerDeadline:
    """Progress messages cannot extend a stuck model-load or clip indefinitely."""
    def __init__(self, now: float):
        self.stage = ("loading", None)
        self.started = now

    def observe(self, progress: dict, now: float):
        stage = (progress.get("stage"), progress.get("clip_id"))
        if stage[0] not in {"loading", "generating", "finished"}:
            self.stage = ("legacy", None)
            return  # Older workers retain the no-progress timeout.
        if stage != self.stage:
            self.stage, self.started = stage, now

    def check(self, now: float):
        limit = 900 if self.stage[0] == "loading" else 1800
        if self.stage[0] not in {"finished", "legacy"} and now - self.started > limit:
            label = "Nạp model" if self.stage[0] == "loading" else f"Tạo đoạn {self.stage[1]}"
            raise TimeoutError(f"{label} quá {limit // 60} phút. Các đoạn đã lưu được giữ lại; có thể tiếp tục phần thiếu.")


class VoiceManager:
    def __init__(self, store: VoiceStore):
        self.store = store
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="voiceover"
        )
        self.controls: dict[str, str] = {}
        self.live: set[str] = set()
        for path in store.root.glob("*/jobs/*.json"):
            if path.name.endswith(".manifest.json"):
                continue
            try:
                job = read_json(path)
                if job.get("state") in {"queued", "running"}:
                    job.update(
                        state="interrupted",
                        message="Tác vụ gián đoạn. Có thể tiếp tục phần còn thiếu.",
                    )
                    write_json(path, job)
            except (ValueError, OSError):
                continue

    def python(self, device: str = "cpu") -> Path:
        override = os.environ.get("CONTENT_BOT_VOICE_PYTHON")
        gpu_python = RUNTIME / ".venv-gpu" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if device == "cuda" and gpu_python.is_file() and not override:
            return gpu_python
        return (
            Path(override)
            if override
            else RUNTIME
            / ".venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )

    def status(self) -> dict:
        status_path = RUNTIME / "runtime-status.json"
        try:
            status = read_json(status_path) if status_path.exists() else {}
            if not isinstance(status, dict):
                raise ValueError("Invalid runtime status")
        except (ValueError, OSError):
            status = {"message": "Không đọc được trạng thái bộ tạo giọng. Chạy lại scripts/setup-voiceover.ps1."}
        available = {device for device in ("cpu", "cuda") if self.python(device).is_file()}
        installed = bool(available)
        compatible = status.get("sdk_version") == SDK_VERSION and status.get("model_revision") == MODEL_REVISION
        prepared = status.get("devices", [])
        devices = [device for device in ("cpu", "cuda") if device in available and device in prepared] if isinstance(prepared, list) and compatible and status.get("ready") else []
        return {
            "installed": installed,
            "ready": bool(devices),
            "sdk_version": SDK_VERSION,
            "presets": status.get("presets", []),
            "devices": devices,
            "message": ("Phiên bản model/runtime đã thay đổi. Chạy lại scripts/setup-voiceover.ps1."
                        if status.get("ready") and not compatible else status.get(
                "message", "Chạy scripts/setup-voiceover.ps1 để chuẩn bị model."
            )) if installed else "Chưa tìm thấy môi trường tạo giọng. Chạy scripts/setup-voiceover.ps1.",
            "model_revision": status.get("model_revision"),
        }

    def manifest(
        self,
        owner: str,
        document: VoiceDocument,
        device: str,
        clip_ids: list[str] | None = None,
    ) -> dict:
        if clip_ids is not None and set(clip_ids) - {c.id for c in document.clips}:
            raise ValueError("Đoạn giọng không tồn tại.")
        clips = []
        for clip in document.clips:
            if clip_ids is not None and clip.id not in clip_ids:
                continue
            key = generation_hash(document, clip, device)
            clips.append(
                {
                    "id": clip.id,
                    "generation_hash": key,
                    "text": normalized_text(clip.spoken_text, document.pronunciation),
                }
            )
        return {
            "schema_version": 1,
            "sdk_version": SDK_VERSION,
            "project_id": document.project_id,
            "video_fingerprint": document.video_fingerprint,
            "document_revision": document.revision,
            "profile": document.profile.model_dump(),
            "device": device,
            "clips": clips,
        }

    def get(self, owner: str, job_id: str):
        with self.store.lock:
            return read_json(self.store.path(owner, "jobs", job_id))

    def start(self, owner: str, project: str, device: str, clip_ids=None):
        runtime_status = self.status()
        if not runtime_status["ready"]:
            raise ValueError(
                "Model chưa sẵn sàng. Chạy scripts/setup-voiceover.ps1 trước."
            )
        if device not in runtime_status["devices"]:
            raise ValueError(f"Thiết bị {device} chưa sẵn sàng. Chạy scripts/setup-voiceover.ps1 cho thiết bị này.")
        with self.store.lock:
            for jid in self.live:
                try:
                    existing = self.get(owner, jid)
                    if existing["project_id"] == project:
                        return existing
                except FileNotFoundError:
                    pass
            document = self.store.get_document(owner, project)
            manifest = self.manifest(owner, document, device, clip_ids)
            jid = uuid.uuid4().hex[:20]
            job = {
                "id": jid,
                "project_id": project,
                "device": device,
                "clip_ids": clip_ids,
                "state": "queued",
                "completed": 0,
                "completed_clip_ids": [],
                "current_clip_id": None,
                "total": len(manifest["clips"]),
                "failed": [],
                "message": "Đang chờ tạo giọng",
                "elapsed_seconds": 0,
                "eta_seconds": None,
                "created_at": time.time(),
            }
            write_json(self.store.path(owner, "jobs", jid), job)
            self.live.add(jid)
            self.executor.submit(self._run, owner, job, manifest)
            return job.copy()

    def control(self, owner: str, jid: str, action: str):
        with self.store.lock:
            job = self.get(owner, jid)
            if action == "resume":
                if jid in self.live:
                    return job
                return self.start(
                    owner, job["project_id"], job["device"], job["clip_ids"]
                )
            if jid in self.live:
                self.controls[jid] = action
                job["message"] = (
                    "Đang tạm dừng sau đoạn hiện tại…"
                    if action == "pause"
                    else "Đang hủy…"
                )
                write_json(self.store.path(owner, "jobs", jid), job)
            return job

    def _run(self, owner, job, manifest):
        jid = job["id"]
        process = None
        started = time.monotonic()
        root = self.store.owner_root(owner) / "work" / jid
        root.mkdir(parents=True, exist_ok=True)
        control_path = root / "control.json"
        progress_path = root / "progress.json"
        target = root / "assets"
        target.mkdir(exist_ok=True)
        attached = set()

        def persist():
            with self.store.lock:
                write_json(self.store.path(owner, "jobs", jid), job)

        try:
            pending = []
            for item in manifest["clips"]:
                key = item["generation_hash"]
                meta_path = self.store.path(owner, "assets", key)
                wav_path = self.store.path(owner, "assets", key, ".wav")
                if meta_path.exists() and wav_path.exists():
                    try:
                        meta = read_json(meta_path)
                        valid = audio_metadata(wav_path)["checksum"] == meta["checksum"]
                    except (OSError, ValueError, KeyError, EOFError, wave.Error):
                        valid = False
                    if valid:
                        self.store.attach(owner, job["project_id"], item["id"], meta)
                        attached.add(item["id"])
                        continue
                pending.append(item)
            manifest["clips"] = pending
            cached_count = len(attached)
            reference = manifest["profile"].get("reference_id")
            if reference:
                shutil.copyfile(
                    self.store.path(owner, "references", reference, ".wav"),
                    root / "reference.wav",
                )
            write_json(root / "manifest.json", manifest)
            job.update(
                state="running", message="Đang nạp model", completed=len(attached), completed_clip_ids=sorted(attached)
            )
            persist()
            if pending:
                if self.controls.get(jid):
                    action = self.controls[jid]
                    job.update(
                        state="paused" if action == "pause" else "canceled",
                        message="Đã dừng trước khi nạp model",
                    )
                    return
                env = dict(os.environ, PYTHONUTF8="1", HF_HUB_DISABLE_TELEMETRY="1")
                with (root / "worker.log").open("w", encoding="utf-8") as log:
                    process = subprocess.Popen(
                        [
                            str(self.python(manifest["device"])),
                            str(RUNTIME / "worker.py"),
                            "run",
                            str(root),
                        ],
                        stdout=log,
                        stderr=log,
                        env=env,
                        creationflags=subprocess.CREATE_NO_WINDOW
                        if os.name == "nt"
                        else 0,
                    )
                    last_change = time.monotonic()
                    deadline = WorkerDeadline(last_change)
                    previous = None
                    while True:
                        action = self.controls.get(jid)
                        if action and (
                            not control_path.exists()
                            or read_json(control_path).get("action") != action
                        ):
                            write_json(control_path, {"action": action})
                        if action == "cancel" and process.poll() is None:
                            process.terminate()
                        if progress_path.exists():
                            progress = read_json(progress_path)
                            deadline.observe(progress, time.monotonic())
                            if progress != previous:
                                previous = progress
                                last_change = time.monotonic()
                            job["message"] = progress.get("message", "Đang tạo giọng")
                            job["failed"] = progress.get("failed", [])
                            job["current_clip_id"] = progress.get("clip_id") if progress.get("stage") == "generating" else None
                            for item in pending:
                                cid, key = item["id"], item["generation_hash"]
                                wav = target / f"{key}.wav"
                                sidecar = target / f"{key}.json"
                                if cid in attached or not wav.exists() or not sidecar.exists():
                                    continue
                                committed = read_json(sidecar)
                                meta = audio_metadata(wav)
                                if (committed.get("checksum") != meta["checksum"]
                                        or committed.get("generation_hash") != key):
                                    raise ValueError("Checkpoint audio không khớp. Tiếp tục để tạo lại phần chưa lưu.")
                                meta.update(
                                    id=key,
                                    generation_hash=key,
                                    device=manifest["device"],
                                )
                                dest = self.store.path(owner, "assets", key, ".wav")
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copyfile(wav, dest.with_suffix(".part"))
                                dest.with_suffix(".part").replace(dest)
                                write_json(self.store.path(owner, "assets", key), meta)
                                self.store.attach(owner, job["project_id"], cid, meta)
                                attached.add(cid)
                            job["completed"] = len(attached)
                            job["completed_clip_ids"] = sorted(attached)
                        job["elapsed_seconds"] = round(time.monotonic() - started, 1)
                        generated_count = len(attached) - cached_count
                        if generated_count > 0:
                            job["eta_seconds"] = round(
                                job["elapsed_seconds"]
                                / generated_count
                                * (job["total"] - len(attached))
                            )
                        else:
                            job["eta_seconds"] = None
                        persist()
                        if process.poll() is not None:
                            if process.returncode and not action:
                                detail = (root / "worker.log").read_text(
                                    encoding="utf-8", errors="replace"
                                )[-1800:]
                                raise RuntimeError(
                                    detail or "Worker giọng đã dừng bất thường."
                                )
                            break
                        if time.monotonic() - last_change > 1800:
                            raise TimeoutError(
                                "Worker không có tiến độ trong 30 phút. Có thể tiếp tục phần còn thiếu."
                            )
                        deadline.check(time.monotonic())
                        time.sleep(0.5)
            action = self.controls.get(jid)
            if not action and len(attached) != job["total"] and not job["failed"]:
                job["failed"] = [
                    {
                        "clip_id": item["id"],
                        "error": "Worker dừng nhưng chưa có audio. Tiếp tục để tạo phần thiếu.",
                    }
                    for item in manifest["clips"]
                    if item["id"] not in attached
                ]
            state = (
                "paused"
                if action == "pause"
                else "canceled"
                if action
                else "failed"
                if job["failed"]
                else "succeeded"
            )
            items = {item["id"]: item for item in manifest["clips"]}
            for failure in job["failed"]:
                item = items.get(failure["clip_id"])
                if item:
                    self.store.mark_failed(owner, job["project_id"], item["id"],
                        item["generation_hash"], manifest["device"], failure["error"])
            job.update(
                state=state,
                completed=len(attached),
                message={
                    "paused": "Đã tạm dừng",
                    "canceled": "Đã hủy",
                    "failed": "Có đoạn lỗi. Tiếp tục để thử lại.",
                    "succeeded": "Đã tạo xong giọng đọc",
                }[state],
            )
        except Exception as exc:
            job.update(state="failed", message=str(exc)[-2000:])
        finally:
            job["current_clip_id"] = None
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            persist()
            with self.store.lock:
                self.live.discard(jid)
                self.controls.pop(jid, None)

    def shutdown(self):
        for jid in list(self.live):
            self.controls[jid] = "cancel"
        self.executor.shutdown(wait=True, cancel_futures=False)
