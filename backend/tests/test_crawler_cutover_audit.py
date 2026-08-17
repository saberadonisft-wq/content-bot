from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import settings
from app.services.crawler_canary import REPORT_SCHEMA
from app.services.crawler_cutover_audit import (
    CrawlerCutoverAuditor,
    load_canary_evidence,
    load_canary_statuses,
)


def _report(
    *, completed_at: datetime, raw: bool = False, canary_id: str = "canary-test"
) -> dict:
    payload = {
        "schema_version": REPORT_SCHEMA,
        "canary_id": canary_id,
        "source_id": "youtube",
        "provider_id": "youtube_public",
        "operation": "search",
        "state": "passed",
        "completed_at": completed_at.isoformat(),
        "input_digest": "a" * 64,
        "observation": {
            "items": 1,
            "completeness": {
                "title": 1,
                "body": 1,
                "author": 0,
                "published_at": 1,
                "metrics": 1,
            },
        },
        "persisted": False,
    }
    if raw:
        payload["raw_payload"] = {"access_token": "sentinel"}
    return payload


def test_canary_evidence_accepts_only_safe_aggregate_reports(tmp_path: Path) -> None:
    first = datetime(2026, 8, 13, 1, tzinfo=UTC)
    (tmp_path / "valid-a.json").write_text(
        json.dumps(_report(completed_at=first)), encoding="utf-8"
    )
    (tmp_path / "valid-b.json").write_text(
        json.dumps(
            _report(
                completed_at=first + timedelta(hours=2),
                canary_id="canary-test-b",
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "unsafe.json").write_text(
        json.dumps(_report(completed_at=first, raw=True)), encoding="utf-8"
    )
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")

    evidence = load_canary_evidence(tmp_path)

    assert [item.report_file for item in evidence] == [
        "valid-a.json",
        "valid-b.json",
    ]


def test_canary_evidence_deduplicates_id_and_rejects_future_timestamp(
    tmp_path: Path,
) -> None:
    checked_at = datetime(2026, 8, 13, 1, tzinfo=UTC)
    (tmp_path / "first.json").write_text(
        json.dumps(_report(completed_at=checked_at)), encoding="utf-8"
    )
    (tmp_path / "copied.json").write_text(
        json.dumps(_report(completed_at=checked_at + timedelta(hours=2))),
        encoding="utf-8",
    )
    (tmp_path / "future.json").write_text(
        json.dumps(
            _report(
                completed_at=checked_at + timedelta(hours=1),
                canary_id="future-canary",
            )
        ),
        encoding="utf-8",
    )

    evidence = load_canary_evidence(tmp_path, checked_at=checked_at)

    assert [item.report_file for item in evidence] == ["first.json"]


def test_canary_evidence_rejects_content_named_like_aggregate_counter(
    tmp_path: Path,
) -> None:
    payload = _report(completed_at=datetime(2026, 8, 13, 1, tzinfo=UTC))
    payload["observation"]["author"] = "raw account name"
    (tmp_path / "unsafe-author.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    assert load_canary_evidence(tmp_path) == ()


def test_canary_status_preserves_only_typed_failure_summary(tmp_path: Path) -> None:
    payload = _report(completed_at=datetime(2026, 8, 13, 1, tzinfo=UTC))
    payload["state"] = "failed"
    payload["error_code"] = "PAYMENT_OR_ACCESS_REQUIRED"
    payload["safe_message"] = "safe"
    (tmp_path / "x.json").write_text(json.dumps(payload), encoding="utf-8")

    statuses = load_canary_statuses(tmp_path)

    assert len(statuses) == 1
    assert statuses[0].state == "failed"
    assert statuses[0].error_code == "PAYMENT_OR_ACCESS_REQUIRED"
    assert "safe" not in repr(statuses[0])


def test_current_cutover_audit_fails_closed_without_external_evidence(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "content_bot_data_dir", tmp_path / "data")
    project_root = Path(__file__).resolve().parents[2]
    report = CrawlerCutoverAuditor(
        project_root=project_root,
        canary_report_root=tmp_path,
    ).audit()

    assert report["source_count"] == 17
    assert report["cutover_ready"] is False
    assert report["cleanup_complete"] is False
    assert report["destructive_actions_performed"] is False
    assert report["cleanroom_audit"]["ready"] is False
    assert report["rollback_drill"]["ready"] is False
    by_source = {item["source_id"]: item for item in report["source_gates"]}
    assert "TWO_SEPARATED_CANARIES_MISSING" in by_source["youtube"]["blockers"]
    assert by_source["youtube"]["next_canary_eligible_at"] is None
    assert by_source["youtube"]["canary_wait_seconds"] == 0
    assert "ACTIVE_PROVIDER_IS_LEGACY" in by_source["xhs"]["blockers"]
    assert by_source["weibo"]["candidate_provider_id"] == "licensed_weibo"
    assert "NONLEGACY_IMPLEMENTATION_MISSING" not in by_source["weibo"]["blockers"]
    assert "ACTIVE_PROVIDER_IS_LEGACY" in by_source["weibo"]["blockers"]
    assert "TWO_SEPARATED_CANARIES_MISSING" in by_source["weibo"]["blockers"]


def test_cutover_audit_uses_configured_application_data_directory(
    monkeypatch, tmp_path: Path
) -> None:
    report_root = tmp_path / "configured-data" / "cbce-canary-reports"
    report_root.mkdir(parents=True)
    completed_at = datetime(2026, 8, 13, 1, tzinfo=UTC)
    (report_root / "youtube-a.json").write_text(
        json.dumps(_report(completed_at=completed_at)), encoding="utf-8"
    )
    monkeypatch.setattr(
        settings, "content_bot_data_dir", tmp_path / "configured-data"
    )

    audit = CrawlerCutoverAuditor(
        project_root=Path(__file__).resolve().parents[2]
    )

    assert audit.canary_report_root == report_root.resolve()
    youtube = next(
        item for item in audit.audit()["source_gates"] if item["source_id"] == "youtube"
    )
    assert youtube["canary_count"] == 1
    assert youtube["next_canary_eligible_at"] is not None


def test_cutover_audit_reports_legacy_artifacts_without_deleting_them(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "vendor" / "mediacrawler"
    artifact.mkdir(parents=True)

    report = CrawlerCutoverAuditor(
        project_root=tmp_path,
        canary_report_root=tmp_path / "reports",
    ).audit()

    assert report["legacy_runtime_artifacts"] == ["vendor/mediacrawler"]
    assert artifact.is_dir()
    assert report["destructive_actions_performed"] is False


def test_cutover_audit_resolves_the_configured_active_provider(
    tmp_path: Path,
) -> None:
    report = CrawlerCutoverAuditor(
        project_root=Path(__file__).resolve().parents[2],
        canary_report_root=tmp_path,
        active_provider_overrides={
            "bilibili": {"search": "cbce_bilibili"}
        },
    ).audit()

    bilibili = next(
        item for item in report["source_gates"] if item["source_id"] == "bilibili"
    )
    assert bilibili["active_provider_id"] == "cbce_bilibili"
    assert "ACTIVE_PROVIDER_IS_LEGACY" not in bilibili["blockers"]
