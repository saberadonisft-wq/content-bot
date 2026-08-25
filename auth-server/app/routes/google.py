from __future__ import annotations

import re
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from ..auth import create_access_token, create_refresh_token
from ..config import settings
from ..database import db

router = APIRouter(prefix="/auth", tags=["Google OAuth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
OAUTH_ATTEMPT_TTL_SECONDS = 5 * 60
_DESKTOP_CALLBACK_PATH = re.compile(r"^/content-bot-google/[A-Za-z0-9_-]{32,128}$")


@dataclass(frozen=True)
class OAuthAttempt:
    return_url: str
    expires_at: float


_oauth_attempts: dict[str, OAuthAttempt] = {}
_oauth_attempts_lock = threading.Lock()


def _validated_desktop_callback(value: str | None) -> str | None:
    if not value or len(value) > 512:
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or not port
        or not 1024 <= port <= 65535
        or parsed.query
        or parsed.fragment
        or not _DESKTOP_CALLBACK_PATH.fullmatch(parsed.path)
    ):
        return None
    return urllib.parse.urlunsplit(("http", f"127.0.0.1:{port}", parsed.path, "", ""))


def _create_oauth_attempt(return_url: str) -> str:
    state = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _oauth_attempts_lock:
        expired = [key for key, attempt in _oauth_attempts.items() if attempt.expires_at <= now]
        for key in expired:
            _oauth_attempts.pop(key, None)
        _oauth_attempts[state] = OAuthAttempt(
            return_url=return_url,
            expires_at=now + OAUTH_ATTEMPT_TTL_SECONDS,
        )
    return state


def _consume_oauth_attempt(state: str | None) -> OAuthAttempt | None:
    if not state:
        return None
    with _oauth_attempts_lock:
        attempt = _oauth_attempts.pop(state, None)
    if not attempt or attempt.expires_at <= time.monotonic():
        return None
    return attempt


def _redirect_with_params(return_url: str, params: dict[str, str]) -> RedirectResponse:
    parsed = urllib.parse.urlsplit(return_url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    target = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", urllib.parse.urlencode(query), "")
    )
    return RedirectResponse(url=target)


@router.get("/google")
def google_auth(desktop_callback: str | None = Query(None)) -> RedirectResponse:
    """Redirect client to Google OAuth consent screen."""
    if not settings.google_client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth chưa được cấu hình (GOOGLE_CLIENT_ID còn trống).",
        )

    callback = _validated_desktop_callback(desktop_callback)
    if desktop_callback and not callback:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Desktop OAuth callback không hợp lệ.",
        )
    return_url = callback or settings.frontend_url.rstrip("/")
    state = _create_oauth_attempt(return_url)
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
        "state": state,
    }
    url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(url=url)


@router.get("/google/callback")
async def google_callback(
    code: str | None = Query(None),
    error: str | None = Query(None),
    state: str | None = Query(None),
) -> RedirectResponse:
    """Handle callback from Google OAuth."""
    attempt = _consume_oauth_attempt(state)
    if not attempt:
        return _redirect_with_params(
            settings.frontend_url.rstrip("/"),
            {"auth_error": "Phiên đăng nhập Google không hợp lệ hoặc đã hết hạn."},
        )
    return_url = attempt.return_url

    if error or not code:
        err_msg = error or "Authorization code missing"
        return _redirect_with_params(return_url, {"auth_error": err_msg})

    if not settings.google_client_id or not settings.google_client_secret:
        return _redirect_with_params(return_url, {"auth_error": "Google OAuth not configured"})

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token_data = token_resp.json()
            google_access_token = token_data.get("access_token")
            if not google_access_token:
                return _redirect_with_params(return_url, {"auth_error": "Failed to exchange Google token"})

            userinfo_resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {google_access_token}"},
            )
            userinfo = userinfo_resp.json()

        email = userinfo.get("email", "").lower().strip()
        google_id = userinfo.get("sub")
        display_name = userinfo.get("name") or email.split("@")[0]
        avatar_url = userinfo.get("picture")

        if not email or not google_id:
            return _redirect_with_params(return_url, {"auth_error": "Google profile missing email"})

        # Find or create user
        existing_user = db.find_user_by_email(email) or db.find_user_by_google_id(google_id)
        now = datetime.now(UTC)

        if existing_user:
            user_id = str(existing_user["_id"])
            user_status = existing_user.get("status", "pending")
            user_role = existing_user.get("role", "user")

            if user_status == "banned":
                return _redirect_with_params(return_url, {"auth_error": "Tài khoản của bạn đã bị khóa"})
            elif user_status == "rejected":
                return _redirect_with_params(return_url, {"auth_error": "Tài khoản của bạn đã bị từ chối"})

            db.update_user(user_id, {
                "google_id": google_id,
                "avatar_url": avatar_url or existing_user.get("avatar_url"),
                "display_name": display_name or existing_user.get("display_name"),
                "last_login_at": now,
            })
        else:
            is_initial_admin = bool(settings.admin_email and email == settings.admin_email.lower().strip())
            user_role = "admin" if is_initial_admin else "user"
            user_status = "approved" if is_initial_admin else "pending"

            user_doc = {
                "email": email,
                "password_hash": None,
                "display_name": display_name,
                "role": user_role,
                "status": user_status,
                "auth_provider": "google",
                "google_id": google_id,
                "avatar_url": avatar_url,
                "created_at": now,
                "approved_at": now if is_initial_admin else None,
                "approved_by": "system" if is_initial_admin else None,
                "last_login_at": now,
            }
            created = db.create_user(user_doc)
            user_id = str(created["_id"])

        # Generate tokens
        access_token = create_access_token(
            user_id=user_id,
            email=email,
            role=user_role,
            user_status=user_status,
        )
        refresh_token, _ = create_refresh_token(user_id=user_id)

        redirect_params = {
            "auth_token": access_token,
            "refresh_token": refresh_token,
            "user_status": user_status,
            "user_role": user_role,
            "user_email": email,
        }
        return _redirect_with_params(return_url, redirect_params)

    except Exception as e:  # noqa: BLE001 - OAuth/provider failures must return to the initiating client.
        return _redirect_with_params(return_url, {"auth_error": str(e)})
