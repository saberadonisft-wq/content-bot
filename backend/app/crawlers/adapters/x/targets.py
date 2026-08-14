"""Strict canonicalization for public X account and Post targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

_POST_ID = re.compile(r"[1-9][0-9]{0,18}")
_USERNAME = re.compile(r"[A-Za-z0-9_]{1,15}")


class XTargetKind(StrEnum):
    POST = "post"
    CREATOR = "creator"


@dataclass(frozen=True, slots=True)
class XTarget:
    kind: XTargetKind
    external_id: str
    canonical_url: str


def parse_x_target(value: str) -> XTarget:
    text = str(value).strip()
    if _POST_ID.fullmatch(text):
        return XTarget(
            XTargetKind.POST,
            text,
            f"https://x.com/i/web/status/{text}",
        )
    if _USERNAME.fullmatch(text.removeprefix("@")):
        username = text.removeprefix("@")
        return XTarget(
            XTargetKind.CREATOR,
            username,
            f"https://x.com/{username}",
        )
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.port:
        raise ValueError("Invalid X target")
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        raise ValueError("Invalid X target host")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 4 and parts[:3] == ["i", "web", "status"]:
        post_id = parts[3]
        if _POST_ID.fullmatch(post_id):
            return XTarget(
                XTargetKind.POST,
                post_id,
                f"https://x.com/i/web/status/{post_id}",
            )
    if len(parts) >= 3 and parts[1] == "status":
        username, post_id = parts[0], parts[2]
        if _USERNAME.fullmatch(username) and _POST_ID.fullmatch(post_id):
            return XTarget(
                XTargetKind.POST,
                post_id,
                f"https://x.com/i/web/status/{post_id}",
            )
    if len(parts) == 1 and _USERNAME.fullmatch(parts[0]):
        return XTarget(
            XTargetKind.CREATOR,
            parts[0],
            f"https://x.com/{parts[0]}",
        )
    raise ValueError("Unsupported X target")
