import json
import pytest
from pathlib import Path
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
