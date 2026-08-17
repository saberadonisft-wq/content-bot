"""Master password-encrypted local vault for API credentials.

All 3rd-party API keys (YouTube, X, Reddit, Meta/Facebook/Instagram, TikTok, Gemini)
are stored exclusively on the user's local disk, protected with AES-256-GCM encryption
derived from a master password via PBKDF2-HMAC-SHA256.

Credentials never touch the cloud Auth Server or remote databases.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
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
    "mongodb_uri",
)


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_SIZE,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


class CredentialVault:
    """Manages master-password-encrypted local credentials."""

    def __init__(self, secrets_dir: Path) -> None:
        self.secrets_dir = secrets_dir.resolve()
        self.vault_file = self.secrets_dir / "credentials.enc"
        self._unlocked = False
        self._cached_credentials: dict[str, str] = {}
        self._current_key: bytes | None = None

    @property
    def is_configured(self) -> bool:
        """Check if the vault file exists on disk."""
        return self.vault_file.is_file()

    @property
    def is_unlocked(self) -> bool:
        """Check if the vault is currently unlocked in memory."""
        return self._unlocked

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

        clean_creds = {}
        if initial_credentials:
            for k, v in initial_credentials.items():
                if k in SUPPORTED_KEYS and v:
                    clean_creds[k] = str(v).strip()

        self._encrypt_and_save(key, salt, clean_creds)
        self._unlocked = True
        self._cached_credentials = clean_creds
        self._current_key = key
        self._apply_to_settings()
        return True

    def unlock(self, master_password: str) -> bool:
        """Unlock the vault with the master password. Returns True on success, raises ValueError on bad password."""
        if not self.is_configured:
            raise ValueError("Chưa thiết lập master password. Vui lòng thiết lập trước.")

        try:
            payload = json.loads(self.vault_file.read_text(encoding="utf-8"))
            salt = base64.b64decode(payload["kdf"]["salt"])
            nonce = base64.b64decode(payload["nonce"])
            ciphertext = base64.b64decode(payload["ciphertext"])

            key = _derive_key(master_password, salt)
            aesgcm = AESGCM(key)
            plaintext = aesgcm.decrypt(nonce, ciphertext, _AAD)
            creds = json.loads(plaintext.decode("utf-8"))

            self._unlocked = True
            self._cached_credentials = creds if isinstance(creds, dict) else {}
            self._current_key = key
            self._apply_to_settings()
            logger.info("Credential vault unlocked successfully.")
            return True
        except InvalidTag as err:
            logger.warning("Failed to unlock credential vault: InvalidTag (wrong password).")
            raise ValueError("Mật khẩu Master không chính xác.") from err
        except (KeyError, ValueError) as err:
            logger.warning("Failed to unlock credential vault due to data format: %s", err)
            raise ValueError("File Vault bị hỏng hoặc sai định dạng.") from err

    def lock(self) -> None:
        """Lock the vault and clear credentials from memory."""
        self._unlocked = False
        self._cached_credentials = {}
        self._current_key = None
        logger.info("Credential vault locked.")

    def save_credentials(self, new_credentials: dict[str, str]) -> dict[str, Any]:
        """Save/update credentials in the vault."""
        if not self._unlocked or self._current_key is None:
            raise ValueError("Vault đang bị khóa. Vui lòng mở khóa bằng Master password trước.")

        payload = json.loads(self.vault_file.read_text(encoding="utf-8"))
        salt = base64.b64decode(payload["kdf"]["salt"])

        for k, v in new_credentials.items():
            if k in SUPPORTED_KEYS:
                val = str(v).strip() if v is not None else ""
                if val:
                    self._cached_credentials[k] = val
                elif k in self._cached_credentials and val == "":
                    # Empty string deletes the key
                    del self._cached_credentials[k]

        self._encrypt_and_save(self._current_key, salt, self._cached_credentials)
        self._apply_to_settings()
        return self.get_status()

    def change_master_password(self, old_password: str, new_password: str) -> bool:
        """Change the vault master password."""
        self.unlock(old_password)
        if not new_password or len(new_password) < 6:
            raise ValueError("Master password mới phải có ít nhất 6 ký tự.")

        salt = secrets.token_bytes(_SALT_SIZE)
        new_key = _derive_key(new_password, salt)
        self._encrypt_and_save(new_key, salt, self._cached_credentials)
        self._current_key = new_key
        return True

    def _encrypt_and_save(self, key: bytes, salt: bytes, data: dict[str, str]) -> None:
        plaintext = json.dumps(data, ensure_ascii=True).encode("utf-8")
        nonce = secrets.token_bytes(_NONCE_SIZE)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, _AAD)

        payload = {
            "version": 1,
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

    def _apply_to_settings(self) -> None:
        """Apply unlocked credentials to backend settings in memory."""
        try:
            from ..config import settings

            for k, v in self._cached_credentials.items():
                if hasattr(settings, k) and v:
                    setattr(settings, k, v)
        except Exception as err:
            logger.warning("Could not apply credentials to settings: %s", err)

    def get_status(self) -> dict[str, Any]:
        """Return non-sensitive status of configured credentials."""
        configured_keys = {}
        masked_keys = {}

        for k in SUPPORTED_KEYS:
            val = self._cached_credentials.get(k, "")
            is_set = bool(val)
            configured_keys[k] = is_set
            if is_set and len(val) >= 8:
                masked_keys[k] = f"••••••••{val[-4:]}"
            elif is_set:
                masked_keys[k] = "••••••••"
            else:
                masked_keys[k] = ""

        return {
            "is_master_password_set": self.is_configured,
            "is_unlocked": self._unlocked,
            "configured_keys": configured_keys,
            "masked_keys": masked_keys,
            "platforms": {
                "youtube": bool(self._cached_credentials.get("youtube_api_key")),
                "x_twitter": bool(self._cached_credentials.get("x_bearer_token")),
                "reddit": bool(
                    self._cached_credentials.get("reddit_client_id")
                    and self._cached_credentials.get("reddit_client_secret")
                ),
                "meta_instagram": bool(
                    self._cached_credentials.get("meta_access_token")
                    and self._cached_credentials.get("instagram_professional_user_id")
                ),
                "facebook_page": bool(
                    self._cached_credentials.get("facebook_page_access_token")
                    and self._cached_credentials.get("facebook_page_id")
                ),
                "tiktok": bool(
                    self._cached_credentials.get("tiktok_client_key")
                    and self._cached_credentials.get("tiktok_client_secret")
                ),
                "gemini": bool(self._cached_credentials.get("gemini_api_key")),
                "mongodb": bool(self._cached_credentials.get("mongodb_uri")),
            },
        }


# Global vault singleton initialized to settings.data_dir / secrets
_vault_instance: CredentialVault | None = None


def get_vault() -> CredentialVault:
    global _vault_instance
    if _vault_instance is None:
        from ..config import settings

        secrets_dir = settings.data_dir / "secrets"
        _vault_instance = CredentialVault(secrets_dir)
    return _vault_instance
