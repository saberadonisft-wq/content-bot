"""Read-only rollback readiness drill for the temporary legacy bridge window."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import settings
from ..crawlers import SOURCE_REGISTRY
from ..crawlers.contracts import ImplementationState, Operation, PolicyState
from .cbce_runtime import provider_overrides

ROLLBACK_DRILL_SCHEMA = "cbce.rollback-drill.v1"
LEGACY_SOURCE_IDS = (
    "xhs",
    "douyin",
    "kuaishou",
    "bilibili",
    "weibo",
    "tieba",
    "zhihu",
)


def audit_rollback_readiness(
    project_root: Path,
    *,
    project_tree_digest: str,
) -> dict[str, Any]:
    """Prove the pre-removal rollback path without opening a browser or database."""
    root = project_root.resolve()
    artifacts = {
        path: (root / path).exists()
        for path in (
            "vendor/mediacrawler",
            "backend/scripts/mediacrawler_adapter.py",
            "backend/scripts/mediacrawler_runner.py",
        )
    }
    legacy_bindings: dict[str, bool] = {}
    for source_id in LEGACY_SOURCE_IDS:
        manifest = SOURCE_REGISTRY.require(source_id)
        legacy_bindings[source_id] = any(
            provider.policy_state is PolicyState.LEGACY_ONLY
            and spec.operation is Operation.SEARCH
            and spec.implementation is ImplementationState.IMPLEMENTED
            and bool(spec.handler_key)
            for provider in manifest.providers
            for spec in provider.operations
        )
    active_overrides = provider_overrides()
    profile_v2 = settings.content_bot_cbce_profile_root.resolve()
    legacy_profile = settings.mediacrawler_profile_dir.resolve()
    profiles_separated = profile_v2 != legacy_profile
    rollback_configuration = {
        "CONTENT_BOT_CBCE_ENABLED": "false",
        "CONTENT_BOT_CBCE_PROVIDER_OVERRIDES": "{}",
    }
    config_digest = hashlib.sha256(
        json.dumps(
            {
                "enabled": settings.content_bot_cbce_enabled,
                "overrides": active_overrides,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    blockers: list[str] = []
    if not all(artifacts.values()):
        blockers.append("LEGACY_ROLLBACK_ARTIFACT_MISSING")
    if not all(legacy_bindings.values()):
        blockers.append("LEGACY_ROLLBACK_BINDING_MISSING")
    if not profiles_separated:
        blockers.append("ROLLBACK_PROFILE_NAMESPACE_COLLISION")
    if len(project_tree_digest) != 64:
        blockers.append("PROJECT_TREE_DIGEST_MISSING")
    return {
        "schema_version": ROLLBACK_DRILL_SCHEMA,
        "checked_at": datetime.now(UTC).isoformat(),
        "project_tree_digest": project_tree_digest,
        "configuration_digest": config_digest,
        "legacy_source_count": len(LEGACY_SOURCE_IDS),
        "legacy_bindings_ready": all(legacy_bindings.values()),
        "legacy_artifacts_ready": all(artifacts.values()),
        "profile_namespaces_separated": profiles_separated,
        "rollback_configuration": rollback_configuration,
        "content_data_mutation_required": False,
        "browser_or_database_opened": False,
        "destructive_actions_performed": False,
        "blockers": blockers,
        "passed": not blockers,
    }
