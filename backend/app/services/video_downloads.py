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
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack, suppress
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .subtitle_jobs import SubtitleJobQueueFull

ACTIVE = {"queued", "running"}
EXTENSIONS = {".mp4", ".webm", ".mkv"}


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


class VideoDownloadManager:
    def __init__(self, root: Path, *, max_workers: int = 2, max_pending: int = 32):
        self.root = root.resolve()
        self.jobs_dir = self.root / "downloads" / "jobs"
        self.work_root = self.root / "downloads" / "work"
        self.upload_dir = self.root / "upload"
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._futures = {}
        self._accepting = True
        self._started = False
        self._max_pending = max_pending
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="video-download")
        if self.jobs_dir.exists():
            for path in self.jobs_dir.glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if not re.fullmatch(r"[a-f0-9]{32}", record["id"]) or record["id"] != path.stem:
                        continue
                    if record["state"] in ACTIVE or record.get("phase") == "interrupted":
                        record.update(state="paused", phase="interrupted", error=None, speed=None, eta=None, updated_at=now())
                        self._save(record)
                    self._records[record["id"]] = record
                except (OSError, ValueError, KeyError, TypeError):
                    continue

    def start(self) -> None:
        """Recover only after application startup, never while importing routes."""
        with self._lock:
            if self._started or not self._accepting:
                return
            self._started = True
            for record in sorted(self._records.values(), key=lambda item: item["created_at"]):
                if record["state"] == "paused" and record["phase"] == "interrupted":
                    self._resume_recovered(record)

    def _resume_recovered(self, record: dict) -> None:
        if record.get("uses_cookies"):
            record.update(phase="needs_cookies", error="Đã giữ file tải dở. Chọn lại file cookies rồi bấm Tiếp tục.")
            self._save(record)
        elif len(self._futures) < self._max_pending:
            self._schedule(record, None)

    def _schedule(self, record: dict, cookie_text: str | None) -> None:
        job_id = record["id"]
        if job_id in self._futures:
            raise SubtitleJobQueueFull("Lượt tải cũ đang dừng. Vui lòng thử lại sau.")
        record.update(state="queued", phase="resuming" if record.get("downloaded_bytes") else "queued",
                      error=None, speed=None, eta=None, updated_at=now(),
                      uses_cookies=bool(cookie_text), attempts=record.get("attempts", 0) + 1)
        self._save(record)
        self._futures[job_id] = self._executor.submit(self._run, job_id, cookie_text)
        self._futures[job_id].add_done_callback(lambda _, key=job_id: self._retire(key))

    @staticmethod
    def _validate_cookies(cookie_text: str | None) -> None:
        if cookie_text and (len(cookie_text.encode("utf-8")) > 1_000_000 or
                            not cookie_text.lstrip().startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File"))):
            raise ValueError("File cookies phải có định dạng Netscape (.txt), tối đa 1 MB.")

    def retry(self, job_id: str, cookie_text: str | None = None) -> dict | None:
        self._validate_cookies(cookie_text)
        with self._lock:
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
            self._schedule(record, cookie_text)
            return deepcopy(record)

    def _work_directory(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("Invalid download job id")
        directory = (self.work_root / job_id).resolve()
        if directory.parent != self.work_root.resolve():
            raise ValueError("Invalid download work directory")
        return directory

    def _save(self, record: dict) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        target = self.jobs_dir / f"{record['id']}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)

    def list(self) -> list[dict]:
        with self._lock:
            active = [record for record in self._records.values() if record["state"] in ACTIVE]
            finished = sorted((record for record in self._records.values() if record["state"] not in ACTIVE),
                              key=lambda item: item["created_at"], reverse=True)[:50]
            return deepcopy(sorted(active + finished, key=lambda item: item["created_at"], reverse=True))

    def metadata(self) -> dict[str, dict]:
        with self._lock:
            return {key: deepcopy(value) for key, value in self._records.items() if value["state"] == "succeeded"}

    def submit(self, urls: list[str], quality: str = "1080", cookie_text: str | None = None) -> list[dict]:
        normalized = list(dict.fromkeys(normalize_video_url(url) for url in urls))
        if not normalized or len(normalized) > 20:
            raise ValueError("Mỗi lượt nhập từ 1 đến 20 link video.")
        if quality not in {"best", "1080", "720", "480"}:
            raise ValueError("Chất lượng video không hợp lệ.")
        self._validate_cookies(cookie_text)
        with self._lock:
            if not self._accepting:
                raise SubtitleJobQueueFull("Video downloader is stopping")
            existing = {}
            for record in self._records.values():
                if record["quality"] == quality and (record["state"] in ACTIVE or
                    (record["state"] == "succeeded" and (self.upload_dir / record["filename"]).is_file())):
                    existing[record["url"]] = record
            new_urls = [url for url in normalized if url not in existing]
            if len(self._futures) + len(new_urls) > self._max_pending:
                raise SubtitleJobQueueFull("Video download queue is full")
            retry_records = {}
            for url in new_urls:
                retryable = [record for record in self._records.values() if record["url"] == url
                             and record["quality"] == quality and record["state"] in {"failed", "paused", "canceled"}]
                if retryable:
                    record = max(retryable, key=lambda item: item["updated_at"])
                    if record.get("uses_cookies") and not cookie_text:
                        raise ValueError("Chọn lại file cookies đăng nhập để tiếp tục lượt tải này.")
                    if record["id"] in self._futures:
                        raise SubtitleJobQueueFull("Lượt tải cũ đang dừng. Vui lòng thử lại sau.")
                    retry_records[url] = record
            for url in new_urls:
                if url in retry_records:
                    record = retry_records[url]
                    self.retry(record["id"], cookie_text)
                    existing[url] = record
                    continue
                job_id = uuid.uuid4().hex
                record = {"id": job_id, "url": url, "quality": quality, "platform": platform_name(url),
                          "state": "queued", "phase": "queued", "progress": None, "title": "", "filename": "",
                          "created_at": now(), "updated_at": now(), "downloaded_bytes": 0, "total_bytes": None,
                          "speed": None, "eta": None, "duration": None, "error": None}
                self._save(record)
                self._records[job_id] = record
                existing[url] = record
                self._schedule(record, cookie_text)
            return deepcopy([existing[url] for url in normalized])

    def _retire(self, job_id: str) -> None:
        with self._lock:
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
        with self._lock:
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

    def _run(self, job_id: str, cookie_text: str | None) -> None:
        process = None
        timeout_timer = None
        timed_out = threading.Event()
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
                payload = {"url": record["url"], "quality": record["quality"], "directory": str(directory),
                           "cookie_file": str(cookie_file) if cookie_file else None}
                cookie_text = None
                with self._lock:
                    if record["state"] != "running":
                        return
                    process = subprocess.Popen(
                        [sys.executable, "-m", "app.services.video_download_worker"],
                        cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE,
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
                    source.replace(self.upload_dir / filename)
                    record.update(state="succeeded", phase="complete", progress=100,
                                  filename=filename, title=completed["title"], duration=completed.get("duration"),
                                  speed=None, eta=None, updated_at=now())
                    self._save(record)
            if record["state"] == "succeeded":
                with suppress(OSError):
                    shutil.rmtree(self._work_directory(job_id))
        except Exception as error:
            with self._lock:
                record = self._records[job_id]
                if record["state"] in ACTIVE:
                    message = str(error) if isinstance(error, RuntimeError) else "Không thể lưu hoặc tải video. Kiểm tra dung lượng ổ đĩa và thử lại."
                    record.update(state="failed", phase="failed", error=message[:500], speed=None, eta=None, updated_at=now())
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
        with self._lock:
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
