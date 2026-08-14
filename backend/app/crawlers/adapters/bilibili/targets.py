"""Parse public Bilibili target URLs without any request signing knowledge."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlsplit

_VIDEO_ID = re.compile(r"^(?:BV[0-9A-Za-z]{10}|av[1-9][0-9]*)$")
_UID = re.compile(r"^[1-9][0-9]*$")
_SHORT_TOKEN = re.compile(r"^[0-9A-Za-z_-]{3,64}$")


class BilibiliTargetKind(StrEnum):
    VIDEO = "video"
    CREATOR = "creator"
    DYNAMIC = "dynamic"
    SHORT_URL = "short_url"


class BilibiliTargetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BilibiliTarget:
    kind: BilibiliTargetKind
    external_id: str | None
    canonical_url: str
    part_index: int | None = None
    requires_resolution: bool = False


def parse_bilibili_target(value: str) -> BilibiliTarget:
    text = str(value).strip()
    if _VIDEO_ID.fullmatch(text):
        return _video_target(text, None)
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise BilibiliTargetError("Malformed Bilibili target") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BilibiliTargetError("Bilibili target must be an HTTP(S) URL or video ID")
    if parsed.username is not None or parsed.password is not None:
        raise BilibiliTargetError("Bilibili target cannot contain user information")
    try:
        if parsed.port not in {None, 80, 443}:
            raise BilibiliTargetError("Bilibili target uses an unsupported port")
    except ValueError as exc:
        raise BilibiliTargetError("Bilibili target has an invalid port") from exc
    host = parsed.hostname.rstrip(".").casefold()
    path = "/" + "/".join(segment for segment in parsed.path.split("/") if segment)

    if host == "b23.tv":
        token = path.removeprefix("/")
        if not _SHORT_TOKEN.fullmatch(token):
            raise BilibiliTargetError("Invalid Bilibili short URL")
        return BilibiliTarget(
            BilibiliTargetKind.SHORT_URL,
            None,
            f"https://b23.tv/{token}",
            requires_resolution=True,
        )

    if not (host == "bilibili.com" or host.endswith(".bilibili.com")):
        raise BilibiliTargetError("URL is not owned by Bilibili")

    video_match = re.fullmatch(r"/video/([^/]+)", path)
    if video_match and _VIDEO_ID.fullmatch(video_match.group(1)):
        query = parse_qs(parsed.query, keep_blank_values=True)
        part_index = _part_index(query)
        return _video_target(video_match.group(1), part_index)

    creator_match = re.fullmatch(r"/([1-9][0-9]*)(?:/video|/upload(?:/video)?)?", path)
    if host == "space.bilibili.com" and creator_match:
        uid = creator_match.group(1)
        return BilibiliTarget(
            BilibiliTargetKind.CREATOR,
            uid,
            f"https://space.bilibili.com/{uid}",
        )

    dynamic_match = re.fullmatch(r"/([1-9][0-9]*)", path)
    if host == "t.bilibili.com" and dynamic_match:
        dynamic_id = dynamic_match.group(1)
        return BilibiliTarget(
            BilibiliTargetKind.DYNAMIC,
            dynamic_id,
            f"https://t.bilibili.com/{dynamic_id}",
        )

    opus_match = re.fullmatch(r"/opus/([1-9][0-9]*)", path)
    if opus_match:
        dynamic_id = opus_match.group(1)
        return BilibiliTarget(
            BilibiliTargetKind.DYNAMIC,
            dynamic_id,
            f"https://www.bilibili.com/opus/{dynamic_id}",
        )
    raise BilibiliTargetError("Unsupported Bilibili target URL")


def _video_target(video_id: str, part_index: int | None) -> BilibiliTarget:
    normalized = (
        f"av{video_id[2:]}" if video_id.casefold().startswith("av") else video_id
    )
    return BilibiliTarget(
        BilibiliTargetKind.VIDEO,
        normalized,
        f"https://www.bilibili.com/video/{normalized}",
        part_index=part_index,
    )


def _part_index(query: dict[str, list[str]]) -> int | None:
    unexpected = set(query) - {"p"}
    if unexpected:
        # Tracking query parameters do not affect identity and are discarded.
        query = {"p": query.get("p", [])}
    values = query.get("p", [])
    if not values:
        return None
    if len(values) != 1 or not values[0].isdigit():
        raise BilibiliTargetError("Invalid Bilibili video part index")
    part = int(values[0])
    if not 1 <= part <= 10_000:
        raise BilibiliTargetError("Invalid Bilibili video part index")
    return part
