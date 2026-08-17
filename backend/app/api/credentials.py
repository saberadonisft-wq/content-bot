"""API endpoints for local master-password protected credential vault."""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..services.credential_vault import get_vault, SUPPORTED_KEYS

router = APIRouter(prefix="/api/v1/credentials", tags=["Credentials"])


class SetupMasterPasswordRequest(BaseModel):
    master_password: str = Field(..., min_length=6, description="Master password to encrypt local credentials")
    initial_credentials: dict[str, str] | None = None


class UnlockVaultRequest(BaseModel):
    master_password: str = Field(..., min_length=1)


class ChangeMasterPasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6)


class UpdateCredentialsRequest(BaseModel):
    credentials: dict[str, str]


@router.get("/status")
def get_credentials_status() -> dict[str, Any]:
    """Get the current status of the credential vault (never returns plain secret values)."""
    vault = get_vault()
    return vault.get_status()


@router.post("/setup")
def setup_master_password(req: SetupMasterPasswordRequest) -> dict[str, Any]:
    """Set up master password for the first time."""
    vault = get_vault()
    if vault.is_configured:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Master password đã được thiết lập trước đó. Vui lòng mở khóa hoặc đổi mật khẩu.",
        )
    try:
        vault.setup_master_password(req.master_password, req.initial_credentials)
        return {
            "success": True,
            "message": "Thiết lập Master Password thành công. Vault đã được mở khóa.",
            "status": vault.get_status(),
        }
    except Exception as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))


@router.post("/unlock")
def unlock_vault(req: UnlockVaultRequest) -> dict[str, Any]:
    """Unlock the credential vault with the master password."""
    vault = get_vault()
    if not vault.is_configured:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Chưa thiết lập Master password. Vui lòng thiết lập trước.",
        )
    try:
        vault.unlock(req.master_password)
        return {
            "success": True,
            "message": "Mở khóa Vault thành công.",
            "status": vault.get_status(),
        }
    except ValueError as err:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(err))


@router.post("/lock")
def lock_vault() -> dict[str, Any]:
    """Lock the credential vault and clear secrets from memory."""
    vault = get_vault()
    vault.lock()
    return {
        "success": True,
        "message": "Đã khóa Vault thành công.",
        "status": vault.get_status(),
    }


@router.post("/change-password")
def change_master_password(req: ChangeMasterPasswordRequest) -> dict[str, Any]:
    """Change the master password."""
    vault = get_vault()
    try:
        vault.change_master_password(req.old_password, req.new_password)
        return {
            "success": True,
            "message": "Đổi Master Password thành công.",
            "status": vault.get_status(),
        }
    except ValueError as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))


@router.put("")
def update_credentials(req: UpdateCredentialsRequest) -> dict[str, Any]:
    """Save or update API credentials in the local encrypted vault."""
    vault = get_vault()
    if not vault.is_unlocked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Vault đang khóa. Vui lòng nhập Master Password để mở khóa trước khi chỉnh sửa.",
        )
    try:
        status_info = vault.save_credentials(req.credentials)
        return {
            "success": True,
            "message": "Cập nhật credentials thành công và đã lưu cục bộ an toàn.",
            "status": status_info,
        }
    except Exception as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))
