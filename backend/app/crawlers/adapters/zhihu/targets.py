"""Zhihu answer, article, video and creator targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

_NUMERIC_ID = re.compile(r"[1-9][0-9]{0,24}")
_SLUG = re.compile(r"[A-Za-z0-9_-]{1,100}")


class ZhihuTargetKind(StrEnum):
    ANSWER = "answer"
    ARTICLE = "article"
    VIDEO = "video"
    CREATOR = "creator"


@dataclass(frozen=True, slots=True)
class ZhihuTarget:
    kind: ZhihuTargetKind
    external_id: str
    canonical_url: str


def parse_zhihu_target(value: str) -> ZhihuTarget:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.port:
        raise ValueError("Invalid Zhihu target")
    host = (parsed.hostname or "").casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if host in {"zhihu.com", "www.zhihu.com"}:
        if (
            len(parts) >= 4
            and parts[0] == "question"
            and _NUMERIC_ID.fullmatch(parts[1])
            and parts[2] == "answer"
            and _NUMERIC_ID.fullmatch(parts[3])
        ):
            identity = parts[3]
            return ZhihuTarget(
                ZhihuTargetKind.ANSWER,
                identity,
                f"https://www.zhihu.com/question/{parts[1]}/answer/{identity}",
            )
        if len(parts) >= 2 and parts[0] == "zvideo" and _NUMERIC_ID.fullmatch(parts[1]):
            return ZhihuTarget(
                ZhihuTargetKind.VIDEO,
                parts[1],
                f"https://www.zhihu.com/zvideo/{parts[1]}",
            )
        if len(parts) >= 2 and parts[0] == "people" and _SLUG.fullmatch(parts[1]):
            return ZhihuTarget(
                ZhihuTargetKind.CREATOR,
                parts[1],
                f"https://www.zhihu.com/people/{parts[1]}",
            )
    if host == "zhuanlan.zhihu.com" and len(parts) >= 2 and parts[0] == "p" and _NUMERIC_ID.fullmatch(parts[1]):
        return ZhihuTarget(
            ZhihuTargetKind.ARTICLE,
            parts[1],
            f"https://zhuanlan.zhihu.com/p/{parts[1]}",
        )
    raise ValueError("Unsupported Zhihu target")
