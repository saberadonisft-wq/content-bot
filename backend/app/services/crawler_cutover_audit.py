"""Read-only Phase 11 cutover audit for the crawler engine."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import settings
from ..crawlers import SOURCE_REGISTRY
from ..crawlers.contracts import (
    ImplementationState,
    Operation,
    PolicyState,
    ProviderManifest,
    ProviderOperationSpec,
)
from ..crawlers.licensed.mediacrawler.policy import (
    LicensedReuseError,
    assert_licensed_reuse_allowed,
)
from ..crawlers.licensed.mediacrawler.source_map import SourceMapError, load_source_map
from ..crawlers.registry import SourceRegistry
from .crawler_canary import REPORT_SCHEMA
from .crawler_cleanroom_audit import (
    CLEANROOM_AUDIT_SCHEMA,
    audit_cleanroom_similarity,
)
from .crawler_rollback_drill import ROLLBACK_DRILL_SCHEMA

CUTOVER_AUDIT_SCHEMA = "cbce.cutover-audit.v1"
MIN_CANARY_SEPARATION_SECONDS = 3_600
_FORBIDDEN_REPORT_KEYS = frozenset(
    {
        "query",
        "target_url",
        "canonical_url",
        "external_id",
        "title",
        "body",
        "author",
        "raw_payload",
        "provider_payload",
        "cookie",
        "access_token",
        "refresh_token",
        "authorization",
    }
)
_AGGREGATE_COMPLETENESS_KEYS = frozenset(
    {"title", "body", "author", "published_at", "metrics"}
)
_PROVENANCE_FILES = {
    "youtube": "YOUTUBE.md",
    "web": "GAME_NEWS_FEEDS.md",
    "steam": "STEAM.md",
    "bluesky": "BLUESKY.md",
    "mastodon": "MASTODON.md",
    "reddit": "REDDIT.md",
    "x": "X.md",
    "xhs": "XHS_DOUYIN_KUAISHOU.md",
    "douyin": "XHS_DOUYIN_KUAISHOU.md",
    "kuaishou": "XHS_DOUYIN_KUAISHOU.md",
    "bilibili": "BILIBILI.md",
    "weibo": "WEIBO_TIEBA.md",
    "tieba": "WEIBO_TIEBA.md",
    "zhihu": "ZHIHU.md",
    "tiktok": "TIKTOK.md",
    "facebook": "META.md",
    "instagram": "META.md",
}


@dataclass(frozen=True, slots=True)
class CanaryEvidence:
    canary_id: str
    source_id: str
    provider_id: str
    operation: str
    completed_at: datetime
    report_file: str


@dataclass(frozen=True, slots=True)
class CanaryStatus:
    canary_id: str
    source_id: str
    provider_id: str
    operation: str
    state: str
    error_code: str | None
    completed_at: datetime
    report_file: str


@dataclass(frozen=True, slots=True)
class SourceCutoverGate:
    source_id: str
    operation: str
    candidate_provider_id: str | None
    active_provider_id: str | None
    canary_count: int
    canary_span_seconds: int
    next_canary_eligible_at: str | None
    canary_wait_seconds: int
    latest_canary_state: str | None
    latest_canary_error_code: str | None
    latest_canary_completed_at: str | None
    provenance_file: str
    blockers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "operation": self.operation,
            "candidate_provider_id": self.candidate_provider_id,
            "active_provider_id": self.active_provider_id,
            "canary_count": self.canary_count,
            "canary_span_seconds": self.canary_span_seconds,
            "next_canary_eligible_at": self.next_canary_eligible_at,
            "canary_wait_seconds": self.canary_wait_seconds,
            "latest_canary_state": self.latest_canary_state,
            "latest_canary_error_code": self.latest_canary_error_code,
            "latest_canary_completed_at": self.latest_canary_completed_at,
            "provenance_file": self.provenance_file,
            "blockers": list(self.blockers),
            "ready": not self.blockers,
        }


def load_canary_evidence(
    report_root: Path,
    *,
    checked_at: datetime | None = None,
) -> tuple[CanaryEvidence, ...]:
    return tuple(
        CanaryEvidence(
            canary_id=status.canary_id,
            source_id=status.source_id,
            provider_id=status.provider_id,
            operation=status.operation,
            completed_at=status.completed_at,
            report_file=status.report_file,
        )
        for status in load_canary_statuses(report_root, checked_at=checked_at)
        if status.state == "passed"
    )


def load_canary_statuses(
    report_root: Path,
    *,
    checked_at: datetime | None = None,
) -> tuple[CanaryStatus, ...]:
    if not report_root.exists():
        return ()
    now = (checked_at or datetime.now(UTC)).astimezone(UTC)
    statuses: list[CanaryStatus] = []
    seen_canary_ids: set[str] = set()
    for path in sorted(report_root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not _safe_canary_report(payload):
            continue
        try:
            completed_at = datetime.fromisoformat(str(payload["completed_at"]))
        except (KeyError, ValueError):
            continue
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        completed_at = completed_at.astimezone(UTC)
        canary_id = str(payload.get("canary_id") or "").strip()
        if (
            not canary_id
            or len(canary_id) > 128
            or canary_id in seen_canary_ids
            or completed_at > now + timedelta(minutes=5)
        ):
            continue
        seen_canary_ids.add(canary_id)
        state = str(payload.get("state") or "")
        if state not in {"passed", "skipped", "failed", "timed_out"}:
            continue
        error_code_value = payload.get("error_code")
        statuses.append(
            CanaryStatus(
                canary_id=canary_id,
                source_id=str(payload.get("source_id") or ""),
                provider_id=str(payload.get("provider_id") or ""),
                operation=str(payload.get("operation") or ""),
                state=state,
                error_code=(
                    str(error_code_value)[:100]
                    if error_code_value is not None
                    else None
                ),
                completed_at=completed_at,
                report_file=path.name,
            )
        )
    return tuple(statuses)


class CrawlerCutoverAuditor:
    def __init__(
        self,
        *,
        project_root: Path,
        registry: SourceRegistry = SOURCE_REGISTRY,
        canary_report_root: Path | None = None,
        active_provider_overrides: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.registry = registry
        self.canary_report_root = (
            canary_report_root.resolve()
            if canary_report_root is not None
            else settings.data_dir / "cbce-canary-reports"
        )
        if active_provider_overrides is None:
            from .cbce_runtime import provider_overrides

            active_provider_overrides = provider_overrides()
        self.active_provider_overrides = {
            str(source_id): dict(operations)
            for source_id, operations in active_provider_overrides.items()
        }

    def audit(self) -> dict[str, Any]:
        checked_at = datetime.now(UTC)
        statuses = load_canary_statuses(
            self.canary_report_root, checked_at=checked_at
        )
        evidence = tuple(
            CanaryEvidence(
                canary_id=status.canary_id,
                source_id=status.source_id,
                provider_id=status.provider_id,
                operation=status.operation,
                completed_at=status.completed_at,
                report_file=status.report_file,
            )
            for status in statuses
            if status.state == "passed"
        )
        source_gates = tuple(
            self._source_gate(
                manifest,
                evidence,
                statuses,
                checked_at=checked_at,
            )
            for manifest in self.registry
        )
        legacy_artifacts = self._legacy_runtime_artifacts()
        cleanroom_audit = self._cleanroom_audit_status()
        rollback_drill = self._rollback_drill_status(
            str(cleanroom_audit.get("project_tree_digest") or "")
        )
        cutover_ready = all(not gate.blockers for gate in source_gates) and cleanroom_audit[
            "ready"
        ] and rollback_drill["ready"]
        return {
            "schema_version": CUTOVER_AUDIT_SCHEMA,
            "checked_at": checked_at.isoformat(),
            "source_count": len(source_gates),
            "source_gates": [gate.as_dict() for gate in source_gates],
            "cutover_ready": cutover_ready,
            "cleanroom_audit": cleanroom_audit,
            "rollback_drill": rollback_drill,
            "legacy_runtime_artifacts": legacy_artifacts,
            "cleanup_complete": cutover_ready and not legacy_artifacts,
            "destructive_actions_performed": False,
        }

    @staticmethod
    def _rollback_drill_status(project_tree_digest: str) -> dict[str, Any]:
        report_path = settings.data_dir / "cbce-audits" / "rollback-drill.json"
        blockers: list[str] = []
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = None
        if not isinstance(report, dict):
            blockers.append("ROLLBACK_DRILL_MISSING")
        else:
            if (
                report.get("schema_version") != ROLLBACK_DRILL_SCHEMA
                or report.get("passed") is not True
                or report.get("content_data_mutation_required") is not False
                or report.get("browser_or_database_opened") is not False
                or report.get("destructive_actions_performed") is not False
            ):
                blockers.append("ROLLBACK_DRILL_INVALID")
            if report.get("project_tree_digest") != project_tree_digest:
                blockers.append("ROLLBACK_DRILL_STALE")
        return {
            "report_file": "cbce-audits/rollback-drill.json",
            "blockers": blockers,
            "ready": not blockers,
        }

    def _cleanroom_audit_status(self) -> dict[str, Any]:
        report_path = settings.data_dir / "cbce-audits" / "cleanroom-similarity.json"
        current = audit_cleanroom_similarity(self.project_root)
        blockers: list[str] = []
        try:
            retained = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            retained = None
        if not isinstance(retained, dict):
            blockers.append("CLEANROOM_AUDIT_MISSING")
        else:
            if (
                retained.get("schema_version") != CLEANROOM_AUDIT_SCHEMA
                or retained.get("passed") is not True
                or retained.get("source_fragments_emitted") is not False
                or retained.get("suspicious_pair_count") != 0
            ):
                blockers.append("CLEANROOM_AUDIT_INVALID")
            if retained.get("project_tree_digest") != current.get(
                "project_tree_digest"
            ):
                blockers.append("CLEANROOM_AUDIT_STALE")
            vendor_digest = str(retained.get("vendor_tree_digest") or "")
            if len(vendor_digest) != 64:
                blockers.append("CLEANROOM_VENDOR_EVIDENCE_MISSING")
        return {
            "report_file": "cbce-audits/cleanroom-similarity.json",
            "project_tree_digest": current.get("project_tree_digest"),
            "blockers": blockers,
            "ready": not blockers,
        }

    def _source_gate(
        self,
        manifest,
        evidence: tuple[CanaryEvidence, ...],
        statuses: tuple[CanaryStatus, ...],
        *,
        checked_at: datetime,
    ) -> SourceCutoverGate:
        operation = _cutover_operation(manifest)
        candidates = _implemented_providers(manifest.providers, operation)
        nonlegacy = [
            (provider, spec)
            for provider, spec in candidates
            if provider.policy_state is not PolicyState.LEGACY_ONLY
        ]
        candidate_provider_id = nonlegacy[0][0].id if nonlegacy else None
        preferred_provider_id = self.active_provider_overrides.get(
            manifest.id, {}
        ).get(operation.value)
        try:
            active_provider_id, _spec = self.registry.executable_provider(
                manifest.id,
                operation,
                preferred_provider_id=preferred_provider_id,
            )
        except ValueError:
            active_provider_id = None
        matching = sorted(
            (
                item
                for item in evidence
                if item.source_id == manifest.id
                and item.provider_id == candidate_provider_id
                and item.operation == operation.value
            ),
            key=lambda item: item.completed_at,
        )
        latest_status = max(
            (
                item
                for item in statuses
                if item.source_id == manifest.id
                and item.provider_id == candidate_provider_id
                and item.operation == operation.value
            ),
            key=lambda item: item.completed_at,
            default=None,
        )
        span = (
            int((matching[-1].completed_at - matching[0].completed_at).total_seconds())
            if len(matching) >= 2
            else 0
        )
        next_eligible: datetime | None = None
        wait_seconds = 0
        if matching and (len(matching) < 2 or span < MIN_CANARY_SEPARATION_SECONDS):
            next_eligible = matching[0].completed_at + timedelta(
                seconds=MIN_CANARY_SEPARATION_SECONDS
            )
            wait_seconds = max(
                0,
                math.ceil((next_eligible - checked_at).total_seconds()),
            )
        provenance_name = _PROVENANCE_FILES[manifest.id]
        provenance_path = (
            self.project_root / "docs" / "crawler-provenance" / provenance_name
        )
        blockers: list[str] = []
        if candidate_provider_id is None:
            blockers.append("NONLEGACY_IMPLEMENTATION_MISSING")
        if candidate_provider_id and candidate_provider_id.startswith("licensed_"):
            try:
                assert_licensed_reuse_allowed(
                    {
                        "non_commercial_learning": settings.content_bot_licensed_reuse_noncommercial_only,
                    }
                )
                load_source_map(
                    self.project_root
                    / "backend"
                    / "app"
                    / "crawlers"
                    / "licensed"
                    / "mediacrawler"
                )
            except LicensedReuseError:
                blockers.append("LICENSED_POLICY_DISABLED")
            except (OSError, SourceMapError, ValueError):
                blockers.append("LICENSED_PROVENANCE_INVALID")
        if active_provider_id is None:
            blockers.append("ACTIVE_PROVIDER_MISSING")
        elif any(
            provider.id == active_provider_id
            and provider.policy_state is PolicyState.LEGACY_ONLY
            for provider in manifest.providers
        ):
            blockers.append("ACTIVE_PROVIDER_IS_LEGACY")
        if candidate_provider_id and (len(matching) < 2 or span < MIN_CANARY_SEPARATION_SECONDS):
            blockers.append("TWO_SEPARATED_CANARIES_MISSING")
        if not provenance_path.is_file():
            blockers.append("PROVENANCE_MISSING")
        return SourceCutoverGate(
            source_id=manifest.id,
            operation=operation.value,
            candidate_provider_id=candidate_provider_id,
            active_provider_id=active_provider_id,
            canary_count=len(matching),
            canary_span_seconds=span,
            next_canary_eligible_at=(
                next_eligible.isoformat() if next_eligible is not None else None
            ),
            canary_wait_seconds=wait_seconds,
            latest_canary_state=(latest_status.state if latest_status else None),
            latest_canary_error_code=(
                latest_status.error_code if latest_status else None
            ),
            latest_canary_completed_at=(
                latest_status.completed_at.isoformat() if latest_status else None
            ),
            provenance_file=provenance_name,
            blockers=tuple(blockers),
        )

    def _legacy_runtime_artifacts(self) -> list[str]:
        candidates = (
            ".gitmodules",
            "vendor/mediacrawler",
            "backend/scripts/mediacrawler_adapter.py",
            "backend/scripts/mediacrawler_runner.py",
            "data/mediacrawler-ready",
        )
        return [
            value
            for value in candidates
            if (self.project_root / value).exists()
        ]


def _cutover_operation(manifest) -> Operation:
    # X's public embed is useful without credentials, but the migration plan
    # explicitly requires its official read provider as cutover evidence.
    if manifest.id == "x" and _implemented_providers(
        manifest.providers, Operation.SEARCH
    ):
        return Operation.SEARCH
    return manifest.primary_operation


def _implemented_providers(
    providers: tuple[ProviderManifest, ...], operation: Operation
) -> list[tuple[ProviderManifest, ProviderOperationSpec]]:
    return [
        (provider, spec)
        for provider in providers
        for spec in provider.operations
        if spec.operation is operation
        and spec.implementation is ImplementationState.IMPLEMENTED
        and spec.handler_key
    ]


def _safe_canary_report(payload: dict[str, Any]) -> bool:
    if payload.get("schema_version") != REPORT_SCHEMA or payload.get("persisted") is not False:
        return False
    if _contains_forbidden_key(payload):
        return False
    digest = str(payload.get("input_digest") or "")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _contains_forbidden_key(value: Any, path: tuple[str, ...] = ()) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).casefold()
            is_aggregate_counter = (
                path == ("observation", "completeness")
                and normalized_key in _AGGREGATE_COMPLETENESS_KEYS
                and isinstance(item, int)
                and not isinstance(item, bool)
                and item >= 0
            )
            if normalized_key in _FORBIDDEN_REPORT_KEYS and not is_aggregate_counter:
                return True
            if _contains_forbidden_key(item, (*path, normalized_key)):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_forbidden_key(item, path) for item in value)
    return False
