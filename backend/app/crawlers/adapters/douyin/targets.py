"""Douyin public video, creator and short-link targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

_VIDEO_ID = re.compile(r"[1-9][0-9]{5,24}")
_CREATOR_ID = re.compile(r"[A-Za-z0-9_-]{8,256}")


class DouyinTargetKind(StrEnum):
    VIDEO = "video"
    CREATOR = "creator"
    SHORT_URL = "short_url"


@dataclass(frozen=True, slots=True)
class DouyinTarget:
    kind: DouyinTargetKind
    external_id: str
    canonical_url: str


def parse_douyin_target(value: str) -> DouyinTarget:
    text = str(value).strip()
    if _VIDEO_ID.fullmatch(text):
        return _video(text)
    parsed = _safe_url(text)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host == "v.douyin.com":
        path = parsed.path.strip("/")
        if not path or len(path) > 128:
            raise ValueError("Invalid Douyin short URL")
        return DouyinTarget(
            DouyinTargetKind.SHORT_URL, path, f"https://v.douyin.com/{path}/"
        )
    if host not in {"douyin.com", "www.douyin.com"}:
        raise ValueError("Invalid Douyin target host")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "video" and _VIDEO_ID.fullmatch(parts[1]):
        return _video(parts[1])
    if len(parts) >= 2 and parts[0] == "user" and _CREATOR_ID.fullmatch(parts[1]):
        return DouyinTarget(
            DouyinTargetKind.CREATOR,
            parts[1],
            f"https://www.douyin.com/user/{parts[1]}",
        )
    raise ValueError("Unsupported Douyin target")


def _video(identity: str) -> DouyinTarget:
    return DouyinTarget(
        DouyinTargetKind.VIDEO,
        identity,
        f"https://www.douyin.com/video/{identity}",
    )


def _safe_url(value: str):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.port:
        raise ValueError("Invalid public target URL")
    return parsed
