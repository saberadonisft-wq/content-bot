"""Stable pseudonyms without persisting raw platform account identifiers."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import tempfile
from pathlib import Path


class IdentityPseudonymizer:
    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("Pseudonymization key must contain at least 32 bytes")
        self._key = bytes(key)

    def pseudonym(self, source_id: str, external_author_id: str) -> str:
        source = source_id.strip().casefold()
        author = external_author_id.strip()
        if not source or not author:
            return ""
        digest = hmac.new(
            self._key,
            f"{source}\0{author}".encode(),
            hashlib.sha256,
        ).hexdigest()[:24]
        return f"{source}_{digest}"


class PseudonymKeyStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        broad = {Path.cwd().resolve(), Path.home().resolve()}
        if self.root in broad:
            raise ValueError("Pseudonym key root is too broad")
        self.path = self.root / "identity-hmac-v1.key"

    def load_or_create(self) -> bytes:
        self.root.mkdir(parents=True, exist_ok=True)
        _restrict_secret_permissions(self.root, directory=True)
        if not self.path.exists():
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".identity-hmac-", dir=self.root
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(secrets.token_bytes(32))
                    stream.flush()
                    os.fsync(stream.fileno())
                _restrict_secret_permissions(temporary, directory=False)
                try:
                    os.link(temporary, self.path)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        _restrict_secret_permissions(self.path, directory=False)
        key = self.path.read_bytes()
        if len(key) != 32:
            raise ValueError("Pseudonym key file is invalid")
        return key

    @staticmethod
    def load_reference(path: Path) -> bytes:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise ValueError("Pseudonym key reference does not exist")
        key = resolved.read_bytes()
        if len(key) != 32:
            raise ValueError("Pseudonym key reference is invalid")
        return key


def _restrict_secret_permissions(path: Path, *, directory: bool) -> None:
    try:
        mode = stat.S_IRUSR | stat.S_IWUSR
        if directory:
            mode |= stat.S_IXUSR
        path.chmod(mode)
    except OSError:
        pass
