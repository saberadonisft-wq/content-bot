from __future__ import annotations

from pathlib import Path

import pytest

from app.services.crawler_cleanroom_audit import audit_cleanroom_similarity
from scripts import crawler_cleanroom_audit


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_similarity_audit_flags_substantial_expression_copy(tmp_path: Path) -> None:
    copied = """
def collect_records(records, cursor, maximum):
    selected = []
    for record in records:
        if record.identifier not in selected and len(selected) < maximum:
            selected.append(record.identifier)
            cursor = record.identifier
    return selected, cursor
"""
    _write(tmp_path / "backend/app/crawlers/new.py", copied)
    _write(tmp_path / "vendor/mediacrawler/old.py", copied)

    report = audit_cleanroom_similarity(tmp_path)

    assert report["passed"] is False
    assert report["suspicious_pair_count"] == 1
    assert report["source_fragments_emitted"] is False
    assert "collect_records" not in repr(report)


def test_similarity_audit_passes_independent_implementations(tmp_path: Path) -> None:
    _write(
        tmp_path / "backend/app/crawlers/new.py",
        "def normalize(value):\n    return value.strip().casefold()\n",
    )
    _write(
        tmp_path / "vendor/mediacrawler/old.py",
        "async def download(client, url):\n    response = await client.get(url)\n    return response.json()\n",
    )

    report = audit_cleanroom_similarity(tmp_path)

    assert report["passed"] is True
    assert report["suspicious_pairs"] == []
    assert len(report["project_tree_digest"]) == 64
    assert len(report["vendor_tree_digest"]) == 64


def test_similarity_cli_rejects_output_path_escape() -> None:
    with pytest.raises(SystemExit, match="direct .json filename"):
        crawler_cleanroom_audit.main(["--output", "../report.json"])
