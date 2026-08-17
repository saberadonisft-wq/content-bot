from __future__ import annotations

import gc
import hashlib
import importlib.util
import json
import math
import re
import subprocess
import sys
import threading
import time
import unicodedata
from array import array
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import imageio_ffmpeg

ALIGNMENT_CACHE_VERSION = 1
ALIGNMENT_ALGORITHM_VERSION = "2026-08-alignment-10ms-v2"
PCM_SAMPLE_RATE = 16_000
ENERGY_FRAME_MS = 10
ALIGNMENT_PRECISION_MS = 10
_TOKEN_RE = re.compile(r"[^\W_]+(?:[’'][^\W_]+)*", re.UNICODE)

AlignmentEngine = Literal["auto", "energy", "faster_whisper"]
ProgressCallback = Callable[[int, str, str], None]


class SubtitleAlignmentError(RuntimeError):
    pass


class SubtitleAlignmentCanceled(SubtitleAlignmentError):
    pass


@dataclass(frozen=True)
class AudioWindow:
    start_ms: int
    end_ms: int
    cue_ids: tuple[str, ...]


@dataclass(frozen=True)
class ObservedWord:
    text: str
    start_ms: int
    end_ms: int
    confidence: float


@dataclass(frozen=True)
class TranscriptToken:
    cue_id: str
    index: int
    text: str
    normalized: str


@dataclass(frozen=True)
class AlignmentSettings:
    engine: AlignmentEngine = "auto"
    lead_in_ms: int = 60
    tail_ms: int = 100
    window_padding_ms: int = 650
    max_window_ms: int = 30_000
    force_manual: bool = False
    whisper_model: str = "small"
    whisper_device: str = "auto"
    whisper_compute_type: str = "auto"
    whisper_model_dir: Path | None = None
    whisper_allow_download: bool = False
    cpu_threads: int = 4


def _quantize_alignment_ms(value: float) -> int:
    """Store forced-alignment timestamps at the canonical 10 ms resolution."""
    return max(0, round(float(value) / ALIGNMENT_PRECISION_MS) * ALIGNMENT_PRECISION_MS)


def _quantize_word_timings(
    words: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Quantize word intervals while keeping them ordered and non-empty."""
    quantized: list[dict[str, Any]] = []
    previous_end = 0
    for word in words:
        start = max(previous_end, _quantize_alignment_ms(int(word["start_ms"])))
        end = _quantize_alignment_ms(int(word["end_ms"]))
        if end <= start:
            end = start + ALIGNMENT_PRECISION_MS
        item = {**word, "start_ms": start, "end_ms": end}
        quantized.append(item)
        previous_end = end
    return quantized


def _emit(
    callback: ProgressCallback | None,
    percent: int,
    phase: str,
    message: str,
) -> None:
    if callback:
        callback(max(0, min(100, percent)), phase, message)


def _check_canceled(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise SubtitleAlignmentCanceled("Alignment was canceled")


def _canonical_cache_payload(
    document: dict[str, Any],
    media: dict[str, Any],
    settings: AlignmentSettings,
    cue_ids: set[str] | None,
) -> dict[str, Any]:
    segments = []
    for cue in document.get("segments", []):
        segments.append(
            {
                "id": cue.get("id"),
                "start_ms": cue.get("start_ms"),
                "end_ms": cue.get("end_ms"),
                "text": cue.get("text"),
                "timing_source": cue.get("timing_source"),
                "revision": cue.get("revision", 0),
            }
        )
    return {
        "version": ALIGNMENT_ALGORITHM_VERSION,
        "audio_hash": media.get("audio_hash") or media.get("fingerprint"),
        "duration_ms": media.get("duration_ms"),
        "segments": segments,
        "cue_ids": sorted(cue_ids) if cue_ids else None,
        "settings": {
            "engine": settings.engine,
            "lead_in_ms": settings.lead_in_ms,
            "tail_ms": settings.tail_ms,
            "window_padding_ms": settings.window_padding_ms,
            "max_window_ms": settings.max_window_ms,
            "force_manual": settings.force_manual,
            "whisper_model": settings.whisper_model,
            "whisper_device": settings.whisper_device,
            "whisper_compute_type": settings.whisper_compute_type,
        },
    }


def alignment_cache_key(
    document: dict[str, Any],
    media: dict[str, Any],
    settings: AlignmentSettings,
    cue_ids: set[str] | None = None,
) -> str:
    encoded = json.dumps(
        _canonical_cache_payload(document, media, settings, cue_ids),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_word(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def transcript_tokens(cue: dict[str, Any]) -> list[TranscriptToken]:
    cue_id = str(cue["id"])
    tokens: list[TranscriptToken] = []
    for index, match in enumerate(_TOKEN_RE.finditer(str(cue.get("text", "")))):
        text = match.group(0)
        normalized = _normalized_word(text)
        if normalized:
            tokens.append(TranscriptToken(cue_id, index, text, normalized))
    return tokens


def _token_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def match_transcript_words(
    expected: list[TranscriptToken],
    observed: list[ObservedWord],
) -> dict[int, tuple[int, float]]:
    """Needleman-Wunsch alignment preserving word order and fuzzy Unicode text."""
    rows = len(expected) + 1
    columns = len(observed) + 1
    deletion_cost = 0.82
    insertion_cost = 0.72
    costs = [[0.0] * columns for _ in range(rows)]
    moves = [[""] * columns for _ in range(rows)]
    for row in range(1, rows):
        costs[row][0] = row * deletion_cost
        moves[row][0] = "delete"
    for column in range(1, columns):
        costs[0][column] = column * insertion_cost
        moves[0][column] = "insert"

    normalized_observed = [_normalized_word(word.text) for word in observed]
    for row in range(1, rows):
        for column in range(1, columns):
            similarity = _token_similarity(
                expected[row - 1].normalized,
                normalized_observed[column - 1],
            )
            substitution_cost = 1.0 - similarity if similarity >= 0.45 else 1.05
            candidates = (
                (costs[row - 1][column - 1] + substitution_cost, "match"),
                (costs[row - 1][column] + deletion_cost, "delete"),
                (costs[row][column - 1] + insertion_cost, "insert"),
            )
            costs[row][column], moves[row][column] = min(candidates, key=lambda item: item[0])

    matches: dict[int, tuple[int, float]] = {}
    row, column = len(expected), len(observed)
    while row > 0 or column > 0:
        move = moves[row][column]
        if move == "match":
            similarity = _token_similarity(
                expected[row - 1].normalized,
                normalized_observed[column - 1],
            )
            if similarity >= 0.45:
                matches[row - 1] = (column - 1, similarity)
            row -= 1
            column -= 1
        elif move == "delete":
            row -= 1
        else:
            column -= 1
    return matches


def build_audio_windows(
    cues: Iterable[dict[str, Any]],
    duration_ms: int,
    *,
    padding_ms: int = 650,
    max_window_ms: int = 30_000,
) -> list[AudioWindow]:
    ordered = sorted(cues, key=lambda cue: (cue["start_ms"], cue["end_ms"], cue["id"]))
    windows: list[AudioWindow] = []
    current_start = 0
    current_end = 0
    current_ids: list[str] = []

    def flush() -> None:
        nonlocal current_ids
        if current_ids:
            windows.append(AudioWindow(current_start, current_end, tuple(current_ids)))
            current_ids = []

    for cue in ordered:
        cue_start = max(0, int(cue["start_ms"]) - padding_ms)
        cue_end = min(duration_ms, int(cue["end_ms"]) + padding_ms)
        if not current_ids:
            current_start, current_end = cue_start, cue_end
            current_ids = [str(cue["id"])]
            continue
        proposed_end = max(current_end, cue_end)
        separated = cue_start > current_end + 2_000
        too_long = proposed_end - current_start > max_window_ms
        if separated or too_long:
            flush()
            current_start, current_end = cue_start, cue_end
            current_ids = [str(cue["id"])]
        else:
            current_end = proposed_end
            current_ids.append(str(cue["id"]))
    flush()
    return windows


def _extract_pcm_window(
    video_path: Path,
    window: AudioWindow,
    *,
    cancel_event: threading.Event | None,
    timeout_seconds: int,
) -> bytes:
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    command = [
        ffmpeg_exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{window.start_ms / 1000:.3f}",
        "-i",
        str(video_path.resolve()),
        "-t",
        f"{max(1, window.end_ms - window.start_ms) / 1000:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(PCM_SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        "pipe:1",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            _check_canceled(cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SubtitleAlignmentError("Audio extraction timed out")
            try:
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise SubtitleAlignmentError(f"Could not extract audio: {detail or 'FFmpeg failed'}")
    return stdout


def _pcm_levels(pcm: bytes) -> list[int]:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder != "little":
        samples.byteswap()
    frame_samples = PCM_SAMPLE_RATE * ENERGY_FRAME_MS // 1000
    levels: list[int] = []
    for start in range(0, len(samples), frame_samples):
        frame = samples[start : start + frame_samples]
        if len(frame) < frame_samples // 2:
            break
        mean_square = sum(sample * sample for sample in frame) // len(frame)
        levels.append(math.isqrt(mean_square))
    return levels


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * max(0.0, min(1.0, percentile)))
    return ordered[index]


def speech_runs_from_levels(levels: list[int]) -> tuple[list[tuple[int, int]], float]:
    if not levels:
        return [], 0.0
    noise = _percentile(levels, 0.2)
    voice = _percentile(levels, 0.9)
    threshold = max(120, round(noise * 2.1), round(noise + (voice - noise) * 0.18))
    active = [level >= threshold for level in levels]

    # Fill short holes to avoid chopping a word on plosives or breaths.
    index = 0
    while index < len(active):
        if active[index]:
            index += 1
            continue
        start = index
        while index < len(active) and not active[index]:
            index += 1
        if start > 0 and index < len(active) and index - start <= 10:
            active[start:index] = [True] * (index - start)

    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(active):
        if not active[index]:
            index += 1
            continue
        start = index
        while index < len(active) and active[index]:
            index += 1
        if index - start >= 4:
            runs.append((start, index))
    contrast = 0.0 if voice <= 0 else max(0.0, min(1.0, (voice - noise) / voice))
    return runs, contrast


def _cue_search_bounds(
    ordered: list[dict[str, Any]],
    index: int,
    duration_ms: int,
    padding_ms: int,
) -> tuple[int, int]:
    cue = ordered[index]
    start = max(0, int(cue["start_ms"]) - padding_ms)
    end = min(duration_ms, int(cue["end_ms"]) + padding_ms)
    if index > 0:
        previous = ordered[index - 1]
        if int(previous["end_ms"]) <= int(cue["start_ms"]):
            boundary = (int(previous["end_ms"]) + int(cue["start_ms"])) // 2
            start = max(start, boundary)
    if index + 1 < len(ordered):
        following = ordered[index + 1]
        if int(cue["end_ms"]) <= int(following["start_ms"]):
            boundary = (int(cue["end_ms"]) + int(following["start_ms"])) // 2
            end = min(end, boundary)
    return start, max(start + 1, end)


def _words_for_range(
    cue: dict[str, Any],
    speech_start_ms: int,
    speech_end_ms: int,
    confidence: float,
) -> list[dict[str, Any]]:
    tokens = transcript_tokens(cue)
    duration = speech_end_ms - speech_start_ms
    if not tokens or duration < len(tokens):
        return []
    weights = [max(1, len(token.normalized)) for token in tokens]
    total_weight = sum(weights)
    cursor_weight = 0
    words: list[dict[str, Any]] = []
    previous_end = speech_start_ms
    for index, (token, weight) in enumerate(zip(tokens, weights, strict=True)):
        start = max(
            previous_end,
            speech_start_ms + round(duration * cursor_weight / total_weight),
        )
        cursor_weight += weight
        end = speech_start_ms + round(duration * cursor_weight / total_weight)
        end = max(start + 1, min(speech_end_ms, end))
        if index == len(tokens) - 1:
            end = speech_end_ms
        words.append(
            {
                "id": f"{cue['id']}-w{index + 1:03d}",
                "text": token.text,
                "start_ms": start,
                "end_ms": end,
                "confidence": round(confidence, 4),
            }
        )
        previous_end = end
    return _quantize_word_timings(words)


def _align_energy_window(
    cues: list[dict[str, Any]],
    ordered_all: list[dict[str, Any]],
    window: AudioWindow,
    pcm: bytes,
    duration_ms: int,
    settings: AlignmentSettings,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    levels = _pcm_levels(pcm)
    runs, contrast = speech_runs_from_levels(levels)
    absolute_runs = [
        (
            window.start_ms + start * ENERGY_FRAME_MS,
            min(window.end_ms, window.start_ms + end * ENERGY_FRAME_MS),
        )
        for start, end in runs
    ]
    aligned: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, Any]] = []
    all_index = {str(cue["id"]): index for index, cue in enumerate(ordered_all)}

    for cue in cues:
        cue_id = str(cue["id"])
        search_start, search_end = _cue_search_bounds(
            ordered_all,
            all_index[cue_id],
            duration_ms,
            settings.window_padding_ms,
        )
        candidates = [
            (max(start, search_start), min(end, search_end))
            for start, end in absolute_runs
            if end > search_start and start < search_end
        ]
        candidates = [(start, end) for start, end in candidates if end > start]
        coarse_start = int(cue["start_ms"])
        coarse_end = int(cue["end_ms"])
        overlapping = [
            run for run in candidates if run[1] > coarse_start and run[0] < coarse_end
        ]
        selected = overlapping
        if not selected and candidates:
            nearest = min(
                candidates,
                key=lambda run: min(abs(run[0] - coarse_end), abs(run[1] - coarse_start)),
            )
            distance = min(abs(nearest[0] - coarse_end), abs(nearest[1] - coarse_start))
            if distance <= settings.window_padding_ms:
                selected = [nearest]
        if not selected:
            unchanged = deepcopy(cue)
            unchanged["needs_review"] = True
            unchanged["confidence"] = min(float(cue.get("confidence") or 1), 0.15)
            aligned[cue_id] = unchanged
            warnings.append(
                {
                    "code": "no_speech_detected",
                    "cue_id": cue_id,
                    "message": "Không tìm thấy vùng giọng nói đủ rõ quanh cue; giữ timing cũ để duyệt tay.",
                }
            )
            continue

        speech_start = min(run[0] for run in selected)
        speech_end = max(run[1] for run in selected)
        coarse_duration = max(1, coarse_end - coarse_start)
        correction_ratio = min(
            1.0,
            (abs(speech_start - coarse_start) + abs(speech_end - coarse_end))
            / (2 * coarse_duration),
        )
        confidence = max(0.2, min(0.78, 0.35 + 0.35 * contrast + 0.2 * (1 - correction_ratio)))
        next_cue = deepcopy(cue)
        next_cue.update(
            {
                "speech_start_ms": speech_start,
                "speech_end_ms": speech_end,
                "start_ms": speech_start,
                "end_ms": speech_end,
                "timing_source": "forced_alignment",
                "timing_precision_ms": ALIGNMENT_PRECISION_MS,
                "confidence": round(confidence, 4),
                "needs_review": confidence < 0.6,
                "words": _words_for_range(cue, speech_start, speech_end, confidence),
            }
        )
        aligned[cue_id] = next_cue
    return aligned, warnings


def _resolve_engine(requested: AlignmentEngine) -> Literal["energy", "faster_whisper"]:
    available = importlib.util.find_spec("faster_whisper") is not None
    if requested == "faster_whisper" and not available:
        raise SubtitleAlignmentError(
            "faster-whisper is not installed; install the backend alignment extra"
        )
    if requested == "auto":
        return "faster_whisper" if available else "energy"
    return requested


def _transcribe_pcm(
    pcm: bytes,
    window: AudioWindow,
    cue_text: str,
    settings: AlignmentSettings,
    model: Any,
) -> list[ObservedWord]:
    import numpy as np

    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = model.transcribe(
        audio,
        language="vi",
        task="transcribe",
        beam_size=1,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 160, "speech_pad_ms": 80},
        initial_prompt=cue_text[:2000],
        condition_on_previous_text=False,
        temperature=0,
    )
    observed: list[ObservedWord] = []
    for segment in segments:
        for word in segment.words or []:
            start_ms = _quantize_alignment_ms(
                window.start_ms + round(float(word.start) * 1000)
            )
            end_ms = _quantize_alignment_ms(
                window.start_ms + round(float(word.end) * 1000)
            )
            if end_ms <= start_ms:
                continue
            observed.append(
                ObservedWord(
                    text=str(word.word).strip(),
                    start_ms=max(window.start_ms, start_ms),
                    end_ms=min(window.end_ms, end_ms),
                    confidence=max(0.0, min(1.0, float(word.probability))),
                )
            )
    return observed


def _load_whisper_model(settings: AlignmentSettings) -> Any:
    from faster_whisper import WhisperModel

    download_root = str(settings.whisper_model_dir) if settings.whisper_model_dir else None
    if settings.whisper_model_dir:
        settings.whisper_model_dir.mkdir(parents=True, exist_ok=True)
    return WhisperModel(
        settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        cpu_threads=max(1, settings.cpu_threads),
        num_workers=1,
        download_root=download_root,
        local_files_only=not settings.whisper_allow_download,
    )


def _interpolated_word_timings(
    cue: dict[str, Any],
    tokens: list[TranscriptToken],
    matched: dict[int, tuple[ObservedWord, float]],
) -> list[dict[str, Any]]:
    if not tokens or not matched:
        return []
    first_match = min(matched)
    last_match = max(matched)
    lower_bound = min(int(cue["start_ms"]), matched[first_match][0].start_ms)
    upper_bound = max(int(cue["end_ms"]), matched[last_match][0].end_ms)
    starts: list[int | None] = [None] * len(tokens)
    ends: list[int | None] = [None] * len(tokens)
    confidences = [0.2] * len(tokens)
    for index, (word, similarity) in matched.items():
        starts[index] = word.start_ms
        ends[index] = word.end_ms
        confidences[index] = word.confidence * similarity

    cursor = 0
    while cursor < len(tokens):
        if starts[cursor] is not None:
            cursor += 1
            continue
        run_start = cursor
        while cursor < len(tokens) and starts[cursor] is None:
            cursor += 1
        run_end = cursor
        left = ends[run_start - 1] if run_start > 0 and ends[run_start - 1] is not None else lower_bound
        right = starts[run_end] if run_end < len(tokens) and starts[run_end] is not None else upper_bound
        available = max(run_end - run_start, int(right) - int(left))
        for offset, index in enumerate(range(run_start, run_end)):
            starts[index] = int(left) + round(available * offset / (run_end - run_start))
            ends[index] = int(left) + round(available * (offset + 1) / (run_end - run_start))

    words: list[dict[str, Any]] = []
    previous_end = lower_bound
    for index, token in enumerate(tokens):
        start = max(previous_end, int(starts[index] or lower_bound))
        end = max(start + 1, int(ends[index] or start + 1))
        words.append(
            {
                "id": f"{cue['id']}-w{index + 1:03d}",
                "text": token.text,
                "start_ms": start,
                "end_ms": end,
                "confidence": round(max(0.0, min(1.0, confidences[index])), 4),
            }
        )
        previous_end = end
    return words


def _align_whisper_window(
    cues: list[dict[str, Any]],
    observed: list[ObservedWord],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    expected = [token for cue in cues for token in transcript_tokens(cue)]
    mapping = match_transcript_words(expected, observed)
    by_cue: dict[str, list[tuple[int, TranscriptToken]]] = {}
    for expected_index, token in enumerate(expected):
        by_cue.setdefault(token.cue_id, []).append((expected_index, token))
    aligned: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, Any]] = []

    for cue in cues:
        cue_id = str(cue["id"])
        indexed_tokens = by_cue.get(cue_id, [])
        local_tokens = [token for _, token in indexed_tokens]
        local_matches: dict[int, tuple[ObservedWord, float]] = {}
        for local_index, (global_index, _) in enumerate(indexed_tokens):
            if global_index in mapping:
                observed_index, similarity = mapping[global_index]
                local_matches[local_index] = (observed[observed_index], similarity)
        match_ratio = len(local_matches) / max(1, len(local_tokens))
        if not local_matches:
            unchanged = deepcopy(cue)
            unchanged["needs_review"] = True
            unchanged["confidence"] = min(float(cue.get("confidence") or 1), 0.1)
            aligned[cue_id] = unchanged
            warnings.append(
                {
                    "code": "transcript_not_matched",
                    "cue_id": cue_id,
                    "message": "ASR không ghép được từ nào với transcript; giữ timing cũ.",
                }
            )
            continue
        words = _quantize_word_timings(
            _interpolated_word_timings(cue, local_tokens, local_matches)
        )
        speech_start = words[0]["start_ms"]
        speech_end = words[-1]["end_ms"]
        confidence = sum(float(word["confidence"]) for word in words) / len(words)
        confidence *= 0.7 + 0.3 * match_ratio
        next_cue = deepcopy(cue)
        next_cue.update(
            {
                "speech_start_ms": speech_start,
                "speech_end_ms": speech_end,
                "start_ms": speech_start,
                "end_ms": speech_end,
                "words": words,
                "timing_source": "forced_alignment",
                "timing_precision_ms": ALIGNMENT_PRECISION_MS,
                "confidence": round(confidence, 4),
                "needs_review": match_ratio < 0.7 or confidence < 0.65,
            }
        )
        aligned[cue_id] = next_cue
        if match_ratio < 0.7:
            warnings.append(
                {
                    "code": "low_transcript_match",
                    "cue_id": cue_id,
                    "message": f"Chỉ ghép được {round(match_ratio * 100)}% số từ; cần duyệt lại.",
                }
            )
    return aligned, warnings


def apply_display_padding(
    cues: list[dict[str, Any]],
    duration_ms: int,
    *,
    lead_in_ms: int,
    tail_ms: int,
) -> list[dict[str, Any]]:
    ordered = sorted(cues, key=lambda cue: (cue["start_ms"], cue["end_ms"], cue["id"]))
    for cue in ordered:
        speech_start = cue.get("speech_start_ms")
        speech_end = cue.get("speech_end_ms")
        if not isinstance(speech_start, int) or not isinstance(speech_end, int):
            continue
        cue["start_ms"] = _quantize_alignment_ms(
            max(0, speech_start - lead_in_ms)
        )
        cue["end_ms"] = min(
            duration_ms,
            _quantize_alignment_ms(speech_end + tail_ms),
        )

    for left, right in pairwise(ordered):
        if left["end_ms"] <= right["start_ms"]:
            continue
        left_speech_end = int(left.get("speech_end_ms", left["end_ms"]))
        right_speech_start = int(right.get("speech_start_ms", right["start_ms"]))
        if left_speech_end <= right_speech_start:
            boundary = _quantize_alignment_ms(
                (left_speech_end + right_speech_start) // 2
            )
            left["end_ms"] = max(left_speech_end, min(left["end_ms"], boundary))
            right["start_ms"] = min(
                right_speech_start,
                max(right["start_ms"], boundary),
            )
    return ordered


def align_subtitle_document(
    video_path: Path,
    document: dict[str, Any],
    media: dict[str, Any],
    *,
    settings: AlignmentSettings = AlignmentSettings(),
    cue_ids: set[str] | None = None,
    cache_dir: Path | None = None,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
    extraction_timeout_seconds: int = 90,
) -> dict[str, Any]:
    if not media.get("has_audio"):
        raise SubtitleAlignmentError("Video has no audio track")
    duration_ms = int(media["duration_ms"])
    cache_key = alignment_cache_key(document, media, settings, cue_ids)
    cache_path = cache_dir / f"{cache_key}.json" if cache_dir else None
    if cache_path and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("cache_version") == ALIGNMENT_CACHE_VERSION:
                result = cached["result"]
                result["cache_hit"] = True
                _emit(progress, 100, "complete", "Dùng kết quả alignment trong cache")
                return result
        except (OSError, json.JSONDecodeError, KeyError, AttributeError):
            pass

    _check_canceled(cancel_event)
    resolved_engine = _resolve_engine(settings.engine)
    source_cues = [deepcopy(cue) for cue in document.get("segments", [])]
    ordered_all = sorted(
        source_cues,
        key=lambda cue: (cue["start_ms"], cue["end_ms"], cue["id"]),
    )
    selected: list[dict[str, Any]] = []
    skipped_manual: list[str] = []
    for cue in ordered_all:
        cue_id = str(cue["id"])
        if cue_ids and cue_id not in cue_ids:
            continue
        if cue.get("timing_source") == "manual" and not settings.force_manual:
            skipped_manual.append(cue_id)
            continue
        selected.append(cue)

    warnings: list[dict[str, Any]] = [
        {
            "code": "manual_timing_locked",
            "cue_id": cue_id,
            "message": "Cue chỉnh tay được khóa và không bị alignment ghi đè.",
        }
        for cue_id in skipped_manual
    ]
    if not selected:
        result = {
            "document": deepcopy(document),
            "warnings": warnings,
            "engine": resolved_engine,
            "cache_hit": False,
            "aligned_cue_count": 0,
        }
        _emit(progress, 100, "complete", "Không có cue cần căn chỉnh")
        return result

    windows = build_audio_windows(
        selected,
        duration_ms,
        padding_ms=settings.window_padding_ms,
        max_window_ms=settings.max_window_ms,
    )
    cue_by_id = {str(cue["id"]): cue for cue in selected}
    aligned_by_id: dict[str, dict[str, Any]] = {}
    whisper_model: Any | None = None
    if resolved_engine == "faster_whisper":
        _emit(progress, 2, "model", "Đang nạp mô hình word timing")
        whisper_model = _load_whisper_model(settings)

    try:
        for index, window in enumerate(windows):
            _check_canceled(cancel_event)
            window_percent = 5 + round(index / max(1, len(windows)) * 85)
            _emit(
                progress,
                window_percent,
                "audio",
                f"Đang xử lý audio window {index + 1}/{len(windows)}",
            )
            pcm = _extract_pcm_window(
                video_path,
                window,
                cancel_event=cancel_event,
                timeout_seconds=extraction_timeout_seconds,
            )
            window_cues = [cue_by_id[cue_id] for cue_id in window.cue_ids]
            if resolved_engine == "faster_whisper":
                observed = _transcribe_pcm(
                    pcm,
                    window,
                    " ".join(str(cue.get("text", "")) for cue in window_cues),
                    settings,
                    whisper_model,
                )
                aligned, window_warnings = _align_whisper_window(window_cues, observed)
            else:
                aligned, window_warnings = _align_energy_window(
                    window_cues,
                    ordered_all,
                    window,
                    pcm,
                    duration_ms,
                    settings,
                )
            aligned_by_id.update(aligned)
            warnings.extend(window_warnings)
    finally:
        if whisper_model is not None:
            del whisper_model
            gc.collect()

    _check_canceled(cancel_event)
    merged = [aligned_by_id.get(str(cue["id"]), cue) for cue in source_cues]
    merged = apply_display_padding(
        merged,
        duration_ms,
        lead_in_ms=settings.lead_in_ms,
        tail_ms=settings.tail_ms,
    )
    next_document = deepcopy(document)
    next_document["segments"] = merged
    if aligned_by_id:
        next_document["timing_source"] = "forced_alignment"
        next_document["timing_precision_ms"] = ALIGNMENT_PRECISION_MS
    result = {
        "document": next_document,
        "warnings": warnings,
        "engine": resolved_engine,
        "cache_hit": False,
        "aligned_cue_count": sum(
            1
            for cue in aligned_by_id.values()
            if cue.get("timing_source") == "forced_alignment"
        ),
    }
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cache_version": ALIGNMENT_CACHE_VERSION, "result": result}
        temporary = cache_path.with_suffix(".json.part")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(cache_path)
    _emit(progress, 100, "complete", "Alignment hoàn tất")
    return result
