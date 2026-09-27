from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

JobKind = Literal["alignment", "generation", "render", "review", "ocr", "asr", "translation", "separation", "scene"]
JobState = Literal["queued", "running", "succeeded", "failed", "canceled"]
JobRunner = Callable[["SubtitleJobContext"], dict[str, Any]]
logger = logging.getLogger(__name__)


class SubtitleJobCanceled(RuntimeError):
    pass


class SubtitleJobQueueFull(RuntimeError):
    pass


class SubtitleJobStorageError(OSError):
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
    details: dict[str, Any] = field(default_factory=dict)
    source_binding: dict[str, str] = field(default_factory=dict)

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

    def update_details(self, details: dict[str, Any]) -> None:
        self.raise_if_canceled()
        self._manager._update_details(self.job_id, details)


class SubtitleJobManager:
    def __init__(self, job_dir: Path, *, max_workers: int = 1, max_pending: int = 32, max_cached: int = 128,
                 recoverable_kinds: tuple[JobKind, ...] = ()) -> None:
        self.job_dir = job_dir
        self._lock = threading.RLock()
        self._records: dict[str, SubtitleJobRecord] = {}
        self._dedupe: dict[tuple[JobKind, str], str] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._futures: dict[str, Future[None]] = {}
        self._last_persist_at: dict[str, float] = {}
        self._unpersisted: set[str] = set()
        self._dedupe_available = True
        self._max_pending = max(1, max_pending)
        self._max_cached = max(0, max_cached)
        self._accepting = True
        self._recoverable_kinds = recoverable_kinds
        self._suspending: set[str] = set()
        self._recovery_queue: dict[str, tuple[SubtitleJobRecord, JobRunner]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="subtitle-job",
        )
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.job_dir.exists():
            return
        try:
            with self._dedupe_connection() as connection:
                self._load_records(connection)
        except (OSError, sqlite3.Error):
            self._dedupe_available = False
            logger.warning("Job dedupe index unavailable; using job JSON history", exc_info=True)
            self._load_records(None)
        self._last_persist_at.clear()

    def _load_records(self, connection) -> None:
        for path in self.job_dir.glob("*.json"):
            record = self._records.get(path.stem) or self._read_record(path.stem)
            if not record:
                continue
            if record.state in {"queued", "running"}:
                record.state = "failed"
                record.phase = "interrupted"
                record.message = "Job bị gián đoạn khi API khởi động lại"
                record.error = "API process restarted before the job completed"
                record.finished_at = _now_iso()
                record.updated_at = record.finished_at
                self._persist_terminal(record)
            self._records[record.id] = record
            if record.state in {"queued", "running", "succeeded"}:
                self._dedupe[(record.kind, record.dedupe_key)] = record.id
            if record.state == "succeeded" and connection is not None:
                self._write_dedupe(record, connection)
            self._trim_records()

    @contextmanager
    def _dedupe_connection(self):
        # Rebuildable index: job JSON remains the durable source of truth.
        # One transaction for history recovery avoids thousands of small files.
        self.job_dir.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.job_dir / ".dedupe.sqlite3")) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS dedupe (kind TEXT, key TEXT, job_id TEXT NOT NULL, PRIMARY KEY (kind, key))"
            )
            yield connection

    def _write_dedupe(self, record: SubtitleJobRecord, connection=None) -> None:
        if connection is None:
            with self._dedupe_connection() as current:
                self._write_dedupe(record, current)
            return
        connection.execute(
            "INSERT INTO dedupe (kind, key, job_id) VALUES (?, ?, ?) "
            "ON CONFLICT (kind, key) DO UPDATE SET job_id=excluded.job_id "
            "WHERE dedupe.job_id != excluded.job_id",
            (record.kind, record.dedupe_key, record.id),
        )

    def _delete_dedupe_entry(self, kind: JobKind, key: str, job_id: str) -> None:
        if not self._dedupe_available:
            return
        try:
            with self._dedupe_connection() as connection:
                connection.execute(
                    "DELETE FROM dedupe WHERE kind=? AND key=? AND job_id=?",
                    (kind, key, job_id),
                )
        except (OSError, sqlite3.Error):
            self._dedupe_available = False
            logger.warning("Could not invalidate a stale job dedupe entry", exc_info=True)

    @staticmethod
    def _is_retryable_ocr_fallback(record: SubtitleJobRecord) -> bool:
        if record.kind != "ocr" or record.state != "succeeded" or not record.result:
            return False
        runtime = record.result.get("runtime")
        return bool(
            isinstance(runtime, dict)
            and runtime.get("effective_device") == "cpu"
            and runtime.get("fallback_reason")
        )

    def _read_dedupe(self, kind: JobKind, key: str) -> str | None:
        if not self._dedupe_available:
            return self._scan_dedupe(kind, key)
        if not (self.job_dir / ".dedupe.sqlite3").exists():
            return None
        try:
            with self._dedupe_connection() as connection:
                row = connection.execute(
                    "SELECT job_id FROM dedupe WHERE kind=? AND key=?", (kind, key)
                ).fetchone()
        except (OSError, sqlite3.Error):
            self._dedupe_available = False
            return self._scan_dedupe(kind, key)
        return row[0] if row else None

    def _scan_dedupe(self, kind: JobKind, key: str) -> str | None:
        # Slower degraded mode avoids rerunning paid work when the index fails.
        latest = None
        for path in self.job_dir.glob("*.json"):
            record = self._records.get(path.stem) or self._read_record(path.stem)
            if (record and record.kind == kind and record.dedupe_key == key
                    and record.state == "succeeded"
                    and (latest is None or record.created_at > latest.created_at)):
                latest = record
        return latest.id if latest else None

    def _read_record(self, job_id: str) -> SubtitleJobRecord | None:
        if not re.fullmatch(r"[a-f0-9]{20}", job_id):
            return None
        try:
            record = SubtitleJobRecord(**json.loads((self.job_dir / f"{job_id}.json").read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        return record if record.id == job_id else None

    def _trim_records(self) -> None:
        completed = [job_id for job_id, record in self._records.items() if record.state not in {"queued", "running"} and job_id not in self._futures and job_id not in self._unpersisted]
        for job_id in completed[:max(0, len(completed) - self._max_cached)]:
            record = self._records.pop(job_id)
            if self._dedupe.get((record.kind, record.dedupe_key)) == job_id:
                self._dedupe.pop((record.kind, record.dedupe_key), None)
            self._last_persist_at.pop(job_id, None)

    def _retire(self, job_id: str, future: Future) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if future.cancelled() and record and record.state == "queued":
                self._finish_canceled(record)
            self._futures.pop(job_id, None)
            self._cancel_events.pop(job_id, None)
            self._last_persist_at.pop(job_id, None)
            self._suspending.discard(job_id)
            self._drain_recovery()
            self._trim_records()

    def recover_interrupted(self, runner_factory: Callable[[dict[str, Any]], JobRunner]) -> None:
        """Restore the latest attempt per input, keeping its ID and checkpoint binding.

        Factories only construct runners; validation and media I/O run in workers.
        Older interrupted attempts superseded by a retry must never run again.
        """
        with self._lock:
            if not self._accepting:
                return
            latest: dict[tuple[JobKind, str], SubtitleJobRecord] = {}
            for path in self.job_dir.glob("*.json"):
                record = self._records.get(path.stem) or self._read_record(path.stem)
                if not record or record.kind not in self._recoverable_kinds:
                    continue
                key = (record.kind, record.dedupe_key)
                if key not in latest or record.created_at > latest[key].created_at:
                    latest[key] = record
            for record in sorted(latest.values(), key=lambda item: item.created_at):
                if record.phase != "interrupted" or record.cancel_requested:
                    continue
                if record.id in self._futures or record.id in self._recovery_queue:
                    continue
                runner = runner_factory(record.snapshot())
                interrupted = deepcopy(record)
                record.state = "queued"
                record.phase = "resuming"
                record.message = "Đang tự tiếp tục các đoạn còn thiếu sau khi API khởi động lại"
                record.error = None
                record.finished_at = None
                record.updated_at = _now_iso()
                try:
                    self._write_record(record)
                except OSError:
                    self._records[record.id] = interrupted
                    self._persist_terminal(interrupted)
                    continue
                self._records[record.id] = record
                self._dedupe[(record.kind, record.dedupe_key)] = record.id
                self._recovery_queue[record.id] = (record, runner)
            self._drain_recovery()

    def _drain_recovery(self) -> None:
        while self._accepting and self._recovery_queue and len(self._futures) < self._max_pending:
            job_id = next(iter(self._recovery_queue))
            record, runner = self._recovery_queue.pop(job_id)
            if record.cancel_requested:
                self._finish_canceled(record)
                continue
            cancel_event = threading.Event()
            self._cancel_events[job_id] = cancel_event
            future = self._executor.submit(self._execute, job_id, runner, cancel_event)
            self._futures[job_id] = future
            future.add_done_callback(lambda finished, identifier=job_id: self._retire(identifier, finished))

    def _write_record(self, record: SubtitleJobRecord) -> None:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        target = self.job_dir / f"{record.id}.json"
        temporary = target.with_suffix(".json.part")
        payload = record.snapshot()
        payload["details"].pop("state_persistence_error", None)
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Cleanup must not mask the original write failure.
        record.details.pop("state_persistence_error", None)
        self._unpersisted.discard(record.id)
        self._last_persist_at[record.id] = time.monotonic()

    def _persist_terminal(self, record: SubtitleJobRecord) -> None:
        """Keep polling/cancellation truthful even when durable storage fails.

        Retain unsaved records until a subsequent read can repair their JSON.
        This cannot guarantee persistence across process death on a full disk.
        """
        try:
            self._write_record(record)
        except OSError as exc:
            record.details["state_persistence_error"] = str(exc)[:2000]
            self._unpersisted.add(record.id)
            self._records[record.id] = record

    def _finish_failed(self, record: SubtitleJobRecord, exc: Exception) -> None:
        record.state = "failed"
        record.phase = "failed"
        record.message = "Xử lý thất bại"
        record.error = str(exc)[:2000]
        record.result = None
        record.finished_at = _now_iso()
        record.updated_at = record.finished_at
        self._dedupe.pop((record.kind, record.dedupe_key), None)
        self._persist_terminal(record)

    def submit(
        self,
        kind: JobKind,
        dedupe_key: str,
        runner: JobRunner,
        details: dict[str, Any] | None = None,
        *, source_binding: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if not self._accepting:
                raise SubtitleJobQueueFull("Job manager is stopping")
            for identifier in list(self._unpersisted):
                self._persist_terminal(self._records[identifier])
                if identifier not in self._futures:
                    self._last_persist_at.pop(identifier, None)
            self._trim_records()
            existing_id = self._dedupe.get((kind, dedupe_key))
            if not existing_id:
                existing_id = self._read_dedupe(kind, dedupe_key)
            if existing_id:
                existing = self._records.get(existing_id) or self._read_record(existing_id)
                if existing and existing.state in {"queued", "running", "succeeded"}:
                    if self._is_retryable_ocr_fallback(existing):
                        # A completed CPU fallback must not permanently mask a
                        # later retry after CUDA recovers without a package
                        # change. Active requests still dedupe while running.
                        self._dedupe.pop((kind, dedupe_key), None)
                        self._delete_dedupe_entry(kind, dedupe_key, existing.id)
                    else:
                        return existing.snapshot()

            if len(self._futures.keys() | self._unpersisted) >= self._max_pending:
                raise SubtitleJobQueueFull("Job queue is full; wait for an active job to finish")

            job_id = uuid.uuid4().hex[:20]
            record = SubtitleJobRecord(
                id=job_id,
                kind=kind,
                dedupe_key=dedupe_key,
                details=deepcopy(details or {}),
                source_binding=deepcopy(source_binding or {}),
            )
            cancel_event = threading.Event()
            # Failed admission must not leave a dedupe entry with no worker.
            try:
                self._write_record(record)
            except OSError as exc:
                raise SubtitleJobStorageError(exc.errno, str(exc)) from exc
            self._records[job_id] = record
            self._dedupe[(kind, dedupe_key)] = job_id
            self._cancel_events[job_id] = cancel_event
            self._futures[job_id] = self._executor.submit(
                self._execute,
                job_id,
                runner,
                cancel_event,
            )
            self._futures[job_id].add_done_callback(lambda future: self._retire(job_id, future))
            return record.snapshot()

    def _execute(
        self,
        job_id: str,
        runner: JobRunner,
        cancel_event: threading.Event,
    ) -> None:
        context = SubtitleJobContext(self, job_id, cancel_event)
        try:
            with self._lock:
                record = self._records[job_id]
                context.raise_if_canceled()
                record.state = "running"
                record.phase = "starting"
                record.message = "Đang khởi động"
                record.started_at = _now_iso()
                record.updated_at = record.started_at
                self._write_record(record)
            result = runner(context)
            with self._lock:
                context.raise_if_canceled()
                record.state = "succeeded"
                record.progress = 100
                record.phase = "complete"
                record.message = "Hoàn tất"
                record.result = result
                record.finished_at = _now_iso()
                record.updated_at = record.finished_at
                self._write_record(record)
                try:
                    self._write_dedupe(record)
                except (OSError, sqlite3.Error):
                    self._dedupe_available = False
                    logger.warning("Job result saved; dedupe index update failed", exc_info=True)
        except SubtitleJobCanceled:
            with self._lock:
                self._finish_canceled(self._records[job_id])
            return
        except Exception as exc:
            with self._lock:
                record = self._records[job_id]
                if job_id in self._suspending and not record.cancel_requested:
                    self._finish_canceled(record)
                    return
                self._finish_failed(record, exc)
            return

    def _finish_canceled(self, record: SubtitleJobRecord) -> None:
        if record.id in self._suspending and not record.cancel_requested:
            record.state = "queued"
            record.phase = "interrupted"
            record.message = "Sẽ tự tiếp tục các đoạn còn thiếu khi API khởi động lại"
            record.error = None
            record.finished_at = None
            record.updated_at = _now_iso()
            self._dedupe.pop((record.kind, record.dedupe_key), None)
            self._persist_terminal(record)
            return
        record.state = "canceled"
        record.phase = "canceled"
        record.message = "Đã hủy"
        record.cancel_requested = True
        record.finished_at = _now_iso()
        record.updated_at = record.finished_at
        self._dedupe.pop((record.kind, record.dedupe_key), None)
        self._persist_terminal(record)

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

    def _update_details(self, job_id: str, details: dict[str, Any]) -> None:
        with self._lock:
            record = self._records[job_id]
            if record.state == "running" and not record.cancel_requested:
                record.details = deepcopy(details)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(job_id) or self._read_record(job_id)
            if record and job_id in self._unpersisted:
                self._persist_terminal(record)
                if job_id not in self._futures:
                    self._last_persist_at.pop(job_id, None)
                self._trim_records()
            return record.snapshot() if record else None

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(job_id) or self._read_record(job_id)
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
            if job_id in self._recovery_queue:
                self._recovery_queue.pop(job_id)
                self._finish_canceled(record)
                return record.snapshot()
            if record.state == "queued" and future and future.cancel():
                return record.snapshot()  # The done callback persists cancellation.
            else:
                self._persist_terminal(record)
            return record.snapshot()

    def stop_accepting(self) -> None:
        with self._lock:
            self._accepting = False

    def shutdown(self, *, wait: bool = True, timeout_seconds: float = 10) -> None:
        with self._lock:
            self._accepting = False
            for job_id, record in list(self._records.items()):
                if record.state in {"queued", "running"}:
                    if record.kind in self._recoverable_kinds and not record.cancel_requested:
                        self._suspending.add(job_id)
                        # Persist before signaling workers, including a forced exit
                        # while an external API request is still winding down.
                        self._finish_canceled(record)
                    cancel_event = self._cancel_events.get(job_id)
                    if cancel_event:
                        cancel_event.set()
            self._recovery_queue.clear()
            futures = list(self._futures.values())
            for future in futures:
                future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
        unfinished = [future for future in futures if not future.done()]
        if wait and unfinished:
            _, pending = wait_futures(unfinished, timeout=max(0, timeout_seconds))
            if pending:
                raise TimeoutError(f"{len(pending)} subtitle job(s) did not stop before the shutdown deadline")
        if wait:
            # Future.done()/wait() precede done callbacks. Finalize under the same
            # lock so shutdown cannot return with completed runtime state retained.
            with self._lock:
                for job_id, future in list(self._futures.items()):
                    if future.done():
                        self._retire(job_id, future)
