"""Run a manual crawler live canary from a JSON request on stdin."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from app.config import settings
from app.services.crawler_canary import CanaryRequest, CrawlerCanaryRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manual aggregate-only crawler live canary"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement that this command performs live requests.",
    )
    parser.add_argument(
        "--output",
        help="Optional report filename under data/cbce-canary-reports.",
    )
    return parser


def _safe_output_path(value: str) -> Path:
    name = Path(value)
    if name.is_absolute() or name.name != value or name.suffix.casefold() != ".json":
        raise ValueError("Output must be a simple .json filename")
    root = (settings.data_dir / "cbce-canary-reports").resolve()
    root.mkdir(parents=True, exist_ok=True)
    candidate = (root / name).resolve()
    if candidate.parent != root:
        raise ValueError("Output path is outside the canary report directory")
    return candidate


def _atomic_write(path: Path, payload: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


async def _main() -> int:
    args = _parser().parse_args()
    if not args.live:
        print("Refusing live network access without --live.", file=sys.stderr)
        return 2
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise TypeError("Canary stdin must contain one JSON object")
        request = CanaryRequest.from_mapping(payload)
        report = await CrawlerCanaryRunner().run(request)
        serialized = json.dumps(
            report.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if args.output:
            _atomic_write(_safe_output_path(args.output), serialized)
        print(serialized)
        return 0 if report.state.value in {"passed", "skipped"} else 1
    except (json.JSONDecodeError, TypeError, ValueError):
        print("Invalid canary request.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
