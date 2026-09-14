"""API endpoints for local master-password protected credential vault."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from ..application_services import AppServices, get_services
from ..config import settings
from ..services.credential_vault import get_vault
from ..services.gemini_subtitles import GeminiSubtitleService


class CredentialRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def sanitized_handler(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # FastAPI normally echoes invalid input, which may be a secret.
                return JSONResponse(status_code=422, content={"detail": [
                    {"loc": error["loc"], "type": error["type"], "msg": "Dữ liệu credential không hợp lệ."}
                    for error in exc.errors()
                ]})
        return sanitized_handler


router = APIRouter(prefix="/api/v1/credentials", tags=["Credentials"], route_class=CredentialRoute)


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


class GeminiKeyImportRequest(BaseModel):
    keys: str = Field(min_length=1, max_length=1_000_000, repr=False)


class GeminiKeyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None
    project_group: str | None = Field(default=None, max_length=100)


class GeminiKeyCheckRequest(BaseModel):
    model: str = Field(default="gemini-3.6-flash", min_length=1, max_length=128)


@router.get("/gemini/keys")
def list_gemini_keys(services: AppServices = Depends(get_services)) -> dict[str, Any]:
    result = get_vault().gemini_key_status(settings.gemini_api_key)
    runtime = services.gemini_subtitle_service.dispatcher.states()
    for key in result["keys"]:
        state = runtime.get(key["id"])
        if state and state != "untested":
            key["state"] = state
    return result


@router.post("/gemini/keys")
def import_gemini_keys(req: GeminiKeyImportRequest) -> dict[str, Any]:
    try:
        return get_vault().edit_gemini_keys(add=req.keys.splitlines(), environment_key=settings.gemini_api_key)
    except ValueError as err:
        raise HTTPException(400, str(err)) from err


@router.patch("/gemini/keys/{key_id}")
def update_gemini_key(key_id: str, req: GeminiKeyUpdateRequest) -> dict[str, Any]:
    changes = req.model_dump(exclude_unset=True)
    if "name" in changes:
        if not changes["name"] or not changes["name"].strip():
            raise HTTPException(400, "Tên key không được để trống.")
        changes["name"] = changes["name"].strip()
    if "enabled" in changes and changes["enabled"] is None:
        raise HTTPException(400, "Trạng thái key không hợp lệ.")
    if changes.get("project_group") is not None:
        changes["project_group"] = changes["project_group"].strip() or None
    try:
        return get_vault().edit_gemini_keys(key_id=key_id, changes=changes, environment_key=settings.gemini_api_key)
    except ValueError as err:
        raise HTTPException(400, str(err)) from err


@router.delete("/gemini/keys/{key_id}")
def delete_gemini_key(key_id: str) -> dict[str, Any]:
    try:
        return get_vault().edit_gemini_keys(key_id=key_id, delete=True, environment_key=settings.gemini_api_key)
    except ValueError as err:
        raise HTTPException(400, str(err)) from err


@router.post("/gemini/keys/{key_id}/check")
def check_gemini_key(key_id: str, req: GeminiKeyCheckRequest) -> dict[str, Any]:
    vault = get_vault()
    key = next((row for row in vault.gemini_keys(settings.gemini_api_key) if row["id"] == key_id), None)
    if key is None:
        raise HTTPException(404, "Không tìm thấy key hoặc Vault đang khóa.")
    try:
        model = GeminiSubtitleService.normalize_model(req.model)
    except RuntimeError as err:
        raise HTTPException(400, str(err)) from err
    try:
        # Read-only model metadata probe: readiness is limited to this permission at this instant.
        with httpx.Client(timeout=30) as client:
            response = client.get(f"https://generativelanguage.googleapis.com/v1beta/models/{model}",
                                  headers={"x-goog-api-key": key["secret"]})
        state = "ready" if response.status_code == 200 else "quota_wait" if response.status_code == 429 else "permission_error" if response.status_code in (400, 401, 403) else "untested"
    except httpx.TransportError:
        state = "untested"
    vault.record_gemini_check(key_id, state=state, model=model, checked_at=datetime.now(UTC).isoformat())
    return vault.gemini_key_status(settings.gemini_api_key)


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
