"""Independent OCR source subtitle extraction using RapidOCR."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from fractions import Fraction
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import av
import numpy as np

from ..schemas import SubtitleDocumentV2, SubtitleOcrRegion
from .gemini_media import atomic_json
from .subtitle_cache import cache_json, cached_json, with_subtitle_cache
from .subtitle_ocr_continuity import CaptionContinuity
from .subtitle_ocr_tracking import FeatureCache, OcrAcceleration, VisualReading

logger = logging.getLogger("content_bot.subtitle_ocr")

OCR_ALGORITHM_VERSION = "rapidocr-v7-caption-continuity"
RAPIDOCR_LINE_CACHE_API_VERSION = "1.2.3"
OCR_PRECISION_MS = 100  # Explicit temporal resolution
REFINEMENT_MAX_BYTES = 24 * 1024 * 1024
REFINEMENT_MAX_FRAMES = 32
FRAME_PREFETCH_MAX_FRAMES = 4
FRAME_PREFETCH_MAX_BYTES = 16 * 1024 * 1024
TEXT_REGION_REUSE_LIMIT = 1


@lru_cache(maxsize=1)
def _line_cache_api_supported() -> bool:
    try:
        return version("rapidocr-onnxruntime") == RAPIDOCR_LINE_CACHE_API_VERSION
    except PackageNotFoundError:
        return False


def _empty_ocr_timings() -> dict[str, float]:
    return {
        "cache_lookup_seconds": 0.0,
        "probe_seconds": 0.0,
        "engine_acquire_seconds": 0.0,
        "decode_seconds": 0.0,
        "image_materialization_seconds": 0.0,
        "frame_queue_wait_seconds": 0.0,
        "frame_queue_full_seconds": 0.0,
        "tracking_update_seconds": 0.0,
        "inference_lock_wait_seconds": 0.0,
        "ocr_call_wall_seconds": 0.0,
        "detector_inference_seconds": 0.0,
        "classifier_inference_seconds": 0.0,
        "recognizer_inference_seconds": 0.0,
        "sample_inference_seconds": 0.0,
        "refinement_inference_seconds": 0.0,
        "probe_inference_seconds": 0.0,
        "postprocess_seconds": 0.0,
        "cache_write_seconds": 0.0,
    }


def _add_timing(timings: dict[str, float], name: str, elapsed: float) -> None:
    timings[name] = timings.get(name, 0.0) + elapsed


def _record_rapidocr_stage_timings(
    timings: dict[str, float] | None, stage_elapsed: Any
) -> None:
    if timings is None or not isinstance(stage_elapsed, (list, tuple)):
        return
    for name, elapsed in zip(
        (
            "detector_inference_seconds",
            "classifier_inference_seconds",
            "recognizer_inference_seconds",
        ),
        stage_elapsed,
    ):
        try:
            _add_timing(timings, name, float(elapsed))
        except (TypeError, ValueError):
            continue


_ACTIVE_OCR_TIMINGS: ContextVar[tuple[dict[str, float], str] | None] = ContextVar(
    "active_subtitle_ocr_timings", default=None
)


def _next_decoded_frame(iterator: Any, timings: dict[str, float]) -> Any:
    started = time.perf_counter()
    try:
        return next(iterator)
    finally:
        _add_timing(timings, "decode_seconds", time.perf_counter() - started)

_ENGINE_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()
_RAPID_OCR_INSTANCE: Any = None


class SubtitleOcrError(RuntimeError):
    def __init__(self, message: str, *, code: str = "ocr_job_error"):
        super().__init__(message)
        self.code = code


class SubtitleOcrCanceled(SubtitleOcrError):
    pass


def _get_ocr_engine() -> Any:
    global _RAPID_OCR_INSTANCE
    with _ENGINE_LOCK:
        if _RAPID_OCR_INSTANCE is None:
            try:
                _RAPID_OCR_INSTANCE, _ = _create_ocr_engine()
            except Exception as exc:
                raise SubtitleOcrError(
                    f"Không thể khởi tạo RapidOCR engine: {exc}"
                ) from exc
        return _RAPID_OCR_INSTANCE


def _create_ocr_engine(*, use_cuda: bool = False) -> tuple[Any, dict[str, list[str]]]:
    from rapidocr_onnxruntime import RapidOCR

    # Keep the same bounded canvas as the measured CPU/GPU runs.
    engine_args = {
        "width_height_ratio": -1,
        "min_height": 0,
        "text_score": 0.0,
        "det_limit_type": "max",
        "det_limit_side_len": 1280,
        "det_model_path": None,
    }
    if not use_cuda:
        return RapidOCR(**engine_args), {}

    import onnxruntime as ort

    if "CUDAExecutionProvider" not in ort.get_available_providers():
        raise SubtitleOcrError(
            "ONNX Runtime không có CUDAExecutionProvider; OCR GPU không khả dụng."
        )
    engine_args.update(
        det_use_cuda=True,
        cls_use_cuda=True,
        cls_model_path=None,
        rec_use_cuda=True,
        rec_model_path=None,
    )

    # RapidOCR 1.2.3 fails to strip cls_/rec_ from use_cuda. Apply the
    # compatibility adapter only while this one-job worker constructs its engine.
    from rapidocr_onnxruntime.utils import UpdateParameters

    originals: dict[str, Any] = {}
    for stage in ("cls", "rec"):
        method_name = f"update_{stage}_params"
        original = getattr(UpdateParameters, method_name)
        originals[method_name] = original

        def make_normalizer(prefix: str, callback: Any) -> Any:
            def normalized(self: Any, config: dict[str, Any], values: dict[str, Any]):
                normalized_values = {
                    key.removeprefix(f"{prefix}_"): value
                    for key, value in values.items()
                }
                return callback(self, config, normalized_values)

            return normalized

        setattr(UpdateParameters, method_name, make_normalizer(stage, original))
    try:
        engine = RapidOCR(**engine_args)
    finally:
        for method_name, original in originals.items():
            setattr(UpdateParameters, method_name, original)

    sessions = {
        "detector": engine.text_detector.infer.session,
        "classifier": engine.text_cls.infer.session,
        "recognizer": engine.text_recognizer.session.session,
    }
    providers = {name: session.get_providers() for name, session in sessions.items()}
    for name, session in sessions.items():
        session.disable_fallback()
        if not providers[name] or providers[name][0] != "CUDAExecutionProvider":
            raise SubtitleOcrError(
                f"OCR {name} không chạy CUDA như yêu cầu: {providers[name]}"
            )
    return engine, providers


def levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if not s2:
        return len(s1)
    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def normalize_text_for_comparison(s: str) -> str:
    s = s.lower()
    s = "".join(
        c for c in unicodedata.normalize("NFKD", s) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[\s\.,!?:;\"'“”‘’_\-]+", "", s)


def text_similarity(s1: str, s2: str) -> float:
    n1 = normalize_text_for_comparison(s1)
    n2 = normalize_text_for_comparison(s2)
    if not n1 and not n2:
        return 1.0
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0
    dist = levenshtein_distance(n1, n2)
    max_len = max(len(n1), len(n2))
    return max(0.0, 1.0 - (dist / max_len))


def ocr_cache_key(
    media: dict[str, Any],
    region: SubtitleOcrRegion,
    *,
    source_language: str | None,
    sample_fps: float,
    min_duration_ms: int,
    max_gap_ms: int = 250,
    auto_probe: bool = False,
    prefetch_frames: bool = True,
    glyph_cache: bool = True,
    acceleration: OcrAcceleration | None = None,
) -> str:
    try:
        engine_version = version("rapidocr-onnxruntime")
    except PackageNotFoundError:
        engine_version = "missing"
    try:
        import onnxruntime as ort

        ort_fingerprint = {
            "version": ort.__version__,
            "available_providers": list(ort.get_available_providers()),
        }
    except Exception as exc:
        ort_fingerprint = {"version": "unavailable", "error_type": type(exc).__name__}
    payload = {
        "algorithm": OCR_ALGORITHM_VERSION,
        "fingerprint": media.get("fingerprint") or media.get("video_id"),
        "file_size_bytes": media.get("file_size_bytes"),
        "duration_ms": media.get("duration_ms"),
        "source_start_ms": media.get("source_start_ms"),
        "rotation": media.get("rotation", 0),
        "region": {
            "x": float(region.x),
            "y": float(region.y),
            "width": float(region.width),
            "height": float(region.height),
        },
        "source_language": source_language,
        "sample_interval_ms": max(50, round(1000.0 / max(1.0, min(30.0, sample_fps)))),
        "min_duration_ms": int(min_duration_ms),
        "max_gap_ms": int(max_gap_ms),
        "auto_probe": auto_probe,
        "prefetch_frames": prefetch_frames,
        "glyph_cache": glyph_cache,
        "acceleration": (acceleration or OcrAcceleration()).model_dump(),
        "engine": engine_version,
        "runtime": ort_fingerprint,
        "model": "ch-PP-OCRv3-bundled",
        "detector": {
            "limit_type": "max",
            "limit_side_len": 1280,
            "always_detect": not (acceleration or OcrAcceleration()).recognition_reuse,
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass
class _OcrCandidate:
    start_ms: int
    end_ms: int
    best_text: str
    best_conf: float
    confidence_sum: float
    frame_count: int
    continuity: CaptionContinuity | None = None
    continuity_attempted: bool = False
    stabilized_frames: int = 0
    votes: dict[str, float] = field(default_factory=dict)
    last_timestamp_ms: int = field(init=False)
    last_text: str = field(init=False)
    last_confidence: float = field(init=False)

    def __post_init__(self) -> None:
        self.last_timestamp_ms = self.start_ms
        self.last_text = self.best_text
        self.last_confidence = self.best_conf

    def vote_until(self, timestamp: int) -> None:
        # Weight actual display intervals, not observation counts: refinement
        # samples are denser and must not outvote a long stable reading.
        weight = max(0, timestamp - self.last_timestamp_ms) * self.last_confidence
        self.votes[self.last_text] = self.votes.get(self.last_text, 0.0) + weight
        self.last_timestamp_ms = timestamp


@dataclass
class _OcrLineCacheEntry:
    box: np.ndarray
    crop: np.ndarray
    text: str
    confidence: float


class _DisplayCropper:
    """Crop in the decoder's native pixel format before expensive BGR conversion."""

    def __init__(self, rotation: int, bounds: tuple[int, int, int, int], enabled: bool):
        self.rotation = rotation % 360
        self.bounds = bounds
        self.enabled = enabled
        self.graph: Any = None
        self.signature: tuple[Any, ...] | None = None
        self.converted_frames = 0
        self.fallback_frames = 0

    def __call__(self, frame: Any) -> np.ndarray:
        if self.enabled and self.rotation in (0, 90, 180, 270):
            try:
                result = self._native_crop(frame)
                self.converted_frames += 1
                return result
            except (av.error.FFmpegError, ValueError, AttributeError, TypeError) as exc:
                # Keep the frame and its original PTS; switch this reader to the
                # established path if its pixel format/filter is unsupported.
                logger.debug("Native OCR crop unavailable, using BGR crop: %s", exc)
                self.enabled = False
                self.graph = None
        self.fallback_frames += 1
        return _display_crop(frame, self.rotation, *self.bounds)

    def _native_crop(self, frame: Any) -> np.ndarray:
        width, height = frame.width, frame.height
        x1, y1, x2, y2 = self.bounds
        if self.rotation == 90:
            x1, y1, x2, y2 = width - y2, x1, width - y1, x2
        elif self.rotation == 180:
            x1, y1, x2, y2 = width - x2, height - y2, width - x1, height - y1
        elif self.rotation == 270:
            x1, y1, x2, y2 = y1, height - x2, y2, height - x1
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise ValueError("OCR native crop exceeds decoded frame")
        # Preserve chroma phase and a halo for the YUV -> RGB conversion. The
        # exact user ROI is cut from BGR after conversion of this smaller patch.
        left, top = max(0, (x1 - 8) // 2 * 2), max(0, (y1 - 8) // 2 * 2)
        right, bottom = min(width, (x2 + 9) // 2 * 2), min(height, (y2 + 9) // 2 * 2)
        signature = (width, height, frame.format.name, frame.time_base, left, top, right, bottom)
        if signature != self.signature:
            graph = av.filter.Graph()
            graph.threads = 1
            source = graph.add_buffer(
                width=width, height=height, format=frame.format,
                time_base=frame.time_base or Fraction(1, 1000),
            )
            crop = graph.add("crop", f"w={right-left}:h={bottom-top}:x={left}:y={top}:exact=1")
            sink = graph.add("buffersink")
            graph.link_nodes(source, crop, sink)
            graph.configure()
            self.graph, self.signature = graph, signature
        self.graph.push(frame)
        cropped = self.graph.pull()
        image = cropped.to_ndarray(format="bgr24")
        image = image[y1-top:y2-top, x1-left:x2-left]
        if self.rotation:
            image = np.rot90(image, k=self.rotation // 90)
        return image.copy(order="C")


class _PrefetchedCropReader:
    """Decode and materialize bounded subtitle crops while OCR runs on the GPU."""

    def __init__(
        self,
        container: Any,
        video_stream: Any,
        *,
        rotation: int,
        bounds: tuple[int, int, int, int],
        timings: dict[str, float],
        cropper: _DisplayCropper | None = None,
    ) -> None:
        self.container = container
        self.video_stream = video_stream
        self.rotation = rotation
        self.bounds = bounds
        self.timings = timings
        self.cropper = cropper or _DisplayCropper(rotation, bounds, False)
        self._frames: deque[tuple[int, np.ndarray]] = deque()
        self._condition = threading.Condition()
        self._queued_bytes = 0
        self._producer_timings = {
            "decode_seconds": 0.0,
            "image_materialization_seconds": 0.0,
        }
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._produce, name="subtitle-ocr-prefetch", daemon=True
        )
        self._started = False
        self._error: BaseException | None = None
        self.decoded_frames = 0
        self.peak_bytes = 0
        self.peak_frames = 0
        self.producer_full_seconds = 0.0
        self.consumer_wait_seconds = 0.0
        self._done = False

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def _put(self, item: tuple[int, np.ndarray]) -> bool:
        wait_started: float | None = None
        with self._condition:
            while not self._stop.is_set():
                bytes_available = (
                    self._queued_bytes + item[1].nbytes <= FRAME_PREFETCH_MAX_BYTES
                )
                if len(self._frames) < FRAME_PREFETCH_MAX_FRAMES and bytes_available:
                    self._frames.append(item)
                    self._queued_bytes += item[1].nbytes
                    self.peak_bytes = max(self.peak_bytes, self._queued_bytes)
                    self.peak_frames = max(self.peak_frames, len(self._frames))
                    self._condition.notify_all()
                    if wait_started is not None:
                        self.producer_full_seconds += time.perf_counter() - wait_started
                    return True
                if wait_started is None:
                    wait_started = time.perf_counter()
                self._condition.wait(timeout=0.1)
            if wait_started is not None:
                self.producer_full_seconds += time.perf_counter() - wait_started
            return False

    def _produce(self) -> None:
        try:
            first_pts_ms: int | None = None
            frame_iterator = iter(self.container.decode(video=0))
            while not self._stop.is_set():
                try:
                    frame = _next_decoded_frame(
                        frame_iterator, self._producer_timings
                    )
                except StopIteration:
                    break
                self.decoded_frames += 1
                if frame.pts is not None and self.video_stream.time_base is not None:
                    current_pts_ms = round(
                        float(frame.pts * self.video_stream.time_base * 1000)
                    )
                elif getattr(frame, "time", None) is not None:
                    current_pts_ms = round(float(frame.time * 1000))
                else:
                    raise SubtitleOcrError(
                        "Video thiếu PTS; không thể xác định thời gian phụ đề."
                    )
                if first_pts_ms is None:
                    first_pts_ms = current_pts_ms
                current_pts_ms -= first_pts_ms

                started = time.perf_counter()
                crop = self.cropper(frame)
                _add_timing(
                    self._producer_timings,
                    "image_materialization_seconds",
                    time.perf_counter() - started,
                )
                if not self._put((current_pts_ms, crop)):
                    return
        except BaseException as exc:
            self._error = exc
        finally:
            with self._condition:
                self._done = True
                self._condition.notify_all()

    def get(self, context: Any | None) -> tuple[int, np.ndarray] | None:
        while True:
            if context:
                context.raise_if_canceled()
            with self._condition:
                if self._frames:
                    item = self._frames.popleft()
                    self._queued_bytes -= item[1].nbytes
                    self._condition.notify_all()
                    return item
                if self._done:
                    if self._error is not None:
                        raise self._error
                    return None
                started = time.perf_counter()
                self._condition.wait(timeout=0.1)
                self.consumer_wait_seconds += time.perf_counter() - started

    def close(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._started:
            self._thread.join()
        for key, elapsed in self._producer_timings.items():
            _add_timing(self.timings, key, elapsed)
        _add_timing(self.timings, "frame_queue_wait_seconds", self.consumer_wait_seconds)
        _add_timing(
            self.timings, "frame_queue_full_seconds", self.producer_full_seconds
        )


def _display_crop(
    frame: Any,
    rotation: int,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
) -> np.ndarray:
    """Materialize an owned ROI crop so queued frames do not retain full BGR images."""
    image = frame.to_ndarray(format="bgr24")
    if rotation % 360:
        image = np.rot90(image, k=(rotation % 360) // 90)
    return image[y1:y2, x1:x2].copy(order="C")


def _extract_crop_text(
    engine: Any, crop_bgr: np.ndarray
) -> tuple[str, float, tuple[tuple[int, int, int, int], ...]]:
    """Run RapidOCR on cropped frame, sort multi-line text vertically, and return (text, confidence)."""
    try:
        lock_started = time.perf_counter()
        with _INFERENCE_LOCK:
            inference_started = time.perf_counter()
            active_timings = _ACTIVE_OCR_TIMINGS.get()
            if active_timings is not None:
                timings, inference_key = active_timings
                _add_timing(
                    timings,
                    "inference_lock_wait_seconds",
                    inference_started - lock_started,
                )
            try:
                results, stage_elapsed = engine(crop_bgr)
                _record_rapidocr_stage_timings(
                    active_timings[0] if active_timings is not None else None,
                    stage_elapsed,
                )
            finally:
                if active_timings is not None:
                    _add_timing(
                        timings, inference_key, time.perf_counter() - inference_started
                    )
    except Exception as exc:
        raise SubtitleOcrError(f"RapidOCR không đọc được khung hình: {exc}") from exc

    return _format_ocr_results(results)


def _format_ocr_results(
    results: Any,
) -> tuple[str, float, tuple[tuple[int, int, int, int], ...]]:
    if not results:
        return "", 0.0, ()

    valid_lines: list[
        tuple[float, float, str, float, tuple[float, float, float, float]]
    ] = []
    for item in results:
        # RapidOCR format: [box, text, confidence] where confidence can be str or float
        box = item[0]
        text = str(item[1]).strip()
        try:
            conf = float(item[2])
        except (ValueError, TypeError):
            conf = 0.5

        if not text or conf < 0.25:
            continue

        # Top-left y for sorting lines vertically
        top_y = min(p[1] for p in box)
        left_x = min(p[0] for p in box)
        valid_lines.append(
            (
                top_y,
                left_x,
                text,
                conf,
                (
                    min(p[0] for p in box),
                    min(p[1] for p in box),
                    max(p[0] for p in box),
                    max(p[1] for p in box),
                ),
            )
        )

    if not valid_lines:
        # Text was detected but could not be read; this is not evidence of a blank frame.
        return "", -1.0, ()

    # Cluster by measured box height, so ordering works across resolutions and crop sizes.
    heights = [
        max(p[1] for p in item[0]) - min(p[1] for p in item[0]) for item in results
    ]
    line_tolerance = max(1.0, float(np.median(heights)) * 0.5)
    rows: list[list[tuple[float, float, str, float, tuple[float, float, float, float]]]] = []
    for line in sorted(valid_lines, key=lambda item: (item[0], item[1])):
        if rows and abs(line[0] - rows[-1][0][0]) <= line_tolerance:
            rows[-1].append(line)
        else:
            rows.append([line])
    joined_text = "\n".join(
        " ".join(part[2] for part in sorted(row, key=lambda item: item[1]))
        for row in rows
    )
    avg_conf = float(np.mean([line[3] for line in valid_lines]))
    row_boxes = tuple(
        (
            int(np.floor(min(line[4][0] for line in row))),
            int(np.floor(min(line[4][1] for line in row))),
            int(np.ceil(max(line[4][2] for line in row))),
            int(np.ceil(max(line[4][3] for line in row))),
        )
        for row in rows
    )
    return joined_text, avg_conf, row_boxes


def _extract_crop_text_with_line_cache(
    engine: Any,
    crop_bgr: np.ndarray,
    previous_lines: list[_OcrLineCacheEntry],
) -> tuple[str, float, tuple[tuple[int, int, int, int], ...], int]:
    """Detect every frame, reusing recognition only for identical detected line crops."""
    active_timings = _ACTIVE_OCR_TIMINGS.get()
    lock_started = time.perf_counter()
    with _INFERENCE_LOCK:
        inference_started = time.perf_counter()
        if active_timings is not None:
            timings, inference_key = active_timings
            _add_timing(
                timings,
                "inference_lock_wait_seconds",
                inference_started - lock_started,
            )
        try:
            required_methods = (
                "text_detector",
                "sorted_boxes",
                "get_crop_img_list",
                "text_recognizer",
                "filter_boxes_rec_by_score",
            )
            if not _line_cache_api_supported() or not getattr(
                engine, "use_text_det", False
            ) or any(not hasattr(engine, name) for name in required_methods) or (
                getattr(engine, "use_angle_cls", False)
                and not hasattr(engine, "text_cls")
            ):
                previous_lines.clear()
                results, stage_elapsed = engine(crop_bgr)
                _record_rapidocr_stage_timings(
                    timings if active_timings is not None else None, stage_elapsed
                )
                return (*_format_ocr_results(results), 0)

            detected_boxes, detector_elapsed = engine.text_detector(crop_bgr)
            _record_rapidocr_stage_timings(
                timings if active_timings is not None else None,
                (detector_elapsed, 0.0, 0.0),
            )
            if detected_boxes is None or len(detected_boxes) == 0:
                previous_lines.clear()
                return "", 0.0, (), 0

            detected_boxes = engine.sorted_boxes(detected_boxes)
            line_crops = engine.get_crop_img_list(crop_bgr, detected_boxes)
            if len(line_crops) != len(detected_boxes):
                previous_lines.clear()
                results, stage_elapsed = engine(crop_bgr)
                _record_rapidocr_stage_timings(
                    timings if active_timings is not None else None, stage_elapsed
                )
                return (*_format_ocr_results(results), 0)

            recognized: list[tuple[str, float] | None] = [None] * len(line_crops)
            consumed: set[int] = set()
            pending_indices: list[int] = []
            cache_hits = 0
            for index, (box, line_crop) in enumerate(zip(detected_boxes, line_crops)):
                matched = None
                for previous_index, previous in enumerate(previous_lines):
                    if previous_index in consumed:
                        continue
                    if np.array_equal(box, previous.box) and np.array_equal(
                        line_crop, previous.crop
                    ):
                        matched = previous_index
                        break
                if matched is None:
                    pending_indices.append(index)
                    continue
                consumed.add(matched)
                previous = previous_lines[matched]
                recognized[index] = (previous.text, previous.confidence)
                cache_hits += 1

            if pending_indices:
                pending_crops = [line_crops[index] for index in pending_indices]
                if getattr(engine, "use_angle_cls", False):
                    pending_crops, _, classifier_elapsed = engine.text_cls(
                        pending_crops
                    )
                    _record_rapidocr_stage_timings(
                        timings if active_timings is not None else None,
                        (0.0, classifier_elapsed, 0.0),
                    )
                rec_results, recognizer_elapsed = engine.text_recognizer(
                    pending_crops
                )
                _record_rapidocr_stage_timings(
                    timings if active_timings is not None else None,
                    (0.0, 0.0, recognizer_elapsed),
                )
                if len(rec_results) != len(pending_indices):
                    previous_lines.clear()
                    results, stage_elapsed = engine(crop_bgr)
                    _record_rapidocr_stage_timings(
                        timings if active_timings is not None else None,
                        stage_elapsed,
                    )
                    return (*_format_ocr_results(results), cache_hits)
                for index, rec_result in zip(pending_indices, rec_results):
                    try:
                        text = str(rec_result[0]).strip()
                        confidence = float(rec_result[1])
                    except (IndexError, TypeError, ValueError):
                        text, confidence = "", 0.0
                    recognized[index] = (text, confidence)

            current_lines: list[_OcrLineCacheEntry] = []
            cache_bytes = 0
            for index, (box, line_crop) in enumerate(zip(detected_boxes, line_crops)):
                text, confidence = recognized[index] or ("", 0.0)
                current_lines.append(
                    _OcrLineCacheEntry(
                        box=np.asarray(box).copy(),
                        crop=line_crop,
                        text=text,
                        confidence=confidence,
                    )
                )
                cache_bytes += line_crop.nbytes
            if len(current_lines) <= 128 and cache_bytes <= 4 * 1024 * 1024:
                previous_lines[:] = current_lines
            else:
                previous_lines.clear()

            score_threshold = float(getattr(engine, "text_score", 0.0))
            results = []
            for box, result in zip(detected_boxes, recognized):
                text, confidence = result or ("", 0.0)
                if confidence >= score_threshold:
                    results.append([box.tolist(), text, str(confidence)])
            return (*_format_ocr_results(results), cache_hits)
        finally:
            if active_timings is not None:
                _add_timing(
                    timings, inference_key, time.perf_counter() - inference_started
                )


def _timed_extract_crop_text(
    engine: Any,
    crop_bgr: np.ndarray,
    timings: dict[str, float],
    inference_key: str,
    *,
    line_cache: list[_OcrLineCacheEntry] | None = None,
) -> tuple[str, float, tuple[tuple[int, int, int, int], ...], int]:
    token = _ACTIVE_OCR_TIMINGS.set((timings, inference_key))
    started = time.perf_counter()
    try:
        if line_cache is not None:
            return _extract_crop_text_with_line_cache(engine, crop_bgr, line_cache)
        return (*_extract_crop_text(engine, crop_bgr), 0)
    except SubtitleOcrError:
        raise
    except Exception as exc:
        raise SubtitleOcrError(f"RapidOCR không đọc được khung hình: {exc}") from exc
    finally:
        _add_timing(
            timings, "ocr_call_wall_seconds", time.perf_counter() - started
        )
        _ACTIVE_OCR_TIMINGS.reset(token)


@dataclass
class _RefinementReading:
    text: str
    confidence: float
    boxes: tuple[tuple[int, int, int, int], ...]
    full_detection: bool


def _recognition_groups(crops: list[np.ndarray], batch_size: int, image_shape: Any):
    """Bucket by aspect ratio and bound the float32 input AFTER padding."""
    channels, height, _ = (int(value) for value in image_shape)
    limit_bytes = 8 * 1024 * 1024
    indices = sorted(range(len(crops)), key=lambda i: crops[i].shape[1] / crops[i].shape[0])
    group: list[int] = []
    first_ratio = 0.0
    for index in indices:
        ratio = crops[index].shape[1] / crops[index].shape[0]
        width = max(1, int(np.ceil(height * ratio)))
        item_bytes = channels * height * width * 4
        if item_bytes > limit_bytes:
            raise SubtitleOcrError("Dòng OCR quá rộng cho giới hạn tensor 8 MiB; hãy thu hẹp vùng OCR.")
        if group and (
            len(group) >= batch_size
            or ratio > first_ratio * 1.5
            or item_bytes * (len(group) + 1) > limit_bytes
        ):
            yield group
            group = []
        if not group:
            first_ratio = ratio
        group.append(index)
    if group:
        yield group


def _read_refinement_batch(
    engine: Any,
    frames: list[tuple[int, np.ndarray, VisualReading | None]],
    timings: dict[str, float],
    options: OcrAcceleration,
    counters: dict[str, int],
    context: Any | None,
) -> list[_RefinementReading]:
    """Detect per frame, then batch CLS/REC across independently selected frames."""
    required = ("text_detector", "sorted_boxes", "get_crop_img_list", "text_recognizer")
    supported = (
        _line_cache_api_supported()
        and bool(getattr(engine, "use_text_det", False))
        and all(hasattr(engine, name) for name in required)
        and (not getattr(engine, "use_angle_cls", False) or hasattr(engine, "text_cls"))
    )
    if not supported:
        fallback_results = []
        for _, image, _ in frames:
            if context:
                context.raise_if_canceled()
            fallback_results.append(
                _RefinementReading(
                    *_timed_extract_crop_text(engine, image, timings, "refinement_inference_seconds")[:3],
                    full_detection=True,
                )
            )
        return fallback_results

    started = time.perf_counter()
    fallback: list[int] = []
    results: list[Any] = [None] * len(frames)
    metadata: list[tuple[int, Any, int, int, bool]] = []
    crops: list[np.ndarray] = []
    crop_bytes = 0
    try:
        with _INFERENCE_LOCK:
            inference_started = time.perf_counter()
            _add_timing(timings, "inference_lock_wait_seconds", inference_started - started)
            try:
                for index, (_, image, geometry) in enumerate(frames):
                    if context:
                        context.raise_if_canceled()
                    if geometry is not None:
                        boxes = np.asarray([
                            [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
                            for x1, y1, x2, y2 in geometry.padded_boxes
                        ], dtype=np.float32)
                        counters["recognition_reuse_attempts"] += 1
                    else:
                        boxes, elapsed = engine.text_detector(image)
                        counters["refinement_detector_calls"] += 1
                        _record_rapidocr_stage_timings(timings, (elapsed, 0.0, 0.0))
                        if boxes is None or len(boxes) == 0:
                            results[index] = _RefinementReading("", 0.0, (), True)
                            continue
                        boxes = engine.sorted_boxes(boxes)
                    if len(boxes) > 128:
                        fallback.append(index)
                        continue
                    line_crops = engine.get_crop_img_list(image, boxes)
                    added_bytes = sum(crop.nbytes for crop in line_crops)
                    if len(line_crops) != len(boxes) or crop_bytes + added_bytes > FRAME_PREFETCH_MAX_BYTES:
                        fallback.append(index)
                        continue
                    crop_bytes += added_bytes
                    offset = len(crops)
                    crops.extend(line_crops)
                    metadata.append((index, boxes, offset, len(line_crops), geometry is not None))

                recognized: list[Any] = [None] * len(crops)
                # Batch size limits LINE crops as well as frames. Never wait for
                # more work at EOF or when tracking requires this group's result.
                for indices in _recognition_groups(
                    crops, options.refinement_batch_size,
                    getattr(engine.text_recognizer, "rec_image_shape", (3, 48, 320)),
                ):
                    if context:
                        context.raise_if_canceled()
                    batch = [crops[index] for index in indices]
                    if getattr(engine, "use_angle_cls", False):
                        batch, _, elapsed = engine.text_cls(batch)
                        _record_rapidocr_stage_timings(timings, (0.0, elapsed, 0.0))
                    readings, elapsed = engine.text_recognizer(batch)
                    _record_rapidocr_stage_timings(timings, (0.0, 0.0, elapsed))
                    if len(readings) != len(batch):
                        raise SubtitleOcrError("OCR batch trả số dòng không khớp với ảnh đầu vào.")
                    counters["refinement_rec_batches"] += 1
                    counters["refinement_rec_crops"] += len(batch)
                    for index, reading in zip(indices, readings):
                        recognized[index] = reading

                for index, boxes, offset, count, reused in metadata:
                    readings = recognized[offset:offset + count]
                    if reused and any(
                        not str(reading[0]).strip() or float(reading[1]) < options.min_reuse_confidence
                        for reading in readings
                    ):
                        fallback.append(index)
                        counters["recognition_reuse_fallbacks"] += 1
                        continue
                    score_threshold = float(getattr(engine, "text_score", 0.0))
                    lines = [
                        [box.tolist(), str(reading[0]), float(reading[1])]
                        for box, reading in zip(boxes, readings)
                        if float(reading[1]) >= score_threshold
                    ]
                    results[index] = _RefinementReading(*_format_ocr_results(lines), full_detection=not reused)
                    if reused:
                        counters["recognition_reuse_hits"] += 1
            finally:
                _add_timing(timings, "refinement_inference_seconds", time.perf_counter() - inference_started)
    except SubtitleOcrError:
        raise
    except Exception as exc:
        # Let supervisor-owned cancellation retain its type and bypass retry.
        if context:
            context.raise_if_canceled()
        raise SubtitleOcrError(f"RapidOCR không đọc được batch khung hình: {exc}") from exc
    finally:
        _add_timing(timings, "ocr_call_wall_seconds", time.perf_counter() - started)
    # Do not recursively acquire the inference lock on the fallback path.
    for index in fallback:
        if context:
            context.raise_if_canceled()
        counters["refinement_full_retries"] += 1
        results[index] = _RefinementReading(
            *_timed_extract_crop_text(
                engine, frames[index][1], timings, "refinement_inference_seconds"
            )[:3],
            full_detection=True,
        )
    return results


def _refine_visual_window(
    engine: Any,
    frames: list[tuple[int, np.ndarray]],
    anchors: list[VisualReading],
    timings: dict[str, float],
    options: OcrAcceleration,
    counters: dict[str, int],
    context: Any | None,
    feature_cache: FeatureCache | None = None,
) -> list[_RefinementReading]:
    """Resolve a bounded window in display order, without binary-search assumptions."""
    resolved: list[_RefinementReading] = []
    feature_cache = feature_cache if feature_cache is not None else FeatureCache()
    offset = 0
    while offset < len(frames):
        if context:
            context.raise_if_canceled()
        group = []
        group_bytes = 0
        while offset < len(frames) and len(group) < options.refinement_batch_size:
            item = frames[offset]
            if group and group_bytes + item[1].nbytes > FRAME_PREFETCH_MAX_BYTES:
                break
            group.append(item)
            group_bytes += item[1].nbytes
            offset += 1
        outputs: list[_RefinementReading | None] = [None] * len(group)
        pending = []
        pending_indices = []
        duplicate_of: dict[int, int] = {}
        tracking_started = time.perf_counter()
        for index, (timestamp, image) in enumerate(group):
            # Inference is a function of pixels; reuse the result, NOT its PTS.
            # This also covers A -> blank -> A without merging occurrences.
            duplicate = next((
                previous for previous in range(index)
                if group[previous][1].shape == image.shape
                and np.array_equal(group[previous][1], image)
            ), None)
            if duplicate is not None:
                duplicate_of[index] = duplicate
                counters["refinement_duplicate_frames"] += 1
                continue
            current_anchors = [a for a in anchors if a.current(timestamp, options)]
            # No morphology or gray conversion for pixel-identical observations.
            matches = [a for a in current_anchors if a.exact_match(image)] if options.selective_refinement else []
            features = None
            if not matches and (options.selective_refinement or options.recognition_reuse):
                if any(a.features is not None for a in current_anchors):
                    features = feature_cache.get(image)
                if options.selective_refinement:
                    matches = [a for a in current_anchors if a.matches(image, features)]
            # Ambiguous signatures must not choose whichever anchor was first.
            match = matches[0] if matches and len({a.text for a in matches}) == 1 else None
            if options.selective_refinement and match is not None:
                outputs[index] = _RefinementReading(match.text, match.confidence, match.boxes, False)
                counters["visual_cache_hits"] += 1
                continue
            geometry = None
            if options.recognition_reuse:
                geometry = next((a for a in current_anchors if a.reusable_geometry(features)), None)
            pending.append((timestamp, image, geometry))
            pending_indices.append(index)
        _add_timing(timings, "tracking_update_seconds", time.perf_counter() - tracking_started)
        readings = _read_refinement_batch(engine, pending, timings, options, counters, context) if pending else []
        counters["visual_refinement_calls"] += len(pending)
        tracking_started = time.perf_counter()
        for index, (timestamp, image, geometry), reading in zip(pending_indices, pending, readings):
            # Separate text recognition time from detector verification time.
            # Full fallback is a fresh verified anchor, even after a REC attempt.
            if reading.full_detection:
                boxes = reading.boxes
                verified = timestamp
            else:
                assert geometry is not None
                boxes = geometry.boxes  # Avoid growing padding on every reuse.
                verified = geometry.verified_timestamp_ms
            outputs[index] = replace(reading, boxes=boxes)
            anchors.append(VisualReading.create(
                timestamp, image, reading.text, reading.confidence, boxes, options,
                feature_cache=feature_cache, verified_timestamp_ms=verified,
            ))
        # Ascending indices also resolve chains of duplicate references.
        for index, previous in duplicate_of.items():
            outputs[index] = outputs[previous]
        _add_timing(timings, "tracking_update_seconds", time.perf_counter() - tracking_started)
        # At most two endpoint anchors plus two newly verified observations.
        if len(anchors) > 4:
            anchors[:] = anchors[:2] + anchors[-2:]
        resolved.extend(output for output in outputs if output is not None)
        if len(resolved) != offset:
            raise SubtitleOcrError("OCR refinement thiếu kết quả khung hình.")
    return resolved


def detect_dominant_script(text: str) -> str:
    """Detect script type: 'zh' (CJK), 'ko' (Hangul), 'ja' (Kana), or 'latin'."""
    zh_count = sum(1 for c in text if 0x4E00 <= ord(c) <= 0x9FFF)
    ko_count = sum(1 for c in text if 0xAC00 <= ord(c) <= 0xD7AF)
    ja_count = sum(
        1 for c in text if (0x3040 <= ord(c) <= 0x309F) or (0x30A0 <= ord(c) <= 0x30FF)
    )
    latin_count = sum(
        1
        for c in text
        if (0x41 <= ord(c) <= 0x5A)
        or (0x61 <= ord(c) <= 0x7A)
        or (0xC0 <= ord(c) <= 0x1EF9)
    )

    total = zh_count + ko_count + ja_count + latin_count
    if total == 0:
        return "zh"
    counts = [
        ("zh", zh_count),
        ("ko", ko_count),
        ("ja", ja_count),
        ("latin", latin_count),
    ]
    counts.sort(key=lambda item: item[1], reverse=True)
    return counts[0][0]


def _display_image(frame: Any, rotation: int) -> np.ndarray:
    image = frame.to_ndarray(format="bgr24")
    return (
        np.ascontiguousarray(np.rot90(image, k=(rotation % 360) // 90))
        if rotation % 360
        else image
    )


def probe_subtitle_y_band(
    video_path: Path,
    media: dict[str, Any],
    *,
    n_probes: int = 15,
    context: Any | None = None,
    timings: dict[str, float] | None = None,
) -> tuple[SubtitleOcrRegion | None, str]:
    """Find a repeated text band; return no region when observations do not support one."""
    duration_ms = int(media.get("duration_ms") or 0)
    if duration_ms <= 0:
        return None, "und"
    targets = np.linspace(
        duration_ms * 0.1, duration_ms * 0.9, max(2, min(n_probes, 30))
    )
    timings = timings if timings is not None else _empty_ocr_timings()
    engine_started = time.perf_counter()
    engine = _get_ocr_engine()
    _add_timing(
        timings, "engine_acquire_seconds", time.perf_counter() - engine_started
    )
    observations: list[tuple[int, float, float, float, float, str]] = []
    container = None
    try:
        container = av.open(str(video_path))
        if not container.streams.video:
            raise SubtitleOcrError("Video không chứa luồng hình ảnh.")
        stream = container.streams.video[0]
        origin = None
        index = 0
        frame_iterator = iter(container.decode(video=0))
        while True:
            if context:
                context.raise_if_canceled()
            if index >= len(targets):
                break
            try:
                frame = _next_decoded_frame(frame_iterator, timings)
            except StopIteration:
                break
            if frame.pts is None or stream.time_base is None:
                raise SubtitleOcrError("Video thiếu PTS; không thể dò vùng phụ đề.")
            timestamp = round(float(frame.pts * stream.time_base * 1000))
            if origin is None:
                origin = timestamp
            if timestamp - origin < targets[index]:
                continue
            materialization_started = time.perf_counter()
            image = _display_image(frame, int(media.get("rotation") or 0))
            height, width = image.shape[:2]
            _add_timing(
                timings,
                "image_materialization_seconds",
                time.perf_counter() - materialization_started,
            )
            lock_started = time.perf_counter()
            with _INFERENCE_LOCK:
                inference_started = time.perf_counter()
                _add_timing(
                    timings,
                    "inference_lock_wait_seconds",
                    inference_started - lock_started,
                )
                try:
                    results, _ = engine(image)
                finally:
                    _add_timing(
                        timings,
                        "probe_inference_seconds",
                        time.perf_counter() - inference_started,
                    )
            for box, text, confidence in results or []:
                if (
                    not isinstance(text, str)
                    or len(text.strip()) < 2
                    or float(confidence) < 0.5
                ):
                    continue
                xs, ys = [point[0] for point in box], [point[1] for point in box]
                observations.append(
                    (
                        index,
                        min(xs) / width * 100,
                        max(xs) / width * 100,
                        min(ys) / height * 100,
                        max(ys) / height * 100,
                        text.strip(),
                    )
                )
            index += 1
    except av.error.FFmpegError as exc:
        raise SubtitleOcrError(f"Lỗi đọc video khi dò vùng OCR: {exc}") from exc
    finally:
        if container is not None:
            container.close()
    # A static logo alone is insufficient evidence of a subtitle band.
    bands: dict[int, list[tuple]] = {}
    for item in observations:
        bands.setdefault(round(((item[3] + item[4]) / 2) / 8), []).append(item)
    supported = [
        band
        for band in bands.values()
        if len({row[0] for row in band}) >= 2 and len({row[5] for row in band}) >= 2
    ]
    if not supported:
        return None, "und"
    best = max(supported, key=lambda rows: (len({row[0] for row in rows}), len(rows)))
    # Include nearby second lines found on the same sampled frames.
    low, high = min(row[3] for row in best), max(row[4] for row in best)
    adjacent = [row for row in observations if row[4] >= low - 6 and row[3] <= high + 6]
    x = min(95.0, max(0.0, min(row[1] for row in adjacent) - 3))
    y = min(95.0, max(0.0, min(row[3] for row in adjacent) - 2.5))
    right = min(100.0, max(row[2] for row in adjacent) + 3)
    bottom = min(100.0, max(row[4] for row in adjacent) + 2.5)
    script = detect_dominant_script(" ".join(row[5] for row in adjacent))
    return SubtitleOcrRegion(
        x=x, y=y, width=max(5.0, right - x), height=max(5.0, bottom - y)
    ), script


@with_subtitle_cache
def extract_subtitles_ocr(
    video_path: Path,
    region: SubtitleOcrRegion | None = None,
    media: dict[str, Any] | None = None,
    *,
    source_language: str | None = None,
    sample_fps: float = 5.0,
    min_duration_ms: int = 300,
    max_gap_ms: int = 250,
    auto_probe: bool = False,
    prefetch_frames: bool = True,
    glyph_cache: bool = True,
    acceleration: OcrAcceleration | None = None,
    cache_dir: Path | None = None,
    context: Any | None = None,
) -> dict[str, Any]:
    """Extract source subtitles from hardcoded on-screen text in video using RapidOCR."""
    started_at = time.monotonic()
    acceleration = OcrAcceleration.model_validate(acceleration or {})
    if not video_path.exists():
        raise SubtitleOcrError(f"Video file not found: {video_path}")

    media = dict(media or {})
    if not (media.get("fingerprint") or media.get("video_id")):
        with video_path.open("rb") as source:
            media["fingerprint"] = hashlib.file_digest(source, "sha256").hexdigest()
    if context:
        context.raise_if_canceled()

    timings = _empty_ocr_timings()
    cache_lookup_started = time.perf_counter()

    # Check cache
    key = ocr_cache_key(
        media,
        region or SubtitleOcrRegion(),
        source_language=source_language,
        sample_fps=sample_fps,
        min_duration_ms=min_duration_ms,
        max_gap_ms=max_gap_ms,
        auto_probe=auto_probe or region is None,
        prefetch_frames=prefetch_frames,
        glyph_cache=glyph_cache,
        acceleration=acceleration,
    )
    cache_file = cache_dir / f"ocr_{key}.json" if cache_dir else None
    if cache_file and cache_file.is_file():
        try:
            cached_data = cached_json(cache_file)
            if (
                cached_data.get("version") == OCR_ALGORITHM_VERSION
                and cached_data.get("cache_key") == key
            ):
                SubtitleDocumentV2.model_validate(cached_data["document"])
                previous_metrics = cached_data.get("metrics")
                if not isinstance(previous_metrics, dict):
                    previous_metrics = {}
                timings["cache_lookup_seconds"] = time.perf_counter() - cache_lookup_started
                if context:
                    context.update(
                        100, "completed", "Trích xuất phụ đề hoàn tất (từ bộ nhớ đệm)"
                    )
                return {
                    **cached_data,
                    "cache_hit": True,
                    "processing_seconds": time.monotonic() - started_at,
                    "cached_metrics": cached_data.get("metrics"),
                    "cached_timings_seconds": cached_data.get("timings_seconds"),
                    "timings_seconds": timings,
                    "metrics": {
                        "ocr_calls": 0,
                        "decoded_frames": 0,
                        "sampled_frames": 0,
                        "sample_ocr_calls": 0,
                        "ocr_observations": 0,
                        "sample_observations": 0,
                        "refinement_observations": 0,
                        "refinement_calls": 0,
                        "frame_cache_hits": 0,
                        "recognition_cache_hits": 0,
                        "visual_cache_hits": 0,
                        "visual_refinement_calls": 0,
                        "refinement_duplicate_frames": 0,
                        "visual_feature_builds": 0,
                        "visual_feature_cache_hits": 0,
                        "recognition_reuse_attempts": 0,
                        "recognition_reuse_hits": 0,
                        "recognition_reuse_fallbacks": 0,
                        "refinement_detector_calls": 0,
                        "refinement_rec_batches": 0,
                        "refinement_rec_crops": 0,
                        "refinement_full_retries": 0,
                        "stabilized_observations": 0,
                        "native_crop_frames": 0,
                        "bgr_crop_frames": 0,
                        "prefetch_peak_frames": 0,
                        "prefetch_peak_bytes": 0,
                        "prefetch_requested": prefetch_frames,
                        "prefetch_enabled": previous_metrics.get(
                            "prefetch_enabled", prefetch_frames
                        ),
                        "recognition_cache_enabled": glyph_cache,
                        "uncertain_frames": 0,
                        "peak_refinement_bytes": 0,
                        "refinement_limited": False,
                    },
                }
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.debug("Ignoring invalid OCR cache: %s", exc)
    timings["cache_lookup_seconds"] = time.perf_counter() - cache_lookup_started

    probe_warning = None
    if region is None or auto_probe:
        if context:
            context.update(2, "probing_y_band", "Đang tự động dò tìm dải phụ đề...")
        probe_started = time.perf_counter()
        probed_region, probed_script = probe_subtitle_y_band(
            video_path, media, context=context, timings=timings
        )
        timings["probe_seconds"] += time.perf_counter() - probe_started
        if probed_region is not None:
            region = probed_region
        else:
            region = region or SubtitleOcrRegion()
            probe_warning = {
                "code": "ocr_probe_uncertain",
                "message": "Chưa xác định được vùng phụ đề. Đã dùng vùng chọn hiện tại; hãy kiểm tra và điều chỉnh.",
            }
        if not source_language:
            source_language = "und" if probed_script == "latin" else probed_script

    if context:
        context.update(5, "loading_model", "Đang nạp mô hình OCR...")
        context.raise_if_canceled()

    engine_started = time.perf_counter()
    engine = _get_ocr_engine()
    _add_timing(
        timings, "engine_acquire_seconds", time.perf_counter() - engine_started
    )

    if context:
        context.update(10, "scanning_frames", "Đang phân tích khung hình video...")
        context.raise_if_canceled()

    container = None
    frame_reader: _PrefetchedCropReader | None = None
    frame_prefetch_metrics = {"peak_frames": 0, "peak_bytes": 0}
    candidates: list[_OcrCandidate] = []
    active_candidate: _OcrCandidate | None = None
    acceleration_counters = {
        "visual_cache_hits": 0,
        "visual_refinement_calls": 0,
        "refinement_duplicate_frames": 0,
        "recognition_reuse_attempts": 0,
        "recognition_reuse_hits": 0,
        "recognition_reuse_fallbacks": 0,
        "refinement_detector_calls": 0,
        "refinement_rec_batches": 0,
        "refinement_rec_crops": 0,
        "refinement_full_retries": 0,
    }
    accelerated_refinement = (
        acceleration.selective_refinement
        or acceleration.recognition_reuse
        or acceleration.refinement_batch_size > 1
    )

    duration_ms = int(media.get("duration_ms") or 0)
    sample_interval_ms = max(50, round(1000.0 / max(1.0, min(30.0, sample_fps))))

    try:
        container = av.open(str(video_path))
        if not container.streams.video:
            raise SubtitleOcrError("Video không chứa luồng hình ảnh (no video stream)")

        video_stream = container.streams.video[0]
        if acceleration.decode_threads:
            video_stream.thread_type = "AUTO"
            video_stream.codec_context.thread_count = acceleration.decode_threads
        orig_w = int(video_stream.width or media.get("width") or 1280)
        orig_h = int(video_stream.height or media.get("height") or 720)
        rotation = int(media.get("rotation") or 0) % 360
        if rotation in (90, 270):
            orig_w, orig_h = orig_h, orig_w

        # Compute crop pixel coordinates clamped to video bounds
        x1 = max(0, min(orig_w - 1, round(region.x / 100.0 * orig_w)))
        y1 = max(0, min(orig_h - 1, round(region.y / 100.0 * orig_h)))
        x2 = max(x1 + 4, min(orig_w, round((region.x + region.width) / 100.0 * orig_w)))
        y2 = max(
            y1 + 4, min(orig_h, round((region.y + region.height) / 100.0 * orig_h))
        )

        if x2 <= x1 or y2 <= y1:
            raise SubtitleOcrError("Vùng chọn OCR không hợp lệ hoặc quá nhỏ")

        crop_storage_bytes = (x2 - x1) * (y2 - y1) * 3
        cropper = _DisplayCropper(rotation, (x1, y1, x2, y2), acceleration.crop_before_bgr)
        prefetch_active = (
            prefetch_frames and crop_storage_bytes <= FRAME_PREFETCH_MAX_BYTES
        )
        next_sample_target_ms = 0
        first_pts_ms: int | None = None
        previous_sample_ms: int | None = None
        observed_precision_ms = sample_interval_ms
        ocr_calls = 0
        sample_ocr_calls = 0
        ocr_observations = 0
        sample_observations = 0
        refinement_observations = 0
        cache_hits = 0
        uncertain_frames = 0
        decoded_frames = 0
        sampled_frames = 0
        prev_crop: np.ndarray | None = None
        prev_text = ""
        prev_conf = 0.0
        prev_boxes: tuple[tuple[int, int, int, int], ...] = ()
        previous_reading: VisualReading | None = None
        feature_cache = FeatureCache()
        sample_line_cache: list[_OcrLineCacheEntry] = []
        recognition_cache_hits = 0
        reuse_count = 0
        buffered: deque[tuple[int, np.ndarray]] = deque()
        buffered_bytes = 0
        peak_buffer_bytes = 0
        refinement_calls = 0
        refinement_limited = False

        def observe(
            timestamp: int, text: str, confidence: float, end: int,
            image: np.ndarray, boxes: tuple[tuple[int, int, int, int], ...],
        ) -> None:
            nonlocal active_candidate, uncertain_frames
            if confidence < 0:
                uncertain_frames += 1
                if (
                    active_candidate is not None
                    and timestamp - active_candidate.end_ms >= max_gap_ms
                ):
                    active_candidate.continuity = None
                    candidates.append(active_candidate)
                    active_candidate = None
                return
            if not text:
                if active_candidate is not None:
                    # A confirmed blank is a real display boundary, even if text repeats later.
                    active_candidate.end_ms = min(active_candidate.end_ms, timestamp)
                    active_candidate.continuity = None
                    candidates.append(active_candidate)
                    active_candidate = None
                return
            exact = active_candidate is not None and unicodedata.normalize(
                "NFC", active_candidate.best_text
            ) == unicodedata.normalize("NFC", text)
            stable = (
                active_candidate is not None and not exact
                and len(active_candidate.votes) < 16
                and active_candidate.continuity is not None
                and active_candidate.continuity.matches(text, image, boxes)
            )
            if active_candidate and (exact or stable):
                active_candidate.vote_until(timestamp)
                active_candidate.last_text = text
                active_candidate.last_confidence = confidence
                active_candidate.end_ms = end
                active_candidate.frame_count += 1
                active_candidate.confidence_sum += confidence
                active_candidate.stabilized_frames += int(stable)
                if not active_candidate.continuity_attempted and boxes:
                    active_candidate.continuity = CaptionContinuity.create(
                        text, image, boxes,
                    )
                    active_candidate.continuity_attempted = True
                return
            if active_candidate:
                active_candidate.end_ms = min(active_candidate.end_ms, timestamp)
                active_candidate.continuity = None
                candidates.append(active_candidate)
            if len(candidates) >= 20000:
                raise SubtitleOcrError(
                    "Video vượt giới hạn 20.000 phụ đề; hãy chia video trước khi trích xuất."
                )
            active_candidate = _OcrCandidate(
                timestamp, end, text, confidence, confidence, 1,
                continuity=CaptionContinuity.create(text, image, boxes),
                continuity_attempted=bool(boxes),
            )

        def record_observation(
            timestamp: int, text: str, confidence: float, end: int,
            image: np.ndarray, boxes: tuple[tuple[int, int, int, int], ...],
        ) -> None:
            nonlocal ocr_observations
            observation_started = time.perf_counter()
            ocr_observations += 1
            try:
                observe(timestamp, text, confidence, end, image, boxes)
            finally:
                _add_timing(
                    timings,
                    "tracking_update_seconds",
                    time.perf_counter() - observation_started,
                )

        frame_iterator = None
        if prefetch_active:
            frame_reader = _PrefetchedCropReader(
                container,
                video_stream,
                rotation=rotation,
                bounds=(x1, y1, x2, y2),
                timings=timings,
                cropper=cropper,
            )
            frame_reader.start()
        else:
            frame_iterator = iter(container.decode(video=0))
        while True:
            if context:
                context.raise_if_canceled()

            if frame_reader is not None:
                prefetched = frame_reader.get(context)
                if prefetched is None:
                    break
                current_pts_ms, crop = prefetched
                decoded_frames += 1
            else:
                try:
                    frame = _next_decoded_frame(frame_iterator, timings)
                except StopIteration:
                    break

                decoded_frames += 1
                # The media probe and preview index use the first video PTS as origin.
                if frame.pts is not None and video_stream.time_base is not None:
                    current_pts_ms = round(
                        float(frame.pts * video_stream.time_base * 1000)
                    )
                elif getattr(frame, "time", None) is not None:
                    current_pts_ms = round(float(frame.time * 1000))
                else:
                    raise SubtitleOcrError(
                        "Video thiếu PTS; không thể xác định thời gian phụ đề."
                    )
                if first_pts_ms is None:
                    first_pts_ms = current_pts_ms
                current_pts_ms -= first_pts_ms

                # Keep only a bounded interval of crops for boundary refinement.
                materialization_started = time.perf_counter()
                crop = cropper(frame)
                _add_timing(
                    timings,
                    "image_materialization_seconds",
                    time.perf_counter() - materialization_started,
                )
            if current_pts_ms < next_sample_target_ms:
                if crop.nbytes <= REFINEMENT_MAX_BYTES:
                    materialization_started = time.perf_counter()
                    buffered.append(
                        (current_pts_ms, crop)
                    )
                    buffered_bytes += crop.nbytes
                    while (
                        len(buffered) > REFINEMENT_MAX_FRAMES
                        or buffered_bytes > REFINEMENT_MAX_BYTES
                    ):
                        buffered_bytes -= buffered.popleft()[1].nbytes
                        refinement_limited = True
                    peak_buffer_bytes = max(peak_buffer_bytes, buffered_bytes)
                    _add_timing(
                        timings,
                        "image_materialization_seconds",
                        time.perf_counter() - materialization_started,
                    )
                else:
                    refinement_limited = True
                continue

            sampled_frames += 1
            if previous_sample_ms is not None:
                observed_precision_ms = max(
                    observed_precision_ms, current_pts_ms - previous_sample_ms
                )
            previous_sample_ms = current_pts_ms
            next_sample_target_ms = current_pts_ms + sample_interval_ms

            # Progress update
            if context and duration_ms > 0:
                pct = min(95, 10 + int((current_pts_ms / duration_ms) * 85))
                context.update(
                    pct,
                    "processing_ocr",
                    f"Đang đọc chữ trên khung hình ({current_pts_ms // 1000}s / {duration_ms // 1000}s)...",
                )

            # Only reuse an identical full crop. Sparse thumbnails can miss a changed digit.
            tracking_started = time.perf_counter()
            use_cached_frame_ocr = (
                prev_crop is not None
                and np.array_equal(crop, prev_crop)
                and reuse_count < TEXT_REGION_REUSE_LIMIT
            )
            reuse_count = reuse_count + 1 if use_cached_frame_ocr else 0
            last_text, last_conf, last_boxes = prev_text, prev_conf, prev_boxes
            _add_timing(
                timings,
                "tracking_update_seconds",
                time.perf_counter() - tracking_started,
            )

            if use_cached_frame_ocr:
                cache_hits += 1
                frame_text = prev_text
                frame_conf = prev_conf
                frame_boxes = prev_boxes
            else:
                ocr_calls += 1
                sample_ocr_calls += 1
                frame_text, frame_conf, frame_boxes, line_cache_hits = _timed_extract_crop_text(
                    engine,
                    crop,
                    timings,
                    "sample_inference_seconds",
                    line_cache=sample_line_cache if glyph_cache else None,
                )
                recognition_cache_hits += line_cache_hits
                prev_text = frame_text
                prev_conf = frame_conf
                prev_boxes = frame_boxes

            # Refine changed readings and stable intermediate displays, including A -> B -> A.
            tracking_started = time.perf_counter()
            stable_intermediate = any(
                np.array_equal(left[1], right[1])
                and (prev_crop is None or not np.array_equal(left[1], prev_crop))
                for left, right in zip(buffered, list(buffered)[1:])
            )
            current_reading = None
            visual_change = False
            if accelerated_refinement:
                if use_cached_frame_ocr and previous_reading is not None:
                    # Same pixels: preserve features and BOTH verification clocks.
                    current_reading = replace(previous_reading, image=crop)
                else:
                    current_reading = VisualReading.create(
                        current_pts_ms, crop, frame_text, frame_conf, frame_boxes, acceleration,
                        feature_cache=feature_cache,
                    )
                if (
                    acceleration.selective_refinement
                    and previous_reading is not None
                    and frame_text == last_text
                    and not stable_intermediate
                    and previous_reading.features is not None
                ):
                    # Inspect intermediate frames even when both endpoint texts
                    # agree, so A -> B -> A and a blank gap remain observable.
                    # Unknown/no-text signatures keep the legacy trigger below.
                    visual_change = any(
                        not previous_reading.current(timestamp, acceleration)
                        or (
                            not previous_reading.exact_match(image)
                            and not previous_reading.matches(image, feature_cache.get(image))
                        )
                        for timestamp, image in buffered
                    )
            _add_timing(
                timings,
                "tracking_update_seconds",
                time.perf_counter() - tracking_started,
            )
            should_refine = bool(buffered) and (frame_text != last_text or stable_intermediate or visual_change)
            if should_refine and accelerated_refinement:
                window = list(buffered)
                anchors = [
                    reading for reading in (previous_reading, current_reading)
                    if reading is not None
                ]
                before = (
                    acceleration_counters["visual_refinement_calls"]
                    + acceleration_counters["refinement_full_retries"]
                )
                readings = _refine_visual_window(
                    engine, window, anchors, timings, acceleration,
                    acceleration_counters, context,
                    feature_cache=feature_cache,
                )
                refinement_calls += (
                    acceleration_counters["visual_refinement_calls"]
                    + acceleration_counters["refinement_full_retries"] - before
                )
                for index, ((timestamp, image), reading) in enumerate(zip(window, readings)):
                    end = window[index + 1][0] if index + 1 < len(window) else current_pts_ms
                    refinement_observations += 1
                    record_observation(timestamp, reading.text, reading.confidence, end, image, reading.boxes)
            if should_refine and not accelerated_refinement:
                previous_image = prev_crop
                refinement_line_cache: list[_OcrLineCacheEntry] = []
                for index, (timestamp, image) in enumerate(buffered):
                    if context:
                        context.raise_if_canceled()
                    if previous_image is not None and np.array_equal(
                        image, previous_image
                    ):
                        text, confidence = last_text, last_conf
                        boxes = last_boxes
                    elif np.array_equal(image, crop):
                        text, confidence = frame_text, frame_conf
                        boxes = frame_boxes
                    else:
                        text, confidence, boxes, line_cache_hits = _timed_extract_crop_text(
                            engine,
                            image,
                            timings,
                            "refinement_inference_seconds",
                            line_cache=(
                                refinement_line_cache if glyph_cache else None
                            ),
                        )
                        recognition_cache_hits += line_cache_hits
                        refinement_calls += 1
                    end = (
                        buffered[index + 1][0]
                        if index + 1 < len(buffered)
                        else current_pts_ms
                    )
                    refinement_observations += 1
                    record_observation(timestamp, text, confidence, end, image, boxes)
                    previous_image, last_text, last_conf = image, text, confidence
                    last_boxes = boxes
            buffered.clear()
            feature_cache.clear()
            buffered_bytes = 0
            sample_observations += 1
            record_observation(
                current_pts_ms,
                frame_text,
                frame_conf,
                current_pts_ms + sample_interval_ms,
                crop, frame_boxes,
            )
            previous_reading = current_reading
            # Both crop paths return owned arrays; the decoder never reuses them.
            prev_crop = crop

        # Inspect the final partial sampling interval so a short trailing display is not lost.
        if buffered and accelerated_refinement:
            window = list(buffered)
            before = acceleration_counters["visual_refinement_calls"] + acceleration_counters["refinement_full_retries"]
            readings = _refine_visual_window(
                engine, window, [previous_reading] if previous_reading else [],
                timings, acceleration, acceleration_counters, context,
                feature_cache=feature_cache,
            )
            refinement_calls += (
                acceleration_counters["visual_refinement_calls"]
                + acceleration_counters["refinement_full_retries"] - before
            )
            for index, ((timestamp, image), reading) in enumerate(zip(window, readings)):
                end = window[index + 1][0] if index + 1 < len(window) else duration_ms or timestamp + sample_interval_ms
                refinement_observations += 1
                record_observation(timestamp, reading.text, reading.confidence, end, image, reading.boxes)
            buffered.clear()
        trailing_line_cache: list[_OcrLineCacheEntry] = []
        for index, (timestamp, image) in enumerate(buffered):
            if context:
                context.raise_if_canceled()
            if prev_crop is not None and np.array_equal(image, prev_crop):
                text, confidence = prev_text, prev_conf
                boxes = prev_boxes
            else:
                text, confidence, boxes, line_cache_hits = _timed_extract_crop_text(
                    engine,
                    image,
                    timings,
                    "refinement_inference_seconds",
                    line_cache=trailing_line_cache if glyph_cache else None,
                )
                recognition_cache_hits += line_cache_hits
                refinement_calls += 1
            end = (
                buffered[index + 1][0]
                if index + 1 < len(buffered)
                else duration_ms or timestamp + sample_interval_ms
            )
            refinement_observations += 1
            record_observation(timestamp, text, confidence, end, image, boxes)
            prev_crop, prev_text, prev_conf = image, text, confidence
            prev_boxes = boxes

        # Close lingering candidate at video end
        if active_candidate is not None:
            active_candidate.continuity = None
            candidates.append(active_candidate)
            active_candidate = None

    except av.error.FFmpegError as exc:
        raise SubtitleOcrError(f"Lỗi đọc video bằng PyAV: {exc}") from exc
    finally:
        if frame_reader is not None:
            frame_reader.close()
            frame_prefetch_metrics = {
                "peak_frames": frame_reader.peak_frames,
                "peak_bytes": frame_reader.peak_bytes,
            }
        if container is not None:
            try:
                container.close()
            except Exception as exc:
                logger.debug("Could not close OCR decoder: %s", exc)

    postprocess_started = time.perf_counter()
    if context:
        context.raise_if_canceled()
        context.update(96, "finalizing", "Đang hoàn tất chuẩn hóa danh sách phụ đề...")

    # Build validated SubtitleCueV2 segments
    segments: list[dict[str, Any]] = []
    cue_index = 1
    previous_end_ms = 0

    for cand in candidates:
        duration = cand.end_ms - cand.start_ms
        if duration < min_duration_ms:
            continue
        cand.vote_until(cand.end_ms)
        text = max(cand.votes, key=cand.votes.get).strip()
        if not text:
            continue

        start_ms = max(previous_end_ms, cand.start_ms)
        end_ms = max(start_ms + min_duration_ms, cand.end_ms)
        if duration_ms > 0 and end_ms > duration_ms:
            end_ms = duration_ms
        if end_ms <= start_ms:
            continue

        avg_conf = cand.confidence_sum / max(1, cand.frame_count)
        needs_review = avg_conf < 0.70 or duration < 350 or cand.stabilized_frames > 0

        cue_dict = {
            "id": f"ocr_{cue_index:04d}",
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": text,
            "secondary_text": None,
            "source_text": text,
            "source_language": source_language or "zh",
            "content_source": "screen",
            "origin_model": "rapidocr-ch-PP-OCRv3",
            "timing_source": "ocr",
            "timing_precision_ms": min(60000, observed_precision_ms),
            "confidence": round(avg_conf, 3),
            "needs_review": needs_review,
            "revision": 0,
        }
        segments.append(cue_dict)
        previous_end_ms = end_ms
        cue_index += 1

    document = {
        "schema_version": 2,
        "document_role": "source",
        "revision": 0,
        "run_id": f"ocr-{key[:16]}",
        "language": source_language or "zh",
        "timebase": "milliseconds",
        "timing_source": "ocr",
        "timing_precision_ms": min(60000, observed_precision_ms),
        "segments": segments,
    }

    # Validate against pydantic schema
    validated_doc = SubtitleDocumentV2.model_validate(document)
    _add_timing(
        timings, "postprocess_seconds", time.perf_counter() - postprocess_started
    )

    result = {
        "version": OCR_ALGORITHM_VERSION,
        "cache_hit": False,
        "processing_seconds": time.monotonic() - started_at,
        "cache_key": key,
        "document": validated_doc.model_dump(),
        "segment_count": len(segments),
        "metrics": {
            **acceleration_counters,
            "stabilized_observations": sum(c.stabilized_frames for c in candidates),
            "visual_feature_builds": feature_cache.builds,
            "visual_feature_cache_hits": feature_cache.hits,
            "native_crop_frames": cropper.converted_frames,
            "bgr_crop_frames": cropper.fallback_frames,
            "decoded_frames": decoded_frames,
            "sampled_frames": sampled_frames,
            "ocr_calls": ocr_calls + refinement_calls,
            "sample_ocr_calls": sample_ocr_calls,
            "ocr_observations": ocr_observations,
            "sample_observations": sample_observations,
            "refinement_observations": refinement_observations,
            "refinement_calls": refinement_calls,
            "peak_refinement_bytes": peak_buffer_bytes,
            "refinement_limited": refinement_limited,
            "frame_cache_hits": cache_hits,
            "recognition_cache_hits": recognition_cache_hits,
            "prefetch_requested": prefetch_frames,
            "prefetch_enabled": prefetch_active,
            "recognition_cache_enabled": glyph_cache,
            "uncertain_frames": uncertain_frames,
            "prefetch_peak_frames": frame_prefetch_metrics["peak_frames"],
            "prefetch_peak_bytes": frame_prefetch_metrics["peak_bytes"],
        },
        "timings_seconds": timings,
        "acceleration": acceleration.model_dump(),
        "warnings": (
            [
                {
                    "code": "ocr_uncertain_frames",
                    "message": f"Có {uncertain_frames} khung hình không đọc rõ; hãy kiểm tra lại biên phụ đề.",
                }
            ]
            if uncertain_frames
            else []
        )
        + ([probe_warning] if probe_warning else []),
        "timing_precision_ms": min(60000, observed_precision_ms),
        "region": {
            "x": region.x,
            "y": region.y,
            "width": region.width,
            "height": region.height,
        },
        "source_language": source_language or "zh",
    }

    if cache_file:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_write_started = time.perf_counter()
            cache_json(cache_file, result, writer=atomic_json)
            timings["cache_write_seconds"] = time.perf_counter() - cache_write_started
        except OSError as exc:
            result["warnings"].append(
                {
                    "code": "ocr_cache_write_failed",
                    "message": "Đã trích xuất OCR nhưng chưa lưu được cache; kiểm tra dung lượng và quyền ghi.",
                }
            )
            logger.warning("Failed to write OCR cache file %s: %s", cache_file, exc)

    if context:
        context.update(
            100,
            "completed",
            f"Trích xuất thành công {len(segments)} đoạn phụ đề trên hình",
        )

    return result
