"""Independent ASR source dialogue extraction using Faster-Whisper."""

from __future__ import annotations

import ctypes
import gc
import hashlib
import importlib
import json
import logging
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import imageio_ffmpeg
import numpy as np

from ..schemas import SubtitleDocumentV2
from .gemini_media import atomic_json
from .model_resources import gpu_model_slot
from .speech_evidence import audio_identity, transcript_hash
from .subtitle_cache import cache_json, cached_json, with_subtitle_cache
from .subtitle_jobs import SubtitleJobCanceled

logger = logging.getLogger("content_bot.subtitle_asr")

ASR_ALGORITHM_VERSION = "faster-whisper-v2-windowed"
ASR_PRECISION_MS = 10
ASR_WINDOW_MS = 60000
ASR_OVERLAP_MS = 2000

_ASR_EXECUTION_LOCK = threading.Lock()
_MODEL_CACHE: dict[str, Any] = {}
_MODEL_CACHE_LOCK = threading.Lock()
_CUDA_DLL_HANDLES: list[Any] = []
_CUDA_DLL_PATHS: set[str] = set()


class SubtitleAsrError(RuntimeError):
    pass


class SubtitleAsrCanceled(SubtitleAsrError):
    pass


def _configure_cuda_runtime() -> None:
    """Expose packaged CUDA DLLs to CTranslate2 on Windows.

    CTranslate2 can detect an NVIDIA device while Windows still cannot resolve
    the cuBLAS DLL shipped by the optional Python CUDA wheels.  Register the
    wheel directories before constructing ``WhisperModel`` so readiness and
    actual inference use the same runtime.  Handles are retained for the
    process lifetime as required by ``os.add_dll_directory``.
    """
    if os.name != "nt":
        return
    candidates: list[Path] = []
    for package_name in (
        "nvidia.cublas",
        "nvidia.cuda_nvrtc",
        "nvidia.cuda_runtime",
        "ctranslate2",
    ):
        try:
            package = importlib.import_module(package_name)
        except (ImportError, OSError):
            continue
        package_file = getattr(package, "__file__", None)
        if package_file:
            package_path = Path(package_file).resolve().parent
        else:
            package_paths = list(getattr(package, "__path__", []))
            if not package_paths:
                continue
            package_path = Path(package_paths[0]).resolve()
        candidates.append(package_path / "bin" if (package_path / "bin").is_dir() else package_path)

    for directory in candidates:
        key = str(directory).lower()
        if not directory.is_dir() or key in _CUDA_DLL_PATHS:
            continue
        _CUDA_DLL_PATHS.add(key)
        try:
            _CUDA_DLL_HANDLES.append(os.add_dll_directory(str(directory)))
        except (AttributeError, OSError):
            # PATH is still useful for older Python/Windows combinations.
            pass
        path_value = os.environ.get("PATH", "")
        if str(directory) not in path_value.split(os.pathsep):
            os.environ["PATH"] = f"{directory}{os.pathsep}{path_value}"


def _cuda_runtime_diagnostic() -> dict[str, str | bool | None]:
    """Check the provider DLL that a CUDA ASR job must load on Windows."""
    if os.name != "nt":
        return {"ready": True, "message": None}
    _configure_cuda_runtime()
    try:
        ctypes.WinDLL("cublas64_12.dll")
    except OSError as exc:
        return {
            "ready": False,
            "message": (
                "Thiếu runtime cuBLAS cho ASR GPU. Cài extra alignment-gpu "
                f"hoặc kiểm tra DLL: {exc}"
            ),
        }
    return {"ready": True, "message": None}


def normalize_source_language(language: str | None) -> str | None:
    value = language.strip().lower() if isinstance(language, str) else None
    if value in (None, "", "auto", "und"):
        return None
    try:
        from faster_whisper.tokenizer import _LANGUAGE_CODES
    except ImportError as exc:
        raise SubtitleAsrError(
            "Chưa cài faster-whisper; cài backend với extra alignment."
        ) from exc
    if value not in _LANGUAGE_CODES:
        raise SubtitleAsrError(f"Ngôn ngữ ASR không được hỗ trợ: {value}")
    return value


@contextmanager
def _execution_slot(context):
    while not _ASR_EXECUTION_LOCK.acquire(timeout=0.1):
        if context:
            context.raise_if_canceled()
    try:
        yield
    finally:
        _ASR_EXECUTION_LOCK.release()


def asr_cache_key(
    media: dict[str, Any],
    *,
    source_language: str | None,
    model_name: str,
    device: str,
    compute_type: str,
) -> str:
    try:
        runtime_version = version("faster-whisper")
    except PackageNotFoundError:
        runtime_version = "missing"
    payload = {
        "algorithm": ASR_ALGORITHM_VERSION,
        "audio_hash": media.get("audio_hash") or media.get("fingerprint"),
        "duration_ms": media.get("duration_ms"),
        "source_language": normalize_source_language(source_language),
        "model_name": model_name,
        "device": device,
        "compute_type": compute_type,
        "source_start_ms": media.get("source_start_ms"),
        "model_signature": media.get("asr_model_signature"),
        "runtime": runtime_version,
        "window_ms": ASR_WINDOW_MS,
        "overlap_ms": ASR_OVERLAP_MS,
        "decoding": {
            "beam_size": 5,
            "word_timestamps": True,
            "vad_filter": True,
            "min_silence_duration_ms": 250,
            "speech_pad_ms": 150,
            "temperature": 0,
            "condition_on_previous_text": False,
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def asr_runtime_status(model_dir: Path | None, allow_download: bool = False) -> dict:
    available = (
        find_spec("faster_whisper") is not None and find_spec("ctranslate2") is not None
    )
    result = {
        "dependency_ready": available,
        "allow_download": allow_download,
        "models": [],
        "devices": [],
        "runtimes": {},
        "message": None
        if available
        else "Chưa cài faster-whisper. Cài backend với extra alignment để dùng ASR.",
    }
    if not available:
        return result
    try:
        import ctranslate2
    except ImportError:
        return {
            **result,
            "dependency_ready": False,
            "message": "Không nạp được CTranslate2. Kiểm tra thư viện runtime của ASR.",
        }
    cuda_runtime = _cuda_runtime_diagnostic()
    result["runtimes"] = {"cuda": cuda_runtime}
    for device in ["cpu", "cuda"]:
        try:
            # CTranslate2 can list CUDA compute types even when this machine has
            # no accessible NVIDIA device. Do not advertise a device that a
            # selected ASR job will immediately reject.
            if (
                device == "cuda"
                and (
                    ctranslate2.get_cuda_device_count() < 1
                    or not cuda_runtime["ready"]
                )
            ):
                continue
            types = sorted(ctranslate2.get_supported_compute_types(device))
            if types:
                result["devices"].append({"id": device, "compute_types": types})
        except (RuntimeError, ValueError):
            continue
    for model in ["tiny", "base", "small", "medium", "large-v3"]:
        try:
            folder = Path(_resolve_whisper_model(model, model_dir, False))
            ready = all(
                (folder / name).is_file()
                for name in ["model.bin", "config.json", "tokenizer.json"]
            )
            result["models"].append(
                {"id": model, "state": "ready" if ready else "incomplete"}
            )
        except SubtitleAsrError:
            result["models"].append({"id": model, "state": "missing"})
    return result


def asr_model_signature(model_name: str, model_dir: Path | None) -> list:
    try:
        folder = Path(_resolve_whisper_model(model_name, model_dir, False))
    except SubtitleAsrError:
        return []
    return [
        (str(p.resolve()), p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(folder.glob("*"))
        if p.is_file()
    ]


def _resolve_whisper_model(
    model_name: str,
    model_dir: Path | None,
    allow_download: bool,
    context: Any | None = None,
) -> str:
    """Resolve Faster-Whisper model path from local cache or download if permitted."""
    from faster_whisper.utils import download_model
    from huggingface_hub.errors import LocalEntryNotFoundError

    if Path(model_name).is_dir():
        return str(Path(model_name).resolve())

    download_root = str(model_dir) if model_dir else None
    if not allow_download and not Path(model_name).is_dir():
        try:
            return download_model(
                model_name, cache_dir=download_root, local_files_only=True
            )
        except LocalEntryNotFoundError:
            if download_root is not None:
                try:
                    return download_model(model_name, local_files_only=True)
                except Exception as exc:
                    logger.debug("Whisper model not found in shared cache: %s", exc)
            raise SubtitleAsrError(
                f"Mô hình Faster-Whisper '{model_name}' chưa được cài đặt cục bộ. "
                "Vui lòng tải model trước trong phần cài đặt hoặc cho phép tự động tải."
            )
        except ValueError as exc:
            raise SubtitleAsrError(
                f"Tên mô hình Faster-Whisper '{model_name}' không hợp lệ: {exc}"
            ) from exc

    if context:
        context.update(
            5, "preparing_model", f"Đang tải/chuẩn bị mô hình '{model_name}'..."
        )
        context.raise_if_canceled()

    try:
        return download_model(
            model_name, cache_dir=download_root, local_files_only=False
        )
    except Exception as exc:
        raise SubtitleAsrError(
            f"Không thể tải mô hình Faster-Whisper '{model_name}': {exc}"
        ) from exc


def _load_cached_whisper_model(
    model_path: str,
    device: str,
    compute_type: str,
    cpu_threads: int,
) -> Any:
    folder = Path(model_path)
    signature = [
        (p.name, p.stat().st_size, p.stat().st_mtime_ns)
        for p in folder.glob("*")
        if p.is_file()
    ]
    cache_key = f"{model_path}:{signature}:{device}:{compute_type}:{cpu_threads}"
    with _MODEL_CACHE_LOCK:
        if cache_key in _MODEL_CACHE:
            return _MODEL_CACHE[cache_key]

        if device == "cuda":
            _configure_cuda_runtime()
        from faster_whisper import WhisperModel

        # Serial execution prevents eviction of a model used by another ASR job.
        while len(_MODEL_CACHE) >= 2:
            _MODEL_CACHE.pop(next(iter(_MODEL_CACHE)))
        gc.collect()

        model = WhisperModel(
            model_path,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            num_workers=1,
            local_files_only=True,
        )
        _MODEL_CACHE[cache_key] = model
        return model


def _extract_audio_pcm(
    video_path: Path,
    context: Any | None = None,
    *,
    start_ms: int = 0,
    duration_ms: int = ASR_WINDOW_MS,
) -> np.ndarray:
    """Extract one bounded 16kHz window while preserving initial silence and audio offsets."""
    if not 0 < duration_ms <= ASR_WINDOW_MS + 2 * ASR_OVERLAP_MS:
        raise SubtitleAsrError("Cửa sổ ASR vượt giới hạn bộ nhớ.")
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-copyts",
        "-start_at_zero",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-i",
        str(video_path.resolve()),
        "-af",
        f"asetpts=PTS-{start_ms / 1000:.3f}/TB,aresample=async=1:first_pts=0",
        "-t",
        f"{duration_ms / 1000:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        "pipe:1",
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    deadline = time.monotonic() + 120
    try:
        while True:
            if context:
                context.raise_if_canceled()
            if time.monotonic() >= deadline:
                raise SubtitleAsrError("Quá thời gian trích xuất cửa sổ âm thanh.")
            try:
                stdout_data, stderr_data = process.communicate(timeout=0.2)
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
            process.communicate(timeout=3)

    if process.returncode != 0:
        err_msg = stderr_data.decode("utf-8", errors="replace").strip()
        raise SubtitleAsrError(f"ffmpeg trích xuất âm thanh thất bại: {err_msg}")

    pcm_int16 = np.frombuffer(stdout_data, dtype=np.int16)
    audio_float32 = pcm_int16.astype(np.float32) / 32768.0
    return audio_float32


@with_subtitle_cache
def extract_subtitles_asr(
    video_path: Path,
    media: dict[str, Any],
    *,
    source_language: str | None = None,
    model_name: str = "small",
    device: str = "auto",
    compute_type: str = "auto",
    model_dir: Path | None = None,
    allow_download: bool = False,
    cache_dir: Path | None = None,
    context: Any | None = None,
) -> dict[str, Any]:
    """Extract source speech dialogue subtitles using Faster-Whisper with VAD and word timestamps."""
    if not video_path.exists():
        raise SubtitleAsrError(f"Video file not found: {video_path}")

    # Check for audio stream
    has_audio = media.get("has_audio", True)
    if not has_audio:
        raise SubtitleAsrError(
            "Video không có âm thanh; vui lòng chọn Đọc phụ đề trên hình (OCR) hoặc Phân tích bằng Gemini."
        )

    if context:
        context.raise_if_canceled()
    source_language = normalize_source_language(source_language)
    try:
        import ctranslate2
    except ImportError as exc:
        raise SubtitleAsrError(
            "Chưa cài faster-whisper; cài backend với extra alignment."
        ) from exc
    duration_ms = int(media.get("duration_ms") or 0)
    if duration_ms <= 0:
        raise SubtitleAsrError("Chưa có thời lượng video hợp lệ để chia cửa sổ ASR.")
    if device not in ("auto", "cpu", "cuda"):
        raise SubtitleAsrError("Thiết bị ASR phải là auto, cpu hoặc cuda.")
    actual_device = (
        ("cuda" if ctranslate2.get_cuda_device_count() else "cpu")
        if device == "auto"
        else device
    )
    if actual_device == "cuda" and not ctranslate2.get_cuda_device_count():
        raise SubtitleAsrError("Không có CUDA khả dụng. Hãy chọn CPU hoặc tự động.")
    supported = ctranslate2.get_supported_compute_types(actual_device)
    actual_compute_type = compute_type
    if actual_compute_type == "auto":
        actual_compute_type = "float16" if actual_device == "cuda" else "int8"
        if actual_compute_type not in supported:
            actual_compute_type = "float32"
    if actual_compute_type not in supported:
        raise SubtitleAsrError(
            f"Kiểu tính toán {actual_compute_type} không hỗ trợ trên {actual_device}."
        )
    if context:
        context.update(5, "preparing_model", f"Kiểm tra mô hình {model_name}...")
    resolved_path = _resolve_whisper_model(
        model_name, model_dir, allow_download, context=context
    )
    model_files = [
        (str(p.resolve()), p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(Path(resolved_path).glob("*"))
        if p.is_file()
    ]
    keyed_media = {**media, "asr_model_signature": model_files}
    key = asr_cache_key(
        keyed_media,
        source_language=source_language,
        model_name=model_name,
        device=actual_device,
        compute_type=actual_compute_type,
    )
    cache_file = cache_dir / f"asr_{key}.json" if cache_dir else None
    if cache_file and cache_file.is_file():
        try:
            cached_data = cached_json(cache_file)
            if (
                cached_data.get("version") == ASR_ALGORITHM_VERSION
                and cached_data.get("cache_key") == key
            ):
                SubtitleDocumentV2.model_validate(cached_data["document"])
                return {
                    **cached_data,
                    "cache_hit": True,
                    "metrics": {
                        "windows": 0,
                        "inference_calls": 0,
                        "peak_audio_samples": 0,
                        "checkpoint_windows": 0,
                    },
                }
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            pass
    cpu_threads = max(1, min(4, os.cpu_count() or 4))
    cues = []
    previous_end_ms = 0
    detected_lang = source_language or "und"
    language_confidence = None
    warnings = []
    fallback_error = None
    metrics = {
        "windows": 0,
        "inference_calls": 0,
        "peak_audio_samples": 0,
        "checkpoint_windows": 0,
    }
    checkpoint_key = hashlib.sha256(f"{key}:window-checkpoint".encode()).hexdigest()
    checkpoint_file = cache_dir / f"asr_{checkpoint_key}.json" if cache_dir else None
    completed_windows: set[int] = set()
    if checkpoint_file and checkpoint_file.is_file():
        try:
            checkpoint = cached_json(checkpoint_file)
            expected_windows = (duration_ms + ASR_WINDOW_MS - 1) // ASR_WINDOW_MS
            checkpoint_windows = {
                int(index) for index in checkpoint.get("completed_windows", [])
            }
            if (
                checkpoint.get("version") == ASR_ALGORITHM_VERSION
                and checkpoint.get("cache_key") == key
                and checkpoint.get("duration_ms") == duration_ms
                and checkpoint_windows
                and checkpoint_windows <= set(range(expected_windows))
            ):
                checkpoint_document = {
                    "schema_version": 2,
                    "document_role": "source",
                    "revision": 0,
                    "run_id": f"asr-checkpoint-{key[:16]}",
                    "language": checkpoint.get("language", "und"),
                    "timebase": "milliseconds",
                    "timing_source": "asr",
                    "timing_precision_ms": ASR_PRECISION_MS,
                    "segments": checkpoint.get("segments", []),
                }
                validated_checkpoint = SubtitleDocumentV2.model_validate(
                    checkpoint_document
                )
                cues = validated_checkpoint.model_dump()["segments"]
                previous_end_ms = max(
                    (int(cue["end_ms"]) for cue in cues), default=0
                )
                detected_lang = checkpoint.get("language", detected_lang)
                language_confidence = checkpoint.get("language_confidence")
                completed_windows = checkpoint_windows
                metrics["checkpoint_windows"] = len(completed_windows)
                warnings.append(
                    {
                        "code": "asr_checkpoint_resumed",
                        "message": f"Đã tiếp tục {len(completed_windows)} cửa sổ ASR đã lưu.",
                    }
                )
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            warnings.append(
                {
                    "code": "asr_checkpoint_ignored",
                    "message": "Checkpoint ASR không hợp lệ; bắt đầu lại các cửa sổ cần thiết.",
                }
            )
    checkpoint_write_failed = False
    with (
        _execution_slot(context),
        gpu_model_slot(
            actual_device, context.raise_if_canceled if context else lambda: None
        ),
    ):
        try:
            whisper_model = _load_cached_whisper_model(
                resolved_path, actual_device, actual_compute_type, cpu_threads
            )
            for core_start in range(0, duration_ms, ASR_WINDOW_MS):
                window_index = core_start // ASR_WINDOW_MS
                if window_index in completed_windows:
                    if context:
                        context.update(
                            min(94, 10 + int(core_start / duration_ms * 84)),
                            "checkpoint",
                            f"Đã khôi phục cửa sổ ASR {core_start // 1000}–{min(duration_ms, core_start + ASR_WINDOW_MS) // 1000}s...",
                        )
                    continue
                if context:
                    context.raise_if_canceled()
                core_end = min(duration_ms, core_start + ASR_WINDOW_MS)
                window_start, window_end = (
                    max(0, core_start - ASR_OVERLAP_MS),
                    min(duration_ms, core_end + ASR_OVERLAP_MS),
                )
                if context:
                    context.update(
                        min(94, 10 + int(core_start / duration_ms * 84)),
                        "extracting_audio",
                        f"Đang đọc âm thanh {core_start // 1000}–{core_end // 1000}s...",
                    )
                audio = _extract_audio_pcm(
                    video_path,
                    context=context,
                    start_ms=window_start,
                    duration_ms=window_end - window_start,
                )
                metrics["windows"] += 1
                metrics["peak_audio_samples"] = max(
                    metrics["peak_audio_samples"], len(audio)
                )
                if not len(audio):
                    continue
                if context:
                    context.update(
                        min(94, 10 + int(core_start / duration_ms * 84)),
                        "transcribing",
                        f"Đang nhận diện {core_start // 1000}–{core_end // 1000}s trên {actual_device}...",
                    )
                segments_gen, info = whisper_model.transcribe(
                    audio,
                    language=source_language,
                    task="transcribe",
                    beam_size=5,
                    word_timestamps=True,
                    vad_filter=True,
                    vad_parameters={
                        "min_silence_duration_ms": 250,
                        "speech_pad_ms": 150,
                    },
                    condition_on_previous_text=False,
                    temperature=0.0,
                )
                metrics["inference_calls"] += 1
                detected_lang = info.language or detected_lang
                language_confidence = (
                    float(info.language_probability)
                    if hasattr(info, "language_probability")
                    else None
                )
                for segment in segments_gen:
                    if context:
                        context.raise_if_canceled()
                    raw_text = segment.text.strip()
                    if not raw_text:
                        continue
                    cue_idx = len(cues) + 1
                    if cue_idx > 20000:
                        raise SubtitleAsrError(
                            "Video vượt giới hạn 20.000 phụ đề. Hãy chia video."
                        )
                    words_data, word_texts = [], []
                    original_words = [
                        w for w in segment.words or [] if str(w.word).strip()
                    ]
                    for word in original_words:
                        raw_start = window_start + round(float(word.start) * 1000)
                        raw_end = window_start + round(float(word.end) * 1000)
                        # The core interval owns word midpoints; overlap supplies acoustic context only.
                        if (
                            not core_start
                            <= (max(0, raw_start) + min(duration_ms, raw_end)) / 2
                            < core_end
                        ):
                            continue
                        start = max(previous_end_ms, 0, raw_start)
                        end = min(duration_ms, raw_end)
                        if end <= start:
                            continue
                        confidence = max(0.0, min(1.0, float(word.probability)))
                        words_data.append(
                            {
                                "id": f"asr_{cue_idx:04d}-w{len(words_data) + 1:03d}",
                                "text": str(word.word).strip(),
                                "start_ms": start,
                                "end_ms": end,
                                "confidence": round(confidence, 3),
                                "alignment_method": "asr_observed",
                            }
                        )
                        word_texts.append(str(word.word))
                    if original_words and not words_data:
                        continue
                    if words_data:
                        start, end = (
                            min(w["start_ms"] for w in words_data),
                            max(w["end_ms"] for w in words_data),
                        )
                        if len(words_data) != len(original_words):
                            raw_text = "".join(word_texts).strip()
                    else:
                        raw_start = window_start + round(float(segment.start) * 1000)
                        raw_end = window_start + round(float(segment.end) * 1000)
                        if not core_start <= (raw_start + raw_end) / 2 < core_end:
                            continue
                        start, end = (
                            max(previous_end_ms, 0, raw_start),
                            min(duration_ms, raw_end),
                        )
                    if end <= start:
                        continue
                    confidence = (
                        sum(w["confidence"] for w in words_data) / len(words_data)
                        if words_data
                        else None
                    )
                    cue = {
                        "id": f"asr_{cue_idx:04d}",
                        "start_ms": start,
                        "end_ms": end,
                        "speech_start_ms": start if words_data else None,
                        "speech_end_ms": end if words_data else None,
                        "text": raw_text,
                        "source_text": raw_text,
                        "secondary_text": None,
                        "source_language": detected_lang,
                        "content_source": "audio",
                        "origin_model": f"faster-whisper-{model_name}",
                        "timing_source": "asr",
                        "timing_precision_ms": ASR_PRECISION_MS,
                        "confidence": confidence,
                        "needs_review": confidence is None
                        or confidence < 0.65
                        or end - start < 250,
                        "words": words_data or None,
                        "revision": 0,
                    }
                    if words_data:
                        cue["speech_evidence"] = {
                            "method": "asr_observed",
                            "audio_identity": audio_identity(media),
                            "transcript_sha256": transcript_hash(cue),
                            "start_ms": start,
                            "end_ms": end,
                            "algorithm": ASR_ALGORITHM_VERSION,
                            "transcript_complete": True,
                        }
                    cues.append(cue)
                    previous_end_ms = end
                del audio
                completed_windows.add(window_index)
                if checkpoint_file:
                    try:
                        cache_json(
                            checkpoint_file,
                            {
                                "version": ASR_ALGORITHM_VERSION,
                                "cache_key": key,
                                "duration_ms": duration_ms,
                                "completed_windows": sorted(completed_windows),
                                "language": detected_lang,
                                "language_confidence": language_confidence,
                                "segments": cues,
                            },
                            writer=atomic_json,
                        )
                    except OSError as exc:
                        if not checkpoint_write_failed:
                            warnings.append(
                                {
                                    "code": "asr_checkpoint_write_failed",
                                    "message": "Đã nhận dạng nhưng chưa lưu được checkpoint cửa sổ; có thể phải chạy lại phần này.",
                                }
                            )
                            checkpoint_write_failed = True
                            logger.warning(
                                "Failed to write ASR checkpoint %s: %s",
                                checkpoint_file,
                                exc,
                            )
        except (SubtitleAsrCanceled, SubtitleJobCanceled):
            raise
        except RuntimeError as exc:
            if (
                device == "auto"
                and actual_device == "cuda"
                and any(
                    token in str(exc).lower()
                    for token in ("out of memory", "cuda", "cublas", "cudnn")
                )
            ):
                fallback_error = str(exc)
            else:
                raise SubtitleAsrError(f"Lỗi ASR trên {actual_device}: {exc}") from exc
        finally:
            # A released GPU slot must not retain resident weights while another engine starts.
            if actual_device == "cuda":
                _MODEL_CACHE.clear()
                if "whisper_model" in locals():
                    del whisper_model
                gc.collect()

    if fallback_error is not None:
        if context:
            context.raise_if_canceled()
            context.update(
                10, "cpu_fallback", "GPU không chạy được mô hình; đang chuyển sang CPU."
            )
        result = extract_subtitles_asr(
            video_path,
            media,
            source_language=source_language,
            model_name=model_name,
            device="cpu",
            compute_type="auto",
            model_dir=model_dir,
            allow_download=allow_download,
            cache_dir=cache_dir,
            context=context,
        )
        result["warnings"] = [
            *result.get("warnings", []),
            {
                "code": "asr_cpu_fallback",
                "message": "GPU không chạy được mô hình. Kết quả này được nhận diện bằng CPU; hãy kiểm tra lại nội dung.",
            },
        ]
        result["requested_device"] = "auto"
        return result

    if context:
        context.update(96, "finalizing", "Đang chuẩn hóa danh sách phụ đề lời thoại...")

    document = {
        "schema_version": 2,
        "document_role": "source",
        "revision": 0,
        "run_id": f"asr-{key[:16]}",
        "language": detected_lang,
        "timebase": "milliseconds",
        "timing_source": "asr",
        "timing_precision_ms": ASR_PRECISION_MS,
        "segments": cues,
    }

    validated_doc = SubtitleDocumentV2.model_validate(document)

    result = {
        "version": ASR_ALGORITHM_VERSION,
        "cache_key": key,
        "document": validated_doc.model_dump(),
        "segment_count": len(cues),
        "detected_language": detected_lang,
        "language_confidence": language_confidence,
        "cache_hit": False,
        "metrics": metrics,
        "warnings": warnings,
        "compute_type": actual_compute_type,
        "timing_precision_ms": ASR_PRECISION_MS,
        "model": model_name,
        "device": actual_device,
    }

    if cache_file:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_json(cache_file, result, writer=atomic_json)
        except OSError as exc:
            result["warnings"].append(
                {
                    "code": "asr_cache_write_failed",
                    "message": "Đã nhận dạng lời thoại nhưng chưa lưu được cache; kiểm tra dung lượng và quyền ghi.",
                }
            )
            logger.warning("Failed to write ASR cache file %s: %s", cache_file, exc)

    if context:
        context.update(
            100,
            "completed",
            f"Trích xuất thành công {len(cues)} đoạn lời thoại (ngôn ngữ: {detected_lang})",
        )

    return result
