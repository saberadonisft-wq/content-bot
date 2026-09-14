"""Bounded processed-WAV cache with cross-process leases and encoder exclusion."""
from __future__ import annotations

import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

MAX_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 256
_KEY = re.compile(r'^[a-f0-9]{64}$')
_ENTRY = re.compile(r'^([a-f0-9]{64})\.(wav|json)$')
_PART = re.compile(r'^[a-f0-9]{64}(?:\.json)?\.[a-f0-9]{32}\.part(?:\.wav)?$')


@contextmanager
def database_lock(path: Path, cancel: threading.Event | None = None):
    """SQLite releases the OS lock even when the supervised worker is killed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=.05, isolation_level=None)
    started = time.monotonic()
    try:
        while True:
            if cancel and cancel.is_set():
                raise InterruptedError('Đã hủy chờ xử lý audio.')
            try:
                connection.execute('BEGIN IMMEDIATE')
                break
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                if time.monotonic() - started > 65:
                    raise TimeoutError('Hết thời gian chờ worker xử lý audio.') from exc
                if cancel:
                    cancel.wait(.05)
                else:
                    time.sleep(.05)
        yield connection
        connection.commit()
    finally:
        connection.close()


class AudioLease:
    def __init__(self, cache, token: str):
        self.cache, self.token = cache, token

    def release(self):
        if self.token:
            with self.cache.metadata() as connection:
                connection.execute('DELETE FROM leases WHERE token = ?', (self.token,))
            self.token = ''

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.release()


class AudioCache:
    def __init__(self, root: Path, *, max_bytes: int = MAX_BYTES, max_entries: int = MAX_ENTRIES):
        self.root = root.resolve()
        self.max_bytes, self.max_entries = max_bytes, max_entries
        if max_bytes <= 0 or max_entries <= 0:
            raise ValueError('Hạn mức cache phải lớn hơn 0.')

    @contextmanager
    def metadata(self):
        with database_lock(self.root / '.cache.sqlite3') as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS leases (token TEXT PRIMARY KEY, key TEXT, expires REAL)')
            connection.execute('CREATE TABLE IF NOT EXISTS usage (key TEXT PRIMARY KEY, touched REAL)')
            connection.execute('DELETE FROM leases WHERE expires < ?', (time.time(),))
            yield connection

    def encoder(self, cancel=None):
        return database_lock(self.root / '.encoder.sqlite3', cancel)

    def pin(self, key: str) -> AudioLease:
        if not _KEY.fullmatch(key):
            raise ValueError('ID cache không hợp lệ.')
        token = uuid.uuid4().hex
        with self.metadata() as connection:
            # Longer than the sync worker's hard 600-second lifetime.
            connection.execute('INSERT INTO leases VALUES (?, ?, ?)', (token, key, time.time() + 660))
            connection.execute('INSERT OR REPLACE INTO usage VALUES (?, ?)', (key, time.time()))
        return AudioLease(self, token)

    def _entries(self):
        groups = {}
        for path in self.root.iterdir():
            match = _ENTRY.fullmatch(path.name)
            if not match or path.is_symlink() or path.resolve().parent != self.root:
                continue
            stat = path.stat()
            entry = groups.setdefault(match[1], {'files': [], 'bytes': 0, 'touched': stat.st_mtime})
            entry['files'].append(path)
            entry['bytes'] += stat.st_size
        return groups

    def clean_abandoned_parts(self):
        # Caller holds encoder exclusion: no current encoder can own a part file here.
        for path in self.root.iterdir():
            if _PART.fullmatch(path.name) and not path.is_symlink() and path.resolve().parent == self.root:
                path.unlink(missing_ok=True)

    def trim(self, *, reserve_bytes: int = 0, new_key: str | None = None) -> dict:
        if reserve_bytes < 0 or new_key is not None and not _KEY.fullmatch(new_key):
            raise ValueError('Dự trù cache không hợp lệ.')
        with self.metadata() as connection:
            groups = self._entries()
            pinned = {row[0] for row in connection.execute('SELECT key FROM leases')}
            usage = dict(connection.execute('SELECT key, touched FROM usage'))
            extra = int(new_key is not None and new_key not in groups)
            protected = [entry for key, entry in groups.items() if key in pinned]
            if (sum(entry['bytes'] for entry in protected) + reserve_bytes > self.max_bytes
                    or len(protected) + extra > self.max_entries):
                raise ValueError('Cache audio đang đầy vì các file còn được sử dụng. Thử lại sau khi nghe/kiểm tra xong.')
            total, count, removed = sum(entry['bytes'] for entry in groups.values()), len(groups), []
            for key in sorted(groups, key=lambda key: usage.get(key, groups[key]['touched'])):
                if total + reserve_bytes <= self.max_bytes and count + extra <= self.max_entries:
                    break
                if key in pinned:
                    continue
                for path in groups[key]['files']:
                    path.unlink(missing_ok=True)
                connection.execute('DELETE FROM usage WHERE key = ?', (key,))
                total -= groups[key]['bytes']
                count -= 1
                removed.append(key)
            # Drop usage-only rows from interrupted creation after their lease expires.
            for key in usage.keys() - groups.keys() - pinned:
                connection.execute('DELETE FROM usage WHERE key = ?', (key,))
            return {'bytes': total, 'entries': count, 'max_bytes': self.max_bytes,
                    'max_entries': self.max_entries, 'removed': removed}
