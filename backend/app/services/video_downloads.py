"""Durable video download queue with isolated, cancellable downloader processes."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack, contextmanager, suppress
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..config import settings
from ..crawlers.runtime import normalize_account_ref
from .subtitle_jobs import SubtitleJobQueueFull

ACTIVE = {"queued", "running"}
EXTENSIONS = {".mp4", ".webm", ".mkv"}
JOB_SCHEMA_VERSION = 2
PUBLICATION_MANIFEST_SCHEMA_VERSION = 1


def now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_video_url(value: str) -> str:
    value = value.strip()
    if len(value) > 4096 or any(ord(char) < 32 for char in value):
        raise ValueError("Link video không hợp lệ.")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        if (parsed.scheme not in {"https", "http"} or not host or "." not in host
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}
                or host.endswith((".localhost", ".local", ".internal"))):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Hãy nhập link http/https của video trên Internet.") from exc
    return urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))


def platform_name(url: str) -> str:
    host = urlsplit(url).hostname or ""
    for name, roots in {
        "Bilibili": ("bilibili.com", "b23.tv", "bilibili.tv"),
        "YouTube": ("youtube.com", "youtu.be"),
        "TikTok": ("tiktok.com",), "Douyin": ("douyin.com",),
        "Facebook": ("facebook.com", "fb.watch"), "Instagram": ("instagram.com",),
        "X": ("x.com", "twitter.com"), "Vimeo": ("vimeo.com",),
    }.items():
        if any(host == root or host.endswith("." + root) for root in roots):
            return name
    return host.removeprefix("www.")


@contextmanager
def _exclusive_file_lock(path: Path, *, timeout_seconds: float = 30):
    """Coordinate durable JSON mutations between separate app processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise SubtitleJobQueueFull(
                        "Trạng thái hàng đợi video đang được process khác cập nhật. Vui lòng thử lại."
                    ) from error
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_UN)


def _atomic_json_write(path: Path, payload: Mapping[str, object]) -> None:
    """Write one JSON document without exposing a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink()


class VideoDownloadManager:
    def __init__(self, root: Path, *, max_workers: int = 2, max_pending: int = 32):
        self.root = root.resolve()
        self.jobs_dir = self.root / "downloads" / "jobs"
        self.state_lock_path = self.jobs_dir / ".manager.lock"
        self.work_root = self.root / "downloads" / "work"
        self.upload_dir = self.root / "upload"
        self._lock = threading.RLock()
        self._state_lock_local = threading.local()
        self._records: dict[str, dict] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._futures = {}
        self._accepting = True
        self._started = False
        self._max_pending = max_pending
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="video-download")
        with self._state_lock():
            self._refresh_records_locked()
            self._reconcile_publications_locked()
            for record in self._records.values():
                if record["state"] in ACTIVE or record.get("phase") == "interrupted":
                    record.update(state="paused", phase="interrupted", error=None, speed=None, eta=None, updated_at=now())
                    self._save(record)

    @contextmanager
    def _state_lock(self):
        """Re-enterable per thread, exclusive across application processes."""
        depth = getattr(self._state_lock_local, "depth", 0)
        if depth:
            self._state_lock_local.depth = depth + 1
            try:
                yield
            finally:
                self._state_lock_local.depth -= 1
            return
        with _exclusive_file_lock(self.state_lock_path):
            self._state_lock_local.depth = 1
            try:
                yield
            finally:
                self._state_lock_local.depth = 0

    @staticmethod
    def _read_record(path: Path) -> dict | None:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(record, dict)
                or not re.fullmatch(r"[a-f0-9]{32}", record["id"])
                or record["id"] != path.stem
                or not isinstance(record.get("state"), str)
            ):
                return None
            # Legacy job JSON has no version; it remains readable and is
            # upgraded only when the record is next persisted.
            record.setdefault("schema_version", 1)
            return record
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _refresh_records_locked(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        for path in self.jobs_dir.glob("*.json"):
            record = self._read_record(path)
            if record is not None:
                current = self._records.get(record["id"])
                # A worker thread owns its in-memory record until its future
                # retires; replacing that dict during a second submit would
                # make the worker finish one object while list() observes a
                # stale copy.  Its atomic JSON writes are still visible to a
                # new process on the next refresh.
                if current is not None and record["id"] in self._futures:
                    continue
                if current is None:
                    self._records[record["id"]] = record
                else:
                    current.clear()
                    current.update(record)

    def _publication_manifest_path(self, job_id: str) -> Path:
        return self._work_directory(job_id) / ".publication.json"

    def _reconcile_publications_locked(self) -> None:
        """Finish a publication whose file move won the race with a crash."""
        for record in self._records.values():
            try:
                manifest_path = self._publication_manifest_path(record["id"])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                filename = manifest.get("filename")
                if (
                    manifest.get("schema_version") != PUBLICATION_MANIFEST_SCHEMA_VERSION
                    or manifest.get("job_id") != record["id"]
                    or not isinstance(filename, str)
                    or Path(filename).name != filename
                    or Path(filename).suffix.lower() not in EXTENSIONS
                    or filename != f"{record['id']}{Path(filename).suffix}"
                ):
                    raise ValueError("invalid publication manifest")
                target = (self.upload_dir / filename).resolve()
                if target.parent != self.upload_dir.resolve() or not target.is_file() or target.stat().st_size <= 0:
                    continue
                record.update(
                    state="succeeded",
                    phase="complete",
                    progress=100,
                    filename=filename,
                    title=str(manifest.get("title") or "Video")[:500],
                    duration=manifest.get("duration"),
                    speed=None,
                    eta=None,
                    error=None,
                    updated_at=now(),
                )
                self._save(record)
                manifest_path.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def start(self) -> None:
        """Recover only after application startup, never while importing routes."""
        with self._lock:
            if self._started or not self._accepting:
                return
            self._started = True
            with self._state_lock():
                self._refresh_records_locked()
                self._reconcile_publications_locked()
                for record in sorted(self._records.values(), key=lambda item: item["created_at"]):
                    if record["state"] == "paused" and record["phase"] == "interrupted":
                        self._resume_recovered(record)

    def _resume_recovered(self, record: dict) -> None:
        if record.get("uses_cookies"):
            record.update(phase="needs_cookies", error="Đã giữ file tải dở. Chọn lại file cookies rồi bấm Tiếp tục.")
            self._save(record)
        elif record.get("uses_connection") and len(self._futures) < self._max_pending:
            try:
                self._schedule(record, None, record.get("connection_id"))
            except ValueError:
                record.update(
                    phase="needs_connection",
                    error="Connection profile không còn hợp lệ. Chọn lại profile rồi thử lại.",
                    updated_at=now(),
                )
                self._save(record)
        elif len(self._futures) < self._max_pending:
            self._schedule(record, None, None)

    @staticmethod
    def _connection_id_for_url(
        url: str, connection_id: str | None
    ) -> str | None:
        if connection_id is None or not str(connection_id).strip():
            return None
        try:
            normalized = normalize_account_ref(connection_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Connection profile không hợp lệ.") from exc
        if len(normalized) > 128:
            raise ValueError("Connection profile tối đa 128 ký tự.")
        if platform_name(url) != "Bilibili":
            raise ValueError(
                "Connection profile hiện chỉ hỗ trợ tải Bilibili qua CBCE."
            )
        return normalized

    @staticmethod
    def _job_key(
        url: str, quality: str, connection_id: str | None
    ) -> tuple[str, str, str]:
        return (url, quality, connection_id or "")

    @staticmethod
    def _validate_intent_key(value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("Download intent key không hợp lệ.")
        value = value.strip()
        if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
            raise ValueError("Download intent key không hợp lệ.")
        return value

    def _schedule(
        self,
        record: dict,
        cookie_text: str | None,
        connection_id: str | None,
    ) -> None:
        job_id = record["id"]
        if job_id in self._futures:
            raise SubtitleJobQueueFull("Lượt tải cũ đang dừng. Vui lòng thử lại sau.")
        normalized_connection = self._connection_id_for_url(
            record["url"], connection_id
        )
        record.update(state="queued", phase="resuming" if record.get("downloaded_bytes") else "queued",
                      error=None, speed=None, eta=None, updated_at=now(),
                      uses_cookies=bool(cookie_text),
                      uses_connection=bool(normalized_connection),
                      connection_id=normalized_connection,
                      attempts=record.get("attempts", 0) + 1)
        self._save(record)
        self._futures[job_id] = self._executor.submit(
            self._run, job_id, cookie_text, normalized_connection
        )
        self._futures[job_id].add_done_callback(lambda _, key=job_id: self._retire(key))

    @staticmethod
    def _validate_cookies(cookie_text: str | None) -> None:
        if cookie_text and (len(cookie_text.encode("utf-8")) > 1_000_000 or
                            not cookie_text.lstrip().startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File"))):
            raise ValueError("File cookies phải có định dạng Netscape (.txt), tối đa 1 MB.")

    def retry(
        self,
        job_id: str,
        cookie_text: str | None = None,
        connection_id: str | None = None,
    ) -> dict | None:
        self._validate_cookies(cookie_text)
        with self._lock, self._state_lock():
            self._refresh_records_locked()
            if not self._accepting:
                raise SubtitleJobQueueFull("Video downloader is stopping")
            record = self._records.get(job_id)
            if record is None:
                return None
            if record["state"] in ACTIVE or (record["state"] == "succeeded" and
                    (self.upload_dir / record["filename"]).is_file()):
                return deepcopy(record)
            if record.get("uses_cookies") and not cookie_text:
                raise ValueError("Chọn lại file cookies đăng nhập để tiếp tục lượt tải này.")
            if len(self._futures) >= self._max_pending:
                raise SubtitleJobQueueFull("Video download queue is full")
            requested_connection = (
                record.get("connection_id")
                if connection_id is None and record.get("uses_connection")
                else connection_id
            )
            if cookie_text and requested_connection:
                raise ValueError("Chọn file cookies hoặc connection profile, không dùng đồng thời.")
            self._schedule(record, cookie_text, requested_connection)
            return deepcopy(record)

    def _work_directory(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("Invalid download job id")
        directory = (self.work_root / job_id).resolve()
        if directory.parent != self.work_root.resolve():
            raise ValueError("Invalid download work directory")
        return directory

    def _save(self, record: dict) -> None:
        record.setdefault("schema_version", JOB_SCHEMA_VERSION)
        target = self.jobs_dir / f"{record['id']}.json"
        _atomic_json_write(target, record)

    def list(self) -> list[dict]:
        with self._lock, self._state_lock():
            self._refresh_records_locked()
            self._reconcile_publications_locked()
            active = [record for record in self._records.values() if record["state"] in ACTIVE]
            finished = sorted((record for record in self._records.values() if record["state"] not in ACTIVE),
                              key=lambda item: item["created_at"], reverse=True)[:50]
            return deepcopy(sorted(active + finished, key=lambda item: item["created_at"], reverse=True))

    def get(self, job_id: str) -> dict | None:
        with self._lock, self._state_lock():
            self._refresh_records_locked()
            self._reconcile_publications_locked()
            record = self._records.get(job_id)
            return deepcopy(record) if record is not None else None

    def queue_capacity(self) -> int:
        """Return the number of additional jobs that can be admitted now.

        Acquisition uses this bounded admission signal to keep a large
        selection durable without pretending every selected item is already
        running.  It deliberately reports executor slots, not published-file
        capacity; finished jobs do not consume the pending queue.
        """
        with self._lock:
            return max(0, self._max_pending - len(self._futures))

    def job_is_published(self, job_id: str) -> bool:
        with self._lock, self._state_lock():
            self._refresh_records_locked()
            self._reconcile_publications_locked()
            record = self._records.get(job_id)
            return bool(
                record
                and record.get("state") == "succeeded"
                and record.get("filename")
                and (self.upload_dir / str(record["filename"])).is_file()
            )

    def metadata(self) -> dict[str, dict]:
        with self._lock, self._state_lock():
            self._refresh_records_locked()
            self._reconcile_publications_locked()
            return {key: deepcopy(value) for key, value in self._records.items() if value["state"] == "succeeded"}

    def attach_provenance(
        self, job_id: str, provenance: Mapping[str, object]
    ) -> dict | None:
        """Attach safe source identity to a download job without worker secrets."""
        if not isinstance(provenance, Mapping):
            raise TypeError("Download provenance must be an object")
        allowed = {
            "candidate_id": 128,
            "source_id": 64,
            "provider_id": 64,
            "external_id": 512,
            "media_id": 512,
            "creator_id": 240,
            "uploader": 240,
            "source_url": 4096,
        }
        safe: dict[str, object] = {}
        for key, limit in allowed.items():
            value = provenance.get(key)
            if value is None:
                continue
            if key == "source_url":
                try:
                    value = normalize_video_url(str(value))
                except ValueError as exc:
                    raise ValueError("Download provenance URL is invalid") from exc
            elif not isinstance(value, str):
                raise ValueError("Download provenance fields must be strings")
            value = str(value).strip()
            if value and len(value) <= limit:
                safe[key] = value
        part_index = provenance.get("part_index")
        if part_index is not None:
            if not isinstance(part_index, int) or isinstance(part_index, bool) or part_index < 1:
                raise ValueError("Download provenance part index is invalid")
            safe["part_index"] = part_index
        with self._lock, self._state_lock():
            self._refresh_records_locked()
            record = self._records.get(job_id)
            if record is None:
                return None
            current = record.get("provenance")
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update(safe)
            record["provenance"] = merged
            record["updated_at"] = now()
            self._save(record)
            return deepcopy(record)

    def submit(
        self,
        urls: list[str],
        quality: str = "1080",
        cookie_text: str | None = None,
        connection_id: str | None = None,
        intent_keys: list[str] | None = None,
    ) -> list[dict]:
        raw_urls = list(urls)
        if intent_keys is not None and len(intent_keys) != len(raw_urls):
            raise ValueError("Số download intent key phải khớp số link video.")
        normalized_pairs: list[tuple[str, str | None]] = []
        seen_urls: set[str] = set()
        for index, url in enumerate(raw_urls):
            normalized_url = normalize_video_url(url)
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            intent_key = (
                self._validate_intent_key(intent_keys[index])
                if intent_keys is not None
                else None
            )
            normalized_pairs.append((normalized_url, intent_key))
        normalized = [url for url, _ in normalized_pairs]
        normalized_intents = [key for _, key in normalized_pairs]
        if len([key for key in normalized_intents if key is not None]) != len(
            {key for key in normalized_intents if key is not None}
        ):
            raise ValueError("Download intent key bị trùng trong cùng một lượt.")
        if not normalized or len(normalized) > 20:
            raise ValueError("Mỗi lượt nhập từ 1 đến 20 link video.")
        if quality not in {"best", "1080", "720", "480"}:
            raise ValueError("Chất lượng video không hợp lệ.")
        self._validate_cookies(cookie_text)
        normalized_connection = None
        if connection_id is not None:
            # Validate against every URL before mutating any job record.
            normalized_connection = self._connection_id_for_url(
                normalized[0], connection_id
            )
            if any(
                self._connection_id_for_url(url, normalized_connection)
                != normalized_connection
                for url in normalized[1:]
            ):
                raise ValueError("Connection profile không khớp với các link tải.")
        if cookie_text and normalized_connection:
            raise ValueError("Chọn file cookies hoặc connection profile, không dùng đồng thời.")
        with self._lock, self._state_lock():
            self._refresh_records_locked()
            if not self._accepting:
                raise SubtitleJobQueueFull("Video downloader is stopping")
            existing: dict[tuple[str, str, str], dict] = {}
            records_by_intent: dict[str, dict] = {}

            def is_published(record: dict) -> bool:
                return bool(
                    record.get("state") == "succeeded"
                    and record.get("filename")
                    and (self.upload_dir / str(record["filename"])).is_file()
                )

            for record in self._records.values():
                key = self._job_key(
                    record["url"],
                    record["quality"],
                    record.get("connection_id"),
                )
                if record["quality"] == quality and (
                    record["state"] in ACTIVE or is_published(record)
                ):
                    existing[key] = record
                intent_key = record.get("intent_key")
                if isinstance(intent_key, str) and intent_key:
                    records_by_intent[intent_key] = record

            resolved: dict[str, dict] = {}
            pending: list[tuple[str, str | None]] = []
            for url, intent_key in normalized_pairs:
                by_intent = records_by_intent.get(intent_key) if intent_key else None
                if by_intent is not None and (
                    by_intent.get("url") == url
                    and by_intent.get("quality") == quality
                    and (by_intent.get("connection_id") or "")
                    == (normalized_connection or "")
                    and (by_intent.get("state") in ACTIVE or is_published(by_intent))
                ):
                    resolved[url] = by_intent
                    continue
                key = self._job_key(url, quality, normalized_connection)
                if key in existing:
                    resolved[url] = existing[key]
                    continue
                pending.append((url, intent_key))

            retry_records: dict[str, dict] = {}
            new_pairs: list[tuple[str, str | None]] = []
            for url, intent_key in pending:
                intent_record = records_by_intent.get(intent_key) if intent_key else None
                if intent_record is not None and not (
                    intent_record.get("url") == url
                    and intent_record.get("quality") == quality
                    and (intent_record.get("connection_id") or "")
                    == (normalized_connection or "")
                ):
                    raise ValueError(
                        "Download intent key đã được gắn với một lượt tải khác."
                    )
                retryable = [
                    record
                    for record in self._records.values()
                    if record["url"] == url
                    and record["quality"] == quality
                    and (record.get("connection_id") or "")
                    == (normalized_connection or "")
                    and (
                        record["state"] in {"failed", "paused", "canceled"}
                    )
                ]
                if (
                    intent_record is not None
                    and intent_record.get("state") == "succeeded"
                    and not is_published(intent_record)
                ):
                    retryable = [intent_record]
                if intent_record is not None and intent_record in retryable:
                    retryable = [intent_record]
                if retryable:
                    record = max(
                        retryable,
                        key=lambda item: (
                            item is intent_record,
                            str(item.get("updated_at") or ""),
                        ),
                    )
                    if record.get("uses_cookies") and not cookie_text:
                        raise ValueError("Chọn lại file cookies đăng nhập để tiếp tục lượt tải này.")
                    if record["id"] in self._futures:
                        raise SubtitleJobQueueFull("Lượt tải cũ đang dừng. Vui lòng thử lại sau.")
                    retry_records[url] = record
                else:
                    new_pairs.append((url, intent_key))

            if len(self._futures) + len(retry_records) + len(new_pairs) > self._max_pending:
                raise SubtitleJobQueueFull("Video download queue is full")
            for url, _intent_key in pending:
                if url not in retry_records:
                    continue
                record = retry_records[url]
                self.retry(record["id"], cookie_text, normalized_connection)
                resolved[url] = record

            for url, intent_key in new_pairs:
                job_id = uuid.uuid4().hex
                record = {"schema_version": JOB_SCHEMA_VERSION, "id": job_id, "url": url, "quality": quality, "platform": platform_name(url),
                          "state": "queued", "phase": "queued", "progress": None, "title": "", "filename": "",
                          "created_at": now(), "updated_at": now(), "downloaded_bytes": 0, "total_bytes": None,
                          "speed": None, "eta": None, "duration": None, "error": None,
                          "uses_cookies": False, "uses_connection": bool(normalized_connection),
                          "connection_id": normalized_connection, "intent_key": intent_key}
                self._save(record)
                self._records[job_id] = record
                resolved[url] = record
                self._schedule(record, cookie_text, normalized_connection)
            return deepcopy([resolved[url] for url in normalized])

    def _retire(self, job_id: str) -> None:
        with self._lock, self._state_lock():
            self._futures.pop(job_id, None)
            if self._started and self._accepting:
                for record in self._records.values():
                    if record["state"] == "paused" and record["phase"] == "interrupted":
                        self._resume_recovered(record)
                        if len(self._futures) >= self._max_pending:
                            break

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            with suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=5, check=False)
        else:
            import contextlib
            import signal
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        if process.poll() is None:
            process.kill()

    def _close_process(self, process: subprocess.Popen) -> None:
        self._kill(process)
        process.wait(timeout=10)
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()

    def cancel(self, job_id: str) -> dict | None:
        with self._lock, self._state_lock():
            self._refresh_records_locked()
            record = self._records.get(job_id)
            if record is None:
                return None
            if record["state"] in ACTIVE:
                record.update(state="canceled", phase="canceled", speed=None, eta=None, updated_at=now())
                self._save(record)
                future = self._futures.get(job_id)
                if future:
                    future.cancel()
            process = self._processes.get(job_id)
            snapshot = deepcopy(record)
        if process:
            self._kill(process)
        return snapshot

    def pause(self, job_id: str) -> dict | None:
        """Pause a job while retaining its work directory/checkpoint."""
        with self._lock, self._state_lock():
            self._refresh_records_locked()
            record = self._records.get(job_id)
            if record is None:
                return None
            if record["state"] in ACTIVE:
                record.update(state="paused", phase="paused", speed=None, eta=None, updated_at=now())
                self._save(record)
                future = self._futures.get(job_id)
                if future:
                    future.cancel()
            process = self._processes.get(job_id)
            snapshot = deepcopy(record)
        if process:
            self._kill(process)
        return snapshot

    def _run(
        self,
        job_id: str,
        cookie_text: str | None,
        connection_id: str | None,
    ) -> None:
        process = None
        timeout_timer = None
        timed_out = threading.Event()
        publication_manifest: Path | None = None
        try:
            with self._lock:
                record = self._records[job_id]
                if record["state"] != "queued":
                    return
                record.update(state="running", phase="resuming" if record.get("downloaded_bytes") else "extracting", updated_at=now())
                self._save(record)
            directory = self._work_directory(job_id)
            directory.mkdir(parents=True, exist_ok=True)
            with ExitStack() as cleanup:
                # Preserve media/checkpoints on failure or shutdown, but always remove secrets.
                cookie_path = directory / f"cookies-{uuid.uuid4().hex}.txt"
                cleanup.callback(lambda: cookie_path.unlink(missing_ok=True))
                cookie_file = None
                if cookie_text:
                    cookie_file = cookie_path
                    cookie_file.write_text(cookie_text, encoding="utf-8")
                    cookie_file.chmod(0o600)
                payload = {"job_id": job_id, "url": record["url"], "quality": record["quality"], "directory": str(directory),
                           "cookie_file": str(cookie_file) if cookie_file else None}
                if connection_id:
                    payload.update(
                        {
                            "profile_root": str(settings.content_bot_cbce_profile_root),
                            "account_ref": connection_id,
                        }
                    )
                cookie_text = None
                worker_cwd = Path(__file__).resolve().parents[2]
                with self._lock:
                    if record["state"] != "running":
                        return
                    process = subprocess.Popen(
                        [sys.executable, "-m", "app.services.video_download_worker"],
                        cwd=worker_cwd, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                        start_new_session=os.name != "nt",
                    )
                    self._processes[job_id] = process
                    cleanup.callback(self._close_process, process)
                def expire():
                    timed_out.set()
                    self._kill(process)
                timeout_timer = threading.Timer(7200, expire)
                timeout_timer.daemon = True
                timeout_timer.start()
                process.stdin.write(json.dumps(payload) + "\n")
                process.stdin.close()
                completed = None
                last_save = 0.0
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    with self._lock:
                        if record["state"] != "running":
                            break
                        if event.get("event") == "error":
                            raise RuntimeError(event["message"])
                        if event.get("event") == "complete":
                            completed = event
                        elif event.get("event") == "progress":
                            for field in ("phase", "downloaded_bytes", "total_bytes", "speed", "eta", "title", "duration"):
                                if field in event and (event[field] is not None or field in {"speed", "eta", "total_bytes"}):
                                    record[field] = event[field]
                            total = record["total_bytes"]
                            record["progress"] = (min(99, round(record["downloaded_bytes"] / total * 100)) if total else None)
                            record["updated_at"] = now()
                            if time.monotonic() - last_save > 1:
                                self._save(record)
                                last_save = time.monotonic()
                process.wait(timeout=10)
                with self._lock:
                    if record["state"] != "running":
                        return
                    if timed_out.is_set():
                        raise RuntimeError("Lượt tải vượt quá 2 giờ. Hãy thử lại với chất lượng thấp hơn.")
                    if process.returncode or not completed:
                        raise RuntimeError("Bộ tải không hoàn tất. Kiểm tra cài đặt yt-dlp và kết nối mạng rồi thử lại.")
                    source = (directory / completed["filename"]).resolve()
                    if source.parent != directory.resolve() or source.suffix not in EXTENSIONS or not source.is_file() or source.stat().st_size == 0:
                        raise RuntimeError("Bộ tải không trả về file video hợp lệ.")
                    self.upload_dir.mkdir(parents=True, exist_ok=True)
                    filename = job_id + source.suffix
                    target = (self.upload_dir / filename).resolve()
                    if target.parent != self.upload_dir.resolve():
                        raise RuntimeError("Đường dẫn file video tải về không hợp lệ.")
                    publication_manifest = self._publication_manifest_path(job_id)
                    _atomic_json_write(
                        publication_manifest,
                        {
                            "schema_version": PUBLICATION_MANIFEST_SCHEMA_VERSION,
                            "job_id": job_id,
                            "filename": filename,
                            "title": str(completed.get("title") or "Video")[:500],
                            "duration": completed.get("duration"),
                        },
                    )
                    source.replace(target)
                    record.update(state="succeeded", phase="complete", progress=100,
                                  filename=filename, title=str(completed.get("title") or "Video")[:500],
                                  duration=completed.get("duration"),
                                  speed=None, eta=None, updated_at=now())
                    self._save(record)
                    publication_manifest.unlink(missing_ok=True)
            if record["state"] == "succeeded":
                with suppress(OSError):
                    shutil.rmtree(self._work_directory(job_id))
        except Exception as error:
            with self._lock:
                record = self._records[job_id]
                if record["state"] in ACTIVE:
                    if publication_manifest is not None and not any(
                        self.upload_dir.glob(f"{job_id}.*")
                    ):
                        publication_manifest.unlink(missing_ok=True)
                    message = str(error) if isinstance(error, RuntimeError) else "Không thể lưu hoặc tải video. Kiểm tra dung lượng ổ đĩa và thử lại."
                    phase = "failed"
                    if record.get("uses_connection") and any(
                        marker in message.casefold()
                        for marker in ("đăng nhập", "cookies", "quyền truy cập")
                    ):
                        phase = "needs_connection"
                        message = (
                            "Connection profile Bilibili chưa đăng nhập hoặc đã hết quyền truy cập. "
                            "Mở đúng profile, hoàn tất đăng nhập rồi thử lại."
                        )
                    record.update(state="failed", phase=phase, error=message[:500], speed=None, eta=None, updated_at=now())
                    self._save(record)
        finally:
            if timeout_timer:
                timeout_timer.cancel()
            with self._lock:
                self._processes.pop(job_id, None)

    def stop_accepting(self) -> None:
        with self._lock:
            self._accepting = False

    def shutdown(self, *, timeout_seconds: float = 10) -> None:
        self.stop_accepting()
        with self._lock, self._state_lock():
            self._refresh_records_locked()
            for record in self._records.values():
                if record["state"] in ACTIVE:
                    record.update(state="paused", phase="interrupted", error=None, speed=None, eta=None, updated_at=now())
                    self._save(record)
            futures = list(self._futures.values())
            processes = list(self._processes.values())
            for future in futures:
                future.cancel()
        for process in processes:
            self._kill(process)
        self._executor.shutdown(wait=False, cancel_futures=True)
        _, pending = wait(futures, timeout=timeout_seconds)
        if pending:
            raise TimeoutError("Video downloads did not stop before the shutdown deadline")
