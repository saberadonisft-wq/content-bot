from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_controlled_vietnamese_alignment_golden_dataset(tmp_path: Path) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(backend_root / "scripts" / "benchmark_subtitle_alignment.py"),
            "--fixture-dir",
            str(backend_root / "tests" / "fixtures" / "alignment"),
            "--report",
            str(tmp_path / "alignment-benchmark.md"),
            "--check",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "alignment-benchmark.md").is_file()
