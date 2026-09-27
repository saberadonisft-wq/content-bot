"""Per-clip PCM DSP cache used by the voice track composer.

The generated voice asset remains immutable.  This cache stores the derived
mono PCM stream after tempo, gain, fade and resampling so a change to one clip
does not run FFmpeg for every other clip in the project.
"""

from __future__ import annotations

import math
import threading
import uuid
import wave
from contextlib import ExitStack
from pathlib import Path

from .audio import audio_metadata
from .audio_cache import MAX_BYTES, MAX_ENTRIES, AudioCache
from .store import digest, read_json, write_json

VERSION = "clip-dsp-pcm16-v1"
SAMPLE_RATE = 48_000


def tempo_filter(rate: float) -> str:
    """Build an atempo chain valid for FFmpeg's 0.5–2.0 range per filter."""

    filters: list[str] = []
    remaining = float(rate)
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining *= 2
    while remaining > 2:
        filters.append("atempo=2")
        remaining /= 2
    if remaining != 1:
        filters.append(f"atempo={remaining}")
    return ",".join(filters)


def _valid_record(
    output: Path,
    manifest: Path,
    specification: dict,
    *,
    allow_silence: bool,
) -> dict | None:
    try:
        record = read_json(manifest)
        actual = audio_metadata(output, allow_silence=allow_silence)
        with wave.open(str(output), "rb") as wav:
            exact_duration = wav.getnframes() * 1000 / wav.getframerate()
        if (
            record.get("specification") == specification
            and record.get("checksum") == actual["checksum"]
            and abs(float(record.get("duration_ms", -1)) - exact_duration) < 0.001
        ):
            return record
    except (OSError, ValueError, KeyError, TypeError, EOFError, wave.Error):
        return None
    return None


def prepare_clip_dsp(
    source: Path,
    *,
    checksum: str,
    rate: float,
    gain: float,
    fade_in_ms: int,
    fade_out_ms: int = 0,
    sample_rate: int = SAMPLE_RATE,
    cache_dir: Path,
    cancel: threading.Event | None = None,
    lease_stack: ExitStack | None = None,
    max_cache_bytes: int = MAX_BYTES,
    max_cache_entries: int = MAX_ENTRIES,
) -> dict:
    """Return a verified cached PCM16 mono clip after bounded DSP."""

    values = (rate, gain, float(fade_in_ms), float(fade_out_ms), float(sample_rate))
    if any(not math.isfinite(value) for value in values):
        raise ValueError("Thông số DSP audio không hợp lệ.")
    # A clip rate is normally 0.5–2, but video speed is folded into the
    # mapped document before composition, so the combined range is 0.25–4.
    if not 0.25 <= rate <= 4 or not 0 <= gain <= 2:
        raise ValueError("Tốc độ hoặc gain audio nằm ngoài giới hạn.")
    if not 0 <= fade_in_ms <= 5000 or not 0 <= fade_out_ms <= 5000:
        raise ValueError("Fade audio nằm ngoài giới hạn.")
    if sample_rate not in {16_000, 22_050, 24_000, 44_100, 48_000}:
        raise ValueError("Sample rate audio không được hỗ trợ.")

    source_meta = audio_metadata(source)
    if source_meta["checksum"] != checksum:
        raise ValueError("Audio gốc đã thay đổi; cần tạo lại đoạn giọng.")
    with wave.open(str(source), "rb") as wav:
        source_duration_ms = wav.getnframes() * 1000 / wav.getframerate()
    output_duration_ms = source_duration_ms / rate
    specification = {
        "algorithm": VERSION,
        "source_checksum": checksum,
        "rate": float(rate),
        "gain": float(gain),
        "fade_in_ms": int(fade_in_ms),
        "fade_out_ms": int(fade_out_ms),
        "sample_rate": int(sample_rate),
    }
    key = digest(specification)
    cache = AudioCache(
        cache_dir,
        max_bytes=max_cache_bytes,
        max_entries=max_cache_entries,
    )
    resources = ExitStack()
    temporary: Path | None = None
    try:
        resources.enter_context(cache.encoder(cancel))
        cache.clean_abandoned_parts()
        if cancel and cancel.is_set():
            raise InterruptedError("Đã hủy chuẩn bị DSP audio.")
        active_leases = lease_stack or resources
        active_leases.enter_context(cache.pin(key))
        output = cache_dir / f"{key}.wav"
        manifest = cache_dir / f"{key}.json"
        cached = _valid_record(
            output,
            manifest,
            specification,
            allow_silence=gain == 0,
        ) if output.exists() and manifest.exists() else None
        if cached:
            return {**cached, "cache_hit": True, "path": output, "cache": cache.trim()}
        # A killed writer may leave one half of an entry behind. Remove only
        # this key's invalid pair before reserving space for its replacement.
        output.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)

        cache_dir.mkdir(parents=True, exist_ok=True)
        estimated_frames = max(1, math.ceil(source_meta["duration_ms"] * sample_rate / 1000 / rate))
        cache.trim(
            reserve_bytes=estimated_frames * 2 + 512 * 1024,
            new_key=key,
        )
        filters: list[str] = []
        tempo = tempo_filter(rate)
        if tempo:
            filters.append(tempo)
        filters.append(f"volume={gain:.8f}")
        if fade_in_ms:
            filters.append(f"afade=t=in:st=0:d={fade_in_ms / 1000:.6f}")
        if fade_out_ms:
            fade_start = max(0, output_duration_ms - fade_out_ms) / 1000
            filters.append(
                f"afade=t=out:st={fade_start:.6f}:d={fade_out_ms / 1000:.6f}"
            )
        from .mix import ffmpeg

        temporary = cache_dir / f"{key}.{uuid.uuid4().hex}.part.wav"
        ffmpeg(
            [
                "-threads",
                "1",
                "-i",
                str(source),
                "-vn",
                "-af",
                ",".join(filters),
                "-filter_threads",
                "1",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-c:a",
                "pcm_s16le",
                str(temporary),
            ],
            cancel=cancel,
            timeout=60,
        )
        measured = audio_metadata(temporary, allow_silence=gain == 0)
        with wave.open(str(temporary), "rb") as wav:
            measured_duration = wav.getnframes() * 1000 / wav.getframerate()
        if audio_metadata(source)["checksum"] != checksum:
            raise ValueError("Audio gốc thay đổi trong lúc xử lý.")
        if cancel and cancel.is_set():
            raise InterruptedError("Đã hủy chuẩn bị DSP audio.")
        temporary.replace(output)
        record = {
            "id": key,
            "filename": output.name,
            "specification": specification,
            **measured,
            "duration_ms": measured_duration,
            "playback_rate": 1,
            "output_measured": True,
            "cache_hit": False,
        }
        write_json(manifest, record)
        return {**record, "path": output, "cache": cache.trim()}
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
        resources.close()
