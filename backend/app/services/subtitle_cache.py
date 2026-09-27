"""Bounded JSON caches with LRU eviction and OS-owned pins for active jobs.

Only OCR/ASR/translation cache filenames are managed. Version stores and models
are never visited. SQLite serializes short file operations across processes;
per-session OS locks release pins after worker termination without PID guessing.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from .gemini_media import atomic_json

MAX_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 2048
MAX_FILE_BYTES = 64 * 1024 * 1024
_ENTRY = re.compile(r"(?:ocr_|asr_|batch-)[a-f0-9]{64}\.json")
_TEMP = re.compile(r"(?:ocr_|asr_|batch-)[a-f0-9]{64}\.[a-f0-9]{32}\.tmp")
_TOKEN = re.compile(r"[a-f0-9]{32}")
_current: ContextVar[CacheSession | None] = ContextVar(
    "subtitle_cache_session", default=None
)
logger = logging.getLogger("content_bot.subtitle_cache")


def _acquire(path):
    stream = path.open("a+b")
    try:
        if not path.stat().st_size:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except BaseException:
        stream.close()
        raise


class CacheSession:
    def __init__(self, root: Path, *, max_bytes=None, max_entries=None):
        self.root = root.resolve()
        self.max_bytes = MAX_BYTES if max_bytes is None else max_bytes
        self.max_entries = MAX_ENTRIES if max_entries is None else max_entries
        if self.max_bytes <= 0 or self.max_entries <= 0:
            raise ValueError("Cache limits must be positive")
        self.token = uuid.uuid4().hex
        self.lock_path = self.root / f".session-{self.token}.lock"
        self.handle = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.handle = _acquire(self.lock_path)
        return self

    def __exit__(self, *_args):
        try:
            with self.metadata() as connection:
                connection.execute("DELETE FROM pins WHERE session = ?", (self.token,))
        except OSError:
            logger.warning(
                "Cache pin cleanup deferred until next access", exc_info=True
            )
        finally:
            if self.handle:
                self.handle.close()
                self.handle = None
            try:
                self.lock_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Cache session marker cleanup deferred", exc_info=True)

    @contextmanager
    def metadata(self):
        connection = None
        try:
            connection = sqlite3.connect(
                self.root / ".subtitle-cache.sqlite3", timeout=2, isolation_level=None
            )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS pins (session TEXT, name TEXT, PRIMARY KEY(session,name))"
            )
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            raise OSError(
                "Không truy cập được sổ cache phụ đề; kiểm tra dung lượng và quyền ghi."
            ) from exc
        finally:
            if connection:
                connection.close()

    def path(self, path):
        path = Path(path)
        if (
            not _ENTRY.fullmatch(path.name)
            or path.is_symlink()
            or path.resolve().parent != self.root
        ):
            raise OSError("Đường dẫn cache phụ đề không hợp lệ.")
        return self.root / path.name

    def _pin(self, connection, name):
        if self.handle is None:
            raise RuntimeError("Cache session must be entered before use")
        connection.execute(
            "INSERT OR IGNORE INTO pins VALUES (?,?)", (self.token, name)
        )

    def _release_abandoned(self, connection):
        for path in self.root.glob(".session-*.lock"):
            token = path.name[len(".session-") : -len(".lock")]
            if token == self.token or not _TOKEN.fullmatch(token) or path.is_symlink():
                continue
            try:
                handle = _acquire(path)
            except OSError:
                continue  # An OS lock, not elapsed time, protects an active worker.
            handle.close()
            connection.execute("DELETE FROM pins WHERE session = ?", (token,))
            path.unlink(missing_ok=True)
        sessions = [
            row[0] for row in connection.execute("SELECT DISTINCT session FROM pins")
        ]
        for token in sessions:
            if (
                _TOKEN.fullmatch(token)
                and not (self.root / f".session-{token}.lock").exists()
            ):
                connection.execute("DELETE FROM pins WHERE session = ?", (token,))

    def read(self, path):
        path = self.path(path)
        with self.metadata() as connection:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Cache JSON vượt kích thước cho phép.")
            value = json.loads(path.read_text(encoding="utf-8"))
            self._pin(connection, path.name)
            path.touch()
            return value

    def write(self, path, value, *, writer=atomic_json):
        path = self.path(path)
        size = len(
            json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        if size > min(MAX_FILE_BYTES, self.max_bytes):
            raise OSError(errno.ENOSPC, "Kết quả vượt hạn mức cache phụ đề.")
        with self.metadata() as connection:
            self._release_abandoned(connection)
            # All cache writers hold this same transaction until atomic replace.
            # A matching temporary left while we own it belongs to a dead writer.
            for temporary in self.root.iterdir():
                if (
                    _TEMP.fullmatch(temporary.name)
                    and not temporary.is_symlink()
                    and temporary.is_file()
                    and temporary.resolve().parent == self.root
                ):
                    temporary.unlink()
            entries = []
            for candidate in self.root.iterdir():
                if (
                    not _ENTRY.fullmatch(candidate.name)
                    or candidate.is_symlink()
                    or not candidate.is_file()
                ):
                    continue
                if candidate.resolve().parent != self.root:
                    continue
                stat = candidate.stat()
                entries.append((stat.st_mtime_ns, candidate, stat.st_size))
            pinned = {row[0] for row in connection.execute("SELECT name FROM pins")}
            others = [
                (modified, p, amount) for modified, p, amount in entries if p != path
            ]
            protected = [(p, amount) for _, p, amount in others if p.name in pinned]
            if (
                sum(amount for _, amount in protected) + size > self.max_bytes
                or len(protected) + 1 > self.max_entries
            ):
                raise OSError(
                    errno.ENOSPC,
                    "Cache phụ đề đang đầy vì các nhóm còn được tác vụ sử dụng.",
                )
            total, count = (
                sum(amount for _, _, amount in others) + size,
                len(others) + 1,
            )
            for _, candidate, amount in sorted(others):
                if total <= self.max_bytes and count <= self.max_entries:
                    break
                if candidate.name in pinned:
                    continue
                candidate.unlink()
                total -= amount
                count -= 1
            writer(path, value)
            self._pin(connection, path.name)


def cached_json(path):
    session = _current.get()
    if session and session.root == Path(path).resolve().parent:
        return session.read(path)
    with CacheSession(Path(path).parent) as session:
        return session.read(path)


def cache_json(path, value, *, writer=atomic_json):
    session = _current.get()
    if session and session.root == Path(path).resolve().parent:
        return session.write(path, value, writer=writer)
    with CacheSession(Path(path).parent) as session:
        return session.write(path, value, writer=writer)


def with_subtitle_cache(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        root = kwargs.get("cache_dir")
        existing = _current.get()
        if root is None or existing and existing.root == Path(root).resolve():
            return function(*args, **kwargs)
        session = CacheSession(Path(root))
        try:
            session.__enter__()
        except OSError:
            # Let the service return extraction with a cache warning or a partial
            # translation error, instead of failing before work can be recovered.
            return function(*args, **kwargs)
        token = _current.set(session)
        try:
            return function(*args, **kwargs)
        finally:
            _current.reset(token)
            session.__exit__()

    return wrapped
