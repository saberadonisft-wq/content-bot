"""Create the retained, non-mutating crawler rollback-drill report."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.config import settings
from app.services.crawler_cleanroom_audit import audit_cleanroom_similarity
from app.services.crawler_rollback_drill import audit_rollback_readiness


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default="rollback-drill.json",
        help="Direct .json filename under CONTENT_BOT_DATA_DIR/cbce-audits.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    filename = Path(args.output)
    if (
        filename.name != args.output
        or filename.suffix.casefold() != ".json"
        or filename.is_absolute()
    ):
        raise SystemExit("--output must be one direct .json filename")
    project_root = Path(__file__).resolve().parents[2]
    similarity = audit_cleanroom_similarity(project_root)
    report = audit_rollback_readiness(
        project_root,
        project_tree_digest=str(similarity.get("project_tree_digest") or ""),
    )
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    report_root = (settings.data_dir / "cbce-audits").resolve()
    report_root.mkdir(parents=True, exist_ok=True)
    destination = report_root / filename.name
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(serialized + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    print(serialized)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
