from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

import imageio_ffmpeg

from .media_probe import probe_media
from .speech_evidence import valid_speech_evidence
from .subtitle_timing import (
    SubtitleTimingError,
    cue_times_ms,
    cue_to_legacy,
    decimal_seconds_to_ms,
    ms_to_seconds,
    ms_to_srt_time,
    parse_timestamp_to_ms,
    stable_cue_id,
    timestamp_precision_ms,
    validate_cues,
)

BURN_PROGRESS: dict[str, float] = {}

# CSS font-size and ASS Fontsize do not describe the same glyph box. With the
# bundled Arimo font, libass needs a 1.5x nominal Fontsize to match the browser
# preview. Geometric ASS values (outline, shadow and spacing) must not use this
# correction because they already map directly to the design canvas.
SUBTITLE_DESIGN_HEIGHT = 720
ASS_FONT_METRIC_CORRECTION = 1.5

ALLOWED_TIMING_SOURCES = frozenset(
    {
        "manual",
        "gemini_estimate",
        "asr_word",
        "forced_alignment",
        "imported_srt",
        "imported_vtt",
        "ocr",
        "asr",
    }
)
LANGUAGE_PATTERN = re.compile(r"^[A-Za-z0-9-]{2,32}$")
MAX_SUBTITLE_CUES = 20_000
MAX_CUE_TEXT_LENGTH = 4000
MAX_WORDS_PER_CUE = 500
MAX_WORD_TEXT_LENGTH = 500

TIMESTAMP_TOKEN = r"\d{1,4}:\d{2}(?::\d{2})?(?:[.,]\d+)?"
INLINE_PATTERN = re.compile(
    rf"^\[?\s*({TIMESTAMP_TOKEN})\s*(?:-->|[-–—>])\s*({TIMESTAMP_TOKEN})\s*\]?:?\s*(.*)$"
)


def parse_timestamp_to_seconds(ts_str: str) -> float:
    return ms_to_seconds(parse_timestamp_to_ms(ts_str))


def seconds_to_srt_time(seconds: float) -> str:
    return ms_to_srt_time(decimal_seconds_to_ms(seconds))


def _strip_json_fence(text: str) -> str:
    match = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE
    )
    return match.group(1).strip() if match else text


def _safe_confidence(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return confidence if 0.0 <= confidence <= 1.0 else None


def _normalize_timing_source(value: object, *, fallback: str) -> str:
    candidate = str(value or "").strip()
    if candidate in ALLOWED_TIMING_SOURCES:
        return candidate
    return fallback if fallback in ALLOWED_TIMING_SOURCES else "gemini_estimate"


def _normalize_precision(value: object, *, fallback: int) -> int:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= 60_000
    ):
        return value
    return min(60_000, max(1, fallback))


def _normalize_revision(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    return 0


def _bounded_text(value: object, *, max_length: int) -> tuple[str, bool]:
    text = "" if value is None else str(value).strip()
    if len(text) <= max_length:
        return text, False
    return text[:max_length].rstrip(), True


def _parse_json_word(
    raw_word: object,
    *,
    cue_id: str,
    index: int,
    used_ids: set[str],
) -> dict[str, Any] | None:
    if not isinstance(raw_word, dict):
        return None
    text, _truncated = _bounded_text(
        raw_word.get("text", ""), max_length=MAX_WORD_TEXT_LENGTH
    )
    if not text:
        return None
    try:
        start_ms, end_ms = cue_times_ms(raw_word)
    except SubtitleTimingError:
        return None
    if start_ms < 0 or end_ms <= start_ms:
        return None
    return {
        "id": stable_cue_id(
            start_ms,
            end_ms,
            text,
            requested_id=raw_word.get("id") or f"{cue_id}-w{index + 1}",
            used_ids=used_ids,
        ),
        "text": text,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "confidence": _safe_confidence(raw_word.get("confidence")),
        **({"alignment_method": raw_word["alignment_method"]} if raw_word.get("alignment_method") in {
            "asr_observed", "ctc_aligned", "energy_estimated", "interpolated", "manual"} else {}),
    }


def _build_cue(
    *,
    start_ms: int,
    end_ms: int,
    text: str,
    requested_id: object | None,
    used_ids: set[str],
    timing_source: str,
    timing_precision_ms: int,
    confidence: float | None = None,
    speech_start_ms: object | None = None,
    speech_end_ms: object | None = None,
    secondary_text: object | None = None,
    words: object | None = None,
    revision: int = 0,
    needs_review: bool | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cue_id = stable_cue_id(
        start_ms,
        end_ms,
        text,
        requested_id=requested_id,
        used_ids=used_ids,
    )
    word_ids: set[str] = set()
    raw_words = words if isinstance(words, list) else []
    if len(raw_words) > MAX_WORDS_PER_CUE and warnings is not None:
        warnings.append(
            {
                "code": "too_many_words",
                "cue_id": cue_id,
                "message": "Cue có quá 500 word timing; chỉ giữ 500 mục đầu.",
            }
        )
    parsed_words: list[dict[str, Any]] = []
    valid_speech = (type(speech_start_ms) is int and type(speech_end_ms) is int
                    and 0 <= speech_start_ms < speech_end_ms)
    for index, raw_word in enumerate(raw_words[:MAX_WORDS_PER_CUE]):
        word = _parse_json_word(
            raw_word,
            cue_id=cue_id,
            index=index,
            used_ids=word_ids,
        )
        if word is None:
            if warnings is not None:
                warnings.append(
                    {
                        "code": "invalid_word",
                        "cue_id": cue_id,
                        "message": f"Word timing {index + 1} không hợp lệ và đã bị bỏ qua.",
                    }
                )
            continue
        in_display = start_ms <= word["start_ms"] < word["end_ms"] <= end_ms
        in_speech = valid_speech and speech_start_ms <= word["start_ms"] < word["end_ms"] <= speech_end_ms
        if not in_display and not in_speech:
            if warnings is not None:
                warnings.append(
                    {
                        "code": "word_outside_cue",
                        "cue_id": cue_id,
                        "message": f"Word timing {index + 1} nằm ngoài cue và đã bị bỏ qua.",
                    }
                )
            continue
        if (
            isinstance(raw_word, dict)
            and len(str(raw_word.get("text", "")).strip()) > MAX_WORD_TEXT_LENGTH
            and warnings is not None
        ):
            warnings.append(
                {
                    "code": "word_text_truncated",
                    "cue_id": cue_id,
                    "message": f"Nội dung word timing {index + 1} đã được giới hạn còn 500 ký tự.",
                }
            )
        parsed_words.append(word)

    secondary, secondary_truncated = _bounded_text(
        secondary_text,
        max_length=MAX_CUE_TEXT_LENGTH,
    )
    if secondary_truncated and warnings is not None:
        warnings.append(
            {
                "code": "secondary_text_truncated",
                "cue_id": cue_id,
                "message": "Phụ đề phụ đã được giới hạn còn 4.000 ký tự.",
            }
        )
    cue = {
        "id": cue_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": text,
        "timing_source": timing_source,
        "timing_precision_ms": timing_precision_ms,
        "confidence": confidence,
        "needs_review": (
            bool(needs_review)
            if needs_review is not None
            else confidence is not None and confidence < 0.65
        ),
        "revision": _normalize_revision(revision),
    }
    if secondary:
        cue["secondary_text"] = secondary
    if (
        isinstance(speech_start_ms, int)
        and not isinstance(speech_start_ms, bool)
        and isinstance(speech_end_ms, int)
        and not isinstance(speech_end_ms, bool)
        and 0 <= speech_start_ms < speech_end_ms
    ):
        cue["speech_start_ms"] = speech_start_ms
        cue["speech_end_ms"] = speech_end_ms
    elif (
        speech_start_ms is not None or speech_end_ms is not None
    ) and warnings is not None:
        warnings.append(
            {
                "code": "invalid_speech_range",
                "cue_id": cue_id,
                "message": "Speech timing không hợp lệ; đã bỏ qua.",
            }
        )
    if parsed_words:
        cue["words"] = parsed_words
    return cue


def parse_subtitles_v2(
    raw_text: str,
    *,
    media_duration_ms: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    text = _strip_json_fence(raw_text.strip())
    warnings: list[dict[str, Any]] = []
    cues: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    language = "vi"
    document_source = "gemini_estimate"
    document_precision = 1000

    if not text:
        document = {
            "schema_version": 2,
            "language": language,
            "timebase": "milliseconds",
            "timing_source": document_source,
            "timing_precision_ms": document_precision,
            "segments": [],
        }
        return document, warnings

    data: object | None = None
    looks_like_json = text.startswith("{") or bool(re.match(r"^\[\s*(?:\{|\])", text))
    if looks_like_json:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            warnings.append(
                {
                    "code": "invalid_json",
                    "message": f"JSON không hợp lệ tại dòng {exc.lineno}, cột {exc.colno}; thử parser văn bản.",
                }
            )

    if data is not None:
        if isinstance(data, dict):
            raw_segments = data.get("segments", data.get("subtitles", []))
            raw_language = str(data.get("language", "vi") or "").strip()
            language = raw_language if LANGUAGE_PATTERN.fullmatch(raw_language) else "vi"
            if language != raw_language:
                warnings.append(
                    {
                        "code": "invalid_language",
                        "message": "Mã ngôn ngữ không hợp lệ; đã dùng giá trị mặc định 'vi'.",
                    }
                )
            raw_document_source = data.get("timing_source", "gemini_estimate")
            document_source = _normalize_timing_source(
                raw_document_source,
                fallback="gemini_estimate",
            )
            if document_source != str(raw_document_source or "").strip():
                warnings.append(
                    {
                        "code": "invalid_timing_source",
                        "message": "Nguồn timing không hợp lệ; đã dùng 'gemini_estimate'.",
                    }
                )
            raw_precision = data.get("timing_precision_ms", 1000)
            document_precision = _normalize_precision(raw_precision, fallback=1000)
            if document_precision != raw_precision:
                warnings.append(
                    {
                        "code": "invalid_timing_precision",
                        "message": "Độ chính xác timing không hợp lệ; đã dùng 1.000 ms.",
                    }
                )
        else:
            raw_segments = data

        if not isinstance(raw_segments, list):
            warnings.append(
                {
                    "code": "invalid_segments",
                    "message": "Trường segments phải là một mảng.",
                }
            )
            raw_segments = []

        if len(raw_segments) > MAX_SUBTITLE_CUES:
            warnings.append(
                {
                    "code": "too_many_segments",
                    "message": f"Kết quả vượt giới hạn {MAX_SUBTITLE_CUES} segment; chỉ giữ trong giới hạn tài nguyên.",
                }
            )

        for index, item in enumerate(raw_segments[:MAX_SUBTITLE_CUES]):
            if not isinstance(item, dict):
                warnings.append(
                    {
                        "code": "invalid_segment",
                        "message": f"Segment {index + 1} không phải object.",
                    }
                )
                continue
            content, content_truncated = _bounded_text(
                item.get("text", item.get("content", "")),
                max_length=MAX_CUE_TEXT_LENGTH,
            )
            if not content:
                warnings.append(
                    {
                        "code": "empty_text",
                        "message": f"Segment {index + 1} không có nội dung.",
                    }
                )
                continue
            if content_truncated:
                warnings.append(
                    {
                        "code": "cue_text_truncated",
                        "message": f"Segment {index + 1} đã được giới hạn còn 4.000 ký tự.",
                    }
                )
            try:
                if "start_ms" in item or "end_ms" in item:
                    start_raw = item.get("start_ms")
                    end_raw = item.get("end_ms")
                    if isinstance(start_raw, bool) or not isinstance(start_raw, int):
                        raise SubtitleTimingError("start_ms must be an integer")
                    if isinstance(end_raw, bool) or not isinstance(end_raw, int):
                        raise SubtitleTimingError("end_ms must be an integer")
                    start_ms, end_ms = start_raw, end_raw
                    inferred_precision = 1
                else:
                    start_raw = item.get("start", item.get("start_time", "0"))
                    end_raw = item.get("end", item.get("end_time", "0"))
                    start_ms = parse_timestamp_to_ms(str(start_raw))
                    end_ms = parse_timestamp_to_ms(str(end_raw))
                    inferred_precision = max(
                        timestamp_precision_ms(str(start_raw)),
                        timestamp_precision_ms(str(end_raw)),
                    )
            except SubtitleTimingError as exc:
                warnings.append(
                    {
                        "code": "invalid_timestamp",
                        "message": str(exc),
                        "cue_id": f"segment-{index + 1}",
                    }
                )
                continue
            if start_ms < 0 or end_ms <= start_ms:
                warnings.append(
                    {
                        "code": "invalid_range",
                        "message": "Segment có end_ms không lớn hơn start_ms.",
                        "cue_id": f"segment-{index + 1}",
                    }
                )
                continue

            requested_precision = item.get(
                "timing_precision_ms", document_precision or inferred_precision
            )
            precision = _normalize_precision(
                requested_precision,
                fallback=inferred_precision,
            )
            raw_source = item.get("timing_source", document_source)
            timing_source = _normalize_timing_source(
                raw_source,
                fallback=document_source,
            )
            if timing_source != str(raw_source or "").strip():
                warnings.append(
                    {
                        "code": "invalid_timing_source",
                        "cue_id": f"segment-{index + 1}",
                        "message": "Nguồn timing của segment không hợp lệ; đã dùng nguồn của tài liệu.",
                    }
                )
            if precision != requested_precision:
                warnings.append(
                    {
                        "code": "invalid_timing_precision",
                        "cue_id": f"segment-{index + 1}",
                        "message": "Độ chính xác timing của segment không hợp lệ; đã dùng giá trị suy luận.",
                    }
                )
            cue = _build_cue(
                start_ms=start_ms,
                end_ms=end_ms,
                text=content,
                requested_id=item.get("id"),
                used_ids=used_ids,
                timing_source=timing_source,
                timing_precision_ms=precision,
                confidence=_safe_confidence(item.get("confidence")),
                speech_start_ms=item.get("speech_start_ms"),
                speech_end_ms=item.get("speech_end_ms"),
                secondary_text=item.get("secondary_text"),
                words=item.get("words"),
                revision=_normalize_revision(item.get("revision", 0)),
                needs_review=item.get("needs_review")
                if isinstance(item.get("needs_review"), bool)
                else None,
                warnings=warnings,
            )
            for field, limit in (("source_text", 4000), ("source_language", 32), ("origin_chunk_id", 64), ("origin_model", 128)):
                if isinstance(item.get(field), str):
                    cue[field] = item[field][:limit]
            if item.get("content_source") in {"audio", "screen", "mixed", "unknown"}:
                cue["content_source"] = item["content_source"]
            if isinstance(item.get("locked"), bool):
                cue["locked"] = item["locked"]
            if media_duration_ms is not None and cue.get("speech_end_ms", 0) > media_duration_ms:
                cue.pop("speech_start_ms", None)
                cue.pop("speech_end_ms", None)
                cue.pop("words", None)
                warnings.append({"code": "speech_outside_video", "cue_id": cue["id"],
                                 "message": "Mốc lời nói vượt video; cần căn lại."})
            evidence = valid_speech_evidence({**cue, "speech_evidence": item.get("speech_evidence")})
            if evidence:
                cue["speech_evidence"] = evidence.model_dump()
            cues.append(cue)

    if data is None:
        lines = [line.strip() for line in text.splitlines()]
        index = 0
        while index < len(lines):
            line = lines[index]
            if not line or line.isdigit() or line.upper() == "WEBVTT":
                index += 1
                continue

            start_raw: str | None = None
            end_raw: str | None = None
            content = ""
            source = "gemini_estimate"

            if "-->" in line:
                match = re.match(
                    rf"^\[?\s*({TIMESTAMP_TOKEN})\s*-->\s*({TIMESTAMP_TOKEN})\s*\]?:?\s*(.*)$",
                    line,
                )
                if match:
                    start_raw, end_raw, content = match.groups()
                    source = "imported_srt" if "," in line else "imported_vtt"
                    if not content.strip():
                        text_lines: list[str] = []
                        cursor = index + 1
                        while cursor < len(lines):
                            candidate = lines[cursor]
                            if not candidate:
                                if text_lines:
                                    break
                                cursor += 1
                                continue
                            if (
                                candidate.isdigit()
                                or "-->" in candidate
                                or INLINE_PATTERN.match(candidate)
                            ):
                                break
                            text_lines.append(candidate)
                            cursor += 1
                        content = "\n".join(text_lines)
                        index = cursor - 1
            else:
                match = INLINE_PATTERN.match(line)
                if match:
                    start_raw, end_raw, content = match.groups()
                    if (
                        not content.strip()
                        and index + 1 < len(lines)
                        and not INLINE_PATTERN.match(lines[index + 1])
                    ):
                        index += 1
                        content = lines[index]

            if start_raw is not None and end_raw is not None:
                content = re.sub(r"^[:\-|\s]+", "", content).strip()
                try:
                    start_ms = parse_timestamp_to_ms(start_raw)
                    end_ms = parse_timestamp_to_ms(end_raw)
                except SubtitleTimingError as exc:
                    warnings.append({"code": "invalid_timestamp", "message": str(exc)})
                    index += 1
                    continue
                if content and end_ms > start_ms:
                    precision = max(
                        timestamp_precision_ms(start_raw),
                        timestamp_precision_ms(end_raw),
                    )
                    cues.append(
                        _build_cue(
                            start_ms=start_ms,
                            end_ms=end_ms,
                            text=content,
                            requested_id=None,
                            used_ids=used_ids,
                            timing_source=source,
                            timing_precision_ms=precision,
                        )
                    )
                elif content:
                    warnings.append(
                        {
                            "code": "invalid_range",
                            "message": "Cue có end_ms không lớn hơn start_ms.",
                        }
                    )
            index += 1

    original_order = [cue["id"] for cue in cues]
    cues.sort(key=lambda cue: (cue["start_ms"], cue["end_ms"], cue["id"]))
    if original_order != [cue["id"] for cue in cues]:
        warnings.append(
            {
                "code": "out_of_order",
                "message": "Các cue đã được sắp lại theo start_ms/end_ms/id.",
            }
        )
    warnings.extend(validate_cues(cues, media_duration_ms=media_duration_ms))
    if cues:
        document_precision = max(cue["timing_precision_ms"] for cue in cues)
        sources = {cue["timing_source"] for cue in cues}
        if len(sources) == 1:
            document_source = next(iter(sources))

    document = {
        "schema_version": 2,
        "language": language,
        "timebase": "milliseconds",
        "timing_source": document_source,
        "timing_precision_ms": document_precision,
        "segments": cues,
    }
    if isinstance(data, dict):
        document["revision"] = _normalize_revision(data.get("revision", 0))
        if isinstance(data.get("run_id"), str):
            document["run_id"] = data["run_id"][:64]
    return document, warnings


def parse_subtitles_text(raw_text: str) -> list[dict[str, Any]]:
    document, _warnings = parse_subtitles_v2(raw_text)
    return [cue_to_legacy(cue) for cue in document["segments"]]


def subtitles_to_srt(subtitles: list[dict[str, Any]], *, use_source: bool = False) -> str:
    blocks = []
    for idx, sub in enumerate(subtitles, 1):
        start_ms, end_ms = cue_times_ms(sub)
        s_time = ms_to_srt_time(start_ms)
        e_time = ms_to_srt_time(end_ms)
        text = str(sub.get("source_text") if use_source and sub.get("source_text") else sub.get("text", ""))
        blocks.append(f"{idx}\n{s_time} --> {e_time}\n{text}\n")
    return "\n".join(blocks)


def _ass_animation_tag(
    animation: str,
    alignment_type: str,
    position: str,
    pos_x: float,
    pos_y: float,
    *,
    play_res_x: int = 1280,
    play_res_y: int = SUBTITLE_DESIGN_HEIGHT,
) -> str:
    if animation == "fade":
        return r"{\fad(450,450)}"
    if animation == "typewriter":
        # ASS has no typewriter primitive; a short fade is the closest renderer-safe fallback.
        return r"{\fad(120,0)}"

    anchors = {
        "left": round(play_res_x * 0.0625),
        "center": round(play_res_x * 0.5),
        "right": round(play_res_x * 0.9375),
    }
    x = (
        int(pos_x / 100 * play_res_x)
        if position == "custom"
        else anchors.get(alignment_type, round(play_res_x * 0.5))
    )
    y = (
        int(pos_y / 100 * play_res_y)
        if position == "custom"
        else {
            "top": round(play_res_y / 9),
            "middle": round(play_res_y * 0.5),
            "bottom": round(play_res_y * 8 / 9),
        }.get(position, round(play_res_y * 8 / 9))
    )
    if animation == "rise":
        return f"{{\\move({x},{y + round(play_res_y / 9)},{x},{y},0,450)}}"
    if animation == "pan":
        return f"{{\\move({x - round(play_res_x * 0.09375)},{y},{x},{y},0,450)}}"
    return ""


def _ass_time(seconds: float) -> str:
    total_centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(total_centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def css_font_size_to_ass(font_size: float, play_res_y: int) -> float:
    """Convert the 720p CSS design font size to libass' nominal Fontsize."""
    return round(
        float(font_size)
        * play_res_y
        / SUBTITLE_DESIGN_HEIGHT
        * ASS_FONT_METRIC_CORRECTION,
        2,
    )


def subtitles_to_ass(
    subtitles: list[dict[str, Any]],
    options: dict[str, Any],
    *,
    play_res_x: int = 1280,
    play_res_y: int = SUBTITLE_DESIGN_HEIGHT,
) -> str:
    """Build the canonical ASS track used by both browser preview and export.

    All authoring sizes are 720p-relative design units. ASS PlayRes performs one
    uniform scale at render time, so fonts, borders, shadows and positions keep
    the same visual percentage at every preview and output resolution.
    """
    play_res_x = max(1, int(play_res_x))
    play_res_y = max(1, int(play_res_y))
    design_scale = play_res_y / SUBTITLE_DESIGN_HEIGHT
    font_name = (
        re.sub(r"[^A-Za-z0-9 ._-]", "", str(options.get("font_name", "Arial")))
        or "Arial"
    )
    design_font_size = int(options.get("font_size", 24))
    ass_font_size = css_font_size_to_ass(design_font_size, play_res_y)
    font_color = hex_to_ass_color(str(options.get("font_color", "#FFFFFF")))
    outline_color = hex_to_ass_color(str(options.get("outline_color", "#000000")))
    shadow_color = hex_to_ass_color(str(options.get("shadow_color", "#000000")))
    bg_opacity = float(options.get("bg_opacity", 0.75))
    bg_color = hex_to_ass_color(
        str(options.get("bg_color", "#000000")), opacity=bg_opacity
    )
    bold = -1 if options.get("bold", False) else 0
    italic = -1 if options.get("italic", False) else 0
    underline = -1 if options.get("underline", False) else 0
    strikethrough = -1 if options.get("strikethrough", False) else 0
    border_style = 3 if options.get("bg_enabled", False) else 1
    back_color = bg_color if options.get("bg_enabled", False) else shadow_color
    position = str(options.get("position", "bottom"))
    alignment_type = str(options.get("alignment_type", "center"))
    vertical_position = (
        position if position in {"top", "middle", "bottom"} else "middle"
    )
    alignment = {
        "top": {"left": 7, "center": 8, "right": 9},
        "middle": {"left": 4, "center": 5, "right": 6},
        "bottom": {"left": 1, "center": 2, "right": 3},
    }[vertical_position].get(alignment_type, 2)
    line_spacing = max(0.8, min(3.0, float(options.get("line_spacing", 1.2))))
    # Event positions use browser/design pixels, not ASS's corrected nominal
    # font size, so multiline anchors stay identical to the live preview.
    line_height = design_font_size * line_spacing * design_scale
    if position == "custom":
        base_x = int(float(options.get("pos_x", 50.0)) / 100 * play_res_x)
        base_y = int(float(options.get("pos_y", 50.0)) / 100 * play_res_y)
    else:
        base_x = {
            "left": round(play_res_x * 0.0625),
            "center": round(play_res_x * 0.5),
            "right": round(play_res_x * 0.9375),
        }.get(alignment_type, round(play_res_x * 0.5))
        base_y = {
            "top": round(play_res_y / 9),
            "middle": round(play_res_y * 0.5),
            "bottom": round(play_res_y * 8 / 9),
        }[position]

    spacing = round(float(options.get("spacing", 0)) * design_scale, 2)
    outline_width = round(float(options.get("outline_width", 2)) * design_scale, 2)
    shadow_width = round(float(options.get("shadow_width", 0)) * design_scale, 2)
    margin = max(1, round(20 * design_scale))

    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {play_res_x}",
            f"PlayResY: {play_res_y}",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Default,{font_name},{ass_font_size},{font_color},{font_color},{outline_color},{back_color},{bold},{italic},{underline},{strikethrough},100,100,{spacing},0,{border_style},{outline_width},{shadow_width},{alignment},{margin},{margin},{margin},1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )
    events: list[str] = []
    animation = str(options.get("animation", "none"))
    for subtitle in subtitles:
        layout = subtitle.get("layout")
        cue_x = int(float(layout["x"]) / 100 * play_res_x) if layout else base_x
        cue_y = int(float(layout["y"]) / 100 * play_res_y) if layout else base_y
        cue_vertical = "middle" if layout else vertical_position
        cue_position = "custom" if layout else position
        cue_alignment = {"left": 4, "center": 5, "right": 6}.get(alignment_type, 5) if layout else alignment
        start_ms, end_ms = cue_times_ms(subtitle)
        start = start_ms / 1000
        end = end_ms / 1000
        primary_lines = [
            (line.strip(), False)
            for line in str(subtitle.get("text", "")).replace("\r\n", "\n").splitlines()
            if line.strip()
        ]
        # secondary_text is retained as source-language metadata for alignment,
        # but only the Vietnamese display text is rendered/exported.
        text_lines = primary_lines
        if not text_lines:
            continue
        for index, line in enumerate(text_lines):
            if cue_vertical == "top":
                y = cue_y + index * line_height
            elif cue_vertical == "bottom":
                y = cue_y - (len(text_lines) - 1 - index) * line_height
            else:
                y = cue_y + (index - (len(text_lines) - 1) / 2) * line_height
            animation_tag = _ass_animation_tag(
                animation,
                alignment_type,
                cue_position,
                cue_x / play_res_x * 100,
                y / play_res_y * 100,
                play_res_x=play_res_x,
                play_res_y=play_res_y,
            )
            animation_inner = animation_tag[1:-1] if animation_tag else ""
            override = (
                f"{{{animation_inner}}}"
                if animation in {"rise", "pan"} and animation_inner
                else f"{{\\pos({cue_x},{round(y)}){animation_inner}}}"
            )
            line, _is_secondary = line
            formatting_override = f"{{\\an{cue_alignment}}}" if layout else ""
            safe_line = (
                line.replace("\\", r"\\")
                .replace("{", "\\{")
                .replace("}", "\\}")
            )
            events.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,"
                f"{override}{formatting_override}{safe_line}"
            )
    return header + ("\n" + "\n".join(events) if events else "") + "\n"


def hex_to_ass_color(hex_color: str, opacity: float = 1.0) -> str:
    """Convert #RRGGBB or #RGB to ASS format &HAABBGGRR where AA is transparency (00=opaque, FF=transparent)."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    alpha = max(0, min(255, round((1.0 - opacity) * 255)))
    if len(hex_color) == 6:
        r, g, b = hex_color[0:2], hex_color[2:4], hex_color[4:6]
        return f"&H{alpha:02X}{b}{g}{r}"
    return f"&H{alpha:02X}FFFFFF"


def burn_subtitles_to_video(
    video_path: Path,
    subtitles: list[dict[str, Any]],
    options: dict[str, Any],
    output_path: Path,
    video_id: str | None = None,
) -> Path:
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    # Uppercase transformation if requested
    processed_subtitles = []
    use_uppercase = options.get("uppercase", False)
    for sub in subtitles:
        item = dict(sub)
        if use_uppercase:
            item["text"] = item["text"].upper()
        processed_subtitles.append(item)

    # Video editing options
    video_speed = options.get("video_speed", 1.0)
    volume = options.get("volume", 1.0)
    fade_in = options.get("fade_in", 0.0)
    fade_out = options.get("fade_out", 0.0)
    aspect_ratio = options.get("aspect_ratio", "16:9")
    bg_fill_type = options.get("bg_fill_type", "blur")
    background_color = str(options.get("bg_color", "#000000")).lstrip("#")
    pad_color = f"0x{background_color}"
    trim_start = options.get("trim_start", 0.0)
    trim_end = options.get("trim_end", None)

    ass_content = subtitles_to_ass(processed_subtitles, options)
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # Convert Windows path for FFmpeg subtitles filter: C:\path\sub.ass -> C\:/path/sub.ass
    temp_srt_path = output_path.parent / f"temp_{uuid.uuid4().hex[:8]}.ass"
    temp_srt_path.write_text(ass_content, encoding="utf-8")
    escaped_srt = str(temp_srt_path.resolve()).replace("\\", "/").replace(":", "\\:")
    sub_filter = f"subtitles='{escaped_srt}'"

    # Input trimming options
    input_args = []
    if trim_start > 0:
        input_args.extend(["-ss", str(trim_start)])
    if trim_end is not None and trim_end > trim_start:
        input_args.extend(["-to", str(trim_end)])

    # Construct Filter Graph
    video_filters = []
    if aspect_ratio == "9:16":
        if bg_fill_type == "blur":
            video_filters.append(
                "split[v1][v2];[v1]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=30[bg];[v2]scale=1080:1920:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2"
            )
        else:
            video_filters.append(
                f"scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(1080-iw)/2:(1920-ih)/2:color={pad_color}"
            )
    elif aspect_ratio == "1:1":
        video_filters.append(
            f"scale=1080:1080:force_original_aspect_ratio=decrease,pad=1080:1080:(1080-iw)/2:(1080-ih)/2:color={pad_color}"
        )

    if video_speed != 1.0:
        video_filters.append(f"setpts=(1/{video_speed})*PTS")

    video_filters.append(sub_filter)

    audio_filters = []
    if volume != 1.0:
        audio_filters.append(f"volume={volume}")
    if fade_in > 0:
        audio_filters.append(f"afade=t=in:ss=0:d={fade_in}")
    if fade_out > 0:
        media_duration = probe_media(video_path)["duration_ms"] / 1000
        source_end = min(float(trim_end or media_duration), media_duration)
        output_duration = max(0.0, (source_end - float(trim_start)) / video_speed)
        fade_start = max(0.0, output_duration - float(fade_out))
        audio_filters.append(f"afade=t=out:st={fade_start:.3f}:d={float(fade_out):.3f}")
    if video_speed != 1.0:
        audio_filters.append(f"atempo={video_speed}")

    vf_param = ",".join(video_filters)
    af_param = ",".join(audio_filters) if audio_filters else "anull"

    cmd = [
        ffmpeg_exe,
        "-y",
        *input_args,
        "-i",
        str(video_path.resolve()),
        "-vf",
        vf_param,
        "-af",
        af_param,
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-preset",
        "ultrafast",
        str(output_path.resolve()),
    ]

    if video_id:
        BURN_PROGRESS[video_id] = 0.0

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        duration_secs = 0.0
        duration_pattern = re.compile(rb"Duration: (\d{2}):(\d{2}):(\d{2}\.\d+)")
        time_pattern = re.compile(rb"time=(\d{2}):(\d{2}):(\d{2}\.\d+)")

        buffer = b""
        while True:
            chunk = process.stderr.read(1024)
            if not chunk and process.poll() is not None:
                break
            buffer += chunk

            while b"\r" in buffer or b"\n" in buffer:
                if b"\r" in buffer and b"\n" in buffer:
                    first_sep = (
                        b"\r" if buffer.find(b"\r") < buffer.find(b"\n") else b"\n"
                    )
                elif b"\r" in buffer:
                    first_sep = b"\r"
                else:
                    first_sep = b"\n"

                line, buffer = buffer.split(first_sep, 1)

                if duration_secs == 0.0:
                    match = duration_pattern.search(line)
                    if match:
                        h, m, s = match.groups()
                        duration_secs = float(h) * 3600 + float(m) * 60 + float(s)

                match = time_pattern.search(line)
                if match and duration_secs > 0:
                    h, m, s = match.groups()
                    current_secs = float(h) * 3600 + float(m) * 60 + float(s)
                    progress = min(
                        100.0, max(0.0, (current_secs / duration_secs) * 100)
                    )
                    if video_id:
                        BURN_PROGRESS[video_id] = round(progress, 2)

        process.wait()
        if process.returncode != 0:
            err = process.stderr.read() if process.stderr else b""
            raise RuntimeError(f"FFmpeg error: {err.decode('utf-8', errors='ignore')}")

        if video_id:
            BURN_PROGRESS[video_id] = 100.0
    finally:
        if temp_srt_path.exists():
            temp_srt_path.unlink()

    return output_path
