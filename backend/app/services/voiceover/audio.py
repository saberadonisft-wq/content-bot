from __future__ import annotations

import array
import hashlib
import subprocess
import sys
import wave
from pathlib import Path

import imageio_ffmpeg


def audio_metadata(path: Path) -> dict:
    """Bounded-memory PCM validation and multi-resolution waveform peaks."""
    checksum = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1024 * 1024):
            checksum.update(block)
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            raise ValueError("Audio phải là WAV PCM16 mono.")
        sr = wav.getframerate()
        frames = wav.getnframes()
        if sr not in {16000, 22050, 24000, 44100, 48000} or not 0 < frames <= sr * 600:
            raise ValueError("Audio rỗng hoặc dài quá 10 phút mỗi đoạn.")
        peaks = []
        total = 0
        while raw := wav.readframes(max(1, sr // 50)):
            values = array.array("h", raw)
            if sys.byteorder != "little":
                values.byteswap()
            peaks.append(round(max(abs(v) for v in values) / 32768, 3))
            total += len(values)
        if total != frames or max(peaks, default=0) < 0.0001:
            raise ValueError("Audio bị cắt hoặc không có tín hiệu.")
    levels = [peaks]
    while len(levels[-1]) > 64:
        prev = levels[-1]
        levels.append([max(prev[i : i + 4]) for i in range(0, len(prev), 4)])
    return {
        "checksum": checksum.hexdigest(),
        "duration_ms": round(frames * 1000 / sr),
        "sample_rate": sr,
        "channels": 1,
        "peaks": levels,
    }


def convert_reference(source: Path, destination: Path) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-t",
            "8",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(destination),
        ],
        check=True,
        capture_output=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    metadata = audio_metadata(destination)
    if metadata["duration_ms"] < 3000:
        destination.unlink(missing_ok=True)
        raise ValueError("Mẫu giọng cần ít nhất 3 giây lời nói sạch.")
    return metadata
