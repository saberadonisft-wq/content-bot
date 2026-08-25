from pathlib import Path

import pytest

from app.config import settings
from app.services.credential_resolver import credential
from app.services.credential_vault import CredentialVault, _derive_key


def test_credential_vault_lifecycle(tmp_path: Path):
    vault = CredentialVault(tmp_path)

    # 1. Initially not configured
    assert not vault.is_configured
    assert not vault.is_unlocked

    # 2. Setup master password with too short password should fail
    with pytest.raises(ValueError, match="ít nhất 6 ký tự"):
        vault.setup_master_password("123")

    # 3. Setup with valid master password
    vault.setup_master_password("MySuperSecretPassword123!", {
        "youtube_api_key": "AIzaSyTestKey123456",
        "x_bearer_token": "AAAAATestTwitterBearerToken1234",
    })
    assert vault.is_configured
    assert vault.is_unlocked

    status = vault.get_status()
    assert status["is_master_password_set"] is True
    assert status["is_unlocked"] is True
    assert status["configured_keys"]["youtube_api_key"] is True
    assert status["configured_keys"]["x_bearer_token"] is True
    assert status["configured_keys"]["reddit_client_id"] is False
    assert status["masked_keys"]["youtube_api_key"].endswith("3456")

    # 4. Lock vault
    vault.lock()
    assert not vault.is_unlocked

    # 5. Unlock with wrong password should fail
    with pytest.raises(ValueError, match="không chính xác"):
        vault.unlock("WrongPassword!")

    # 6. Unlock with correct password
    assert vault.unlock("MySuperSecretPassword123!") is True
    assert vault.is_unlocked

    # 7. Update credentials
    vault.save_credentials({
        "gemini_api_key": "GeminiKey9999",
        "reddit_client_id": "RedditID1",
        "reddit_client_secret": "RedditSec2",
    })
    updated_status = vault.get_status()
    assert updated_status["platforms"]["gemini"] is True
    assert updated_status["platforms"]["reddit"] is True

    # 8. Change master password
    assert vault.change_master_password("MySuperSecretPassword123!", "NewMasterPass888!") is True
    vault.lock()

    # Old password no longer works
    with pytest.raises(ValueError):
        vault.unlock("MySuperSecretPassword123!")

    # New password works
    assert vault.unlock("NewMasterPass888!") is True


def test_vault_resolution_prefers_vault_then_falls_back_to_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = CredentialVault(tmp_path)
    monkeypatch.setattr(settings, "gemini_api_key", "env-gemini-key")
    monkeypatch.setattr("app.services.credential_resolver.get_vault", lambda: vault)

    vault.setup_master_password("vault-password", {"gemini_api_key": "vault-gemini-key"})
    assert settings.gemini_api_key == "env-gemini-key"
    assert credential("gemini_api_key") == "vault-gemini-key"

    vault.lock()
    assert credential("gemini_api_key") == "env-gemini-key"
    status = vault.get_status()
    assert status["configured_keys"]["gemini_api_key"] is True
    assert status["vault_configured_keys"]["gemini_api_key"] is False
    assert status["env_configured_keys"]["gemini_api_key"] is True
    assert status["credential_sources"]["gemini_api_key"] == "environment"
    assert status["masked_keys"]["gemini_api_key"] == ""


def test_unlock_migrates_legacy_mongodb_uri_out_of_vault(tmp_path: Path) -> None:
    vault = CredentialVault(tmp_path)
    password = "migration-password"
    salt = b"0123456789abcdef"
    key = _derive_key(password, salt)
    vault.secrets_dir.mkdir(parents=True, exist_ok=True)
    vault._encrypt_and_save(
        key,
        salt,
        {"mongodb_uri": "mongodb://legacy.invalid", "gemini_api_key": "keep-me"},
    )

    assert vault.unlock(password) is True
    assert vault.get_credential("mongodb_uri") is None
    assert vault.get_credential("gemini_api_key") == "keep-me"
    assert "mongodb_uri" not in vault.get_status()["configured_keys"]

    payload = vault.vault_file.read_text(encoding="utf-8")
    assert "legacy.invalid" not in payload


def test_vault_unlocks_automatically_after_application_restart(tmp_path: Path) -> None:
    vault = CredentialVault(tmp_path)
    vault.setup_master_password(
        "remember-this-password",
        {"gemini_api_key": "persistent-gemini-key"},
    )

    restarted_vault = CredentialVault(tmp_path)

    assert restarted_vault.is_unlocked is True
    assert restarted_vault.get_credential("gemini_api_key") == "persistent-gemini-key"


def test_explicit_lock_disables_automatic_unlock(tmp_path: Path) -> None:
    vault = CredentialVault(tmp_path)
    vault.setup_master_password(
        "remember-this-password",
        {"gemini_api_key": "persistent-gemini-key"},
    )

    vault.lock()
    restarted_vault = CredentialVault(tmp_path)

    assert restarted_vault.is_unlocked is False
    assert restarted_vault.get_credential("gemini_api_key") is None
