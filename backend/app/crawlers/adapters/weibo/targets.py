"""Strict public Weibo Post and creator target canonicalization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

_NUMERIC_ID = re.compile(r"[1-9][0-9]{0,19}")
_BID = re.compile(r"[A-Za-z0-9]{6,16}")


class WeiboTargetKind(StrEnum):
    POST = "post"
    CREATOR = "creator"


@dataclass(frozen=True, slots=True)
class WeiboTarget:
    kind: WeiboTargetKind
    external_id: str
    canonical_url: str


def parse_weibo_target(value: str) -> WeiboTarget:
    text = str(value).strip()
    if _NUMERIC_ID.fullmatch(text):
        return _post(text)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.port:
        raise ValueError("Invalid Weibo target")
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host not in {
        "weibo.com",
        "www.weibo.com",
        "m.weibo.cn",
        "weibo.cn",
        "www.weibo.cn",
    }:
        raise ValueError("Invalid Weibo target host")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"detail", "status"}:
        identity = parts[1]
        if _NUMERIC_ID.fullmatch(identity) or _BID.fullmatch(identity):
            return _post(identity)
    if len(parts) >= 2 and parts[0] in {"u", "profile"}:
        creator_id = parts[1]
        if _NUMERIC_ID.fullmatch(creator_id):
            return _creator(creator_id)
    if len(parts) >= 2 and _NUMERIC_ID.fullmatch(parts[0]) and _BID.fullmatch(
        parts[1]
    ):
        return _post(parts[1])
    raise ValueError("Unsupported Weibo target")


def _post(identity: str) -> WeiboTarget:
    return WeiboTarget(
        WeiboTargetKind.POST,
        identity,
        f"https://m.weibo.cn/detail/{identity}",
    )


def _creator(identity: str) -> WeiboTarget:
    return WeiboTarget(
        WeiboTargetKind.CREATOR,
        identity,
        f"https://weibo.com/u/{identity}",
    )
