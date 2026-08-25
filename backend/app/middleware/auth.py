from __future__ import annotations

import logging
from typing import Any

import httpx
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import settings

logger = logging.getLogger("content_bot.auth_middleware")

security = HTTPBearer(auto_error=False)

# Cached public key to avoid network roundtrips on every request
_CACHED_PUBLIC_KEY: str | None = None


def get_public_key() -> str:
    """Retrieve RSA public key from settings or fetch from auth server."""
    global _CACHED_PUBLIC_KEY

    # 1. Configured static public key
    if settings.content_bot_auth_public_key:
        return settings.content_bot_auth_public_key

    # 2. Return cached key
    if _CACHED_PUBLIC_KEY:
        return _CACHED_PUBLIC_KEY

    # 3. Fetch from auth server
    auth_server_url = settings.content_bot_auth_server_url.rstrip("/")
    if not auth_server_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Máy chủ chưa được cấu hình AUTH_SERVER_URL để xác thực JWT.",
        )

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{auth_server_url}/auth/public-key")
            if resp.status_code == 200:
                data = resp.json()
                key = data.get("public_key")
                if key:
                    _CACHED_PUBLIC_KEY = key
                    return key
    except Exception as e:
        logger.error(f"Failed to fetch public key from auth server: {e}")

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Không thể kết nối đến máy chủ xác thực để lấy khóa công khai.",
    )


def verify_token(token: str) -> dict[str, Any]:
    """Verify and decode RS256 JWT access token."""
    public_key = get_public_key()
    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            options={"verify_signature": True, "verify_exp": True},
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token không hợp lệ: {e!s}",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user(
    auth: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict[str, Any]:
    """Dependency returning authenticated user payload or mock user if auth is disabled."""
    if not settings.content_bot_auth_enabled:
        # Development / offline mode: bypass auth with approved admin permissions
        return {
            "sub": "dev_user",
            "email": "local_dev@content-bot.local",
            "role": "admin",
            "status": "approved",
        }

    if not auth or not auth.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Yêu cầu đăng nhập để sử dụng tính năng này.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = verify_token(auth.credentials)
    return payload


async def require_approved_user(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Ensure user is approved before executing actions."""
    if not settings.content_bot_auth_enabled:
        return user

    status_val = user.get("status")
    if status_val == "pending":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản của bạn đang chờ phê duyệt từ quản trị viên.",
        )
    elif status_val == "banned":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản của bạn đã bị khóa.",
        )
    elif status_val != "approved":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản chưa được kích hoạt.",
        )

    return user


async def require_admin_user(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Ensure user is approved and has admin role."""
    if not settings.content_bot_auth_enabled:
        return user

    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chỉ quản trị viên mới có quyền thực hiện hành động này.",
        )

    return user
