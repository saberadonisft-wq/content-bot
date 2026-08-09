from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

TIMESTAMP_PATTERN = re.compile(
    r"^\s*(?:(?P<hours>\d{1,4}):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{2})(?:[.,](?P<fraction>\d{1,9}))?\s*$"
)
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class SubtitleTimingError(ValueError):
    """Raised when a subtitle timestamp cannot be represented safely."""


def decimal_seconds_to_ms(value: str | float | Decimal) -> int:
    if isinstance(value, bool):
        raise SubtitleTimingError("boolean is not a valid timestamp")
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SubtitleTimingError(f"invalid seconds value: {value!r}") from exc
    if not seconds.is_finite() or seconds < 0:
        raise SubtitleTimingError(
            f"timestamp must be finite and non-negative: {value!r}"
        )
    return int((seconds * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_timestamp_to_ms(value: str | float | Decimal) -> int:
    """Parse HH:MM:SS.mmm, MM:SS.mmm, or decimal seconds into integer ms."""

    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return decimal_seconds_to_ms(value)

    raw = str(value).strip()
    match = TIMESTAMP_PATTERN.fullmatch(raw)
    if not match:
        return decimal_seconds_to_ms(raw.replace(",", "."))

    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    if (match.group("hours") is not None and minutes >= 60) or seconds >= 60:
        raise SubtitleTimingError(f"invalid timestamp component: {value!r}")

    fraction = match.group("fraction") or ""
    fraction_ms = int((fraction + "000")[:3]) if fraction else 0
    if len(fraction) > 3 and int(fraction[3:4]) >= 5:
        fraction_ms += 1

    total_ms = ((hours * 60 + minutes) * 60 + seconds) * 1000 + fraction_ms
    return total_ms


def timestamp_precision_ms(value: str | float | Decimal) -> int:
    raw = str(value).strip()
    if "." not in raw and "," not in raw:
        return 1000
    fraction = re.split(r"[.,]", raw, maxsplit=1)[1]
    digits = len(re.match(r"\d*", fraction).group(0))
    if digits <= 0:
        return 1000
    if digits == 1:
        return 100
    if digits == 2:
        return 10
    return 1


def ms_to_srt_time(total_ms: int) -> str:
    if isinstance(total_ms, bool) or not isinstance(total_ms, int):
        raise SubtitleTimingError("SRT time requires integer milliseconds")
    if total_ms < 0:
        raise SubtitleTimingError("SRT time cannot be negative")
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def ms_to_webvtt_time(total_ms: int) -> str:
    return ms_to_srt_time(total_ms).replace(",", ".")


def ms_to_seconds(total_ms: int) -> float:
    if isinstance(total_ms, bool) or not isinstance(total_ms, int):
        raise SubtitleTimingError("milliseconds must be an integer")
    return total_ms / 1000


def stable_cue_id(
    start_ms: int,
    end_ms: int,
    text: str,
    *,
    requested_id: object | None = None,
    used_ids: set[str] | None = None,
) -> str:
    used = used_ids if used_ids is not None else set()
    candidate = str(requested_id or "").strip()
    if not ID_PATTERN.fullmatch(candidate) or candidate in used:
        digest = hashlib.sha1(f"{start_ms}\0{end_ms}\0{text}".encode()).hexdigest()[:12]
        candidate = f"cue-{digest}"
    base = candidate
    suffix = 2
    while candidate in used:
        candidate = f"{base[:60]}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def cue_times_ms(cue: dict[str, Any]) -> tuple[int, int]:
    if "start_ms" in cue and "end_ms" in cue:
        start_ms = cue["start_ms"]
        end_ms = cue["end_ms"]
        if isinstance(start_ms, bool) or not isinstance(start_ms, int):
            raise SubtitleTimingError("start_ms must be an integer")
        if isinstance(end_ms, bool) or not isinstance(end_ms, int):
            raise SubtitleTimingError("end_ms must be an integer")
        return start_ms, end_ms

    if "start_seconds" in cue and "end_seconds" in cue:
        return (
            decimal_seconds_to_ms(cue["start_seconds"]),
            decimal_seconds_to_ms(cue["end_seconds"]),
        )

    return (
        parse_timestamp_to_ms(str(cue.get("start_time", cue.get("start", "0")))),
        parse_timestamp_to_ms(str(cue.get("end_time", cue.get("end", "0")))),
    )


def cue_to_legacy(cue: dict[str, Any]) -> dict[str, Any]:
    start_ms, end_ms = cue_times_ms(cue)
    legacy = {
        "start_time": ms_to_srt_time(start_ms),
        "end_time": ms_to_srt_time(end_ms),
        "start_seconds": ms_to_seconds(start_ms),
        "end_seconds": ms_to_seconds(end_ms),
        "text": str(cue.get("text", "")),
    }
    if cue.get("secondary_text"):
        legacy["secondary_text"] = str(cue["secondary_text"])
    return legacy


def validate_cues(
    cues: Iterable[dict[str, Any]],
    *,
    media_duration_ms: int | None = None,
    large_gap_ms: int = 15_000,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    overlap_owner_id: str | None = None
    overlap_max_end_ms = -1
    seen_ids: set[str] = set()

    for index, cue in enumerate(cues):
        cue_id = str(cue.get("id", f"index-{index}"))
        try:
            start_ms, end_ms = cue_times_ms(cue)
        except SubtitleTimingError as exc:
            warnings.append(
                {"code": "invalid_timestamp", "cue_id": cue_id, "message": str(exc)}
            )
            continue

        if cue_id in seen_ids:
            warnings.append(
                {
                    "code": "duplicate_id",
                    "cue_id": cue_id,
                    "message": "Cue ID bị trùng.",
                }
            )
        seen_ids.add(cue_id)
        if start_ms < 0 or end_ms <= start_ms:
            warnings.append(
                {
                    "code": "invalid_range",
                    "cue_id": cue_id,
                    "message": "Khoảng thời gian cue không hợp lệ.",
                }
            )
        if media_duration_ms is not None and end_ms > media_duration_ms:
            warnings.append(
                {
                    "code": "after_media_end",
                    "cue_id": cue_id,
                    "message": "Cue kết thúc sau thời lượng media.",
                    "delta_ms": end_ms - media_duration_ms,
                }
            )

        if previous is not None:
            previous_id = str(previous.get("id", "previous"))
            previous_start, previous_end = cue_times_ms(previous)
            if start_ms < previous_start:
                warnings.append(
                    {
                        "code": "out_of_order",
                        "cue_id": cue_id,
                        "message": "Cue chưa được sắp theo thời gian.",
                    }
                )
            if start_ms < overlap_max_end_ms:
                warnings.append(
                    {
                        "code": "overlap",
                        "cue_id": cue_id,
                        "related_cue_id": overlap_owner_id or previous_id,
                        "message": "Hai cue đang chồng lấn; hệ thống không tự dịch timing.",
                        "delta_ms": overlap_max_end_ms - start_ms,
                    }
                )
            elif start_ms - previous_end > large_gap_ms:
                warnings.append(
                    {
                        "code": "large_gap",
                        "cue_id": cue_id,
                        "related_cue_id": previous_id,
                        "message": "Khoảng trống giữa hai cue lớn hơn ngưỡng kiểm tra.",
                        "delta_ms": start_ms - previous_end,
                    }
                )
        if end_ms > overlap_max_end_ms:
            overlap_max_end_ms = end_ms
            overlap_owner_id = cue_id
        previous = cue
    return warnings


def transform_project_cues(
    cues: Iterable[dict[str, Any]],
    *,
    trim_start_ms: int = 0,
    trim_end_ms: int | None = None,
    video_speed: str | float | Decimal = Decimal(1),
) -> list[dict[str, Any]]:
    if trim_start_ms < 0:
        raise SubtitleTimingError("trim_start_ms cannot be negative")
    if trim_end_ms is not None and trim_end_ms <= trim_start_ms:
        raise SubtitleTimingError("trim_end_ms must be greater than trim_start_ms")
    try:
        speed = Decimal(str(video_speed))
    except InvalidOperation as exc:
        raise SubtitleTimingError("video_speed is invalid") from exc
    if not speed.is_finite() or speed <= 0:
        raise SubtitleTimingError("video_speed must be finite and greater than zero")

    def project_ms(source_ms: int) -> int:
        return int(
            (Decimal(source_ms - trim_start_ms) / speed).quantize(
                Decimal(1),
                rounding=ROUND_HALF_UP,
            )
        )

    transformed: list[dict[str, Any]] = []
    for cue in cues:
        start_ms, end_ms = cue_times_ms(cue)
        clip_end = trim_end_ms if trim_end_ms is not None else end_ms
        if end_ms <= trim_start_ms or start_ms >= clip_end:
            continue
        source_start = max(start_ms, trim_start_ms)
        source_end = min(end_ms, clip_end)
        next_start = project_ms(source_start)
        next_end = project_ms(source_end)
        if next_end <= next_start:
            continue

        item = dict(cue)
        item["source_start_ms"] = source_start
        item["source_end_ms"] = source_end
        item["start_ms"] = next_start
        item["end_ms"] = next_end

        speech_start = cue.get("speech_start_ms")
        speech_end = cue.get("speech_end_ms")
        if (
            isinstance(speech_start, int)
            and not isinstance(speech_start, bool)
            and isinstance(speech_end, int)
            and not isinstance(speech_end, bool)
        ):
            clipped_speech_start = max(speech_start, source_start)
            clipped_speech_end = min(speech_end, source_end)
            if clipped_speech_end > clipped_speech_start:
                projected_speech_start = project_ms(clipped_speech_start)
                projected_speech_end = project_ms(clipped_speech_end)
                if projected_speech_end > projected_speech_start:
                    item["speech_start_ms"] = projected_speech_start
                    item["speech_end_ms"] = projected_speech_end
                else:
                    item.pop("speech_start_ms", None)
                    item.pop("speech_end_ms", None)
            else:
                item.pop("speech_start_ms", None)
                item.pop("speech_end_ms", None)

        words: list[dict[str, Any]] = []
        for word in cue.get("words") or []:
            word_start, word_end = cue_times_ms(word)
            clipped_start = max(word_start, source_start)
            clipped_end = min(word_end, source_end)
            if clipped_end <= clipped_start:
                continue
            mapped_word = dict(word)
            mapped_word["start_ms"] = project_ms(clipped_start)
            mapped_word["end_ms"] = project_ms(clipped_end)
            words.append(mapped_word)
        if cue.get("words") is not None:
            item["words"] = words
        transformed.append(item)
    return transformed
