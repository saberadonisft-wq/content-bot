"""Isolated profile namespace and cross-process ownership lock."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Self

from ..registry import SourceRegistry

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ACCOUNT_KEY = re.compile(r"^[0-9a-f]{24}$")
_DELETE_MARKER = ".cbce-delete-pending"


class ProfileInUse(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProfileRef:
    source_id: str
    account_key: str
    path: Path
    owned: bool = True


class ProfileNamespace:
    def __init__(
        self,
        root: Path,
        registry: SourceRegistry,
        *,
        forbidden_roots: tuple[Path, ...] = (),
    ) -> None:
        self.root = root.expanduser().resolve()
        broad_roots = {Path.cwd().resolve(), Path.home().resolve()}
        if self.root in broad_roots:
            raise ValueError("Browser profile root is too broad")
        for forbidden in forbidden_roots:
            old_root = forbidden.expanduser().resolve()
            if self.root == old_root or self.root.is_relative_to(old_root):
                raise ValueError("CBCE profile root overlaps a legacy profile namespace")
        self.registry = registry

    def profile(self, source_id: str, account_ref: str = "default") -> ProfileRef:
        canonical = self.registry.resolve_id(source_id)
        if self.registry.get(canonical) is None or not _SAFE_ID.fullmatch(canonical):
            raise ValueError(f"Unknown or unsafe source ID: {source_id!r}")
        normalized_account = " ".join(account_ref.strip().casefold().split())
        if not normalized_account or len(normalized_account) > 512:
            raise ValueError("Account reference must contain 1-512 characters")
        account_key = hashlib.sha256(normalized_account.encode("utf-8")).hexdigest()[:24]
        source_root = (self.root / canonical).resolve()
        if (source_root / _DELETE_MARKER).exists():
            raise ProfileInUse(
                f"Browser profiles are pending deletion for source: {canonical}"
            )
        path = (source_root / account_key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Profile escaped its namespace")
        path.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(self.root)
        _restrict_permissions(path.parent)
        _restrict_permissions(path)
        return ProfileRef(canonical, account_key, path)

    def delete_source_profiles(
        self,
        source_id: str,
        *,
        confirmation: str,
    ) -> int:
        """Delete only app-owned profiles for one canonical source.

        The exact confirmation phrase and per-profile locks make this unsuitable
        for accidental/background cleanup. Browser profiles are never included
        in automatic retention.
        """
        canonical = self.registry.resolve_id(source_id)
        if self.registry.get(canonical) is None or not _SAFE_ID.fullmatch(canonical):
            raise ValueError(f"Unknown or unsafe source ID: {source_id!r}")
        expected = f"DELETE {canonical} PROFILES"
        if confirmation != expected:
            raise ValueError(f"Profile deletion requires confirmation: {expected}")
        source_root = (self.root / canonical).resolve()
        if source_root.parent != self.root or not source_root.is_relative_to(self.root):
            raise ValueError("Profile deletion target escaped its namespace")
        if not source_root.exists():
            return 0
        account_paths = tuple(
            path.resolve()
            for path in source_root.iterdir()
            if path.is_dir() and _ACCOUNT_KEY.fullmatch(path.name)
        )
        locks: list[ProfileLock] = []
        try:
            for path in account_paths:
                profile = ProfileRef(canonical, path.name, path)
                lock = ProfileLock(
                    profile,
                    owner_id=f"delete-{os.getpid()}",
                    allow_delete_pending=True,
                )
                lock.acquire()
                locks.append(lock)
            marker = source_root / _DELETE_MARKER
            marker.write_text(
                json.dumps(
                    {
                        "source_id": canonical,
                        "requested_at": datetime.now(UTC).isoformat(),
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            _restrict_permissions(marker)
        finally:
            for lock in reversed(locks):
                lock.release()
        shutil.rmtree(source_root)
        return len(account_paths)


class ProfileLock:
    """Own byte zero of a profile-local lock file until release."""

    def __init__(
        self,
        profile: ProfileRef,
        *,
        owner_id: str,
        clock=time.monotonic,
        sleep=time.sleep,
        allow_delete_pending: bool = False,
    ) -> None:
        if not owner_id.strip() or len(owner_id) > 128:
            raise ValueError("owner_id must contain 1-128 characters")
        self.profile = profile
        self.owner_id = owner_id
        self._clock = clock
        self._sleep = sleep
        self._allow_delete_pending = allow_delete_pending
        self._stream: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._stream is not None

    def acquire(self, timeout_seconds: float = 0) -> None:
        if self.acquired:
            raise RuntimeError("Profile lock is already acquired")
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds cannot be negative")
        delete_marker = self.profile.path.parent / _DELETE_MARKER
        if delete_marker.exists() and not self._allow_delete_pending:
            raise ProfileInUse(
                f"Browser profiles are pending deletion: {self.profile.source_id}"
            )
        lock_path = self.profile.path / ".cbce-profile.lock"
        stream = lock_path.open("a+b")
        _restrict_permissions(lock_path)
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = self._clock() + timeout_seconds
        while True:
            try:
                _try_lock(stream)
                if delete_marker.exists() and not self._allow_delete_pending:
                    _unlock(stream)
                    stream.close()
                    raise ProfileInUse(
                        f"Browser profiles are pending deletion: {self.profile.source_id}"
                    )
                break
            except OSError as exc:
                if self._clock() >= deadline:
                    stream.close()
                    raise ProfileInUse(
                        f"Browser profile is already owned: {self.profile.source_id}/{self.profile.account_key}"
                    ) from exc
                self._sleep(min(0.05, max(0, deadline - self._clock())))
        self._stream = stream
        self._write_metadata()

    def _write_metadata(self) -> None:
        assert self._stream is not None
        payload = json.dumps(
            {
                "owner_id": self.owner_id,
                "pid": os.getpid(),
                "acquired_at": datetime.now(UTC).isoformat(),
                "source_id": self.profile.source_id,
                "account_key": self.profile.account_key,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self._stream.seek(1)
        self._stream.truncate()
        self._stream.write(payload)
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            _unlock(stream)
        finally:
            stream.close()

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()


def _try_lock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _restrict_permissions(path: Path) -> None:
    try:
        mode = stat.S_IRUSR | stat.S_IWUSR
        if path.is_dir():
            mode |= stat.S_IXUSR
        path.chmod(mode)
    except OSError:
        # Windows ACL hardening is verified separately by the worker installer;
        # chmod still removes broad POSIX permissions where supported.
        pass
