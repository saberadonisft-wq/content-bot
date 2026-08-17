from __future__ import annotations

from datetime import UTC, datetime
from fastapi import APIRouter, HTTPException, status

from ..auth import (
    create_access_token,
    create_refresh_token,
    doc_to_user_response,
    verify_password,
)
from ..config import settings
from ..database import db
from ..models import LoginRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login", response_model=TokenResponse)
def login(request: LoginRequest) -> TokenResponse:
    """Log in with email and password."""
    email_clean = request.email.lower().strip()
    user = db.find_user_by_email(email_clean)

    if not user or not user.get("password_hash"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email hoặc mật khẩu không chính xác.",
        )

    if not verify_password(request.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email hoặc mật khẩu không chính xác.",
        )

    user_status = user.get("status", "pending")
    if user_status == "banned":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản này đã bị khóa bởi quản trị viên.",
        )
    elif user_status == "rejected":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản này đã bị từ chối phê duyệt.",
        )

    # Update last_login_at
    db.update_user(str(user["_id"]), {"last_login_at": datetime.now(UTC)})
    user["last_login_at"] = datetime.now(UTC)

    user_id = str(user["_id"])
    role = user.get("role", "user")
    access_token = create_access_token(
        user_id=user_id,
        email=user["email"],
        role=role,
        user_status=user_status,
    )
    refresh_token, _ = create_refresh_token(user_id=user_id)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        user=doc_to_user_response(user),
    )
