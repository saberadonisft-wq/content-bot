from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException

from ..crawlers import SOURCE_REGISTRY
from ..crawlers.contracts import (
    AuthMode,
    AvailabilityState,
    ImplementationState,
    Operation,
    SchedulePolicy,
)
from ..mongo import MongoStore
from ..schemas import KeywordInput, KeywordOutput, SourceOutput
from ..services.cbce_runtime import (
    cbce_provider_rollout_status,
    provider_overrides,
)
from ..services.channel_scans import CHANNEL_SCANNERS, channel_mode, normalize_channel
from ..services.connectors import SourceConnector
from ..services.runs import RunManager, utcnow
from ..services.text import normalized


def _keyword_output(keyword: dict) -> KeywordOutput:
    channels = [
        {**row, "mode": channel_mode(row.get("source_id", "web"))}
        for row in keyword.get("channels", [])
    ]
    return KeywordOutput(
        id=keyword["id"],
        name=keyword["name"],
        include_terms=keyword.get("include_terms", []),
        exclude_terms=keyword.get("exclude_terms", []),
        source_ids=keyword.get("source_ids", []),
        channels=channels,
        enabled=keyword.get("enabled", True),
        interval_minutes=keyword.get("interval_minutes", 360),
        max_items_per_source=keyword.get("max_items_per_source", 500),
        next_run_at=keyword.get("next_run_at"),
        created_at=keyword["created_at"],
        updated_at=keyword["updated_at"],
    )


def build_catalog_router(
    get_store: Callable[[], MongoStore],
    connectors: dict[str, SourceConnector],
    run_manager: RunManager,
) -> APIRouter:
    SOURCE_REGISTRY.validate_bindings(
        connectors,
        CHANNEL_SCANNERS,
        renderer_ids={"x"},
    )
    router = APIRouter(prefix="/api/v1")

    def default_keyword_sources() -> list[str]:
        selected: list[str] = []
        for manifest in SOURCE_REGISTRY:
            connector = connectors.get(manifest.id)
            if connector is None or not connector.configured:
                continue
            if any(
                spec.implementation is ImplementationState.IMPLEMENTED
                and spec.schedule_policy is SchedulePolicy.BACKGROUND_SAFE
                and bool(spec.handler_key)
                for _, spec in SOURCE_REGISTRY.operation_specs(manifest.id, Operation.SEARCH)
            ):
                selected.append(manifest.id)
        return selected

    def normalized_channels(payload: KeywordInput, existing: dict | None = None) -> list[dict]:
        previous_by_url = {
            row.get("normalized_url"): row
            for row in (existing or {}).get("channels", [])
            if row.get("normalized_url")
        }
        channels: list[dict] = []
        seen_urls: set[str] = set()
        for submitted in payload.channels:
            try:
                candidate = normalize_channel(submitted.model_dump())
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            normalized_url = candidate["normalized_url"]
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            previous = previous_by_url.get(normalized_url)
            if previous:
                candidate["id"] = previous["id"]
                candidate["checkpoint"] = previous.get("checkpoint", {})
                candidate["last_scanned_at"] = previous.get("last_scanned_at")
                candidate["last_status"] = previous.get("last_status")
                candidate["last_error"] = previous.get("last_error")
            channels.append(candidate)
        return channels

    def selected_global_sources(payload: KeywordInput, channels: list[dict]) -> list[str]:
        """Keep global discovery selection independent from saved channels.

        An empty selection remains channel-only when channels were submitted,
        preserving the current UI behavior. Callers can now explicitly select
        global sources and channels in the same topic.
        """
        if payload.source_ids:
            return list(
                dict.fromkeys(
                    SOURCE_REGISTRY.resolve_id(source_id)
                    for source_id in payload.source_ids
                )
            )
        return [] if channels else default_keyword_sources()

    @router.get("/sources", response_model=list[SourceOutput])
    async def list_sources(deep: bool = False):
        manifests = list(SOURCE_REGISTRY)
        statuses = await asyncio.gather(
            *(
                (
                    connectors[manifest.id].deep_healthcheck()
                    if deep
                    else connectors[manifest.id].healthcheck()
                )
                for manifest in manifests
            )
        )
        checked_at = datetime.now(UTC)
        configured_overrides = provider_overrides()
        outputs: list[SourceOutput] = []
        for manifest, status in zip(manifests, statuses, strict=True):
            operations: list[dict] = []
            for provider in manifest.providers:
                for spec in provider.operations:
                    if spec.implementation is ImplementationState.PLANNED:
                        availability = None
                        reason_code = "NOT_IMPLEMENTED"
                        message = spec.cta or "This operation is planned but not implemented."
                    elif spec.operation is Operation.RENDER_EMBED:
                        availability = AvailabilityState.READY.value
                        reason_code = None
                        message = "Public embed is available."
                    elif (
                        selected_provider := configured_overrides.get(
                            manifest.id, {}
                        ).get(spec.operation.value)
                    ) and selected_provider != provider.id:
                        availability = AvailabilityState.DISABLED_BY_POLICY.value
                        reason_code = "PROVIDER_NOT_SELECTED"
                        message = "A different provider is selected for this operation."
                    elif (
                        rollout := cbce_provider_rollout_status(
                            manifest.id, provider.id, spec.operation
                        )
                    ) is not None:
                        availability = (
                            AvailabilityState.READY.value
                            if rollout["ready"]
                            else AvailabilityState.SETUP_REQUIRED.value
                        )
                        reason_code = rollout["reason_code"]
                        message = rollout["detail"]
                    elif status.state == "ready":
                        availability = AvailabilityState.READY.value
                        reason_code = None
                        message = status.detail
                    else:
                        availability = {
                            "auth_required": AvailabilityState.AUTH_REQUIRED.value,
                            "permission_required": AvailabilityState.PERMISSION_REQUIRED.value,
                            "degraded": AvailabilityState.DEGRADED.value,
                            "rate_limited": AvailabilityState.RATE_LIMITED.value,
                            "disabled_by_policy": AvailabilityState.DISABLED_BY_POLICY.value,
                        }.get(status.state, AvailabilityState.SETUP_REQUIRED.value)
                        reason_code = status.reason_code or "LOCAL_PREREQUISITE_MISSING"
                        message = status.detail
                    operations.append(
                        {
                            "id": spec.operation.value,
                            "provider_id": provider.id,
                            "coverage": spec.coverage.value,
                            "implementation": spec.implementation.value,
                            "availability": availability,
                            "enabled": bool(
                                spec.implementation is ImplementationState.IMPLEMENTED
                                and availability == AvailabilityState.READY.value
                                and spec.handler_key
                            ),
                            "reason_code": reason_code,
                            "detail": message,
                            "auth_modes": [mode.value for mode in spec.auth_modes],
                            "schedule_policy": spec.schedule_policy.value,
                            "target_kinds": [kind.value for kind in spec.target_kinds],
                            "budget_limits": (
                                {
                                    "max_items": spec.budget_limits.max_items,
                                    "max_requests": spec.budget_limits.max_requests,
                                    "deadline_seconds": spec.budget_limits.deadline_seconds,
                                }
                                if spec.budget_limits is not None
                                else None
                            ),
                        }
                    )
            search_specs = SOURCE_REGISTRY.operation_specs(manifest.id, Operation.SEARCH)
            scan_specs = SOURCE_REGISTRY.operation_specs(manifest.id, Operation.SCAN_CHANNEL)
            primary_ready = any(
                operation["id"] == manifest.primary_operation.value
                and operation["enabled"]
                for operation in operations
            )
            legacy_state = "ready" if primary_ready else status.state
            legacy_detail = (
                next(
                    operation["detail"]
                    for operation in operations
                    if operation["id"] == manifest.primary_operation.value
                    and operation["enabled"]
                )
                if primary_ready
                else status.detail
            )
            outputs.append(
                SourceOutput(
                    id=manifest.id,
                    label=manifest.label,
                    group=manifest.group_label,
                    order=manifest.order,
                    primary_operation=manifest.primary_operation.value,
                    state=legacy_state,
                    detail=legacy_detail,
                    global_search=any(
                        spec.implementation is ImplementationState.IMPLEMENTED and bool(spec.handler_key)
                        for _, spec in search_specs
                    ),
                    watchlist_filter=any(
                        spec.implementation is ImplementationState.IMPLEMENTED and bool(spec.handler_key)
                        for _, spec in scan_specs
                    ),
                    requires_login=any(
                        spec.implementation is ImplementationState.IMPLEMENTED
                        and AuthMode.BROWSER_PROFILE in spec.auth_modes
                        for provider in manifest.providers
                        for spec in provider.operations
                    ),
                    interaction_fields=list(
                        dict.fromkeys(
                            metric.legacy_key or metric.id for metric in manifest.metrics
                        )
                    ),
                    metrics=[
                        {
                            "id": metric.id,
                            "label": metric.label,
                            "semantics": metric.semantics,
                            "legacy_key": metric.legacy_key,
                        }
                        for metric in manifest.metrics
                    ],
                    legacy_aliases=list(manifest.legacy_aliases),
                    provider_selection=[provider.id for provider in manifest.providers],
                    operations=operations,
                    health_summary={
                        "state": status.state,
                        "detail": status.detail,
                        "checked_at": checked_at.isoformat(),
                        "probe": status.probe,
                    },
                    coverage_disclaimer=manifest.coverage_disclaimer,
                )
            )
        return outputs

    @router.get("/keywords", response_model=list[KeywordOutput])
    def list_keywords():
        return [_keyword_output(row) for row in get_store().keywords()]

    @router.post("/keywords", response_model=KeywordOutput, status_code=201)
    def create_keyword(payload: KeywordInput):
        storage = get_store()
        if storage.keyword_name_exists(normalized(payload.name)):
            raise HTTPException(409, "A keyword with that name already exists")
        channels = normalized_channels(payload)
        source_ids = selected_global_sources(payload, channels)
        invalid = set(source_ids).difference(connectors)
        if invalid:
            raise HTTPException(422, f"Unknown sources: {', '.join(sorted(invalid))}")
        now = utcnow()
        row = storage.create_keyword(
            {
                "name": payload.name.strip(),
                "normalized_name": normalized(payload.name),
                "include_terms": payload.include_terms,
                "exclude_terms": payload.exclude_terms,
                "source_ids": source_ids,
                "source_selection_version": 2,
                "channels": channels,
                "source_checkpoints": {},
                "enabled": payload.enabled,
                "interval_minutes": payload.interval_minutes,
                "max_items_per_source": payload.max_items_per_source,
                "next_run_at": now + timedelta(minutes=payload.interval_minutes)
                if payload.enabled
                else None,
                "created_at": now,
                "updated_at": now,
            }
        )
        return _keyword_output(row)

    @router.patch("/keywords/{keyword_id}", response_model=KeywordOutput)
    def update_keyword(keyword_id: int, payload: KeywordInput):
        storage = get_store()
        existing = storage.keyword(keyword_id)
        if not existing:
            raise HTTPException(404, "Keyword not found")
        if storage.keyword_name_exists(normalized(payload.name), keyword_id):
            raise HTTPException(409, "A keyword with that name already exists")
        now = utcnow()
        channels = normalized_channels(payload, existing)
        source_ids = selected_global_sources(payload, channels)
        invalid = set(source_ids).difference(connectors)
        if invalid:
            raise HTTPException(422, f"Unknown sources: {', '.join(sorted(invalid))}")
        row = storage.update_keyword(
            keyword_id,
            {
                "name": payload.name.strip(),
                "normalized_name": normalized(payload.name),
                "include_terms": payload.include_terms,
                "exclude_terms": payload.exclude_terms,
                "source_ids": source_ids,
                "source_selection_version": 2,
                "channels": channels,
                "enabled": payload.enabled,
                "interval_minutes": payload.interval_minutes,
                "max_items_per_source": payload.max_items_per_source,
                "next_run_at": now + timedelta(minutes=payload.interval_minutes)
                if payload.enabled
                else None,
                "updated_at": now,
            },
        )
        run_manager.rescore_keyword(keyword_id)
        return _keyword_output(row)

    @router.delete("/keywords/{keyword_id}", status_code=204)
    async def delete_keyword(keyword_id: int):
        if not await asyncio.to_thread(get_store().delete_keyword, keyword_id):
            raise HTTPException(404, "Keyword not found")

    return router
