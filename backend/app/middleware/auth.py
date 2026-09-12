from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from weakref import WeakKeyDictionary

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import settings
from ..services.http_pool import get_client

logger = logging.getLogger("content_bot.auth_middleware")

security = HTTPBearer(auto_error=False)

@dataclass
class _RemoteKey:
    key: str | None = None
    url: str = ""
    expires_at: float = 0
    retry_at: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_keys: WeakKeyDictionary = WeakKeyDictionary()


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Không thể kết nối đến máy chủ xác thực để lấy khóa công khai.",
    )


async def get_public_key() -> str:
    """Fetch asynchronously once per loop/server, with bounded cache and failure backoff."""
    if settings.content_bot_auth_public_key:
        return settings.content_bot_auth_public_key
    url = settings.content_bot_auth_server_url.rstrip("/")
    if not url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Máy chủ chưa được cấu hình AUTH_SERVER_URL để xác thực JWT.",
        )
    loop = asyncio.get_running_loop()
    if loop not in _keys:
        _keys[loop] = _RemoteKey()
    cache = _keys[loop]
    async with cache.lock:
        now = monotonic()
        if cache.url != url:
            cache.url, cache.key, cache.expires_at, cache.retry_at = url, None, 0, 0
        if cache.key and now < cache.expires_at:
            return cache.key
        if now < cache.retry_at:
            raise _unavailable()
        try:
            client = await get_client(timeout=httpx.Timeout(5.0), follow_redirects=False)
            response = await client.get(f"{url}/auth/public-key")
            response.raise_for_status()
            key = response.json().get("public_key")
            if not isinstance(key, str) or not key.strip():
                raise ValueError("Auth server returned no public key")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            cache.retry_at = monotonic() + 1.0
            logger.warning("Public key fetch failed (%s)", type(exc).__name__)
            raise _unavailable() from exc
        cache.key, cache.expires_at = key, monotonic() + 300
        return key


async def verify_token(token: str) -> dict[str, Any]:
    """Verify and decode RS256 JWT access token."""
    public_key = await get_public_key()
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
    request: Request,
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

    if isinstance(getattr(request.state, "user", None), dict):
        return request.state.user
    payload = await verify_token(auth.credentials)
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
