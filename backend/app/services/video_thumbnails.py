"""Bounded thumbnail generation with per-output single-flight and atomic publication."""

from __future__ import annotations

import hashlib
import subprocess
import threading
import uuid
from concurrent.futures import Future
from pathlib import Path

import imageio_ffmpeg


class ThumbnailError(RuntimeError):
    pass


class ThumbnailBusy(ThumbnailError):
    pass


class ThumbnailTimeout(ThumbnailError):
    pass


class VideoThumbnails:
    def __init__(self, *, concurrency: int = 2, max_pending: int = 16, timeout_seconds: float = 90, queue_seconds: float = 2):
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, concurrency))
        self._flights: dict[Path, Future] = {}
        self._processes: set[subprocess.Popen] = set()
        self._stopping = threading.Event()
        self.max_pending = max(1, max_pending)
        self.timeout_seconds = timeout_seconds
        self.queue_seconds = queue_seconds

    @staticmethod
    def _fingerprint(source: Path) -> str:
        stat = source.stat()
        return hashlib.sha256(f"video-thumbnail-v1:{source}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()

    @staticmethod
    def _cached(output: Path, fingerprint: str) -> bool:
        try:
            return output.stat().st_size > 0 and output.with_suffix(".fingerprint").read_text(encoding="ascii") == fingerprint
        except OSError:
            return False

    def get(self, source: Path, output: Path) -> Path:
        source, output = source.resolve(), output.resolve()
        while True:
            fingerprint = self._fingerprint(source)
            if self._cached(output, fingerprint):
                return output
            with self._lock:
                if self._stopping.is_set():
                    raise ThumbnailBusy("Thumbnail service is stopping")
                if self._cached(output, fingerprint):
                    return output
                future = self._flights.get(output)
                leader = future is None
                if leader:
                    if len(self._flights) >= self.max_pending:
                        raise ThumbnailBusy("Thumbnail queue is full")
                    future = self._flights[output] = Future()
            if not leader:
                try:
                    future.result(timeout=self.timeout_seconds + self.queue_seconds + 5)
                except TimeoutError as exc:
                    raise ThumbnailBusy("Thumbnail is still being generated") from exc
                # Source may have changed while waiting for the preceding job.
                continue
            try:
                self._generate(source, output, fingerprint)
                future.set_result(output)
                return output
            except BaseException as exc:
                future.set_exception(exc)
                raise
            finally:
                with self._lock:
                    self._flights.pop(output, None)

    def _generate(self, source: Path, output: Path, fingerprint: str) -> None:
        if not self._slots.acquire(timeout=self.queue_seconds):
            raise ThumbnailBusy("Thumbnail workers are busy")
        part = output.with_name(f"{output.stem}.{uuid.uuid4().hex}.part.jpg")
        marker = part.with_suffix(".fingerprint")
        process = None
        try:
            with self._lock:
                if self._stopping.is_set():
                    raise ThumbnailBusy("Thumbnail service is stopping")
                output.parent.mkdir(parents=True, exist_ok=True)
                process = subprocess.Popen([
                    imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(source), "-ss", "00:00:01", "-frames:v", "1", "-an",
                    "-vf", "scale=480:-2", "-q:v", "2", "-threads", "1", str(part),
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self._processes.add(process)
            try:
                returncode = process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait(timeout=5)
                raise ThumbnailTimeout("Thumbnail generation timed out") from exc
            if returncode != 0 or not part.is_file() or part.stat().st_size == 0:
                raise ThumbnailError("FFmpeg did not generate a thumbnail")
            if self._fingerprint(source) != fingerprint:
                raise ThumbnailBusy("Video changed while generating its thumbnail")
            marker.write_text(fingerprint, encoding="ascii")
            part.replace(output)
            marker.replace(output.with_suffix(".fingerprint"))
        except OSError as exc:
            raise ThumbnailError("Thumbnail generation failed") from exc
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                with self._lock:
                    self._processes.discard(process)
            try:
                part.unlink(missing_ok=True)
                marker.unlink(missing_ok=True)
            finally:
                self._slots.release()

    def shutdown(self) -> None:
        with self._lock:
            self._stopping.set()
            processes = list(self._processes)
        for process in processes:
            if process.poll() is None:
                process.kill()
        for process in processes:
            process.wait(timeout=5)
