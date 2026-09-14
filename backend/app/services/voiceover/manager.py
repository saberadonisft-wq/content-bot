from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from ..subtitle_jobs import SubtitleJobQueueFull
from .audio import audio_metadata
from .models import (
    MODEL_ID,
    MODEL_REVISION,
    SDK_VERSION,
    V2_MODEL_ID,
    V2_MODEL_REVISION,
    VoiceDocument,
)
from .store import VoiceStore, generation_hash, normalized_text, read_json, write_json

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNTIME = REPO_ROOT / "runtimes" / "voiceover"


def stop_worker(process):
    """Windows venv Python launches a child; killing only the launcher leaks it."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, check=False,
        )
    if process.poll() is None:
        process.kill()


def keep_worker_alive(path):
    stopped = threading.Event()
    path.touch()

    def heartbeat():
        while not stopped.wait(2):
            try:
                path.touch()
            except OSError:
                return  # Worker releases its resources if this lease expires.

    threading.Thread(target=heartbeat, name="voice-heartbeat", daemon=True).start()
    return stopped


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
    def __init__(self, store: VoiceStore, *, max_pending: int = 8):
        self.store = store
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="voiceover"
        )
        self.controls: dict[str, str] = {}
        self.live: set[str] = set()
        self._accepting = True
        self._max_pending = max(1, max_pending)
        self._futures = {}
        self._processes = {}
        self.sync_runner = None
        for path in store.root.glob("*/jobs/*.json"):
            if path.name.endswith(".manifest.json"):
                continue
            try:
                job = read_json(path)
                if job.get("state") in {"queued", "running", "interrupted"}:
                    old_root = path.parent.parent / "work" / path.stem
                    if old_root.is_dir():
                        write_json(old_root / "control.json", {"action": "pause"})
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

    def status(self, model_id: str = MODEL_ID) -> dict:
        is_v2 = model_id == V2_MODEL_ID
        expected_revision = V2_MODEL_REVISION if is_v2 else MODEL_REVISION
        setup_command = "scripts/setup-voiceover.ps1" + (" -Device cuda -Engine v2turbo" if is_v2 else "")
        status_path = RUNTIME / ("runtime-status-v2turbo.json" if is_v2 else "runtime-status.json")
        try:
            status = read_json(status_path) if status_path.exists() else {}
            if not isinstance(status, dict):
                raise TypeError("Invalid runtime status")
        except (ValueError, TypeError, OSError):
            status = {"message": "Không đọc được trạng thái bộ tạo giọng. Chạy lại scripts/setup-voiceover.ps1."}
        available = {device for device in ("cpu", "cuda") if self.python(device).is_file()}
        installed = bool(available)
        compatible = status.get("sdk_version") == SDK_VERSION and status.get("model_revision") == expected_revision
        prepared = status.get("devices", [])
        devices = [device for device in ("cpu", "cuda") if device in available and device in prepared] if isinstance(prepared, list) and compatible and status.get("ready") else []
        if is_v2:
            devices = [device for device in devices if device == "cuda"]
        return {
            "installed": installed,
            "ready": bool(devices),
            "sdk_version": SDK_VERSION,
            "presets": status.get("presets", []) if compatible else [],
            "devices": devices,
            "message": (f"Phiên bản model/runtime đã thay đổi. Chạy lại {setup_command}."
                        if status.get("ready") and not compatible else status.get(
                "message", f"Chạy {setup_command} để chuẩn bị model."
            )) if installed else f"Chưa tìm thấy môi trường tạo giọng. Chạy {setup_command}.",
            "model_revision": status.get("model_revision"),
            "model_id": model_id,
            "setup_command": setup_command,
        }

    def engine_statuses(self) -> list[dict]:
        return [{**self.status(model_id), "model_revision": revision, "name": name}
                for model_id, revision, name in [
                    (MODEL_ID, MODEL_REVISION, "VieNeu v3 Turbo"),
                    (V2_MODEL_ID, V2_MODEL_REVISION, "VieNeu v2 Turbo · so sánh"),
                ]]

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

    def start(self, owner: str, project: str, device: str, clip_ids=None, *, subtitle_document=None, sync_session_id=None):
        document = self.store.get_document(owner, project)
        runtime_status = self.status(document.profile.model_id) if document.profile.model_id != MODEL_ID else self.status()
        if not runtime_status["ready"]:
            raise ValueError(
                f"Model chưa sẵn sàng. Chạy {runtime_status.get('setup_command', 'scripts/setup-voiceover.ps1')} trước."
            )
        if device not in runtime_status["devices"]:
            raise ValueError(f"Thiết bị {device} chưa sẵn sàng. Chạy scripts/setup-voiceover.ps1 cho thiết bị này.")
        with self.store.lock:
            if not self._accepting:
                raise SubtitleJobQueueFull("Voice manager is stopping")
            for jid in self.live:
                try:
                    existing = self.get(owner, jid)
                    if existing["project_id"] == project:
                        return existing
                except FileNotFoundError:
                    pass
            if len(self.live) >= self._max_pending:
                raise SubtitleJobQueueFull("Voice job queue is full")
            document = self.store.get_document(owner, project)
            if document.profile.model_id != runtime_status.get("model_id", MODEL_ID):
                raise ValueError("Engine vừa thay đổi. Thử tạo giọng lại.")
            manifest = self.manifest(owner, document, device, clip_ids)
            jid = uuid.uuid4().hex[:20]
            if subtitle_document is not None:
                sync_session_id = sync_session_id or uuid.uuid4().hex
                sync_root = self.store.owner_root(owner) / 'sync-generation' / sync_session_id
                if not (sync_root / 'input.json').exists():
                    write_json(sync_root / 'input.json', {'document': subtitle_document,
                        'clip_ids': [item['id'] for item in manifest['clips']]})
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
                "sync_session_id": sync_session_id,
            }
            write_json(self.store.path(owner, "jobs", jid), job)
            self.live.add(jid)
            future = self.executor.submit(self._run, owner, job, manifest)
            self._futures[jid] = future
            future.add_done_callback(lambda result: self._retire(owner, job, result))
            return job.copy()

    def _retire(self, owner, job, future):
        with self.store.lock:
            jid = job["id"]
            if future.cancelled():
                job.update(state="canceled", message="Đã hủy trước khi tạo giọng")
                write_json(self.store.path(owner, "jobs", jid), job)
            self._futures.pop(jid, None)
            self.live.discard(jid)
            self.controls.pop(jid, None)

    def control(self, owner: str, jid: str, action: str):
        with self.store.lock:
            job = self.get(owner, jid)
            if action == "resume":
                if jid in self.live:
                    return job
                return self.start(
                    owner, job["project_id"], job["device"], job["clip_ids"], sync_session_id=job.get('sync_session_id')
                )
            if jid in self.live:
                self.controls[jid] = action
                job["message"] = (
                    "Đang tạm dừng và giữ các đoạn đã lưu…"
                    if action == "pause"
                    else "Đang hủy…"
                )
                write_json(self.store.path(owner, "jobs", jid), job)
            return job

    def _recover_checkpoints(self, owner, manifest):
        """Recover committed audio missed by a previous supervisor, without inference."""
        missing = {
            item["generation_hash"] for item in manifest["clips"]
            if not self.store.path(owner, "assets", item["generation_hash"]).exists()
            or not self.store.path(owner, "assets", item["generation_hash"], ".wav").exists()
        }
        if not missing:
            return
        for sidecar in (self.store.owner_root(owner) / "work").glob("*/assets/*.json"):
            key = sidecar.stem
            if key not in missing:
                continue
            try:
                committed = read_json(sidecar)
                wav = sidecar.with_suffix(".wav")
                meta = audio_metadata(wav)
                if committed.get("generation_hash") != key or committed.get("checksum") != meta["checksum"]:
                    continue
                meta.update(id=key, generation_hash=key, device=manifest["device"])
                dest = self.store.path(owner, "assets", key, ".wav")
                dest.parent.mkdir(parents=True, exist_ok=True)
                temporary = dest.with_name(f"{key}.{uuid.uuid4().hex}.part")
                shutil.copyfile(wav, temporary)
                temporary.replace(dest)
                write_json(self.store.path(owner, "assets", key), meta)
                missing.remove(key)
                if not missing:
                    break
            except (OSError, ValueError, KeyError, EOFError, wave.Error):
                continue  # Partial/corrupt pairs must be generated again.

    def _run(self, owner, job, manifest):
        jid = job["id"]
        process = None
        heartbeat = None
        sync_started = False
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
            self._recover_checkpoints(owner, manifest)
            cached_assets = {}
            verified = {}
            current_clips = {clip.id: clip for clip in self.store.get_document(owner, job['project_id']).clips}
            for item in manifest["clips"]:
                key = item["generation_hash"]
                current_clip = current_clips.get(item['id'])
                # Processed or same-text retry assets have their own immutable ID.
                # Resume must preserve a valid attached waveform instead of reverting to raw cache.
                asset_key = current_clip.asset_id if current_clip and current_clip.asset_id and current_clip.generation_hash == key else key
                meta_path = self.store.path(owner, "assets", asset_key)
                wav_path = self.store.path(owner, "assets", asset_key, ".wav")
                if meta_path.exists() and wav_path.exists():
                    try:
                        meta = verified.get(asset_key) or read_json(meta_path)
                        valid = meta['generation_hash'] == key and (asset_key in verified or audio_metadata(wav_path)["checksum"] == meta["checksum"])
                    except (OSError, ValueError, KeyError, EOFError, wave.Error):
                        valid = False
                    if valid:
                        verified[asset_key] = meta
                        cached_assets[item["id"]] = meta
                        attached.add(item["id"])
                        continue
                pending.append(item)
            self.store.attach_many(owner, job["project_id"], cached_assets)
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
                heartbeat_path = root / "supervisor.heartbeat"
                heartbeat = keep_worker_alive(heartbeat_path)
                env = dict(os.environ, PYTHONUTF8="1", HF_HUB_DISABLE_TELEMETRY="1",
                           VOICE_SUPERVISOR_HEARTBEAT=str(heartbeat_path.resolve()),
                           VOICE_WORKER_LOCK=str((RUNTIME / ".worker.lock").resolve()))
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
                        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
                        if os.name == "nt"
                        else 0,
                    )
                    with self.store.lock:
                        self._processes[jid] = process
                        if not self._accepting and process.poll() is None:
                            stop_worker(process)
                    last_change = time.monotonic()
                    deadline = WorkerDeadline(last_change)
                    previous = None
                    items_by_id = {item["id"]: item for item in pending}
                    generation_started = None
                    while True:
                        worker_exited = process.poll() is not None
                        action = self.controls.get(jid)
                        if not action and control_path.exists():
                            external_action = read_json(control_path).get("action")
                            if external_action in {"pause", "cancel"}:
                                action = self.controls[jid] = external_action
                        if action and (
                            not control_path.exists()
                            or read_json(control_path).get("action") != action
                        ):
                            write_json(control_path, {"action": action})
                        if action in {"pause", "cancel"} and process.poll() is None:
                            stop_worker(process)
                        if progress_path.exists():
                            progress = read_json(progress_path)
                            deadline.observe(progress, time.monotonic())
                            if progress != previous:
                                previous = progress
                                last_change = time.monotonic()
                            job["message"] = progress.get("message", "Đang tạo giọng")
                            job["failed"] = progress.get("failed", [])
                            job["current_clip_id"] = progress.get("clip_id") if progress.get("stage") == "generating" else None
                            if generation_started is None and progress.get("stage") == "generating":
                                generation_started = time.monotonic()
                            # The worker publishes completed IDs only after its sidecar is committed.
                            ready_ids = set(progress.get("completed", [])) - attached
                            if worker_exited:
                                # A crash may land between committing a sidecar and publishing progress.
                                ready_ids.update(items_by_id.keys() - attached)
                            new_assets = {}
                            for cid in ready_ids:
                                item = items_by_id.get(cid)
                                if item is None:
                                    continue
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
                                new_assets[cid] = meta
                                attached.add(cid)
                            self.store.attach_many(owner, job["project_id"], new_assets)
                            job["completed"] = len(attached)
                            job["completed_clip_ids"] = sorted(attached)
                        job["elapsed_seconds"] = round(time.monotonic() - started, 1)
                        generated_count = len(attached) - cached_count
                        if generated_count > 0 and generation_started is not None:
                            job["eta_seconds"] = round(
                                (time.monotonic() - generation_started)
                                / generated_count
                                * (job["total"] - len(attached))
                            )
                        else:
                            job["eta_seconds"] = None
                        persist()
                        if process.poll() is not None:
                            if not worker_exited:
                                continue  # Read final progress/checkpoints once after exit.
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
            if state in {'succeeded', 'failed'} and not action and job.get('sync_session_id') and self.sync_runner:
                # The first TTS worker has exited; run CPU checks and bounded repairs sequentially.
                # Keep this job active so pause/cancel/shutdown cover the complete pipeline.
                if heartbeat is not None:
                    heartbeat.set()
                    heartbeat = None
                job.update(state='running', phase='sync', message='Đang kiểm tra độ khớp sau tạo giọng', eta_seconds=None)
                persist()
                sync_started = True
                self.sync_runner(self, owner, job, persist)
        except Exception as exc:
            job.update(state="failed", message=str(exc)[-2000:])
            if job.get('sync_session_id') and self.sync_runner and not sync_started and not self.controls.get(jid):
                # A partial first pass can still recover missing clips within the separate repair budget.
                # Release a failed/stuck original worker before any repair worker starts.
                if heartbeat is not None:
                    heartbeat.set()
                    heartbeat = None
                if process is not None and process.poll() is None:
                    stop_worker(process)
                    process.wait(timeout=10)
                job.update(state='running', phase='sync', message='Đang kiểm tra và bổ sung các đoạn còn thiếu', eta_seconds=None)
                persist()
                try:
                    self.sync_runner(self, owner, job, persist)
                except Exception as sync_error:
                    job.update(state='failed', message=str(sync_error)[-2000:])
        finally:
            if heartbeat is not None:
                heartbeat.set()
            job["current_clip_id"] = None
            if process is not None and process.poll() is None:
                stop_worker(process)
                process.wait(timeout=10)
            persist()
            with self.store.lock:
                self._processes.pop(jid, None)
                self.live.discard(jid)
                self.controls.pop(jid, None)

    def stop_accepting(self):
        with self.store.lock:
            self._accepting = False

    def shutdown(self, *, timeout_seconds: float = 10):
        with self.store.lock:
            self._accepting = False
            for jid in list(self.live):
                self.controls[jid] = "cancel"
            futures = list(self._futures.values())
            for future in futures:
                future.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)
        unfinished = [future for future in futures if not future.done()]
        if unfinished:
            _, pending = wait(unfinished, timeout=max(0, timeout_seconds))
            if pending:
                with self.store.lock:
                    processes = list(self._processes.values())
                for process in processes:
                    if process.poll() is None:
                        stop_worker(process)
                for process in processes:
                    process.wait(timeout=5)
                _, pending = wait(pending, timeout=5)
                if pending:
                    raise TimeoutError(f"{len(pending)} voice job(s) exceeded the shutdown deadline")
