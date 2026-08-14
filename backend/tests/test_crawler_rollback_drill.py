from __future__ import annotations

from pathlib import Path

from app.services.crawler_rollback_drill import (
    LEGACY_SOURCE_IDS,
    audit_rollback_readiness,
)


def _artifacts(root: Path) -> None:
    (root / "vendor/mediacrawler").mkdir(parents=True)
    scripts = root / "backend/scripts"
    scripts.mkdir(parents=True)
    (scripts / "mediacrawler_adapter.py").touch()
    (scripts / "mediacrawler_runner.py").touch()


def test_rollback_drill_proves_non_mutating_legacy_switch(
    monkeypatch, tmp_path: Path
) -> None:
    _artifacts(tmp_path)
    monkeypatch.setattr(
        "app.services.crawler_rollback_drill.settings.content_bot_cbce_provider_overrides",
        '{"bilibili":{"search":"cbce_bilibili"}}',
    )
    monkeypatch.setattr(
        "app.services.crawler_rollback_drill.settings.content_bot_cbce_profile_root",
        tmp_path / "profiles-v2",
    )
    monkeypatch.setattr(
        "app.services.crawler_rollback_drill.settings.mediacrawler_profile_dir",
        tmp_path / "profiles-v1",
    )

    report = audit_rollback_readiness(tmp_path, project_tree_digest="a" * 64)

    assert report["passed"] is True
    assert report["legacy_source_count"] == len(LEGACY_SOURCE_IDS) == 7
    assert report["rollback_configuration"] == {
        "CONTENT_BOT_CBCE_ENABLED": "false",
        "CONTENT_BOT_CBCE_PROVIDER_OVERRIDES": "{}",
    }
    assert report["content_data_mutation_required"] is False
    assert report["browser_or_database_opened"] is False
    assert report["destructive_actions_performed"] is False


def test_rollback_drill_fails_when_vendor_is_already_missing(tmp_path: Path) -> None:
    report = audit_rollback_readiness(tmp_path, project_tree_digest="a" * 64)

    assert report["passed"] is False
    assert "LEGACY_ROLLBACK_ARTIFACT_MISSING" in report["blockers"]
