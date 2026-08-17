from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth import require_admin
from ..database import db
from ..models import (
    MessageResponse,
    ReleaseChannel,
    ReleaseCreateRequest,
    ReleaseResponse,
    UpdateCheckResponse,
)

router = APIRouter(tags=["Update & Releases"])


def parse_version(v: str) -> tuple[int, ...]:
    """Parse version string into a comparable tuple of integers."""
    clean = re.sub(r"[^\d.]", "", v.strip().lstrip("v"))
    parts = [int(p) for p in clean.split(".") if p.isdigit()]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer_version(candidate: str, current: str) -> bool:
    """Return True if candidate version is strictly newer than current version."""
    try:
        return parse_version(candidate) > parse_version(current)
    except Exception:
        return candidate.strip() != current.strip()


def doc_to_release_response(doc: dict[str, Any]) -> ReleaseResponse:
    return ReleaseResponse(
        id=str(doc.get("_id", "")),
        version=doc["version"],
        channel=doc.get("channel", "stable"),
        download_url=doc["download_url"],
        sha256=doc["sha256"],
        file_size=doc.get("file_size", 0),
        changelog=doc.get("changelog", ""),
        mandatory=doc.get("mandatory", False),
        min_supported_version=doc.get("min_supported_version"),
        published_at=doc.get("published_at", datetime.now(UTC)),
        published_by=doc.get("published_by"),
    )


@router.get("/api/v1/update/check", response_model=UpdateCheckResponse)
def check_update(
    current_version: str = Query(..., description="Current client app version, e.g. 0.1.0"),
    channel: ReleaseChannel = Query("stable", description="Release channel: stable or beta"),
) -> UpdateCheckResponse:
    """Check if a newer version of Content Bot is available on the specified channel."""
    latest = db.get_latest_release(channel=channel)
    if not latest:
        return UpdateCheckResponse(
            update_available=False,
            current_version=current_version,
            latest_version=current_version,
            channel=channel,
        )

    latest_ver = latest["version"]
    if is_newer_version(latest_ver, current_version):
        return UpdateCheckResponse(
            update_available=True,
            current_version=current_version,
            latest_version=latest_ver,
            channel=channel,
            download_url=latest["download_url"],
            sha256=latest["sha256"],
            file_size=latest.get("file_size", 0),
            changelog=latest.get("changelog", ""),
            mandatory=latest.get("mandatory", False),
            published_at=latest.get("published_at"),
        )

    return UpdateCheckResponse(
        update_available=False,
        current_version=current_version,
        latest_version=latest_ver,
        channel=channel,
    )


@router.get("/admin/releases", response_model=list[ReleaseResponse])
def list_releases(
    channel: ReleaseChannel | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    admin: dict[str, Any] = Depends(require_admin),
) -> list[ReleaseResponse]:
    """List published app releases."""
    docs = db.list_releases(channel=channel, skip=skip, limit=limit)
    return [doc_to_release_response(d) for d in docs]


@router.post("/admin/releases", response_model=ReleaseResponse, status_code=status.HTTP_201_CREATED)
def publish_release(
    req: ReleaseCreateRequest,
    admin: dict[str, Any] = Depends(require_admin),
) -> ReleaseResponse:
    """Publish a new app release package metadata."""
    admin_identifier = admin.get("email") or str(admin.get("_id"))

    doc = {
        "version": req.version.strip(),
        "channel": req.channel,
        "download_url": req.download_url.strip(),
        "sha256": req.sha256.strip().lower(),
        "file_size": req.file_size,
        "changelog": req.changelog.strip(),
        "mandatory": req.mandatory,
        "min_supported_version": req.min_supported_version,
        "published_at": datetime.now(UTC),
        "published_by": admin_identifier,
    }

    created = db.create_release(doc)
    return doc_to_release_response(created)


@router.delete("/admin/releases/{version}", response_model=MessageResponse)
def delete_release(
    version: str,
    channel: ReleaseChannel = Query("stable"),
    admin: dict[str, Any] = Depends(require_admin),
) -> MessageResponse:
    """Delete a release version metadata."""
    db.delete_release(version=version, channel=channel)
    return MessageResponse(message=f"Đã xóa phiên bản {version} ({channel}).")
