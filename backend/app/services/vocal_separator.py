"""Explicit separation methods; never silently replace AI stems with center reduction."""
from __future__ import annotations

import gc
import importlib.util
import tempfile
import uuid
from pathlib import Path

import numpy as np

from .model_resources import gpu_model_slot
from .subtitle_jobs import SubtitleJobCanceled
from .vocal_audio import (
    VocalSeparatorError,
    check,
    ffmpeg,
    separate_windows,
    update,
    wave_stats,
)

SEPARATOR_VERSION = "stems-v2-window30-context2"
CENTER_WARNING = (
    "Khử kênh giữa chỉ giữ phần khác biệt trái/phải và xuất hai kênh cùng pha. "
    "Có thể mất nhạc, lời thoại và hiệu ứng ở giữa; lời lệch bên vẫn còn. "
    "Đây không phải stem nhạc đã tách bằng AI; cần nghe trước khi dùng."
)


def is_demucs_available():
    return all(importlib.util.find_spec(name) is not None
               for name in ("demucs", "torch", "torchaudio"))


def separate_vocals_phase_cancellation(
    audio_path, output_no_vocals, *, ffmpeg_bin=None, context=None, timeout_seconds=7200,
):
    """Explicit lossy center reduction: both output channels carry (L-R)/2.

    Reject mono/dual-mono; caller must surface CENTER_WARNING.
    """
    audio_path, output_no_vocals = Path(audio_path), Path(output_no_vocals)
    if audio_path.resolve() == output_no_vocals.resolve():
        raise VocalSeparatorError("Không được ghi đè audio nguồn.")
    check(context)
    output_no_vocals.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".center-", dir=output_no_vocals.parent) as tmp:
        source, output = Path(tmp) / "source.wav", Path(tmp) / "center-reduced.wav"
        update(context, 5, "decode", "Đang giải mã audio để khử kênh giữa")
        ffmpeg(["-i", audio_path, "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
                "-c:a", "pcm_s16le", source], context=context, ffmpeg_bin=ffmpeg_bin,
               timeout_seconds=timeout_seconds)
        before = wave_stats(source, context)
        update(context, 35, "separation", "Đang khử kênh giữa; kết quả cần nghe kiểm tra")
        ffmpeg(["-i", source, "-af", "pan=stereo|c0=0.5*c0-0.5*c1|c1=0.5*c0-0.5*c1",
                "-c:a", "pcm_s16le", output], context=context, ffmpeg_bin=ffmpeg_bin,
               timeout_seconds=timeout_seconds)
        after = wave_stats(output, context)
        if after["mono_rms"] < max(1e-5, before["rms"] * .01):
            raise VocalSeparatorError(
                "Nguồn mono/dual-mono hoặc có quá ít tín hiệu stereo khác biệt. "
                "Khử kênh giữa sẽ làm mất gần hết âm thanh; hãy dùng Demucs hoặc giữ audio gốc."
            )
        if after["frames"] != before["frames"] or after["peak"] >= 1:
            raise VocalSeparatorError("Audio sau khử kênh giữa sai thời lượng/biên độ.")
        check(context)
        output.replace(output_no_vocals)
    return output_no_vocals


def separate_vocals_demucs(
    audio_path, output_dir, *, model_name="htdemucs", device="auto",
    model_repo=None, context=None,
):
    """Offline model only. Bound memory with PCM windows; publish both stems together."""
    check(context)
    if device not in {"auto", "cpu", "cuda"}:
        raise VocalSeparatorError("Thiết bị tách nền phải là auto, cpu hoặc cuda.")
    if not is_demucs_available():
        raise VocalSeparatorError("Runtime chưa có Demucs, PyTorch và torchaudio; chưa thể tách nền AI.")
    if model_repo is None or not Path(model_repo).is_dir():
        raise VocalSeparatorError("Chưa cấu hình thư mục model Demucs cục bộ; không tự tải model khi chạy job.")
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    if device == "cuda" and not torch.cuda.is_available():
        raise VocalSeparatorError("CUDA không khả dụng cho Demucs.")
    device = "cuda" if device == "cuda" or device == "auto" and torch.cuda.is_available() else "cpu"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    model = None
    with tempfile.TemporaryDirectory(prefix=".demucs-", dir=output_dir) as tmp:
        root = Path(tmp)
        publish = root / "stems"
        publish.mkdir()
        update(context, 2, "waiting_model", "Đang chờ tài nguyên model tách nền")
        with gpu_model_slot(device, lambda: check(context)):
            try:
                model = get_model(model_name, repo=Path(model_repo))
                model.to(device)
                if "vocals" not in model.sources or len(model.sources) < 2:
                    raise VocalSeparatorError("Model không có stem vocals và nhạc nền độc lập.")
                sr, channels = model.samplerate, model.audio_channels
                if channels != 2 or not 8000 <= sr <= 96000:
                    raise VocalSeparatorError("Model có sample rate/số kênh chưa hỗ trợ.")
                pcm = root / "input.f32"
                update(context, 5, "decode", "Đang giải mã audio cho Demucs")
                ffmpeg(["-i", audio_path, "-map", "0:a:0", "-vn", "-ac", channels,
                        "-ar", sr, "-f", "f32le", pcm], context=context)

                def predict(chunk):
                    mean, std = float(chunk.mean()), float(chunk.std())
                    if std < 1e-8:
                        return {"vocals": np.zeros_like(chunk), "background": chunk}
                    # All-channel variance handles anti-phase stereo without division by zero.
                    normalized = torch.from_numpy((chunk - mean) / std)
                    with torch.inference_mode():
                        estimates = apply_model(model, normalized[None], device=device,
                                                progress=False, shifts=0, num_workers=0)[0]
                        estimates = estimates.cpu().numpy() * std + mean / len(model.sources)
                    index = model.sources.index("vocals")
                    return {"vocals": estimates[index],
                            "background": np.delete(estimates, index, axis=0).sum(axis=0)}

                raw_paths = {name: root / f"{name}.f32" for name in ("vocals", "background")}
                frames, gain = separate_windows(pcm, raw_paths, predict,
                                               sample_rate=sr, channels=channels, context=context)
            except (SubtitleJobCanceled, VocalSeparatorError):
                raise
            except Exception as exc:
                raise VocalSeparatorError(f"Demucs không tách được audio: {exc}") from exc
            finally:
                model = None
                gc.collect()
                if device == "cuda":
                    torch.cuda.empty_cache()
        metrics = {}
        for name, raw in raw_paths.items():
            destination = publish / f"{name}.wav"
            ffmpeg(["-f", "f32le", "-ar", sr, "-ac", channels, "-i", raw,
                    "-af", f"volume={gain:.12g}", "-c:a", "pcm_s16le", destination], context=context)
            metrics[name] = wave_stats(destination, context)
            if metrics[name]["frames"] != frames or metrics[name]["peak"] > .981:
                raise VocalSeparatorError("Stem không đạt kiểm tra số sample/biên độ.")
        check(context)
        final = output_dir / f"stems-{uuid.uuid4().hex}"
        publish.replace(final)
    return {"method": "demucs", "version": SEPARATOR_VERSION, "model": model_name,
            "device": device, "gain": gain, "metrics": metrics,
            "stems": {name: str(final / f"{name}.wav") for name in raw_paths},
            "warnings": ["Tách AI có thể còn rò thoại hoặc mất nhạc/SFX; cần nghe kiểm tra trước khi dùng."]}


def separate_vocals(
    audio_path, output_dir, *, prefer_demucs=True, device="auto",
    model_name="htdemucs", model_repo=None, context=None,
):
    """Center reduction is opt-in. Never return the input as a vocal stem."""
    audio_path, output_dir = Path(audio_path), Path(output_dir)
    check(context)
    if not audio_path.is_file():
        raise VocalSeparatorError("Không tìm thấy audio nguồn.")
    if prefer_demucs:
        return separate_vocals_demucs(audio_path, output_dir, device=device,
                                     model_name=model_name, model_repo=model_repo, context=context)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"center-reduced-{uuid.uuid4().hex}.wav"
    separate_vocals_phase_cancellation(audio_path, output, context=context)
    return {"method": "center_reduction", "version": SEPARATOR_VERSION,
            "stems": {"background": str(output)}, "warnings": [CENTER_WARNING]}
