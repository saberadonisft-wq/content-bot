"""Master password-encrypted local vault for API credentials.

All 3rd-party API keys (YouTube, X, Reddit, Meta/Facebook/Instagram, TikTok, Gemini)
are stored exclusively on the user's local disk, protected with AES-256-GCM encryption
derived from a master password via PBKDF2-HMAC-SHA256. The derived unlock key can be
remembered for app restarts; on Windows it is additionally protected for the current
OS account with DPAPI.

Credentials never touch the cloud Auth Server or remote databases.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import secrets
import sys
import threading
import uuid
from copy import deepcopy
from ctypes import wintypes
from functools import wraps
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger("content_bot.credential_vault")

_AAD = b"content-bot:credential-vault:v1"
_PBKDF2_ITERATIONS = 100_000
_SALT_SIZE = 16
_NONCE_SIZE = 12
_KEY_SIZE = 32  # 256 bits
_DPAPI_DESCRIPTION = "Content Bot credential vault"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01

SUPPORTED_KEYS = (
    "youtube_api_key",
    "x_bearer_token",
    "reddit_client_id",
    "reddit_client_secret",
    "reddit_user_agent",
    "meta_access_token",
    "instagram_professional_user_id",
    "facebook_page_access_token",
    "facebook_page_id",
    "facebook_page_username",
    "tiktok_client_key",
    "tiktok_client_secret",
    "tiktok_redirect_uri",
    "gemini_api_key",
)


def _serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_SIZE,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data)
    return (
        _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))),
        buffer,
    )


def _protect_for_current_user(data: bytes) -> bytes:
    """Protect an unlock key with the current Windows account (DPAPI)."""

    if sys.platform != "win32":
        return data
    source, source_buffer = _blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        _DPAPI_DESCRIPTION,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        # Keep the input buffer alive until CryptProtectData has returned.
        del source_buffer
        ctypes.windll.kernel32.LocalFree(output.pbData)


def _unprotect_for_current_user(data: bytes) -> bytes:
    if sys.platform != "win32":
        return data
    source, source_buffer = _blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        del source_buffer
        ctypes.windll.kernel32.LocalFree(output.pbData)


class CredentialVault:
    """Manages master-password-encrypted local credentials."""

    def __init__(self, secrets_dir: Path) -> None:
        self._lock = threading.RLock()
        self.secrets_dir = secrets_dir.resolve()
        self.vault_file = self.secrets_dir / "credentials.enc"
        self.device_unlock_file = self.secrets_dir / "credentials.unlock"
        self._unlocked = False
        self._cached_credentials: dict[str, Any] = {}
        self._gemini_checks: dict[str, dict[str, Any]] = {}
        self._current_key: bytes | None = None
        self._try_auto_unlock()

    @property
    def is_configured(self) -> bool:
        """Check if the vault file exists on disk."""
        return self.vault_file.is_file()

    @property
    def is_unlocked(self) -> bool:
        """Check if the vault is currently unlocked in memory."""
        return self._unlocked

    @_serialized
    def setup_master_password(
        self,
        master_password: str,
        initial_credentials: dict[str, str] | None = None,
    ) -> bool:
        """Set up a new vault with the given master password."""
        if not master_password or len(master_password) < 6:
            raise ValueError("Master password phải có ít nhất 6 ký tự.")

        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        salt = secrets.token_bytes(_SALT_SIZE)
        key = _derive_key(master_password, salt)

        clean_creds: dict[str, str] = {}
        if initial_credentials:
            for k, v in initial_credentials.items():
                if k in SUPPORTED_KEYS and v:
                    clean_creds[k] = str(v).strip()

        self._encrypt_and_save(key, salt, clean_creds)
        self._unlocked = True
        self._cached_credentials = clean_creds
        self._current_key = key
        self._remember_unlock_key(key)
        return True

    @_serialized
    def unlock(self, master_password: str) -> bool:
        """Unlock the vault with the master password. Returns True on success, raises ValueError on bad password."""
        if not self.is_configured:
            raise ValueError("Chưa thiết lập master password. Vui lòng thiết lập trước.")

        try:
            payload = self._read_payload()
            salt = base64.b64decode(payload["kdf"]["salt"])
            key = _derive_key(master_password, salt)
            self._unlock_with_key(key, payload)
            self._remember_unlock_key(key)
            logger.info("Credential vault unlocked successfully.")
            return True
        except InvalidTag as err:
            logger.warning("Failed to unlock credential vault: InvalidTag (wrong password).")
            raise ValueError("Mật khẩu Master không chính xác.") from err
        except (KeyError, TypeError, ValueError) as err:
            logger.warning("Failed to unlock credential vault due to data format: %s", err)
            raise ValueError("File Vault bị hỏng hoặc sai định dạng.") from err

    @_serialized
    def lock(self) -> None:
        """Lock the vault and clear credentials from memory."""
        self._unlocked = False
        self._cached_credentials = {}
        self._current_key = None
        try:
            self.device_unlock_file.unlink(missing_ok=True)
        except OSError as err:
            logger.warning("Unable to remove the saved device unlock key: %s", err)
        logger.info("Credential vault locked.")

    @_serialized
    def save_credentials(self, new_credentials: dict[str, str]) -> dict[str, Any]:
        """Save/update credentials in the vault."""
        if not self._unlocked or self._current_key is None:
            raise ValueError("Vault đang bị khóa. Vui lòng mở khóa bằng Master password trước.")

        payload = json.loads(self.vault_file.read_text(encoding="utf-8"))
        salt = base64.b64decode(payload["kdf"]["salt"])

        updated = deepcopy(self._cached_credentials)
        for k, v in new_credentials.items():
            if k in SUPPORTED_KEYS:
                if k == "gemini_api_key" and "_gemini_keyring" in updated:
                    raise ValueError("Hãy quản lý Gemini key trong danh sách Gemini AI.")
                val = str(v).strip() if v is not None else ""
                if val:
                    updated[k] = val
                elif k in updated and val == "":
                    # Empty string deletes the key
                    del updated[k]

        self._encrypt_and_save(self._current_key, salt, updated)
        self._cached_credentials = updated
        return self.get_status()

    @_serialized
    def change_master_password(self, old_password: str, new_password: str) -> bool:
        """Change the vault master password."""
        self.unlock(old_password)
        if not new_password or len(new_password) < 6:
            raise ValueError("Master password mới phải có ít nhất 6 ký tự.")

        salt = secrets.token_bytes(_SALT_SIZE)
        new_key = _derive_key(new_password, salt)
        self._encrypt_and_save(new_key, salt, self._cached_credentials)
        self._current_key = new_key
        self._remember_unlock_key(new_key)
        return True

    def _read_payload(self) -> dict[str, Any]:
        payload = json.loads(self.vault_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("Invalid credential vault payload")
        return payload

    def _unlock_with_key(self, key: bytes, payload: dict[str, Any] | None = None) -> None:
        if len(key) != _KEY_SIZE:
            raise ValueError("Invalid credential vault key")
        payload = payload or self._read_payload()
        salt = base64.b64decode(payload["kdf"]["salt"])
        nonce = base64.b64decode(payload["nonce"])
        ciphertext = base64.b64decode(payload["ciphertext"])
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _AAD)
        creds = json.loads(plaintext.decode("utf-8"))
        raw_creds = creds if isinstance(creds, dict) else {}
        clean_creds = {
            name: str(value).strip()
            for name, value in raw_creds.items()
            if name in SUPPORTED_KEYS and value is not None and str(value).strip()
        }
        if "_gemini_keyring" in raw_creds:
            ring = raw_creds["_gemini_keyring"]
            if not isinstance(ring, dict) or ring.get("version") != 1 or not isinstance(ring.get("keys"), list):
                raise ValueError("Invalid Gemini keyring")
            clean_creds["_gemini_keyring"] = ring
        self._cached_credentials = clean_creds
        self._current_key = key
        self._unlocked = True
        if clean_creds != raw_creds:
            # Drop unsupported legacy entries, including the old MongoDB URI,
            # as soon as the vault can be safely rewritten.
            self._encrypt_and_save(key, salt, clean_creds)

    def _remember_unlock_key(self, key: bytes) -> None:
        """Remember the vault key for this OS user so restarts can unlock it."""

        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        protected = _protect_for_current_user(key)
        temp_file = self.device_unlock_file.with_suffix(".unlock.tmp")
        temp_file.write_bytes(protected)
        try:
            os.chmod(temp_file, 0o600)
        except OSError:
            pass
        temp_file.replace(self.device_unlock_file)

    def _try_auto_unlock(self) -> None:
        if not self.vault_file.is_file() or not self.device_unlock_file.is_file():
            return
        try:
            key = _unprotect_for_current_user(self.device_unlock_file.read_bytes())
            self._unlock_with_key(key)
            logger.info("Credential vault unlocked automatically for the current OS user.")
        except Exception as err:
            # A copied/tampered token or a different Windows account must not stop startup.
            self._unlocked = False
            self._cached_credentials = {}
            self._current_key = None
            logger.warning("Credential vault auto-unlock was unavailable: %s", type(err).__name__)

    def _encrypt_and_save(self, key: bytes, salt: bytes, data: dict[str, Any]) -> None:
        plaintext = json.dumps(data, ensure_ascii=True).encode("utf-8")
        nonce = secrets.token_bytes(_NONCE_SIZE)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, _AAD)

        payload = {
            "version": 2 if "_gemini_keyring" in data else 1,
            "kdf": {
                "algorithm": "pbkdf2_hmac_sha256",
                "iterations": _PBKDF2_ITERATIONS,
                "salt": base64.b64encode(salt).decode("utf-8"),
            },
            "nonce": base64.b64encode(nonce).decode("utf-8"),
            "ciphertext": base64.b64encode(ciphertext).decode("utf-8"),
        }

        temp_file = self.vault_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_file.replace(self.vault_file)

    @_serialized
    def get_credential(self, key: str) -> str | None:
        """Return a Vault credential only while the Vault is unlocked."""

        if not self._unlocked or key not in SUPPORTED_KEYS:
            return None
        value = self._cached_credentials.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    @_serialized
    def gemini_keyring_configured(self) -> bool:
        # The public format version is also a fail-closed marker while locked.
        return "_gemini_keyring" in self._cached_credentials or (
            self.is_configured and self._read_payload().get("version", 1) >= 2
        )

    @_serialized
    def gemini_keys(self, environment_key: str | None = None) -> list[dict[str, Any]]:
        """Internal secret snapshots. Never return these directly through an API."""
        if self.gemini_keyring_configured():
            ring = self._cached_credentials.get("_gemini_keyring", {})
            return deepcopy(ring.get("keys", [])) if self._unlocked else []
        legacy = self.get_credential("gemini_api_key") or (environment_key or "").strip()
        return [{"id": "gemini-legacy", "name": "Gemini (cấu hình cũ)", "secret": legacy,
                 "enabled": True, "project_group": None}] if legacy else []

    @_serialized
    def gemini_key_status(self, environment_key: str | None = None) -> dict[str, Any]:
        rows = []
        for key in self.gemini_keys(environment_key):
            check = self._gemini_checks.get(key["id"], {})
            rows.append({k: key[k] for k in ("id", "name", "enabled")} | {
                "project_group": None,  # Legacy response field; groups are no longer used.
                "masked_key": "••••••••" + (key["secret"][-4:] if len(key["secret"]) >= 8 else ""),
                "state": check.get("state", "untested") if key["enabled"] else "disabled",
                "checked_model": check.get("checked_model"), "checked_at": check.get("checked_at"),
            })
        return {"version": 1, "configured": self.gemini_keyring_configured(), "keys": rows}

    @_serialized
    def edit_gemini_keys(self, *, add: list[str] | None = None, key_id: str | None = None,
                         changes: dict[str, Any] | None = None, delete: bool = False,
                         environment_key: str | None = None) -> dict[str, Any]:
        if not self._unlocked or self._current_key is None:
            raise ValueError("Vault đang khóa. Vui lòng mở khóa trước.")
        rows = self.gemini_keys(environment_key)
        added = duplicates = 0
        known = {row["secret"] for row in rows}
        for value in add or []:
            value = value.strip()
            if not value:
                continue
            if value in known:
                duplicates += 1
                continue
            if any(ch.isspace() for ch in value):
                raise ValueError("Một key không được chứa khoảng trắng bên trong.")
            rows.append({"id": "gk-" + uuid.uuid4().hex, "name": f"Gemini {len(rows) + 1}",
                         "secret": value, "enabled": True})
            known.add(value)
            added += 1
        if key_id is not None:
            row = next((row for row in rows if row["id"] == key_id), None)
            if row is None:
                raise ValueError("Không tìm thấy Gemini key.")
            if delete:
                rows.remove(row)
            else:
                row.update({k: v for k, v in (changes or {}).items() if k in {"name", "enabled"}})
        for row in rows:
            row.pop("project_group", None)
        updated = deepcopy(self._cached_credentials)
        updated.pop("gemini_api_key", None)
        updated["_gemini_keyring"] = {"version": 1, "keys": rows}
        salt = base64.b64decode(self._read_payload()["kdf"]["salt"])
        self._encrypt_and_save(self._current_key, salt, updated)
        self._cached_credentials = updated
        return self.gemini_key_status(environment_key) | {"added": added, "duplicates": duplicates}

    @_serialized
    def record_gemini_check(self, key_id: str, *, state: str, model: str, checked_at: str) -> None:
        self._gemini_checks[key_id] = {"state": state, "checked_model": model, "checked_at": checked_at}

    @_serialized
    def get_status(self) -> dict[str, Any]:
        """Return non-sensitive status of configured credentials."""
        from ..config import settings

        vault_configured_keys: dict[str, bool] = {}
        env_configured_keys: dict[str, bool] = {}
        configured_keys: dict[str, bool] = {}
        masked_keys: dict[str, str] = {}
        credential_sources: dict[str, str] = {}

        for k in SUPPORTED_KEYS:
            vault_value = self.get_credential(k)
            env_value = getattr(settings, k, None)
            env_value = env_value.strip() if isinstance(env_value, str) else env_value
            vault_set = bool(vault_value)
            env_set = bool(env_value)
            effective_set = vault_set or env_set
            vault_configured_keys[k] = vault_set
            env_configured_keys[k] = env_set
            configured_keys[k] = effective_set
            credential_sources[k] = "vault" if vault_set else "environment" if env_set else "none"
            if vault_value and len(vault_value) >= 8:
                masked_keys[k] = f"••••••••{vault_value[-4:]}"
            elif vault_value:
                masked_keys[k] = "••••••••"
            else:
                masked_keys[k] = ""

        def effective(key: str) -> str | None:
            vault_value = self.get_credential(key)
            if vault_value:
                return vault_value
            env_value = getattr(settings, key, None)
            return env_value.strip() if isinstance(env_value, str) and env_value.strip() else None

        if self.gemini_keyring_configured():
            active = [key for key in self.gemini_keys() if key["enabled"]]
            configured_keys["gemini_api_key"] = bool(active)
            vault_configured_keys["gemini_api_key"] = bool(active)
            credential_sources["gemini_api_key"] = "vault" if active else "none"

        return {
            "is_master_password_set": self.is_configured,
            "is_unlocked": self._unlocked,
            "configured_keys": configured_keys,
            "vault_configured_keys": vault_configured_keys,
            "env_configured_keys": env_configured_keys,
            "credential_sources": credential_sources,
            "masked_keys": masked_keys,
            "platforms": {
                "youtube": bool(effective("youtube_api_key")),
                "x_twitter": bool(effective("x_bearer_token")),
                "reddit": bool(
                    effective("reddit_client_id") and effective("reddit_client_secret")
                ),
                "meta_instagram": bool(
                    effective("meta_access_token") and effective("instagram_professional_user_id")
                ),
                "facebook_page": bool(
                    effective("facebook_page_access_token") and effective("facebook_page_id")
                ),
                "tiktok": bool(
                    effective("tiktok_client_key") and effective("tiktok_client_secret")
                ),
                "gemini": configured_keys["gemini_api_key"],
            },
        }


# Global vault singleton initialized to settings.data_dir / secrets
_vault_instance: CredentialVault | None = None
_vault_instance_lock = threading.Lock()


def get_vault() -> CredentialVault:
    global _vault_instance
    with _vault_instance_lock:
        if _vault_instance is None:
            from ..config import settings

            secrets_dir = settings.data_dir / "secrets"
            _vault_instance = CredentialVault(secrets_dir)
        return _vault_instance
