"""Exercise actual FFmpeg timelines, including seek, late audio and VFR."""
import subprocess
import threading
import wave

import imageio_ffmpeg
import numpy as np
import pytest

from app.services.gemini_media import extract_audio
from app.services.gemini_subtitles import GeminiSubtitleService, GeminiSubtitleSettings
from app.services.media_probe import probe_media


def run_ffmpeg(args):
    return subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, check=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def first_pulse(path):
    with wave.open(str(path), "rb") as audio:
        data = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(float) / 32768
    frames = data[:len(data) // 160 * 160].reshape(-1, 160)
    energy = np.sqrt(np.mean(frames ** 2, axis=1))
    return np.flatnonzero(energy > 0.1)[0] * 10


@pytest.mark.parametrize("source_offset,vfr", [(0, False), (5, False), (0, True)])
def test_audio_and_seek_preserve_source_timeline(tmp_path, source_offset, vfr):
    video = tmp_path / "source.mkv"
    # Audio stream begins at 1.2s; a pulse 1s into that stream is at source 2.2s.
    args = ["-f", "lavfi", "-i", "color=c=black:s=160x90:r=30:d=5",
            "-itsoffset", "1.2", "-f", "lavfi", "-i",
            r"aevalsrc=if(between(t\,1\,1.5)\,0.4*sin(2*PI*440*t)\,0):s=16000:d=3.8",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le"]
    if vfr:
        args.extend(["-vf", r"select=if(lt(t\,2)\,1\,not(mod(n\,3)))", "-fps_mode", "vfr"])
    args.extend(["-output_ts_offset", str(source_offset), str(video)])
    run_ffmpeg(args)
    media = probe_media(video)
    event = threading.Event()
    extracted = tmp_path / "audio.wav"
    extract_audio(video, extracted, duration_ms=5000, cancel_event=event)
    assert abs(first_pulse(extracted) - 2200) <= 20
    assert media["source_start_ms"] == source_offset * 1000
    if vfr:
        assert media["is_vfr"]
    service = GeminiSubtitleService(GeminiSubtitleSettings(job_root=tmp_path))
    for seek_ms in (500, 1733):
        proxy = tmp_path / f"proxy-{seek_ms}.mp4"
        service._create_proxy(video, proxy, start_seconds=seek_ms / 1000, duration_seconds=3, cancel_event=event)
        proxy_audio = tmp_path / "proxy.wav"
        extract_audio(proxy, proxy_audio, duration_ms=3000, cancel_event=event)
        assert abs(first_pulse(proxy_audio) + seek_ms - 2200) <= 30
