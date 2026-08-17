from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .database import db
from .models import UserResponse

security = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except Exception:
        return False


def create_access_token(
    user_id: str,
    email: str,
    role: str,
    user_status: str,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a signed RS256 JWT access token."""
    now = datetime.now(UTC)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.access_token_expire_minutes)

    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "status": user_status,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }

    private_key, _ = settings.ensure_jwt_keys()
    return jwt.encode(payload, private_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(
    user_id: str,
    expires_delta: timedelta | None = None,
) -> tuple[str, datetime]:
    """Generate an opaque secure refresh token."""
    now = datetime.now(UTC)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(days=settings.refresh_token_expire_days)

    token = secrets.token_urlsafe(48)
    db.store_refresh_token(token, user_id, expire)
    return token, expire


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify an RS256 JWT token using the public key."""
    _, public_key = settings.ensure_jwt_keys()
    try:
        return jwt.decode(
            token,
            public_key,
            algorithms=[settings.jwt_algorithm],
            options={"verify_signature": True, "verify_exp": True},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def doc_to_user_response(doc: dict[str, Any]) -> UserResponse:
    """Convert a database user document into a UserResponse model."""
    return UserResponse(
        id=str(doc["_id"]),
        email=doc["email"],
        display_name=doc.get("display_name") or doc["email"].split("@")[0],
        role=doc.get("role", "user"),
        status=doc.get("status", "pending"),
        auth_provider=doc.get("auth_provider", "email"),
        avatar_url=doc.get("avatar_url"),
        created_at=doc.get("created_at") or datetime.now(UTC),
        approved_at=doc.get("approved_at"),
        approved_by=doc.get("approved_by"),
        last_login_at=doc.get("last_login_at"),
    )


async def get_current_user(
    auth: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict[str, Any]:
    """FastAPI dependency to extract and verify the current authenticated user."""
    if not auth or not auth.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(auth.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token claims",
        )

    user = db.find_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    return user


async def require_approved_user(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Ensure user is active and approved."""
    user_status = current_user.get("status")
    if user_status == "pending":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản của bạn đang chờ quản trị viên phê duyệt.",
        )
    elif user_status == "banned":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản của bạn đã bị khóa.",
        )
    elif user_status == "rejected":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản của bạn đã bị từ chối.",
        )
    elif user_status != "approved":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản chưa được kích hoạt.",
        )

    return current_user


async def require_admin(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Ensure user has admin role."""
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chỉ quản trị viên mới có quyền thực hiện thao tác này.",
        )
    return current_user
