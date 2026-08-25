"""Explicit deletion boundary for crawler-owned source data."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from ..config import settings
from ..crawlers import SOURCE_REGISTRY
from ..crawlers.runtime import ProfileInUse, ProfileNamespace
from ..mongo import PersistenceStore


class CrawlerDataDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: str
    delete_profiles: bool = False


def build_crawler_data_router(
    get_store: Callable[[], PersistenceStore],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/crawler-data")

    @router.post("/{source_id}/delete")
    async def delete_source_data(
        source_id: str,
        payload: CrawlerDataDeleteRequest,
    ):
        canonical = SOURCE_REGISTRY.resolve_id(source_id)
        if SOURCE_REGISTRY.get(canonical) is None:
            raise HTTPException(404, "Unknown crawler source")
        expected = (
            f"DELETE {canonical} DATA AND PROFILES"
            if payload.delete_profiles
            else f"DELETE {canonical} DATA"
        )
        if payload.confirmation != expected:
            raise HTTPException(422, f"Confirmation must equal: {expected}")
        profiles_deleted = 0
        if payload.delete_profiles:
            namespace = ProfileNamespace(
                settings.content_bot_cbce_profile_root,
                SOURCE_REGISTRY,
                forbidden_roots=(settings.mediacrawler_profile_dir,),
            )
            try:
                profiles_deleted = await asyncio.to_thread(
                    namespace.delete_source_profiles,
                    canonical,
                    confirmation=f"DELETE {canonical} PROFILES",
                )
            except ProfileInUse as exc:
                raise HTTPException(
                    409,
                    "A crawler is using this source profile; stop it before deletion.",
                ) from exc
        deleted = await asyncio.to_thread(
            get_store().delete_source_data, canonical
        )
        return {
            "source_id": canonical,
            **deleted,
            "profiles_deleted": profiles_deleted,
        }

    return router
