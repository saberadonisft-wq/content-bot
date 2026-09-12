from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..application_services import AppServices, get_services
from ..schemas import (
    BatchOutput,
    RunRequest,
    SourceRunOutput,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def batch_output(batch: dict) -> BatchOutput:
    return BatchOutput(
        id=batch["id"],
        keyword_id=batch["keyword_id"],
        trigger=batch["trigger"],
        session_number=batch.get("session_number", 1),
        new_item_count=batch.get(
            "new_item_count",
            sum(row.get("ingested_count", 0) for row in batch.get("source_runs", [])),
        ),
        state=batch["state"],
        started_at=batch.get("started_at"),
        finished_at=batch.get("finished_at"),
        error_message=batch.get("error_message"),
        source_runs=[
            SourceRunOutput(
                id=row["id"],
                source_id=row["source_id"],
                channel_id=row.get("channel_id"),
                channel_url=row.get("channel_url"),
                channel_label=row.get("channel_label"),
                state=row["state"],
                phase=row.get("phase", row["state"]),
                progress_mode=row.get("progress_mode", "determinate"),
                progress_current=row.get(
                    "progress_current", row.get("fetched_count", 0)
                ),
                progress_total=row.get("progress_total"),
                progress_percent=(
                    100.0
                    if row.get("state") == "succeeded"
                    else (
                        round(
                            min(
                                row.get("fetched_count", 0)
                                / row["progress_total"]
                                * 100,
                                99,
                            ),
                            1,
                        )
                        if row.get("progress_mode", "determinate") == "determinate"
                        and row.get("progress_total")
                        else None
                    )
                ),
                message=row.get("message"),
                browser_state=row.get("browser_state"),
                fetched_count=row.get("fetched_count", 0),
                ingested_count=row.get("ingested_count", 0),
                started_at=row.get("started_at"),
                finished_at=row.get("finished_at"),
                heartbeat_at=row.get("heartbeat_at"),
                error_message=row.get("error_message"),
            )
            for row in batch.get("source_runs", [])
        ],
    )


@router.post("/api/v1/runs", response_model=BatchOutput, status_code=202)
async def create_run(
    payload: RunRequest, *, services: AppServices = Depends(get_services)
):
    if not await asyncio.to_thread(services.store.keyword, payload.keyword_id):
        raise HTTPException(404, "Keyword not found")
    try:
        batch_id = await services.run_manager.start_batch(
            payload.keyword_id,
            payload.trigger,
            payload.source_ids,
            payload.channel_ids,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    batch = await asyncio.to_thread(services.store.batch, batch_id)
    return batch_output(batch)


@router.get("/api/v1/runs", response_model=list[BatchOutput])
def list_runs(
    keyword_id: int | None = None,
    limit: int = Query(default=10, ge=1, le=100),
    *,
    services: AppServices = Depends(get_services),
):
    rows = services.store.batches(keyword_id, limit)
    return [batch_output(row) for row in rows]


@router.get("/api/v1/runs/{batch_id}", response_model=BatchOutput)
def get_run(batch_id: str, *, services: AppServices = Depends(get_services)):
    batch = services.store.batch(batch_id)
    if not batch:
        raise HTTPException(404, "Run not found")
    return batch_output(batch)


@router.post("/api/v1/runs/{batch_id}/cancel", status_code=202)
async def cancel_run(batch_id: str, *, services: AppServices = Depends(get_services)):
    if not await services.run_manager.cancel_batch(batch_id):
        raise HTTPException(409, "Run is not active")
    return {"id": batch_id, "state": "cancelling"}


@router.get("/api/v1/events")
async def event_stream(services: AppServices = Depends(get_services)):
    async def stream():
        async for event in services.events.subscribe():
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/v1/runs/{batch_id}/events")
async def run_event_stream(
    batch_id: str, *, services: AppServices = Depends(get_services)
):
    async def stream():
        async for event in services.events.subscribe():
            if event.get("type") == "connected" or event.get("batch_id") == batch_id:
                yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
