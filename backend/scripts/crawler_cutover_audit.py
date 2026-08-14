"""Print the read-only Phase 11 crawler cutover audit."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.crawler_cutover_audit import CrawlerCutoverAuditor


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    report = CrawlerCutoverAuditor(project_root=project_root).audit()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["cleanup_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
