"""Speaker pitch estimation and male/female gender diarization for multi-voice dubbing."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import av
import numpy as np

logger = logging.getLogger("content_bot.speaker_pitch_diarization")


def estimate_f0_autocorr(
    signal: np.ndarray,
    sample_rate: int = 16000,
    f0_min: float = 65.0,
    f0_max: float = 350.0,
) -> float | None:
    """Estimate fundamental frequency F0 (Hz) using normalized autocorrelation.

    Returns median F0 in Hz, or None if signal is unvoiced/silent.
    """
    if len(signal) < sample_rate * 0.1:  # Need at least 100ms
        return None

    # Frame window (30ms) and hop (15ms)
    frame_len = int(sample_rate * 0.03)
    hop_len = int(sample_rate * 0.015)

    lag_min = int(sample_rate / f0_max)
    lag_max = int(sample_rate / f0_min)

    f0_estimates: list[float] = []

    for i in range(0, len(signal) - frame_len, hop_len):
        frame = signal[i : i + frame_len].astype(np.float32)
        # Remove DC offset
        frame = frame - np.mean(frame)
        energy = np.sum(frame**2)
        if energy < 1e-4:
            continue

        # Autocorrelation
        autocorr = np.correlate(frame, frame, mode="full")
        autocorr = autocorr[len(frame) - 1 :]  # Non-negative lags

        # Search peak within [lag_min, lag_max]
        if len(autocorr) <= lag_max:
            continue

        search_window = autocorr[lag_min:lag_max]
        peak_idx = int(np.argmax(search_window)) + lag_min
        peak_val = autocorr[peak_idx]

        # Voiced threshold (normalized correlation > 0.35)
        if peak_val / (autocorr[0] + 1e-6) > 0.35:
            f0 = float(sample_rate / peak_idx)
            f0_estimates.append(f0)

    if not f0_estimates or len(f0_estimates) < 3:
        return None

    return float(np.median(f0_estimates))


def extract_audio_segment(
    audio_path: Path,
    start_ms: int,
    end_ms: int,
    target_sr: int = 16000,
) -> np.ndarray:
    """Extract audio slice between [start_ms, end_ms] as mono float32 array."""
    container = av.open(str(audio_path))
    if not container.streams.audio:
        return np.zeros(0, dtype=np.float32)

    stream = container.streams.audio[0]
    resampler = av.AudioResampler(format="s16", layout="mono", rate=target_sr)

    start_s = max(0.0, start_ms / 1000.0)
    end_s = max(start_s + 0.05, end_ms / 1000.0)

    samples: list[np.ndarray] = []
    time_base = float(stream.time_base) if stream.time_base else 1.0 / stream.rate

    for frame in container.decode(audio=0):
        pts_s = float(frame.pts * time_base) if frame.pts is not None else float(frame.time or 0.0)
        frame_dur = float(frame.samples / stream.rate)

        if pts_s + frame_dur < start_s:
            continue
        if pts_s > end_s:
            break

        resampled = resampler.resample(frame)
        if resampled:
            for r in resampled:
                samples.append(r.to_ndarray())

    container.close()

    if not samples:
        return np.zeros(0, dtype=np.float32)

    int16_arr = np.concatenate(samples, axis=1).squeeze()
    return int16_arr.astype(np.float32) / 32768.0


def diarize_subtitle_cues_by_pitch(
    audio_path: Path,
    cues: list[dict[str, Any]],
    *,
    default_pitch_threshold: float = 165.0,
) -> list[dict[str, Any]]:
    """Estimate pitch and classify each subtitle cue as 'male' or 'female'.

    Uses adaptive 2-cluster thresholding if pitch distribution is clearly bimodal.
    """
    if not audio_path.exists() or not cues:
        for c in cues:
            c.setdefault("speaker_gender", "female")
        return cues

    f0_results: list[float | None] = []

    for cue in cues:
        s_ms = int(cue["start_ms"])
        e_ms = int(cue["end_ms"])
        try:
            sig = extract_audio_segment(audio_path, s_ms, e_ms)
            f0 = estimate_f0_autocorr(sig)
            f0_results.append(f0)
        except Exception:
            f0_results.append(None)

    valid_f0s = [f for f in f0_results if f is not None]

    threshold = default_pitch_threshold
    # Adaptive threshold if enough data points
    if len(valid_f0s) >= 4:
        low_p = float(np.percentile(valid_f0s, 25))
        high_p = float(np.percentile(valid_f0s, 75))
        # If there is a clear split (> 35Hz spread)
        if high_p - low_p >= 35.0:
            threshold = (low_p + high_p) / 2.0
            logger.info("Adaptive male/female pitch threshold computed: %.1f Hz", threshold)

    for idx, cue in enumerate(cues):
        f0 = f0_results[idx]
        if f0 is not None:
            gender = "female" if f0 >= threshold else "male"
            cue["speaker_gender"] = gender
            cue["f0_hz"] = round(f0, 1)
        else:
            # Inherit previous cue gender or default to female
            prev_gender = cues[idx - 1].get("speaker_gender", "female") if idx > 0 else "female"
            cue["speaker_gender"] = prev_gender

    return cues
