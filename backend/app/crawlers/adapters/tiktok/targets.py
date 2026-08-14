"""TikTok international creator, video and short-link targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

_USERNAME = re.compile(r"[A-Za-z0-9._]{2,24}")
_VIDEO_ID = re.compile(r"[1-9][0-9]{5,24}")


class TikTokTargetKind(StrEnum):
    VIDEO = "video"
    CREATOR = "creator"
    SHORT_URL = "short_url"


@dataclass(frozen=True, slots=True)
class TikTokTarget:
    kind: TikTokTargetKind
    external_id: str
    canonical_url: str


def parse_tiktok_target(value: str) -> TikTokTarget:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.port:
        raise ValueError("Invalid TikTok target")
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if host in {"vm.tiktok.com", "vt.tiktok.com"}:
        path = parsed.path.strip("/")
        if not path or len(path) > 128:
            raise ValueError("Invalid TikTok short URL")
        return TikTokTarget(
            TikTokTargetKind.SHORT_URL, path, f"https://{host}/{path}/"
        )
    if host not in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
        raise ValueError("Invalid TikTok target host")
    if not parts or not parts[0].startswith("@"):
        raise ValueError("Unsupported TikTok target")
    username = parts[0][1:]
    if not _USERNAME.fullmatch(username):
        raise ValueError("Invalid TikTok username")
    if len(parts) == 1:
        return TikTokTarget(
            TikTokTargetKind.CREATOR, username, f"https://www.tiktok.com/@{username}"
        )
    if (
        len(parts) >= 3
        and parts[1] == "video"
        and _VIDEO_ID.fullmatch(parts[2])
    ):
        return TikTokTarget(
            TikTokTargetKind.VIDEO,
            parts[2],
            f"https://www.tiktok.com/@{username}/video/{parts[2]}",
        )
    raise ValueError("Unsupported TikTok target")
