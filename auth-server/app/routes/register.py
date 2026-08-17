from __future__ import annotations

from datetime import UTC, datetime
from fastapi import APIRouter, HTTPException, status

from ..auth import doc_to_user_response, hash_password
from ..config import settings
from ..database import db
from ..models import MessageResponse, RegisterRequest

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
def register(request: RegisterRequest) -> MessageResponse:
    """Register a new user account."""
    email_clean = request.email.lower().strip()
    existing_user = db.find_user_by_email(email_clean)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email này đã được sử dụng. Vui lòng chọn email khác hoặc đăng nhập.",
        )

    # Check if this user matches the configured initial admin
    is_initial_admin = bool(settings.admin_email and email_clean == settings.admin_email.lower().strip())
    user_role = "admin" if is_initial_admin else "user"
    user_status = "approved" if is_initial_admin else "pending"
    approved_at = datetime.now(UTC) if is_initial_admin else None

    user_doc = {
        "email": email_clean,
        "password_hash": hash_password(request.password),
        "display_name": request.display_name.strip(),
        "role": user_role,
        "status": user_status,
        "auth_provider": "email",
        "google_id": None,
        "avatar_url": None,
        "created_at": datetime.now(UTC),
        "approved_at": approved_at,
        "approved_by": "system" if is_initial_admin else None,
        "last_login_at": None,
    }

    created = db.create_user(user_doc)
    user_resp = doc_to_user_response(created)

    if user_status == "approved":
        msg = "Đăng ký thành công tài khoản Quản trị viên."
    else:
        msg = "Đăng ký thành công! Tài khoản của bạn đang chờ Quản trị viên duyệt trước khi có thể sử dụng."

    return MessageResponse(
        message=msg,
        status=user_status,
        user=user_resp,
    )
