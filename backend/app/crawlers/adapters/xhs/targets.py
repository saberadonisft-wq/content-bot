"""Xiaohongshu/RedNote public note, creator and short-link targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

_NOTE_ID = re.compile(r"[0-9a-fA-F]{24}")
_CREATOR_ID = re.compile(r"[0-9a-zA-Z_-]{8,64}")


class XhsTargetKind(StrEnum):
    NOTE = "note"
    CREATOR = "creator"
    SHORT_URL = "short_url"


@dataclass(frozen=True, slots=True)
class XhsTarget:
    kind: XhsTargetKind
    external_id: str
    canonical_url: str


def parse_xhs_target(value: str) -> XhsTarget:
    text = str(value).strip()
    if _NOTE_ID.fullmatch(text):
        identity = text.casefold()
        return XhsTarget(
            XhsTargetKind.NOTE,
            identity,
            f"https://www.xiaohongshu.com/explore/{identity}",
        )
    parsed = _safe_url(text)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host == "xhslink.com" or host.endswith(".xhslink.com"):
        path = parsed.path.strip("/")
        if not path or len(path) > 256:
            raise ValueError("Invalid Xiaohongshu short URL")
        return XhsTarget(XhsTargetKind.SHORT_URL, path, f"https://xhslink.com/{path}")
    allowed = {
        "xiaohongshu.com",
        "www.xiaohongshu.com",
        "rednote.com",
        "www.rednote.com",
    }
    if host not in allowed:
        raise ValueError("Invalid Xiaohongshu target host")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"explore", "discovery"}:
        identity = parts[-1].casefold()
        if _NOTE_ID.fullmatch(identity):
            domain = "www.rednote.com" if "rednote.com" in host else "www.xiaohongshu.com"
            return XhsTarget(
                XhsTargetKind.NOTE,
                identity,
                f"https://{domain}/explore/{identity}",
            )
    if len(parts) >= 3 and parts[:2] == ["user", "profile"]:
        identity = parts[2]
        if _CREATOR_ID.fullmatch(identity):
            domain = "www.rednote.com" if "rednote.com" in host else "www.xiaohongshu.com"
            return XhsTarget(
                XhsTargetKind.CREATOR,
                identity,
                f"https://{domain}/user/profile/{identity}",
            )
    raise ValueError("Unsupported Xiaohongshu target")


def _safe_url(value: str):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.port:
        raise ValueError("Invalid public target URL")
    return parsed
