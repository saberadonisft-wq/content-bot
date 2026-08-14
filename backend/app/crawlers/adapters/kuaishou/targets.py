"""Kuaishou public video, creator and short-link targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

_IDENTITY = re.compile(r"[A-Za-z0-9_-]{6,128}")


class KuaishouTargetKind(StrEnum):
    VIDEO = "video"
    CREATOR = "creator"
    SHORT_URL = "short_url"


@dataclass(frozen=True, slots=True)
class KuaishouTarget:
    kind: KuaishouTargetKind
    external_id: str
    canonical_url: str


def parse_kuaishou_target(value: str) -> KuaishouTarget:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.port:
        raise ValueError("Invalid Kuaishou target")
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if host == "v.kuaishou.com":
        path = parsed.path.strip("/")
        if not path or len(path) > 128:
            raise ValueError("Invalid Kuaishou short URL")
        return KuaishouTarget(
            KuaishouTargetKind.SHORT_URL, path, f"https://v.kuaishou.com/{path}"
        )
    if host not in {"kuaishou.com", "www.kuaishou.com"}:
        raise ValueError("Invalid Kuaishou target host")
    if len(parts) >= 2 and parts[0] == "short-video" and _IDENTITY.fullmatch(parts[1]):
        return KuaishouTarget(
            KuaishouTargetKind.VIDEO,
            parts[1],
            f"https://www.kuaishou.com/short-video/{parts[1]}",
        )
    if len(parts) >= 2 and parts[0] == "profile" and _IDENTITY.fullmatch(parts[1]):
        return KuaishouTarget(
            KuaishouTargetKind.CREATOR,
            parts[1],
            f"https://www.kuaishou.com/profile/{parts[1]}",
        )
    raise ValueError("Unsupported Kuaishou target")
