import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.credential_resolver import credential
from app.services.credential_vault import CredentialVault


def test_migration_disable_delete_and_locked_precedence(tmp_path, monkeypatch):
    vault = CredentialVault(tmp_path)
    vault.setup_master_password("password", {"gemini_api_key": "legacy-secret"})
    monkeypatch.setattr("app.services.credential_resolver.get_vault", lambda: vault)
    monkeypatch.setattr(settings, "gemini_api_key", "environment-secret")
    rows = vault.edit_gemini_keys(add=[" new-secret ", "new-secret", "legacy-secret"])
    assert rows["added"] == 1 and rows["duplicates"] == 2
    assert rows["keys"][0]["id"] == "gemini-legacy"
    assert "new-secret" not in json.dumps(rows)
    for row in rows["keys"]:
        vault.edit_gemini_keys(key_id=row["id"], changes={"enabled": False})
    assert credential("gemini_api_key") is None
    assert vault.get_status()["platforms"]["gemini"] is False
    with pytest.raises(ValueError, match="danh sách"):
        vault.save_credentials({"gemini_api_key": "resurrect"})
    vault.lock()
    assert credential("gemini_api_key") is None
    vault = CredentialVault(tmp_path)
    assert vault.gemini_keys("environment-secret") == []
    vault.unlock("password")
    for row in rows["keys"]:
        vault.edit_gemini_keys(key_id=row["id"], delete=True)
    assert vault.gemini_keys("environment-secret") == []
    vault.change_master_password("password", "password-new")
    vault.lock()
    vault.unlock("password-new")
    assert vault.gemini_keys("environment-secret") == []


def test_concurrent_import_and_other_credentials_do_not_lose_updates(tmp_path):
    vault = CredentialVault(tmp_path)
    vault.setup_master_password("password")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(vault.edit_gemini_keys, add=[f"secret-{i:04d}"]) for i in range(80)]
        futures.append(pool.submit(vault.save_credentials, {"youtube_api_key": "youtube-secret"}))
        for future in futures:
            future.result()
    recovered = CredentialVault(tmp_path)
    assert len(recovered.gemini_keys()) == 80
    assert recovered.get_credential("youtube_api_key") == "youtube-secret"
    assert "secret-" not in recovered.vault_file.read_text()


def test_failed_save_leaves_previous_memory_and_disk(tmp_path, monkeypatch):
    vault = CredentialVault(tmp_path)
    vault.setup_master_password("password")
    vault.edit_gemini_keys(add=["existing-secret"])
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(vault, "_encrypt_and_save", fail)
    with pytest.raises(OSError):
        vault.edit_gemini_keys(add=["new-secret"])
    assert len(vault.gemini_keys()) == 1
    assert len(CredentialVault(tmp_path).gemini_keys()) == 1


def test_key_api_masks_secrets_and_updates(tmp_path, monkeypatch):
    vault = CredentialVault(tmp_path)
    vault.setup_master_password("password")
    monkeypatch.setattr("app.api.credentials.get_vault", lambda: vault)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    client = TestClient(app)
    response = client.post("/api/v1/credentials/gemini/keys", json={"keys": "secret-123456\nsecret-123456"})
    assert response.status_code == 200
    assert "secret-123456" not in response.text
    key_id = response.json()["keys"][0]["id"]
    response = client.patch(f"/api/v1/credentials/gemini/keys/{key_id}", json={"enabled": False, "project_group": " project-a "})
    assert response.json()["keys"][0]["project_group"] is None
    assert response.json()["keys"][0]["state"] == "disabled"
    assert client.delete(f"/api/v1/credentials/gemini/keys/{key_id}").json()["keys"] == []
    for invalid in (["secret-invalid-array"], "secret-over-limit" * 100_000):
        response = client.post("/api/v1/credentials/gemini/keys", json={"keys": invalid})
        assert response.status_code == 422
        assert "secret-" not in response.text
