"""Facebook public Page, content and short-link targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlsplit

_ID = re.compile(r"[1-9][0-9]{4,24}")
_PAGE = re.compile(r"[A-Za-z0-9.]{2,75}")


class FacebookTargetKind(StrEnum):
    PAGE = "page"
    CONTENT = "content"
    SHORT_URL = "short_url"


@dataclass(frozen=True, slots=True)
class FacebookTarget:
    kind: FacebookTargetKind
    external_id: str
    canonical_url: str


def parse_facebook_target(value: str) -> FacebookTarget:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.port:
        raise ValueError("Invalid Facebook target")
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if host == "fb.watch":
        path = parsed.path.strip("/")
        if not path or len(path) > 128:
            raise ValueError("Invalid Facebook short URL")
        return FacebookTarget(
            FacebookTargetKind.SHORT_URL, path, f"https://fb.watch/{path}/"
        )
    if host not in {
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "fb.com",
        "www.fb.com",
    }:
        raise ValueError("Invalid Facebook target host")
    query = parse_qs(parsed.query)
    if parts == ["watch"] and query.get("v") and _ID.fullmatch(query["v"][0]):
        identity = query["v"][0]
        return FacebookTarget(
            FacebookTargetKind.CONTENT,
            identity,
            f"https://www.facebook.com/watch/?v={identity}",
        )
    if len(parts) >= 2 and parts[0] in {"reel", "videos"} and _ID.fullmatch(parts[1]):
        identity = parts[1]
        return FacebookTarget(
            FacebookTargetKind.CONTENT,
            identity,
            f"https://www.facebook.com/{parts[0]}/{identity}/",
        )
    if len(parts) >= 3 and parts[1] == "posts" and _ID.fullmatch(parts[2]):
        page = parts[0]
        if not _PAGE.fullmatch(page):
            raise ValueError("Invalid Facebook Page")
        identity = parts[2]
        return FacebookTarget(
            FacebookTargetKind.CONTENT,
            identity,
            f"https://www.facebook.com/{page}/posts/{identity}/",
        )
    if len(parts) == 1 and _PAGE.fullmatch(parts[0]) and parts[0] not in {"login", "watch"}:
        return FacebookTarget(
            FacebookTargetKind.PAGE,
            parts[0],
            f"https://www.facebook.com/{parts[0]}/",
        )
    raise ValueError("Unsupported Facebook target")
