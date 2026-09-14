"""Local detection and lossless validation for media-assisted cue splitting."""
from __future__ import annotations

import re
import unicodedata

MAX_CUE_CHARACTERS = 84
MAX_CUE_DURATION_MS = 6000


def normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def sentence_count(text: str) -> int:
    """Count sentence candidates, ignoring decimals, common abbreviations and pauses."""
    text = normalized_text(text)
    text = re.sub(r"(?<=\d)[.,](?=\d)", "", text)
    text = re.sub(r"\b(?:TS|ThS|PGS|GS|BS|TP|Mr|Mrs|Ms|Dr|Prof|St)\.", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:[^\W\d_]\.){2,}", "", text)
    # An ellipsis is usually hesitation, not several sentences.
    text = re.sub(r"\.{2,}|…+", " ", text)
    return sum(any(char.isalnum() for char in part) for part in re.split(r"[.!?。！？]+", text))


def is_long_cue(cue: dict) -> bool:
    return (len(normalized_text(cue["text"])) > MAX_CUE_CHARACTERS
            or cue["end_ms"] - cue["start_ms"] > MAX_CUE_DURATION_MS
            or len(cue["text"].strip().splitlines()) > 2
            or sentence_count(cue["text"]) > 1)


def _content(text: str) -> str:
    # Ignore punctuation/spacing changes only; letters, digits and their order must survive.
    return "".join(c for c in unicodedata.normalize("NFC", text)
                   if not c.isspace() and not unicodedata.category(c).startswith("P"))


def validate_long_split(before: dict, after: list[dict], *, allow_retime: bool = False) -> None:
    if before.get("locked") or not is_long_cue(before):
        raise ValueError("Chỉ tách cue dài chưa khóa.")
    if any(is_long_cue(cue) for cue in after):
        raise ValueError("Cue sau tách vẫn quá dài hoặc chứa nhiều câu: tối đa một câu, 84 ký tự, 6 giây và 2 dòng.")
    if _content(before["text"]) != _content(" ".join(cue["text"] for cue in after)):
        raise ValueError("Tách câu phải giữ đầy đủ từ ngữ và thứ tự của bản dịch, không viết lại hoặc rút gọn.")
    if before.get("source_text") and _content(before["source_text"]) != _content("".join(cue.get("source_text") or "" for cue in after)):
        raise ValueError("Lời gốc sau tách bị thiếu, lặp hoặc thay đổi; chia lại đầy đủ theo các cue.")
    previous_end = 0 if allow_retime else before["start_ms"]
    for cue in after:
        if cue["start_ms"] < previous_end or (not allow_retime and cue["end_ms"] > before["end_ms"]):
            raise ValueError("Các cue sau tách phải theo thứ tự, không chồng nhau và nằm trong cue gốc.")
        previous_end = cue["end_ms"]
