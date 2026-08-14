"""Strict Baidu Tieba thread, forum and creator target canonicalization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, quote, urlsplit

_THREAD_ID = re.compile(r"[1-9][0-9]{0,19}")
_CREATOR_ID = re.compile(r"[A-Za-z0-9._~%+-]{1,256}")


class TiebaTargetKind(StrEnum):
    THREAD = "thread"
    FORUM = "forum"
    CREATOR = "creator"


@dataclass(frozen=True, slots=True)
class TiebaTarget:
    kind: TiebaTargetKind
    external_id: str
    canonical_url: str


def parse_tieba_target(value: str) -> TiebaTarget:
    text = str(value).strip()
    if _THREAD_ID.fullmatch(text):
        return _thread(text)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.port:
        raise ValueError("Invalid Tieba target")
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host not in {"tieba.baidu.com", "www.tieba.baidu.com"}:
        raise ValueError("Invalid Tieba target host")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "p" and _THREAD_ID.fullmatch(parts[1]):
        return _thread(parts[1])
    query = parse_qs(parsed.query, keep_blank_values=False)
    if parts == ["f"] and query.get("kw"):
        forum = " ".join(query["kw"][0].strip().split())
        if not forum or len(forum) > 100:
            raise ValueError("Invalid Tieba forum")
        return TiebaTarget(
            TiebaTargetKind.FORUM,
            forum,
            f"https://tieba.baidu.com/f?kw={quote(forum)}",
        )
    if parts[:2] == ["home", "main"] and query.get("id"):
        creator = query["id"][0]
        if not _CREATOR_ID.fullmatch(creator):
            raise ValueError("Invalid Tieba creator")
        return TiebaTarget(
            TiebaTargetKind.CREATOR,
            creator,
            f"https://tieba.baidu.com/home/main?id={quote(creator, safe='')}",
        )
    raise ValueError("Unsupported Tieba target")


def _thread(identity: str) -> TiebaTarget:
    return TiebaTarget(
        TiebaTargetKind.THREAD,
        identity,
        f"https://tieba.baidu.com/p/{identity}",
    )
