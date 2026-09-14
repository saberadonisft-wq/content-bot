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
import uuid
from array import array
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import imageio_ffmpeg

from .speech_evidence import SpeechEvidence, audio_identity, transcript_hash

ALIGNMENT_CACHE_VERSION = 4
ALIGNMENT_ALGORITHM_VERSION = "2026-09-source-evidence-v7"
# Recognition is independent of the rules that match a cue to observed words.
ASR_OBSERVATION_VERSION = "2026-09-source-evidence-v6"
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
    source_language: str | None = None
    max_shift_ms: int = 1000
    preserve_display: bool = False


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
    segments = deepcopy(document.get("segments", []))
    return {
        "version": ALIGNMENT_ALGORITHM_VERSION,
        "audio_hash": media.get("audio_hash") or media.get("fingerprint"),
        "duration_ms": media.get("duration_ms"),
        "segments": segments,
        "cue_ids": sorted(cue_ids) if cue_ids is not None else None,
        "document_revision": document.get("revision", 0),
        "run_id": document.get("run_id"),
        "document_language": document.get("language"),
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
            "source_language": settings.source_language,
            "max_shift_ms": settings.max_shift_ms,
            "preserve_display": settings.preserve_display,
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


def alignment_transcript(cue: dict[str, Any]) -> str:
    return str(cue.get("source_text") or cue.get("secondary_text") or cue.get("text") or "")


def transcript_tokens(cue: dict[str, Any]) -> list[TranscriptToken]:
    cue_id = str(cue["id"])
    tokens: list[TranscriptToken] = []
    for index, match in enumerate(_TOKEN_RE.finditer(alignment_transcript(cue))):
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
        "-copyts", "-start_at_zero",
        "-ss",
        f"{window.start_ms / 1000:.3f}",
        "-i",
        str(video_path.resolve()),
        "-af", f"asetpts=PTS-{window.start_ms / 1000:.3f}/TB,aresample=async=1:first_pts=0",
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
                "alignment_method": "energy_estimated",
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
                "alignment_method": "energy_estimated",
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
        language=settings.source_language,
        task="transcribe",
        beam_size=1,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 160, "speech_pad_ms": 80},
        # Treat the source transcript as a hypothesis to compare after recognition.
        # Supplying it as previous dialogue can bias ASR or make it skip that line.
        initial_prompt=None,
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


def _resolve_whisper_model_path(settings: AlignmentSettings) -> str:
    from faster_whisper.utils import download_model
    from huggingface_hub.errors import LocalEntryNotFoundError

    download_root = str(settings.whisper_model_dir) if settings.whisper_model_dir else None
    model = settings.whisper_model
    if not settings.whisper_allow_download and not Path(model).is_dir():
        try:
            model = download_model(model, cache_dir=download_root, local_files_only=True)
        except LocalEntryNotFoundError:
            if download_root is None:
                raise
            # Reuse a model already installed in the user's shared cache; no copy/download.
            model = download_model(model, local_files_only=True)
    return model


def _whisper_model_signature(path: str) -> dict:
    folder = Path(path)
    files = [folder / name for name in ('model.bin', 'config.json', 'tokenizer.json', 'preprocessor_config.json')]
    files.extend(folder.glob('vocabulary.*'))
    return {'path': str(folder.resolve()), 'files': [
        (item.name, item.stat().st_size, item.stat().st_mtime_ns) for item in files if item.is_file()]}


def _load_whisper_model(settings: AlignmentSettings) -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel(
        _resolve_whisper_model_path(settings),
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        cpu_threads=max(1, settings.cpu_threads),
        num_workers=1,
        download_root=str(settings.whisper_model_dir) if settings.whisper_model_dir else None,
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
                "alignment_method": "asr_observed" if index in matched else "interpolated",
            }
        )
        previous_end = end
    return words


def _align_cjk_source_spans(cues: list[dict[str, Any]], observed: list[ObservedWord]):
    """Use character correspondence to locate original ASR word spans, without
    inventing sub-word timestamps for languages without whitespace segmentation.
    """
    expected = []
    for cue in cues:
        for index, char in enumerate(_normalized_word(alignment_transcript(cue))):
            expected.append(TranscriptToken(cue["id"], index, char, char))
    expanded, original_indexes = [], []
    for index, word in enumerate(observed):
        for char in _normalized_word(word.text):
            expanded.append(ObservedWord(char, word.start_ms, word.end_ms, word.confidence))
            original_indexes.append(index)
    if len(expected) * len(expanded) > 1_000_000:
        return {c["id"]: {**c, "needs_review": True} for c in cues}, [{"code": "alignment_window_too_complex", "message": "Vùng căn chứa quá nhiều ký tự; giữ timing để kiểm tra theo phạm vi nhỏ hơn."}]
    mapping = match_transcript_words(expected, expanded)
    owners: dict[int, set[str]] = {}
    word_units: dict[int, set[int]] = {}
    for index, original_index in enumerate(original_indexes):
        word_units.setdefault(original_index, set()).add(index)
    for index, (matched, _) in mapping.items():
        owners.setdefault(original_indexes[matched], set()).add(expected[index].cue_id)
    aligned, warnings = {}, []
    for cue in cues:
        indexes = [index for index, token in enumerate(expected) if token.cue_id == cue["id"]]
        matched = [mapping[index][0] for index in indexes if index in mapping]
        enough = bool(indexes) and len(matched) == len(indexes)
        shared = any(len(owners[original_indexes[index]]) > 1 for index in matched)
        contiguous = bool(matched) and matched == list(range(matched[0], matched[-1] + 1))
        complete_words = bool(matched) and all(word_units[original_indexes[index]] <= set(matched)
                                             for index in matched)
        phrase = ''.join(expected[index].normalized for index in indexes)
        observed_text = ''.join(word.text for word in expanded)
        occurrences = []
        cursor = observed_text.find(phrase) if phrase else -1
        while cursor >= 0:
            final = cursor + len(phrase) - 1
            if (cursor == min(word_units[original_indexes[cursor]])
                    and final == max(word_units[original_indexes[final]])):
                occurrences.append(cursor)
            cursor = observed_text.find(phrase, cursor + 1)
        ambiguous = len(occurrences) > 1
        confidence = min((expanded[index].confidence for index in matched), default=0)
        if not enough or shared or not contiguous or not complete_words or ambiguous or confidence < 0.65:
            aligned[cue["id"]] = {**cue, "needs_review": True}
            warnings.append({"code": "source_alignment_uncertain", "cue_id": cue["id"],
                             "message": "ASR chưa khớp đầy đủ lời gốc hoặc dùng chung biên từ giữa các cue; giữ timing cũ.",
                             "diagnostics": {"matched_units": len(matched), "expected_units": len(indexes),
                                 "minimum_confidence": confidence, "shared_word_boundary": shared,
                                 "contiguous_match": contiguous, "complete_word_boundaries": complete_words,
                                 "ambiguous_occurrence": ambiguous,
                                 "observed_text": "".join(word.text for word in observed)}})
            continue
        start = min(expanded[index].start_ms for index in matched)
        end = max(expanded[index].end_ms for index in matched)
        aligned[cue["id"]] = {**cue, "start_ms": start, "end_ms": end,
                              "speech_start_ms": start, "speech_end_ms": end, "words": None,
                              "timing_source": "forced_alignment", "timing_precision_ms": ALIGNMENT_PRECISION_MS,
                              "alignment_method": "asr_observed",
                              "confidence": confidence, "needs_review": False}
    return aligned, warnings


def _align_whisper_window(
    cues: list[dict[str, Any]],
    observed: list[ObservedWord],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if any(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", alignment_transcript(cue)) for cue in cues):
        return _align_cjk_source_spans(cues, observed)
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
        if not local_tokens or len(local_matches) != len(local_tokens):
            unchanged = deepcopy(cue)
            unchanged["needs_review"] = True
            unchanged["confidence"] = min(float(cue.get("confidence") or 1), 0.1)
            aligned[cue_id] = unchanged
            warnings.append(
                {
                    "code": "transcript_not_matched",
                    "cue_id": cue_id,
                    "message": "ASR chưa ghép đầy đủ lời gốc; giữ timing cũ.",
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
                "alignment_method": "asr_observed",
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
        left_speech_end = int(left.get("speech_end_ms") if left.get("speech_end_ms") is not None else left["end_ms"])
        right_speech_start = int(right.get("speech_start_ms") if right.get("speech_start_ms") is not None else right["start_ms"])
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
    _check_canceled(cancel_event)
    resolved_engine = _resolve_engine(settings.engine)
    source_cues = [deepcopy(cue) for cue in document.get("segments", [])]
    ordered_all = sorted(
        source_cues,
        key=lambda cue: (cue["start_ms"], cue["end_ms"], cue["id"]),
    )
    selected: list[dict[str, Any]] = []
    skipped_manual: list[str] = []
    skipped_evidence: list[str] = []
    skipped_long: list[str] = []
    for cue in ordered_all:
        cue_id = str(cue["id"])
        if cue_ids is not None and cue_id not in cue_ids:
            continue
        if cue.get("locked") or (cue.get("timing_source") == "manual" and not settings.force_manual):
            skipped_manual.append(cue_id)
            continue
        if resolved_engine == "energy" and cue.get("content_source") in {"screen", "mixed"}:
            skipped_evidence.append(cue_id)
            continue
        if cue.get("origin_chunk_id") and not (cue.get("source_text") or cue.get("secondary_text")):
            skipped_evidence.append(cue_id)
            continue
        window_start = max(0, int(cue["start_ms"]) - settings.window_padding_ms)
        window_end = min(duration_ms, int(cue["end_ms"]) + settings.window_padding_ms)
        if window_end - window_start > settings.max_window_ms:
            skipped_long.append(cue_id)
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
    warnings.extend({"code": "alignment_source_unverified", "cue_id": cue_id,
                     "message": "Chưa có lời gốc hoặc bằng chứng phù hợp với engine căn audio; giữ timing hiện tại."}
                    for cue_id in skipped_evidence)
    warnings.extend({"code": "alignment_window_limit", "cue_id": cue_id,
                     "message": "Cue vượt độ dài cửa sổ căn cho phép; giữ timing và chia cue trước khi căn lại."}
                    for cue_id in skipped_long)
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

    model_signature = None
    model_settings = settings
    if cache_dir and resolved_engine == 'faster_whisper' and not settings.whisper_allow_download:
        model_path = _resolve_whisper_model_path(settings)
        model_signature = _whisper_model_signature(model_path)
        model_settings = replace(settings, whisper_model=model_path)
    cache_key = alignment_cache_key(document, media, settings, cue_ids)
    if model_signature:
        cache_key = hashlib.sha256((cache_key + json.dumps(model_signature, sort_keys=True)).encode()).hexdigest()
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

    language_groups: dict[str | None, list[dict[str, Any]]] = {}
    for cue in selected:
        language = cue.get("source_language") or settings.source_language
        if not language and not (cue.get("source_text") or cue.get("secondary_text")):
            language = document.get("language")
        language = str(language).lower().split("-")[0] if language and language != "unknown" else None
        if language and not re.fullmatch(r"[a-z]{2,3}", language):
            language = None
        language_groups.setdefault(language, []).append(cue)
    windows = []
    window_languages = {}
    for language, group in language_groups.items():
        for window in build_audio_windows(group, duration_ms, padding_ms=settings.window_padding_ms, max_window_ms=settings.max_window_ms):
            windows.append(window)
            window_languages[window] = language
    windows.sort(key=lambda window: window.start_ms)
    cue_by_id = {str(cue["id"]): cue for cue in selected}
    aligned_by_id: dict[str, dict[str, Any]] = {}
    source_observations = []
    whisper_model: Any | None = None
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
                try:
                    observed_path = None
                    observed = None
                    if cache_dir and model_signature:
                        observation_key = hashlib.sha256(json.dumps({
                            'version': ASR_OBSERVATION_VERSION, 'model': model_signature,
                            'pcm_sha256': hashlib.sha256(pcm).hexdigest(),
                            'start_ms': window.start_ms, 'end_ms': window.end_ms,
                            'language': window_languages[window], 'device': settings.whisper_device,
                            'compute_type': settings.whisper_compute_type,
                        }, sort_keys=True).encode()).hexdigest()
                        observed_path = cache_dir / 'observations' / f'{observation_key}.json'
                        try:
                            rows = json.loads(observed_path.read_text(encoding='utf-8'))
                            candidate = [ObservedWord(**row) for row in rows]
                            if all(window.start_ms <= word.start_ms < word.end_ms <= window.end_ms
                                   and math.isfinite(word.confidence) and 0 <= word.confidence <= 1
                                   and isinstance(word.text, str) for word in candidate):
                                observed = candidate
                        except (OSError, ValueError, TypeError):
                            pass
                    observation_hit = observed is not None
                    if observed is None:
                        if whisper_model is None:
                            _emit(progress, window_percent, 'model', 'Đang nạp mô hình word timing')
                            whisper_model = _load_whisper_model(model_settings)
                        observed = _transcribe_pcm(
                            pcm, window, " ".join(alignment_transcript(cue) for cue in window_cues),
                            replace(settings, source_language=window_languages[window]), whisper_model)
                        _check_canceled(cancel_event)
                        if observed_path:
                            observed_path.parent.mkdir(parents=True, exist_ok=True)
                            temporary = observed_path.with_suffix(f'.{uuid.uuid4().hex}.part')
                            temporary.write_text(json.dumps([word.__dict__ for word in observed], ensure_ascii=False), encoding='utf-8')
                            temporary.replace(observed_path)
                    source_observations.append({"start_ms": window.start_ms, "end_ms": window.end_ms,
                        "cue_ids": list(window.cue_ids), "language": window_languages[window],
                        "cache_hit": observation_hit,
                        "words": [word.__dict__ for word in observed]})
                    aligned, window_warnings = _align_whisper_window(window_cues, observed)
                except ValueError:
                    aligned = {cue["id"]: {**cue, "needs_review": True} for cue in window_cues}
                    window_warnings = [{"code": "alignment_language_unsupported", "message": "ASR không hỗ trợ ngôn ngữ hoặc đầu vào vùng này; giữ timing cũ."}]
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
    merged = [deepcopy(aligned_by_id.get(str(cue["id"]), cue)) for cue in source_cues]
    merged = apply_display_padding(
        merged,
        duration_ms,
        lead_in_ms=settings.lead_in_ms,
        tail_ms=settings.tail_ms,
    )
    padded_by_id = {cue["id"]: cue for cue in merged}
    # Padding must not change locked/unselected cues, including their existing speech bounds.
    successful_ids = {cue_id for cue_id, cue in aligned_by_id.items()
                      if cue.get("timing_source") == "forced_alignment" and not cue.get("needs_review")}
    merged = []
    for original in source_cues:
        cue_id = str(original["id"])
        if cue_id in successful_ids:
            aligned = padded_by_id[cue_id]
            if max(abs(aligned["speech_start_ms"] - original["start_ms"]), abs(aligned["speech_end_ms"] - original["end_ms"])) > settings.max_shift_ms:
                merged.append({**original, "needs_review": True})
                successful_ids.remove(cue_id)
                warnings.append({"code": "alignment_shift_limit", "cue_id": cue_id, "message": "Đề xuất timing vượt mức dịch chuyển cho phép; giữ timing cũ."})
                continue
            aligned["speech_evidence"] = SpeechEvidence(
                method=aligned.pop("alignment_method", "energy_estimated"),
                audio_identity=audio_identity(media), transcript_sha256=transcript_hash(original),
                start_ms=aligned["speech_start_ms"], end_ms=aligned["speech_end_ms"],
                algorithm=ALIGNMENT_ALGORITHM_VERSION,
                transcript_complete=resolved_engine == "faster_whisper",
            ).model_dump()
            # Screen text timing is independent of when the source line is spoken.
            if settings.preserve_display or original.get("content_source") in {"screen", "mixed"}:
                for field in ("start_ms", "end_ms", "timing_source", "timing_precision_ms"):
                    aligned[field] = original.get(field, aligned.get(field))
            aligned["revision"] = int(original.get("revision", 0)) + 1
            merged.append(aligned)
        else:
            unchanged = deepcopy(original)
            if cue_id in aligned_by_id and aligned_by_id[cue_id].get("needs_review"):
                unchanged["needs_review"] = True
            merged.append(unchanged)
    next_document = deepcopy(document)
    next_document["segments"] = merged
    if successful_ids:
        next_document["revision"] = int(document.get("revision", 0)) + 1
        if not settings.preserve_display and any(cue["id"] in successful_ids
                and cue.get("content_source") not in {"screen", "mixed"} for cue in source_cues):
            next_document["timing_source"] = "forced_alignment"
            next_document["timing_precision_ms"] = ALIGNMENT_PRECISION_MS
    result = {
        "document": next_document,
        "warnings": warnings,
        "engine": resolved_engine,
        "cache_hit": False,
        "aligned_cue_count": len(successful_ids),
        "source_observations": source_observations,
    }
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"cache_version": ALIGNMENT_CACHE_VERSION, "result": result}
        temporary = cache_path.with_suffix(f".{uuid.uuid4().hex}.part")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(cache_path)
    _emit(progress, 100, "complete", "Alignment hoàn tất")
    return result
