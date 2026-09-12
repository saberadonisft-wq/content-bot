"""HTTP boundary for the view-only external Live Wall."""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..middleware.auth import get_current_user
from ..services.live_wall import (
    DisplayBounds,
    LiveWallChannel,
    LiveWallFailure,
    LiveWallManager,
)
from ..storage_protocol import PersistenceStore


class LiveWallDisplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: int = Field(ge=-100_000, le=100_000)
    top: int = Field(ge=-100_000, le=100_000)
    width: int = Field(ge=640, le=20_000)
    height: int = Field(ge=480, le=12_000)


class LiveWallSessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword_id: int = Field(gt=0)
    channel_ids: list[str] = Field(min_length=1, max_length=6)
    display: LiveWallDisplayInput

    @model_validator(mode="after")
    def validate_channel_ids(self) -> LiveWallSessionInput:
        if len(set(self.channel_ids)) != len(self.channel_ids):
            raise ValueError("channel_ids must be unique")
        return self


class UserBrowserOpenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword_id: int = Field(gt=0)
    channel_ids: list[str] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_channel_ids(self) -> UserBrowserOpenInput:
        if len(set(self.channel_ids)) != len(self.channel_ids):
            raise ValueError("channel_ids must be unique")
        return self


class LiveWallProfileDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: str = Field(max_length=64)


def build_live_wall_router(
    manager: LiveWallManager | Callable[[], LiveWallManager],
    get_store: Callable[[], PersistenceStore],
) -> APIRouter:
    manager_provider = manager if callable(manager) else lambda: manager
    router = APIRouter(prefix="/api/v1/live-wall", tags=["Live Wall"])

    @router.get("/session")
    async def session_status(user: dict = Depends(get_current_user)):
        manager = manager_provider()
        return await manager.status(_user_id(user))

    def saved_channels(
        keyword_id: int, channel_ids: list[str]
    ) -> list[LiveWallChannel]:
        keyword = get_store().keyword(keyword_id)
        if not keyword:
            raise HTTPException(404, "Keyword not found")
        by_id = {
            str(channel.get("id") or ""): channel
            for channel in keyword.get("channels", [])
        }
        unknown = [channel_id for channel_id in channel_ids if channel_id not in by_id]
        if unknown:
            raise HTTPException(
                422, f"Unknown channel IDs for this keyword: {', '.join(unknown)}"
            )
        channels: list[LiveWallChannel] = []
        for channel_id in channel_ids:
            stored = by_id[channel_id]
            url = str(stored.get("normalized_url") or stored.get("url") or "")
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise HTTPException(422, f"Channel {channel_id} has no safe HTTPS URL")
            channels.append(
                LiveWallChannel(
                    id=channel_id,
                    source_id=str(stored.get("source_id") or "web"),
                    label=str(
                        stored.get("label") or stored.get("source_id") or "Channel"
                    )[:120],
                    url=url,
                )
            )
        return channels

    @router.post("/open-browser", status_code=202)
    async def open_user_browser(
        payload: UserBrowserOpenInput,
        _user: dict = Depends(get_current_user),
    ):
        manager = manager_provider()
        try:
            return await manager.open_in_user_browser(
                saved_channels(payload.keyword_id, payload.channel_ids)
            )
        except LiveWallFailure as exc:
            raise HTTPException(
                409, {"reason_code": exc.code, "message": exc.detail}
            ) from exc

    @router.put("/session", status_code=202)
    async def open_session(
        payload: LiveWallSessionInput,
        user: dict = Depends(get_current_user),
    ):
        manager = manager_provider()
        try:
            return await manager.open_or_update(
                _user_id(user),
                payload.keyword_id,
                saved_channels(payload.keyword_id, payload.channel_ids),
                DisplayBounds(**payload.display.model_dump()),
            )
        except LiveWallFailure as exc:
            raise HTTPException(
                409, {"reason_code": exc.code, "message": exc.detail}
            ) from exc

    @router.delete("/session")
    async def close_session(user: dict = Depends(get_current_user)):
        manager = manager_provider()
        try:
            return await manager.close(_user_id(user))
        except LiveWallFailure as exc:
            raise HTTPException(
                409, {"reason_code": exc.code, "message": exc.detail}
            ) from exc

    @router.delete("/profile")
    async def delete_profile(
        payload: LiveWallProfileDeleteInput,
        user: dict = Depends(get_current_user),
    ):
        manager = manager_provider()
        try:
            return await manager.delete_profile(_user_id(user), payload.confirmation)
        except LiveWallFailure as exc:
            raise HTTPException(
                409, {"reason_code": exc.code, "message": exc.detail}
            ) from exc

    return router


def _user_id(user: dict) -> str:
    return str(user.get("sub") or user.get("id") or user.get("email") or "dev_user")
