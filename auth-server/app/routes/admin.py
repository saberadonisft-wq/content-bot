from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth import doc_to_user_response, require_admin
from ..database import db
from ..models import (
    AdminUpdateRoleRequest,
    AdminUpdateStatusRequest,
    MessageResponse,
    UserResponse,
)

router = APIRouter(prefix="/admin", tags=["Admin Management"])


@router.get("/users", response_model=list[UserResponse])
def list_users(
    status_filter: str | None = Query(None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    admin: dict[str, Any] = Depends(require_admin),
) -> list[UserResponse]:
    """List all registered users with optional status filter."""
    docs = db.list_users(status=status_filter, skip=skip, limit=limit)
    return [doc_to_user_response(d) for d in docs]


@router.get("/users/pending", response_model=list[UserResponse])
def list_pending_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    admin: dict[str, Any] = Depends(require_admin),
) -> list[UserResponse]:
    """Convenience endpoint to list users waiting for admin approval."""
    docs = db.list_users(status="pending", skip=skip, limit=limit)
    return [doc_to_user_response(d) for d in docs]


@router.put("/users/{user_id}/approve", response_model=UserResponse)
def approve_user(
    user_id: str,
    admin: dict[str, Any] = Depends(require_admin),
) -> UserResponse:
    """Approve a pending user so they can access the application."""
    user = db.find_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Người dùng không tồn tại.",
        )

    admin_identifier = admin.get("email") or str(admin.get("_id"))
    updated = db.update_user(user_id, {
        "status": "approved",
        "approved_at": datetime.now(UTC),
        "approved_by": admin_identifier,
    })
    return doc_to_user_response(updated or user)


@router.put("/users/{user_id}/reject", response_model=UserResponse)
def reject_user(
    user_id: str,
    admin: dict[str, Any] = Depends(require_admin),
) -> UserResponse:
    """Reject a pending user request."""
    user = db.find_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Người dùng không tồn tại.",
        )

    # Don't allow admin to reject themselves
    if str(user["_id"]) == str(admin["_id"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Không thể tự từ chối tài khoản của chính mình.",
        )

    db.revoke_all_user_tokens(user_id)
    updated = db.update_user(user_id, {
        "status": "rejected",
    })
    return doc_to_user_response(updated or user)


@router.put("/users/{user_id}/ban", response_model=UserResponse)
def ban_user(
    user_id: str,
    admin: dict[str, Any] = Depends(require_admin),
) -> UserResponse:
    """Ban an active user and invalidate their sessions."""
    user = db.find_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Người dùng không tồn tại.",
        )

    if str(user["_id"]) == str(admin["_id"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Không thể tự khóa tài khoản của chính mình.",
        )

    db.revoke_all_user_tokens(user_id)
    updated = db.update_user(user_id, {
        "status": "banned",
    })
    return doc_to_user_response(updated or user)


@router.put("/users/{user_id}/unban", response_model=UserResponse)
def unban_user(
    user_id: str,
    admin: dict[str, Any] = Depends(require_admin),
) -> UserResponse:
    """Unban a previously banned user and restore approval."""
    user = db.find_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Người dùng không tồn tại.",
        )

    updated = db.update_user(user_id, {
        "status": "approved",
    })
    return doc_to_user_response(updated or user)


@router.put("/users/{user_id}/role", response_model=UserResponse)
def update_user_role(
    user_id: str,
    request: AdminUpdateRoleRequest,
    admin: dict[str, Any] = Depends(require_admin),
) -> UserResponse:
    """Change a user's role (admin or user)."""
    user = db.find_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Người dùng không tồn tại.",
        )

    if str(user["_id"]) == str(admin["_id"]) and request.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Không thể tự giáng quyền admin của chính mình.",
        )

    updated = db.update_user(user_id, {
        "role": request.role,
    })
    return doc_to_user_response(updated or user)


@router.delete("/users/{user_id}", response_model=MessageResponse)
def delete_user(
    user_id: str,
    admin: dict[str, Any] = Depends(require_admin),
) -> MessageResponse:
    """Permanently delete a user account and all active tokens."""
    user = db.find_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Người dùng không tồn tại.",
        )

    if str(user["_id"]) == str(admin["_id"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Không thể tự xóa tài khoản của chính mình.",
        )

    db.revoke_all_user_tokens(user_id)
    db.delete_user(user_id)
    return MessageResponse(message="Xóa người dùng thành công.")
