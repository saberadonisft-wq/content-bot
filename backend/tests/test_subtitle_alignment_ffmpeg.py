from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg

from app.services.media_probe import probe_media
from app.services.subtitle_alignment import AlignmentSettings, align_subtitle_document


def test_ffmpeg_pcm_pipe_refines_real_audio_pulse(tmp_path: Path) -> None:
    video_path = tmp_path / "pulse.mp4"
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=30:d=3",
            "-f",
            "lavfi",
            "-i",
            r"aevalsrc=if(between(t\,0.7\,1.23)\,0.4*sin(2*PI*440*t)\,0):s=16000:d=3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(video_path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    media = probe_media(video_path)
    document = {
        "schema_version": 2,
        "language": "vi",
        "timebase": "milliseconds",
        "timing_source": "gemini_estimate",
        "timing_precision_ms": 1000,
        "segments": [
            {
                "id": "s1",
                "start_ms": 500,
                "end_ms": 1500,
                "text": "Xin chào bạn",
                "timing_source": "gemini_estimate",
                "timing_precision_ms": 1000,
                "confidence": 0.8,
                "needs_review": False,
                "revision": 0,
            }
        ],
    }

    result = align_subtitle_document(
        video_path,
        document,
        media,
        settings=AlignmentSettings(engine="energy"),
    )
    cue = result["document"]["segments"][0]

    assert result["aligned_cue_count"] == 1
    assert 690 <= cue["speech_start_ms"] <= 710
    assert 1220 <= cue["speech_end_ms"] <= 1240
    assert cue["start_ms"] == cue["speech_start_ms"] - 60
    assert cue["end_ms"] == cue["speech_end_ms"] + 100
