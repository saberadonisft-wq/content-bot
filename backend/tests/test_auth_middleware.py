from __future__ import annotations

from datetime import UTC, datetime, timedelta
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.middleware.auth import (
    get_current_user,
    require_admin_user,
    require_approved_user,
)


@pytest.fixture
def rsa_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem


@pytest.fixture
def test_app():
    app = FastAPI()

    @app.get("/public")
    def public_endpoint():
        return {"status": "ok"}

    @app.get("/protected")
    def protected_endpoint(user: dict = Depends(require_approved_user)):
        return {"user": user["email"], "status": user["status"]}

    @app.get("/admin-only")
    def admin_endpoint(user: dict = Depends(require_admin_user)):
        return {"admin": user["email"]}

    return app


def test_auth_disabled_by_default(test_app):
    settings.content_bot_auth_enabled = False
    client = TestClient(test_app)

    resp = client.get("/protected")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    admin_resp = client.get("/admin-only")
    assert admin_resp.status_code == 200


def test_auth_enabled_requires_token(test_app, rsa_keys):
    private_pem, public_pem = rsa_keys
    settings.content_bot_auth_enabled = True
    settings.content_bot_auth_public_key = public_pem

    client = TestClient(test_app)

    # 1. No token -> 401
    resp = client.get("/protected")
    assert resp.status_code == 401

    # 2. Token with pending status -> 403
    pending_payload = {
        "sub": "user123",
        "email": "pending@example.com",
        "role": "user",
        "status": "pending",
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
    }
    pending_token = jwt.encode(pending_payload, private_pem, algorithm="RS256")
    pending_resp = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {pending_token}"},
    )
    assert pending_resp.status_code == 403
    assert "chờ phê duyệt" in pending_resp.json()["detail"]

    # 3. Token with approved status -> 200
    approved_payload = {
        "sub": "user123",
        "email": "approved@example.com",
        "role": "user",
        "status": "approved",
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
    }
    approved_token = jwt.encode(approved_payload, private_pem, algorithm="RS256")
    approved_resp = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {approved_token}"},
    )
    assert approved_resp.status_code == 200
    assert approved_resp.json()["user"] == "approved@example.com"

    # 4. User is not admin calling admin endpoint -> 403
    admin_resp = client.get(
        "/admin-only",
        headers={"Authorization": f"Bearer {approved_token}"},
    )
    assert admin_resp.status_code == 403

    # 5. Admin token calling admin endpoint -> 200
    admin_payload = {
        "sub": "admin123",
        "email": "admin@example.com",
        "role": "admin",
        "status": "approved",
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
    }
    admin_token = jwt.encode(admin_payload, private_pem, algorithm="RS256")
    admin_resp2 = client.get(
        "/admin-only",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert admin_resp2.status_code == 200
    assert admin_resp2.json()["admin"] == "admin@example.com"

    # Reset
    settings.content_bot_auth_enabled = False
    settings.content_bot_auth_public_key = ""
