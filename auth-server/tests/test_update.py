from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.database import db
from app.routes.update import is_newer_version, parse_version

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    if hasattr(db._users_col, "_docs"):
        db._users_col._docs.clear()
    if hasattr(db._tokens_col, "_docs"):
        db._tokens_col._docs.clear()
    if hasattr(db._releases_col, "_docs"):
        db._releases_col._docs.clear()
    yield


def get_admin_token() -> str:
    # Register auto-approved admin
    res = client.post(
        "/auth/register",
        json={
            "email": settings.admin_email,
            "password": "adminPassword123!",
            "display_name": "Admin",
        },
    )
    if res.status_code != 201:
        # Login if exists
        login_res = client.post(
            "/auth/login",
            json={"email": settings.admin_email, "password": "adminPassword123!"},
        )
        return login_res.json()["access_token"]
    login_res = client.post(
        "/auth/login",
        json={"email": settings.admin_email, "password": "adminPassword123!"},
    )
    return login_res.json()["access_token"]


def test_version_helpers():
    assert parse_version("0.1.0") == (0, 1, 0)
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("2.0") == (2, 0, 0)

    assert is_newer_version("0.2.0", "0.1.0") is True
    assert is_newer_version("1.0.0", "0.9.9") is True
    assert is_newer_version("0.1.0", "0.1.0") is False
    assert is_newer_version("0.1.0", "0.2.0") is False


def test_check_update_empty():
    res = client.get("/api/v1/update/check?current_version=0.1.0&channel=stable")
    assert res.status_code == 200
    data = res.json()
    assert data["update_available"] is False
    assert data["current_version"] == "0.1.0"
    assert data["latest_version"] == "0.1.0"


def test_publish_and_check_update_flow():
    token = get_admin_token()
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Publish release v0.2.0
    release_payload = {
        "version": "0.2.0",
        "channel": "stable",
        "download_url": "https://releases.example.com/stable/0.2.0/content-bot.zip",
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "file_size": 10485760,
        "changelog": "- Tự động cập nhật R2\n- Bảo mật Master Password",
        "mandatory": False,
    }

    pub_res = client.post("/admin/releases", json=release_payload, headers=headers)
    assert pub_res.status_code == 201
    pub_data = pub_res.json()
    assert pub_data["version"] == "0.2.0"
    assert pub_data["download_url"] == release_payload["download_url"]

    # 2. Check update for client on v0.1.0 (should have update available)
    check_res = client.get("/api/v1/update/check?current_version=0.1.0&channel=stable")
    assert check_res.status_code == 200
    check_data = check_res.json()
    assert check_data["update_available"] is True
    assert check_data["latest_version"] == "0.2.0"
    assert check_data["sha256"] == release_payload["sha256"]
    assert "Master Password" in check_data["changelog"]

    # 3. Check update for client already on v0.2.0 (should be up to date)
    check_res2 = client.get("/api/v1/update/check?current_version=0.2.0&channel=stable")
    assert check_res2.status_code == 200
    assert check_res2.json()["update_available"] is False

    # 4. List releases
    list_res = client.get("/admin/releases", headers=headers)
    assert list_res.status_code == 200
    releases = list_res.json()
    assert len(releases) >= 1
    assert releases[0]["version"] == "0.2.0"

    # 5. Delete release
    del_res = client.delete("/admin/releases/0.2.0?channel=stable", headers=headers)
    assert del_res.status_code == 200

    # 6. Check update again after deletion
    check_res3 = client.get("/api/v1/update/check?current_version=0.1.0&channel=stable")
    assert check_res3.status_code == 200
    assert check_res3.json()["update_available"] is False
