"""Encrypted local storage for TikTok OAuth credentials.

The vault is intentionally file-backed and provider-specific. Tokens never enter
MongoDB, process arguments, logs, or browser storage. A random AES-256 key and
the ciphertext are stored as separate owner-only files below the application
data directory.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ...runtime.privacy import _restrict_secret_permissions
from .official_provider import TikTokDisplayConfig, TikTokTokenBundle

_AAD = b"content-bot:tiktok-oauth:v1"
_SCHEMA_VERSION = 1
_MAX_VAULT_BYTES = 32_768


@dataclass(frozen=True, slots=True)
class StoredTikTokCredential:
    open_id: str
    scopes: frozenset[str]
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    issued_at: datetime
    access_expires_at: datetime
    refresh_expires_at: datetime
    authorized_username: str = ""

    def __post_init__(self) -> None:
        bundle = TikTokTokenBundle(
            open_id=self.open_id,
            scopes=self.scopes,
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_in=max(1, int((self.access_expires_at - self.issued_at).total_seconds())),
            refresh_expires_in=max(
                1, int((self.refresh_expires_at - self.issued_at).total_seconds())
            ),
        )
        for value in (self.issued_at, self.access_expires_at, self.refresh_expires_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("TikTok credential timestamps must be timezone-aware")
        if (
            self.access_expires_at <= self.issued_at
            or self.refresh_expires_at <= self.issued_at
        ):
            raise ValueError("TikTok credential expiry is invalid")
        if bundle.open_id != self.open_id:
            raise ValueError("TikTok credential is invalid")
        username = self.authorized_username.strip().lstrip("@").casefold()
        if len(username) > 100 or any(character.isspace() for character in username):
            raise ValueError("TikTok authorized username is invalid")
        object.__setattr__(self, "scopes", bundle.scopes)
        object.__setattr__(self, "authorized_username", username)

    @property
    def access_expired(self) -> bool:
        return datetime.now(UTC) >= self.access_expires_at

    @property
    def refresh_expired(self) -> bool:
        return datetime.now(UTC) >= self.refresh_expires_at

    def display_config(self) -> TikTokDisplayConfig:
        if self.access_expired:
            raise ValueError("TikTok access token has expired")
        return TikTokDisplayConfig(self.open_id, self.scopes, self.access_token)


class TikTokTokenVault:
    """AES-GCM vault with atomic, owner-only local files."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        broad = {Path.cwd().resolve(), Path.home().resolve()}
        if self.root in broad:
            raise ValueError("TikTok token vault root is too broad")
        self.key_path = self.root / "tiktok-oauth-aes256-v1.key"
        self.token_path = self.root / "tiktok-oauth-v1.enc"

    def save(
        self,
        bundle: TikTokTokenBundle,
        *,
        authorized_username: str = "",
        issued_at: datetime | None = None,
    ) -> StoredTikTokCredential:
        issued = (issued_at or datetime.now(UTC)).astimezone(UTC)
        credential = StoredTikTokCredential(
            open_id=bundle.open_id,
            scopes=bundle.scopes,
            access_token=bundle.access_token,
            refresh_token=bundle.refresh_token,
            issued_at=issued,
            access_expires_at=issued + timedelta(seconds=bundle.expires_in),
            refresh_expires_at=issued + timedelta(seconds=bundle.refresh_expires_in),
            authorized_username=authorized_username,
        )
        plaintext = json.dumps(
            self._serialize(credential),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        key = self._load_or_create_key()
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, _AAD)
        envelope = json.dumps(
            {
                "schema_version": _SCHEMA_VERSION,
                "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
                "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        if len(envelope) > _MAX_VAULT_BYTES:
            raise ValueError("TikTok token vault payload is too large")
        self._atomic_write(self.token_path, envelope)
        return credential

    def load(self) -> StoredTikTokCredential | None:
        if not self.token_path.exists():
            return None
        if not self.key_path.is_file():
            raise ValueError("TikTok token vault key is missing")
        raw = self.token_path.read_bytes()
        if not raw or len(raw) > _MAX_VAULT_BYTES:
            raise ValueError("TikTok token vault is invalid")
        try:
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or envelope.get("schema_version") != 1:
                raise ValueError
            nonce = base64.urlsafe_b64decode(envelope["nonce"].encode("ascii"))
            ciphertext = base64.urlsafe_b64decode(
                envelope["ciphertext"].encode("ascii")
            )
            if len(nonce) != 12 or not ciphertext:
                raise ValueError
            plaintext = AESGCM(self._read_key()).decrypt(nonce, ciphertext, _AAD)
            payload = json.loads(plaintext)
            if not isinstance(payload, dict):
                raise TypeError
            return self._deserialize(payload)
        except (InvalidTag, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("TikTok token vault cannot be decrypted") from exc

    def delete(self) -> bool:
        if not self.token_path.exists():
            return False
        self.token_path.unlink()
        return True

    def _load_or_create_key(self) -> bytes:
        self.root.mkdir(parents=True, exist_ok=True)
        _restrict_secret_permissions(self.root, directory=True)
        if not self.key_path.exists():
            self._atomic_write(self.key_path, secrets.token_bytes(32), exclusive=True)
        return self._read_key()

    def _read_key(self) -> bytes:
        _restrict_secret_permissions(self.key_path, directory=False)
        key = self.key_path.read_bytes()
        if len(key) != 32:
            raise ValueError("TikTok token vault key is invalid")
        return key

    def _atomic_write(self, path: Path, payload: bytes, *, exclusive: bool = False) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _restrict_secret_permissions(temporary, directory=False)
            if exclusive:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            else:
                os.replace(temporary, path)
            _restrict_secret_permissions(path, directory=False)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _serialize(credential: StoredTikTokCredential) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "open_id": credential.open_id,
            "scopes": sorted(credential.scopes),
            "access_token": credential.access_token,
            "refresh_token": credential.refresh_token,
            "issued_at": credential.issued_at.astimezone(UTC).isoformat(),
            "access_expires_at": credential.access_expires_at.astimezone(UTC).isoformat(),
            "refresh_expires_at": credential.refresh_expires_at.astimezone(UTC).isoformat(),
            "authorized_username": credential.authorized_username,
        }

    @staticmethod
    def _deserialize(payload: dict[str, Any]) -> StoredTikTokCredential:
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("TikTok credential schema is unsupported")
        scopes = payload.get("scopes")
        if not isinstance(scopes, list) or any(not isinstance(item, str) for item in scopes):
            raise ValueError("TikTok credential scopes are invalid")
        return StoredTikTokCredential(
            open_id=str(payload["open_id"]),
            scopes=frozenset(scopes),
            access_token=str(payload["access_token"]),
            refresh_token=str(payload["refresh_token"]),
            issued_at=datetime.fromisoformat(str(payload["issued_at"])),
            access_expires_at=datetime.fromisoformat(str(payload["access_expires_at"])),
            refresh_expires_at=datetime.fromisoformat(str(payload["refresh_expires_at"])),
            authorized_username=str(payload.get("authorized_username", "")),
        )
