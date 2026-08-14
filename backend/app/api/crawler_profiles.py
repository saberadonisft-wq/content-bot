"""Manual CBCE profile-login API; no cookies or profile paths cross this boundary."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from ..services.crawler_login import CrawlerLoginManager


class LoginStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(default=1_200, ge=30, le=7_200)


def build_crawler_profiles_router(manager: CrawlerLoginManager) -> APIRouter:
    router = APIRouter(prefix="/api/v1/crawler/profiles")

    @router.get("/{source_id}/login")
    async def login_status(source_id: str):
        try:
            return manager.status(source_id).as_dict()
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/{source_id}/login", status_code=status.HTTP_202_ACCEPTED)
    async def start_login(source_id: str, payload: LoginStartRequest):
        try:
            return (
                await manager.start(
                    source_id, timeout_seconds=payload.timeout_seconds
                )
            ).as_dict()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            reason_code = str(exc)
            detail = {
                "BROWSER_EXECUTABLE_OR_PROFILE_INVALID": (
                    "Configure a valid browser executable and isolated profile root."
                ),
                "BROWSER_EXTRA_NOT_INSTALLED": (
                    "Install the optional Playwright browser runtime first."
                ),
            }.get(reason_code, "Clean-room browser preflight is not ready.")
            raise HTTPException(409, detail) from exc

    @router.delete("/{source_id}/login", status_code=status.HTTP_200_OK)
    async def stop_login(source_id: str, response: Response):
        try:
            result = await manager.stop(source_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        response.status_code = status.HTTP_200_OK
        return result.as_dict()

    return router
