from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

JobKind = Literal["alignment", "generation", "render"]
JobState = Literal["queued", "running", "succeeded", "failed", "canceled"]
JobRunner = Callable[["SubtitleJobContext"], dict[str, Any]]


class SubtitleJobCanceled(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class SubtitleJobRecord:
    id: str
    kind: JobKind
    dedupe_key: str
    state: JobState = "queued"
    progress: int = 0
    phase: str = "queued"
    message: str = "Đang chờ xử lý"
    cancel_requested: bool = False
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.__dict__)


class SubtitleJobContext:
    def __init__(
        self,
        manager: SubtitleJobManager,
        job_id: str,
        cancel_event: threading.Event,
    ) -> None:
        self._manager = manager
        self.job_id = job_id
        self.cancel_event = cancel_event

    def update(self, progress: int, phase: str, message: str) -> None:
        self.raise_if_canceled()
        self._manager._update_progress(self.job_id, progress, phase, message)

    def raise_if_canceled(self) -> None:
        if self.cancel_event.is_set():
            raise SubtitleJobCanceled("Job was canceled")


class SubtitleJobManager:
    def __init__(self, job_dir: Path, *, max_workers: int = 1) -> None:
        self.job_dir = job_dir
        self._lock = threading.RLock()
        self._records: dict[str, SubtitleJobRecord] = {}
        self._dedupe: dict[tuple[JobKind, str], str] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._futures: dict[str, Future[None]] = {}
        self._last_persist_at: dict[str, float] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="subtitle-job",
        )
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.job_dir.exists():
            return
        for path in self.job_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                record = SubtitleJobRecord(**payload)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            if record.state in {"queued", "running"}:
                record.state = "failed"
                record.phase = "interrupted"
                record.message = "Job bị gián đoạn khi API khởi động lại"
                record.error = "API process restarted before the job completed"
                record.finished_at = _now_iso()
                record.updated_at = record.finished_at
                self._write_record(record)
            self._records[record.id] = record
            if record.state in {"queued", "running", "succeeded"}:
                self._dedupe[(record.kind, record.dedupe_key)] = record.id

    def _write_record(self, record: SubtitleJobRecord) -> None:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        target = self.job_dir / f"{record.id}.json"
        temporary = target.with_suffix(".json.part")
        temporary.write_text(
            json.dumps(record.snapshot(), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(target)
        self._last_persist_at[record.id] = time.monotonic()

    def submit(
        self,
        kind: JobKind,
        dedupe_key: str,
        runner: JobRunner,
    ) -> dict[str, Any]:
        with self._lock:
            existing_id = self._dedupe.get((kind, dedupe_key))
            if existing_id:
                existing = self._records.get(existing_id)
                if existing and existing.state in {"queued", "running", "succeeded"}:
                    return existing.snapshot()

            job_id = uuid.uuid4().hex[:20]
            record = SubtitleJobRecord(id=job_id, kind=kind, dedupe_key=dedupe_key)
            cancel_event = threading.Event()
            self._records[job_id] = record
            self._dedupe[(kind, dedupe_key)] = job_id
            self._cancel_events[job_id] = cancel_event
            self._write_record(record)
            self._futures[job_id] = self._executor.submit(
                self._execute,
                job_id,
                runner,
                cancel_event,
            )
            return record.snapshot()

    def _execute(
        self,
        job_id: str,
        runner: JobRunner,
        cancel_event: threading.Event,
    ) -> None:
        with self._lock:
            record = self._records[job_id]
            if cancel_event.is_set():
                self._finish_canceled(record)
                return
            record.state = "running"
            record.phase = "starting"
            record.message = "Đang khởi động"
            record.started_at = _now_iso()
            record.updated_at = record.started_at
            self._write_record(record)

        context = SubtitleJobContext(self, job_id, cancel_event)
        try:
            result = runner(context)
            context.raise_if_canceled()
        except SubtitleJobCanceled:
            with self._lock:
                self._finish_canceled(self._records[job_id])
            return
        except Exception as exc:
            with self._lock:
                record = self._records[job_id]
                record.state = "failed"
                record.phase = "failed"
                record.message = "Xử lý thất bại"
                record.error = str(exc)[:2000]
                record.finished_at = _now_iso()
                record.updated_at = record.finished_at
                self._write_record(record)
                self._dedupe.pop((record.kind, record.dedupe_key), None)
            return

        with self._lock:
            record = self._records[job_id]
            record.state = "succeeded"
            record.progress = 100
            record.phase = "complete"
            record.message = "Hoàn tất"
            record.result = result
            record.finished_at = _now_iso()
            record.updated_at = record.finished_at
            self._write_record(record)

    def _finish_canceled(self, record: SubtitleJobRecord) -> None:
        record.state = "canceled"
        record.phase = "canceled"
        record.message = "Đã hủy"
        record.cancel_requested = True
        record.finished_at = _now_iso()
        record.updated_at = record.finished_at
        self._write_record(record)
        self._dedupe.pop((record.kind, record.dedupe_key), None)

    def _update_progress(
        self,
        job_id: str,
        progress: int,
        phase: str,
        message: str,
    ) -> None:
        with self._lock:
            record = self._records[job_id]
            if record.state != "running":
                return
            record.progress = max(record.progress, min(99, max(0, progress)))
            record.phase = phase[:64]
            record.message = message[:500]
            record.updated_at = _now_iso()
            last_persist = self._last_persist_at.get(job_id, 0.0)
            if time.monotonic() - last_persist >= 0.2:
                self._write_record(record)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(job_id)
            return record.snapshot() if record else None

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(job_id)
            if not record:
                return None
            if record.state in {"succeeded", "failed", "canceled"}:
                return record.snapshot()
            record.cancel_requested = True
            record.message = "Đang hủy an toàn…"
            record.updated_at = _now_iso()
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event:
                cancel_event.set()
            future = self._futures.get(job_id)
            if record.state == "queued" and future and future.cancel():
                self._finish_canceled(record)
            else:
                self._write_record(record)
            return record.snapshot()

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            for job_id, record in self._records.items():
                if record.state in {"queued", "running"}:
                    cancel_event = self._cancel_events.get(job_id)
                    if cancel_event:
                        cancel_event.set()
        self._executor.shutdown(wait=wait, cancel_futures=True)
