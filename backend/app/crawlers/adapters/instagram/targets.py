"""Instagram public professional-account and media targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

_USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}")
_SHORTCODE = re.compile(r"[A-Za-z0-9_-]{5,64}")


class InstagramTargetKind(StrEnum):
    MEDIA = "media"
    ACCOUNT = "account"


@dataclass(frozen=True, slots=True)
class InstagramTarget:
    kind: InstagramTargetKind
    external_id: str
    canonical_url: str


def parse_instagram_target(value: str) -> InstagramTarget:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.port:
        raise ValueError("Invalid Instagram target")
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host not in {"instagram.com", "www.instagram.com"}:
        raise ValueError("Invalid Instagram target host")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"p", "reel", "tv"} and _SHORTCODE.fullmatch(parts[1]):
        return InstagramTarget(
            InstagramTargetKind.MEDIA,
            parts[1],
            f"https://www.instagram.com/{parts[0]}/{parts[1]}/",
        )
    if len(parts) == 1 and _USERNAME.fullmatch(parts[0]) and parts[0] not in {"accounts", "explore"}:
        return InstagramTarget(
            InstagramTargetKind.ACCOUNT,
            parts[0],
            f"https://www.instagram.com/{parts[0]}/",
        )
    raise ValueError("Unsupported Instagram target")
