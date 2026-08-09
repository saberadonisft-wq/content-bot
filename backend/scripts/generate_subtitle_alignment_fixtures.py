"""Generate deterministic Vietnamese TTS fixtures for alignment regression tests."""

from __future__ import annotations

import argparse
import base64
import json
import math
import random
import shutil
import subprocess
import tempfile
import wave
from array import array
from pathlib import Path

import imageio_ffmpeg

SAMPLE_RATE = 16_000
FRAME_SAMPLES = SAMPLE_RATE // 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "alignment",
    )
    return parser.parse_args()


def _speak_to_wave(text: str, target: Path, *, rate: int, volume: int) -> None:
    escaped_text = text.replace("'", "''")
    escaped_target = str(target.resolve()).replace("'", "''")
    script = f"""
Add-Type -AssemblyName System.Speech
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
$voice = $synth.GetInstalledVoices() | Where-Object {{ $_.VoiceInfo.Culture.Name -eq 'vi-VN' }} | Select-Object -First 1
if ($null -eq $voice) {{ throw 'No vi-VN Windows speech voice is installed.' }}
$synth.SelectVoice($voice.VoiceInfo.Name)
$synth.Rate = {rate}
$synth.Volume = {volume}
$synth.SetOutputToWaveFile('{escaped_target}')
$synth.Speak('{escaped_text}')
$synth.Dispose()
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        raise RuntimeError("PowerShell is required to synthesize alignment fixtures")
    result = subprocess.run(
        [powershell, "-NoProfile", "-EncodedCommand", encoded],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def _convert_to_pcm(source: Path, target: Path) -> None:
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _read_pcm(path: Path) -> array:
    with wave.open(str(path), "rb") as stream:
        if (
            stream.getnchannels() != 1
            or stream.getsampwidth() != 2
            or stream.getframerate() != SAMPLE_RATE
        ):
            raise RuntimeError(f"Unexpected WAV format: {path}")
        samples = array("h")
        samples.frombytes(stream.readframes(stream.getnframes()))
    return samples


def _write_pcm(path: Path, samples: array) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(SAMPLE_RATE)
        stream.writeframes(samples.tobytes())


def _trim_and_normalize(samples: array) -> array:
    peak = max((abs(sample) for sample in samples), default=0)
    if peak == 0:
        raise RuntimeError("Speech synthesizer returned silent audio")
    normalized = array("h", (round(sample * 12_000 / peak) for sample in samples))
    levels: list[int] = []
    for start in range(0, len(normalized), FRAME_SAMPLES):
        frame = normalized[start : start + FRAME_SAMPLES]
        if not frame:
            break
        levels.append(math.isqrt(sum(sample * sample for sample in frame) // len(frame)))
    threshold = max(120, round(max(levels) * 0.035))
    active = [index for index, level in enumerate(levels) if level >= threshold]
    if not active:
        raise RuntimeError("Could not find synthesized speech boundaries")
    start = max(0, active[0] * FRAME_SAMPLES - FRAME_SAMPLES)
    end = min(len(normalized), (active[-1] + 2) * FRAME_SAMPLES)
    return normalized[start:end]


def _scaled(samples: array, factor: float) -> array:
    return array("h", (round(sample * factor) for sample in samples))


def _silence(milliseconds: int) -> array:
    return array("h", [0]) * round(milliseconds * SAMPLE_RATE / 1000)


def _mix_background(samples: array, *, tone: bool = False, noise: bool = False) -> array:
    rng = random.Random(20260809)
    mixed = array("h")
    for index, sample in enumerate(samples):
        background = 0
        if tone:
            background += round(85 * math.sin(2 * math.pi * 180 * index / SAMPLE_RATE))
            background += round(55 * math.sin(2 * math.pi * 270 * index / SAMPLE_RATE))
        if noise:
            background += rng.randint(-95, 95)
        mixed.append(max(-32768, min(32767, sample + background)))
    return mixed


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    labels: list[dict[str, object]] = []

    definitions = [
        ("fast_speech", "nói nhanh", "Chúng ta bắt đầu ngay bây giờ nhé.", 5, 100, 420, 520),
        ("quiet_speech", "nói nhỏ", "Bạn có nghe rõ câu này không?", -1, 100, 650, 550),
        ("filler_words", "từ đệm", "Ờ, à, để tôi nghĩ một chút nhé.", -2, 100, 540, 480),
        ("background_music", "nhạc nền", "Nhạc nền không được che mất lời nói.", 0, 100, 720, 500),
        ("background_noise", "tiếng ồn", "Bộ lọc cần tìm đúng điểm bắt đầu.", 1, 100, 580, 620),
    ]

    with tempfile.TemporaryDirectory(prefix="subtitle-alignment-tts-") as temp:
        temp_dir = Path(temp)

        def synthesize(case_id: str, text: str, rate: int, volume: int) -> array:
            raw = temp_dir / f"{case_id}-raw.wav"
            pcm = temp_dir / f"{case_id}-pcm.wav"
            _speak_to_wave(text, raw, rate=rate, volume=volume)
            _convert_to_pcm(raw, pcm)
            return _trim_and_normalize(_read_pcm(pcm))

        for case_id, category, transcript, rate, volume, lead_ms, tail_ms in definitions:
            core = synthesize(case_id, transcript, rate, volume)
            if case_id == "quiet_speech":
                core = _scaled(core, 0.16)
            speech_start_ms = lead_ms
            speech_end_ms = lead_ms + round(len(core) * 1000 / SAMPLE_RATE)
            clip = _silence(lead_ms) + core + _silence(tail_ms)
            clip = _mix_background(
                clip,
                tone=case_id == "background_music",
                noise=case_id == "background_noise",
            )
            filename = f"{case_id}.wav"
            _write_pcm(output_dir / filename, clip)
            labels.append(
                {
                    "id": case_id,
                    "file": filename,
                    "category": category,
                    "transcript": transcript,
                    "speech_start_ms": speech_start_ms,
                    "speech_end_ms": speech_end_ms,
                    "coarse_start_ms": max(0, speech_start_ms - 250),
                    "coarse_end_ms": min(
                        round(len(clip) * 1000 / SAMPLE_RATE), speech_end_ms + 250
                    ),
                }
            )

        pause_first = synthesize("long-pause-a", "Tôi nói câu đầu tiên.", 0, 100)
        pause_second = synthesize("long-pause-b", "Sau khoảng lặng là câu thứ hai.", 0, 100)
        pause_core = pause_first + _silence(900) + pause_second
        pause_lead = 500
        pause_clip = _silence(pause_lead) + pause_core + _silence(500)
        _write_pcm(output_dir / "long_pause.wav", pause_clip)
        pause_end = pause_lead + round(len(pause_core) * 1000 / SAMPLE_RATE)
        labels.append(
            {
                "id": "long_pause",
                "file": "long_pause.wav",
                "category": "khoảng im lặng",
                "transcript": "Tôi nói câu đầu tiên. Sau khoảng lặng là câu thứ hai.",
                "speech_start_ms": pause_lead,
                "speech_end_ms": pause_end,
                "coarse_start_ms": 250,
                "coarse_end_ms": pause_end + 250,
            }
        )

        speaker_a = synthesize("two-speakers-a", "Tôi sẽ nói trước.", -2, 100)
        speaker_b = synthesize("two-speakers-b", "Còn tôi trả lời sau.", 3, 100)
        speaker_core = speaker_a + _silence(180) + _scaled(speaker_b, 0.72)
        speaker_lead = 600
        speaker_clip = _silence(speaker_lead) + speaker_core + _silence(550)
        _write_pcm(output_dir / "two_speakers.wav", speaker_clip)
        speaker_end = speaker_lead + round(len(speaker_core) * 1000 / SAMPLE_RATE)
        labels.append(
            {
                "id": "two_speakers",
                "file": "two_speakers.wav",
                "category": "hai người nói mô phỏng",
                "transcript": "Tôi sẽ nói trước. Còn tôi trả lời sau.",
                "speech_start_ms": speaker_lead,
                "speech_end_ms": speaker_end,
                "coarse_start_ms": 350,
                "coarse_end_ms": speaker_end + 250,
            }
        )

    payload = {
        "schema_version": 1,
        "language": "vi-VN",
        "sample_rate": SAMPLE_RATE,
        "provenance": "Windows Microsoft An TTS; deterministic controlled boundaries",
        "cases": labels,
    }
    (output_dir / "labels.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Generated {len(labels)} fixtures in {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
