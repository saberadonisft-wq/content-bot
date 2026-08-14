from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, HTTPException

from ..mongo import MongoStore
from ..services.runs import utcnow


def build_health_router(
    get_store: Callable[[], MongoStore],
    source_count: Callable[[], int],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/health")
    async def health():
        return {"status": "ok", "time": utcnow().isoformat(), "sources": source_count()}

    @router.get("/ready")
    async def ready():
        if not await asyncio.to_thread(get_store().ping_cached):
            raise HTTPException(status_code=503, detail="MongoDB is not ready")
        return {"status": "ready", "time": utcnow().isoformat(), "mongo_ready": True}

    return router
