"""Reproducible local OCR benchmark on labeled, generated media (no paid APIs)."""

import argparse
import hashlib
import json
import platform
import statistics
import sys
import threading
import uuid
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.schemas import SubtitleOcrRegion
from app.services.subtitle_ocr import extract_subtitles_ocr


def resident_bytes():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in [
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                ]
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        api = ctypes.WinDLL("psapi", use_last_error=True)
        api.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        counter = Counters()
        counter.cb = ctypes.sizeof(counter)
        if not api.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counter), counter.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return counter.WorkingSetSize
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
        1 if sys.platform == "darwin" else 1024
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/subtitle-remediation/phase2")
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    video = args.output / "labeled-english.mkv"
    labels = ["I have 100 apples", "I have 200 apples", "I do not agree", "I do agree"]
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = (
        ImageFont.truetype(str(font_path), 28)
        if font_path.exists()
        else ImageFont.load_default(size=28)
    )
    duration = 8000
    with av.open(str(video), "w") as container:
        stream = container.add_stream("ffv1", rate=20)
        stream.width, stream.height, stream.pix_fmt = 640, 360, "bgr0"
        stream.time_base = stream.codec_context.time_base = Fraction(1, 1000)
        for milliseconds in range(0, duration, 50):
            image = Image.new("RGB", (640, 360), "#202020")
            draw = ImageDraw.Draw(image)
            draw.text((80, 282), labels[milliseconds // 2000], font=font, fill="white")
            frame = av.VideoFrame.from_ndarray(np.array(image), format="rgb24")
            frame.pts, frame.time_base = milliseconds, Fraction(1, 1000)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    truth = [
        {"text": text, "start_ms": index * 2000, "end_ms": (index + 1) * 2000}
        for index, text in enumerate(labels)
    ]
    (args.output / "ground-truth.json").write_text(
        json.dumps(
            {
                "kind": "generated English text",
                "video": video.name,
                "font": str(font_path),
                "labels": truth,
                "duration_ms": duration,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    stop = threading.Event()
    memory = []

    def sample():
        while not stop.wait(0.02):
            memory.append(resident_bytes())

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    runs = []
    with video.open("rb") as content:
        fingerprint = hashlib.file_digest(content, "sha256").hexdigest()
    run_cache = args.output / "cache" / uuid.uuid4().hex
    try:
        for index in range(3):
            result = extract_subtitles_ocr(
                video,
                SubtitleOcrRegion(x=5, y=72, width=90, height=23),
                {"duration_ms": duration, "fingerprint": fingerprint},
                source_language="en",
                sample_fps=3,
                min_duration_ms=100,
                cache_dir=run_cache if index else None,
            )
            actual = [
                {k: cue[k] for k in ["text", "start_ms", "end_ms"]}
                for cue in result["document"]["segments"]
            ]
            runs.append(
                {
                    "run": index,
                    "seconds": result["processing_seconds"],
                    "cache_hit": result["cache_hit"],
                    "metrics": result["metrics"],
                    "matches_ground_truth": actual == truth,
                    "actual": actual,
                }
            )
    finally:
        stop.set()
        sampler.join(timeout=1)
    report = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "runs": runs,
        "rss_peak_bytes": max(memory, default=0),
        "inference_run_median_seconds": statistics.median(
            r["seconds"] for r in runs if not r["cache_hit"]
        ),
        "scope": "Real PyAV decode + installed RapidOCR, generated English only. Not a multilingual real-video quality assessment.",
    }
    (args.output / "benchmark.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "matches_ground_truth": all(r["matches_ground_truth"] for r in runs),
                "seconds": [round(r["seconds"], 3) for r in runs],
                "report": str(args.output / "benchmark.json"),
            }
        )
    )
    if not all(r["matches_ground_truth"] for r in runs):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
