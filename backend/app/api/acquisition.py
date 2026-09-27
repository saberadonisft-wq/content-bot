from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, SecretStr, model_validator

from ..application_services import AppServices, get_services
from ..services.acquisition import AcquisitionError

router = APIRouter(prefix="/api/v1/acquisition", tags=["acquisition"])


class AcquisitionLimits(BaseModel):
    max_candidates: int = Field(default=20, ge=1, le=100)
    max_pages: int = Field(default=20, ge=1, le=20)
    deadline_seconds: int = Field(default=300, ge=30, le=600)


class AcquisitionRunRequest(BaseModel):
    mode: Literal["video", "creator", "playlist", "search"]
    targets: list[str] = Field(default_factory=list, max_length=20)
    query: str | None = Field(default=None, max_length=180)
    source_id: str | None = Field(default=None, max_length=64)
    provider_id: str | None = Field(default=None, max_length=64)
    connection_id: str | None = Field(default=None, max_length=128)
    limits: AcquisitionLimits = Field(default_factory=AcquisitionLimits)
    filters: dict[str, str | int | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shape(self) -> AcquisitionRunRequest:
        if self.mode == "search" and not (self.query or "").strip():
            raise ValueError("query is required for search mode")
        if self.mode != "search" and not self.targets:
            raise ValueError("at least one target is required")
        return self


class DownloadSelectionRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=100)
    quality: Literal["best", "1080", "720", "480"] = "1080"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    cookie_text: SecretStr | None = Field(default=None, max_length=1_000_000)
    connection_id: str | None = Field(default=None, max_length=128)


class AcquisitionContinueRequest(BaseModel):
    expected_generation: int = Field(ge=0)


class DownloadSelectionControlRequest(BaseModel):
    cookie_text: SecretStr | None = Field(default=None, max_length=1_000_000)


class AcquisitionChannelRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    label: str = Field(default="", max_length=120)
    connection_id: str | None = Field(default=None, max_length=128)


class AcquisitionChannelPatch(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    connection_id: str | None = Field(default=None, max_length=128)


class AcquisitionSubscriptionRequest(BaseModel):
    enabled: bool = True
    initial_policy: Literal["baseline", "backfill"] = "baseline"
    auto_download: bool = False
    interval_minutes: int = Field(default=360, ge=30, le=10_080)
    quality: Literal["best", "1080", "720", "480"] = "1080"
    max_items: int = Field(default=20, ge=1, le=100)


def _raise_acquisition(error: AcquisitionError) -> None:
    status = {
        "NOT_FOUND": 404,
        "UNSUPPORTED_OPERATION": 409,
        "RUN_STOPPING": 409,
        "RUN_NOT_CONTINUABLE": 409,
        "QUEUE_FULL": 429,
        "RATE_LIMITED": 429,
        "BUDGET_EXCEEDED": 422,
        "SERVICE_STOPPING": 503,
        "STORAGE_UNAVAILABLE": 503,
        "FEATURE_DISABLED": 409,
    }.get(error.code, 422)
    headers = None
    if error.retry_after is not None:
        headers = {"Retry-After": str(max(0, int(error.retry_after)))}
    raise HTTPException(
        status_code=status,
        detail={
            "code": error.code,
            "message": str(error),
            "retryable": error.code
            in {"SOURCE_UNAVAILABLE", "RATE_LIMITED", "QUEUE_FULL", "RUN_STOPPING"},
            "retry_after": error.retry_after,
            "run_id": error.run_id,
            "job_id": error.job_id,
        },
        headers=headers,
    ) from error


@router.post("/runs", status_code=202)
def create_run(
    request: AcquisitionRunRequest,
    services: AppServices = Depends(get_services),
):
    try:
        payload = request.model_dump(exclude_none=True)
        payload["limits"] = request.limits.model_dump()
        return services.acquisition.create_run(payload)
    except AcquisitionError as error:
        _raise_acquisition(error)


@router.get("/capabilities")
def acquisition_capabilities(services: AppServices = Depends(get_services)):
    return services.acquisition.capabilities()


@router.get("/runs")
def list_runs(
    limit: int = Query(default=50, ge=1, le=100),
    services: AppServices = Depends(get_services),
):
    return {"items": services.acquisition.list_runs(limit=limit), "limit": limit}


@router.get("/runs/{run_id}")
def get_run(run_id: str, services: AppServices = Depends(get_services)):
    run = services.acquisition.get_run(run_id)
    if run is None:
        _raise_acquisition(AcquisitionError("Không tìm thấy lượt cào.", code="NOT_FOUND"))
    return run


@router.get("/runs/{run_id}/candidates")
def list_candidates(
    run_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    media_type: Literal["video", "audio", "live"] | None = None,
    only_not_downloaded: bool = False,
    services: AppServices = Depends(get_services),
):
    try:
        return services.acquisition.list_candidates(
            run_id,
            offset=offset,
            limit=limit,
            media_type=media_type,
            only_not_downloaded=only_not_downloaded,
        )
    except AcquisitionError as error:
        _raise_acquisition(error)


@router.post("/runs/{run_id}/pause")
def pause_run(run_id: str, services: AppServices = Depends(get_services)):
    try:
        run = services.acquisition.pause(run_id)
    except AcquisitionError as error:
        _raise_acquisition(error)
    if run is None:
        _raise_acquisition(AcquisitionError("Không tìm thấy lượt cào.", code="NOT_FOUND"))
    return run


@router.post("/runs/{run_id}/resume", status_code=202)
def resume_run(run_id: str, services: AppServices = Depends(get_services)):
    try:
        run = services.acquisition.resume(run_id)
    except AcquisitionError as error:
        _raise_acquisition(error)
    if run is None:
        _raise_acquisition(AcquisitionError("Không tìm thấy lượt cào.", code="NOT_FOUND"))
    return run


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, services: AppServices = Depends(get_services)):
    try:
        run = services.acquisition.cancel(run_id)
    except AcquisitionError as error:
        _raise_acquisition(error)
    if run is None:
        _raise_acquisition(AcquisitionError("Không tìm thấy lượt cào.", code="NOT_FOUND"))
    return run


@router.post("/runs/{run_id}/continue", status_code=202)
def continue_run(
    run_id: str,
    request: AcquisitionContinueRequest,
    services: AppServices = Depends(get_services),
):
    try:
        run = services.acquisition.continue_run(run_id, expected_generation=request.expected_generation)
    except AcquisitionError as error:
        _raise_acquisition(error)
    if run is None:
        _raise_acquisition(AcquisitionError("Không tìm thấy lượt cào.", code="NOT_FOUND"))
    return run


@router.post("/download-selections", status_code=202)
def create_download_selection(
    request: DownloadSelectionRequest,
    services: AppServices = Depends(get_services),
):
    try:
        return services.acquisition.select_downloads(
            request.candidate_ids,
            quality=request.quality,
            idempotency_key=request.idempotency_key,
            cookie_text=request.cookie_text.get_secret_value() if request.cookie_text else None,
            connection_id=request.connection_id,
        )
    except AcquisitionError as error:
        _raise_acquisition(error)


@router.get("/download-selections")
def list_download_selections(
    limit: int = Query(default=50, ge=1, le=100),
    services: AppServices = Depends(get_services),
):
    return {"items": services.acquisition.list_selections(limit=limit), "limit": limit}


@router.get("/download-selections/{selection_id}")
def get_download_selection(selection_id: str, services: AppServices = Depends(get_services)):
    selection = services.acquisition.get_selection(selection_id)
    if selection is None:
        _raise_acquisition(AcquisitionError("Không tìm thấy selection.", code="NOT_FOUND"))
    return selection


def _control_download_selection(
    selection_id: str,
    action: str,
    request: DownloadSelectionControlRequest | None,
    services: AppServices,
):
    try:
        return services.acquisition.control_selection(
            selection_id,
            action,
            cookie_text=request.cookie_text.get_secret_value() if request and request.cookie_text else None,
        )
    except AcquisitionError as error:
        _raise_acquisition(error)


@router.post("/download-selections/{selection_id}/pause")
def pause_download_selection(selection_id: str, services: AppServices = Depends(get_services)):
    return _control_download_selection(selection_id, "pause", None, services)


@router.post("/download-selections/{selection_id}/resume", status_code=202)
def resume_download_selection(
    selection_id: str,
    request: DownloadSelectionControlRequest | None = None,
    services: AppServices = Depends(get_services),
):
    return _control_download_selection(selection_id, "resume", request, services)


@router.post("/download-selections/{selection_id}/cancel")
def cancel_download_selection(selection_id: str, services: AppServices = Depends(get_services)):
    return _control_download_selection(selection_id, "cancel", None, services)


@router.get("/channels")
def list_acquisition_channels(
    limit: int = Query(default=100, ge=1, le=100),
    services: AppServices = Depends(get_services),
):
    return {"items": services.acquisition.list_channels(limit=limit), "limit": limit}


@router.post("/channels", status_code=201)
def save_acquisition_channel(
    request: AcquisitionChannelRequest,
    services: AppServices = Depends(get_services),
):
    try:
        return services.acquisition.save_channel(
            request.url,
            label=request.label,
            connection_id=request.connection_id,
        )
    except AcquisitionError as error:
        _raise_acquisition(error)


@router.patch("/channels/{channel_id}")
def update_acquisition_channel(
    channel_id: str,
    request: AcquisitionChannelPatch,
    services: AppServices = Depends(get_services),
):
    try:
        result = services.acquisition.update_channel(
            channel_id,
            **request.model_dump(exclude_unset=True),
        )
    except AcquisitionError as error:
        _raise_acquisition(error)
    if result is None:
        _raise_acquisition(AcquisitionError("Không tìm thấy kênh.", code="NOT_FOUND"))
    return result


@router.delete("/channels/{channel_id}", status_code=204)
def delete_acquisition_channel(
    channel_id: str,
    services: AppServices = Depends(get_services),
):
    if not services.acquisition.delete_channel(channel_id):
        _raise_acquisition(AcquisitionError("Không tìm thấy kênh.", code="NOT_FOUND"))


@router.put("/channels/{channel_id}/subscription")
def update_acquisition_subscription(
    channel_id: str,
    request: AcquisitionSubscriptionRequest,
    services: AppServices = Depends(get_services),
):
    try:
        result = services.acquisition.update_subscription(
            channel_id,
            enabled=request.enabled,
            initial_policy=request.initial_policy,
            auto_download=request.auto_download,
            interval_minutes=request.interval_minutes,
            quality=request.quality,
            max_items=request.max_items,
        )
    except AcquisitionError as error:
        _raise_acquisition(error)
    if result is None:
        _raise_acquisition(AcquisitionError("Không tìm thấy kênh.", code="NOT_FOUND"))
    return result
