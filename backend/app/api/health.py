from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
from fastapi import APIRouter, HTTPException, Query

from ..config import settings
from ..mongo import MongoStore
from ..services.runs import utcnow
from ..version import APP_VERSION


def build_health_router(
    get_store: Callable[[], MongoStore],
    source_count: Callable[[], int],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": APP_VERSION,
            "time": utcnow().isoformat(),
            "sources": source_count(),
        }

    @router.get("/version")
    async def get_version():
        return {"version": APP_VERSION}

    @router.get("/update/check")
    async def check_update(channel: str = Query("stable")):
        auth_url = settings.content_bot_auth_server_url or "http://127.0.0.1:8080"
        auth_url = auth_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(
                    f"{auth_url}/api/v1/update/check",
                    params={"current_version": APP_VERSION, "channel": channel},
                )
                if res.status_code == 200:
                    return res.json()
        except Exception:
            pass

        return {
            "update_available": False,
            "current_version": APP_VERSION,
            "latest_version": APP_VERSION,
            "channel": channel,
        }

    @router.get("/ready")
    async def ready():
        if not await asyncio.to_thread(get_store().ping_cached):
            raise HTTPException(status_code=503, detail="MongoDB is not ready")
        return {"status": "ready", "time": utcnow().isoformat(), "mongo_ready": True}

    return router
