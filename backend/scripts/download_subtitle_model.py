"""Download a Faster Whisper model into Content Bot's local model cache."""

from __future__ import annotations

import argparse
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download an optional Faster Whisper model for subtitle alignment."
    )
    parser.add_argument(
        "--model",
        default="small",
        choices=("tiny", "base", "small", "medium", "large-v3"),
    )
    parser.add_argument("--cache-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        from faster_whisper.utils import download_model
    except ImportError as exc:
        raise SystemExit(
            "faster-whisper is not installed; run launcher.ps1 -Action "
            "setup-subtitles first"
        ) from exc

    cache_dir = args.cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    model_path = download_model(args.model, cache_dir=str(cache_dir))
    print(f"Downloaded {args.model} to {model_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
