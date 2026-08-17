from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import (
    create_access_token,
    create_refresh_token,
    doc_to_user_response,
    get_current_user,
)
from ..config import settings
from ..database import db
from ..models import (
    MessageResponse,
    PublicKeyResponse,
    RefreshTokenRequest,
    TokenResponse,
    UserResponse,
)

router = APIRouter(prefix="/auth", tags=["Token & Profile"])


@router.post("/refresh", response_model=TokenResponse)
def refresh_token(request: RefreshTokenRequest) -> TokenResponse:
    """Exchange a valid refresh token for a new access token and refresh token."""
    stored_token = db.find_refresh_token(request.refresh_token)
    if not stored_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token không hợp lệ hoặc đã hết hạn.",
        )

    # Check expiration
    expires_at = stored_token.get("expires_at")
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at and expires_at < datetime.now(UTC):
        db.revoke_refresh_token(request.refresh_token)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token đã hết hạn. Vui lòng đăng nhập lại.",
        )

    user = db.find_user_by_id(stored_token["user_id"])
    if not user:
        db.revoke_refresh_token(request.refresh_token)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Người dùng không tồn tại.",
        )

    user_status = user.get("status", "pending")
    if user_status == "banned":
        db.revoke_refresh_token(request.refresh_token)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản đã bị khóa.",
        )
    elif user_status == "rejected":
        db.revoke_refresh_token(request.refresh_token)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản đã bị từ chối phê duyệt.",
        )

    # Revoke old refresh token (token rotation)
    db.revoke_refresh_token(request.refresh_token)

    user_id = str(user["_id"])
    role = user.get("role", "user")
    new_access_token = create_access_token(
        user_id=user_id,
        email=user["email"],
        role=role,
        user_status=user_status,
    )
    new_refresh_token, _ = create_refresh_token(user_id=user_id)

    return TokenResponse(
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        user=doc_to_user_response(user),
    )


@router.get("/me", response_model=UserResponse)
def get_me(current_user: dict[str, Any] = Depends(get_current_user)) -> UserResponse:
    """Retrieve profile and latest status of the currently authenticated user."""
    return doc_to_user_response(current_user)


@router.post("/logout", response_model=MessageResponse)
def logout(
    request: RefreshTokenRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> MessageResponse:
    """Revoke refresh token and log out user."""
    db.revoke_refresh_token(request.refresh_token)
    return MessageResponse(message="Đăng xuất thành công.")


@router.get("/public-key", response_model=PublicKeyResponse)
def get_public_key() -> PublicKeyResponse:
    """Public endpoint returning the RSA public key in PEM format."""
    _, public_key = settings.ensure_jwt_keys()
    return PublicKeyResponse(
        public_key=public_key,
        algorithm=settings.jwt_algorithm,
    )
