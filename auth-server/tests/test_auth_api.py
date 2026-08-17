from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.database import db

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    if hasattr(db._users_col, "_docs"):
        db._users_col._docs.clear()
    if hasattr(db._tokens_col, "_docs"):
        db._tokens_col._docs.clear()
    yield


def test_public_key_endpoint():
    response = client.get("/auth/public-key")
    assert response.status_code == 200
    data = response.json()
    assert "public_key" in data
    assert "BEGIN PUBLIC KEY" in data["public_key"]
    assert data["algorithm"] == "RS256"


def test_regular_user_registration_pending():
    response = client.post(
        "/auth/register",
        json={
            "email": "testuser@example.com",
            "password": "strongPassword123!",
            "display_name": "Test User",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "pending"
    assert "chờ Quản trị viên duyệt" in data["message"]
    assert data["user"]["role"] == "user"


def test_admin_user_auto_approved():
    response = client.post(
        "/auth/register",
        json={
            "email": settings.admin_email,
            "password": "adminPassword123!",
            "display_name": "Site Admin",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "approved"
    assert data["user"]["role"] == "admin"


def test_duplicate_email_registration_fails():
    client.post(
        "/auth/register",
        json={
            "email": "user@example.com",
            "password": "strongPassword123!",
            "display_name": "User One",
        },
    )
    duplicate = client.post(
        "/auth/register",
        json={
            "email": "user@example.com",
            "password": "anotherPassword123!",
            "display_name": "User Two",
        },
    )
    assert duplicate.status_code == 400
    assert "đã được sử dụng" in duplicate.json()["detail"]


def test_wrong_password_fails():
    client.post(
        "/auth/register",
        json={
            "email": "user@example.com",
            "password": "strongPassword123!",
            "display_name": "User One",
        },
    )
    wrong = client.post(
        "/auth/login",
        json={
            "email": "user@example.com",
            "password": "wrongPassword!",
        },
    )
    assert wrong.status_code == 401


def test_regular_user_cannot_access_admin_endpoints():
    client.post(
        "/auth/register",
        json={
            "email": "user@example.com",
            "password": "strongPassword123!",
            "display_name": "User One",
        },
    )
    login_resp = client.post(
        "/auth/login",
        json={
            "email": "user@example.com",
            "password": "strongPassword123!",
        },
    )
    token = login_resp.json()["access_token"]
    admin_call = client.get(
        "/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert admin_call.status_code == 403


def test_full_approval_flow_and_admin_actions():
    # 1. Register Admin
    admin_reg = client.post(
        "/auth/register",
        json={
            "email": settings.admin_email,
            "password": "adminPassword123!",
            "display_name": "Site Admin",
        },
    )
    assert admin_reg.status_code == 201

    # 2. Login Admin
    admin_login = client.post(
        "/auth/login",
        json={
            "email": settings.admin_email,
            "password": "adminPassword123!",
        },
    )
    assert admin_login.status_code == 200
    admin_data = admin_login.json()
    admin_token = admin_data["access_token"]
    assert admin_data["user"]["role"] == "admin"
    assert admin_data["user"]["status"] == "approved"

    # 3. Register Regular User (Pending)
    user_reg = client.post(
        "/auth/register",
        json={
            "email": "worker@example.com",
            "password": "workerPassword123!",
            "display_name": "Worker One",
        },
    )
    assert user_reg.status_code == 201
    user_id = user_reg.json()["user"]["id"]

    # 4. User logs in (receives pending token)
    user_login = client.post(
        "/auth/login",
        json={
            "email": "worker@example.com",
            "password": "workerPassword123!",
        },
    )
    assert user_login.status_code == 200
    user_data = user_login.json()
    assert user_data["user"]["status"] == "pending"
    user_refresh = user_data["refresh_token"]

    # 5. Admin lists pending users
    pending_list = client.get(
        "/admin/users/pending",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert pending_list.status_code == 200
    pending_users = pending_list.json()
    assert len(pending_users) == 1
    assert pending_users[0]["email"] == "worker@example.com"

    # 6. Admin approves user
    approve_resp = client.put(
        f"/admin/users/{user_id}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "approved"

    # 7. User refreshes token
    refresh_resp = client.post(
        "/auth/refresh",
        json={"refresh_token": user_refresh},
    )
    assert refresh_resp.status_code == 200
    new_user_data = refresh_resp.json()
    assert new_user_data["user"]["status"] == "approved"

    # 8. User calls /auth/me
    me_resp = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {new_user_data['access_token']}"},
    )
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == "worker@example.com"
    assert me_resp.json()["status"] == "approved"

    # 9. Admin promotes user to admin
    role_resp = client.put(
        f"/admin/users/{user_id}/role",
        json={"role": "admin"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert role_resp.status_code == 200
    assert role_resp.json()["role"] == "admin"

    # 10. Admin bans user
    ban_resp = client.put(
        f"/admin/users/{user_id}/ban",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert ban_resp.status_code == 200
    assert ban_resp.json()["status"] == "banned"

    # 11. Banned user cannot log in
    banned_login = client.post(
        "/auth/login",
        json={
            "email": "worker@example.com",
            "password": "workerPassword123!",
        },
    )
    assert banned_login.status_code == 403

    # 12. Admin unbans user
    unban_resp = client.put(
        f"/admin/users/{user_id}/unban",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert unban_resp.status_code == 200
    assert unban_resp.json()["status"] == "approved"
