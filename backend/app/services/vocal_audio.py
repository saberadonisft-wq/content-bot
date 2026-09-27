"""Bounded audio I/O shared by separation methods."""
import math
import subprocess
import tempfile
import time
import wave
from contextlib import ExitStack

import imageio_ffmpeg
import numpy as np


class VocalSeparatorError(RuntimeError):
    pass


def check(context):
    if context:
        context.raise_if_canceled()


def update(context, progress, phase, message):
    check(context)
    if context:
        context.update(progress, phase, message)


def ffmpeg(args, *, context=None, ffmpeg_bin=None, timeout_seconds=7200):
    check(context)
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            [ffmpeg_bin or imageio_ffmpeg.get_ffmpeg_exe(),
             "-v", "error", "-nostdin", "-y", *map(str, args)],
            stdout=subprocess.DEVNULL, stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + timeout_seconds
        try:
            while process.poll() is None:
                check(context)
                if time.monotonic() >= deadline:
                    raise VocalSeparatorError("Xử lý audio quá thời gian chờ.")
                time.sleep(.05)
            check(context)
            if process.returncode:
                log.seek(0, 2)
                log.seek(max(0, log.tell() - 1500))
                raise VocalSeparatorError(
                    "FFmpeg không xử lý được audio: " + log.read().decode(errors="replace")
                )
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def wave_stats(path, context=None):
    energy = mono_energy = 0.0
    peak = frames = 0
    with wave.open(str(path), "rb") as audio:
        channels, rate = audio.getnchannels(), audio.getframerate()
        if channels != 2 or audio.getsampwidth() != 2 or rate <= 0:
            raise VocalSeparatorError("Đầu ra phải là WAV PCM16 stereo.")
        expected = audio.getnframes()
        while raw := audio.readframes(rate):
            check(context)
            samples = np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(np.float64) / 32768
            frames += len(samples)
            energy += float(np.square(samples).sum()) / 2
            mono_energy += float(np.square(samples.mean(axis=1)).sum())
            peak = max(peak, float(np.abs(samples).max()))
    if not frames or frames != expected:
        raise VocalSeparatorError("Audio rỗng hoặc bị cắt khi ghi.")
    return {"frames": frames, "sample_rate": rate, "channels": channels,
            "rms": math.sqrt(energy / frames), "mono_rms": math.sqrt(mono_energy / frames),
            "peak": peak}


def separate_windows(pcm_path, output_paths, predictor, *, sample_rate, channels, context=None):
    """30 s owned cores with 2 s context either side; no full-file arrays."""
    frame_bytes = channels * 4
    frames, remainder = divmod(pcm_path.stat().st_size, frame_bytes)
    if remainder or not frames:
        raise VocalSeparatorError("PCM giải mã rỗng hoặc thiếu sample.")
    core, overlap = sample_rate * 30, sample_rate * 2
    peak = 0.0
    with ExitStack() as stack:
        source = stack.enter_context(pcm_path.open("rb"))
        outputs = {name: stack.enter_context(path.open("wb")) for name, path in output_paths.items()}
        for start in range(0, frames, core):
            check(context)
            end = min(start + core, frames)
            lower, upper = max(0, start - overlap), min(frames, end + overlap)
            source.seek(lower * frame_bytes)
            raw = source.read((upper - lower) * frame_bytes)
            if len(raw) != (upper - lower) * frame_bytes:
                raise VocalSeparatorError("PCM bị thay đổi trong lúc tách.")
            chunk = np.frombuffer(raw, dtype="<f4").reshape(-1, channels).T.copy()
            if not np.isfinite(chunk).all():
                raise VocalSeparatorError("Audio nguồn chứa NaN/Infinity.")
            stems = predictor(chunk)
            check(context)
            if set(stems) != set(outputs):
                raise VocalSeparatorError("Model không trả đủ các stem yêu cầu.")
            for name, values in stems.items():
                values = np.asarray(values, dtype=np.float32)
                if values.shape != chunk.shape or not np.isfinite(values).all():
                    raise VocalSeparatorError("Model trả stem sai kích thước hoặc có NaN/Infinity.")
                owned = values[:, start - lower:end - lower]
                peak = max(peak, float(np.abs(owned).max()))
                outputs[name].write(owned.T.astype("<f4").tobytes())
            update(context, 15 + int(65 * end / frames), "separation",
                   f"Đã tách {end / sample_rate:.1f}/{frames / sample_rate:.1f} giây")
    return frames, min(1.0, .98 / peak) if peak else 1.0
