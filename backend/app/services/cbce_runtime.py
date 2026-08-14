"""Feature-gated construction of the clean-room crawler runtime."""

from __future__ import annotations

import json
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from ..config import settings
from ..crawlers import SOURCE_REGISTRY
from ..crawlers.contracts import ImplementationState, Operation
from ..crawlers.observed_dom_contract import (
    DOM_SOURCE_SPECS,
    DomContractUnavailable,
    load_observed_dom_contract,
)
from ..crawlers.runtime import (
    BrowserExecutableNotFound,
    BrowserExecutableResolver,
    CrawlerSupervisor,
    ProfileNamespace,
)


def provider_overrides(raw: str | None = None) -> dict[str, dict[str, str]]:
    value = settings.content_bot_cbce_provider_overrides if raw is None else raw
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("CONTENT_BOT_CBCE_PROVIDER_OVERRIDES must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError("CBCE provider overrides must be a JSON object")
    result: dict[str, dict[str, str]] = {}
    for raw_source_id, raw_operations in payload.items():
        source_id = SOURCE_REGISTRY.resolve_id(str(raw_source_id))
        manifest = SOURCE_REGISTRY.get(source_id)
        if manifest is None or not isinstance(raw_operations, dict):
            raise ValueError(f"Invalid CBCE provider override source: {raw_source_id}")
        provider_ids = {provider.id for provider in manifest.providers}
        result[source_id] = {}
        for operation, provider_id in raw_operations.items():
            provider_id = str(provider_id)
            if provider_id not in provider_ids:
                raise ValueError(
                    f"Unknown provider {provider_id!r} for source {source_id!r}"
                )
            try:
                operation_id = Operation(str(operation))
            except ValueError as exc:
                raise ValueError(
                    f"Provider override targets unknown operation {operation!r}"
                ) from exc
            specs = SOURCE_REGISTRY.operation_specs(source_id, operation_id)
            provider_specs = {
                candidate: spec for candidate, spec in specs
            }
            if not specs or provider_id not in provider_specs:
                raise ValueError(
                    f"Provider {provider_id!r} does not expose {operation!r} for {source_id!r}"
                )
            if provider_specs[provider_id].implementation is not ImplementationState.IMPLEMENTED:
                raise ValueError(
                    f"Provider {provider_id!r} is not implemented for {source_id!r}/{operation!r}"
                )
            result[source_id][operation_id.value] = provider_id
    return result


def build_cbce_supervisor() -> CrawlerSupervisor | None:
    """Return no runtime while the feature flag is off; never alter v1 bridge behavior."""
    if not settings.content_bot_cbce_enabled:
        return None
    provider_overrides()  # fail fast on unsafe/misspelled rollout configuration
    return CrawlerSupervisor(
        event_queue_size=settings.content_bot_cbce_event_queue_size,
        cleanup_timeout=settings.content_bot_cbce_cleanup_timeout_seconds,
    )


def cbce_rollout_status() -> dict[str, Any]:
    preflight = cbce_browser_preflight()
    return {
        "enabled": settings.content_bot_cbce_enabled,
        "profile_root": str(settings.content_bot_cbce_profile_root),
        "provider_overrides": provider_overrides(),
        "browser": preflight,
    }


def cbce_provider_rollout_status(
    source_id: str,
    provider_id: str,
    operation: Operation | str,
) -> dict[str, Any] | None:
    """Return the local rollout gate for a clean-room browser provider."""
    if not provider_id.startswith("cbce_"):
        return None
    operation_id = operation if isinstance(operation, Operation) else Operation(operation)
    if not settings.content_bot_cbce_enabled:
        return {
            "ready": False,
            "reason_code": "CBCE_FEATURE_DISABLED",
            "detail": "Enable the clean-room crawler runtime before using this provider.",
        }
    overrides = provider_overrides()
    selected = overrides.get(SOURCE_REGISTRY.resolve_id(source_id), {}).get(
        operation_id.value
    )
    if selected != provider_id:
        return {
            "ready": False,
            "reason_code": "PROVIDER_NOT_SELECTED",
            "detail": "Select this experimental provider explicitly for this operation before running it.",
        }
    if (
        operation_id is not Operation.SEARCH
        and overrides.get(SOURCE_REGISTRY.resolve_id(source_id), {}).get("search")
        != provider_id
    ):
        return {
            "ready": False,
            "reason_code": "SOURCE_PROVIDER_NOT_SELECTED",
            "detail": "Select the clean-room source provider before enabling its additional operation.",
        }
    preflight = cbce_browser_preflight()
    if preflight["ready"] and source_id in DOM_SOURCE_SPECS:
        try:
            load_observed_dom_contract(
                settings.content_bot_cbce_contract_root,
                source_id,
            )
        except DomContractUnavailable as exc:
            return {
                "ready": False,
                "reason_code": exc.reason_code,
                "detail": exc.safe_message,
            }
    return {
        "ready": bool(preflight["ready"]),
        "reason_code": preflight["reason_code"],
        "detail": (
            "Clean-room browser provider is ready."
            if preflight["ready"]
            else "Clean-room browser runtime or profile preflight is not ready."
        ),
    }


def cbce_browser_preflight() -> dict[str, Any]:
    executable: Path | None = None
    reason_code: str | None = None
    try:
        ProfileNamespace(
            settings.content_bot_cbce_profile_root,
            SOURCE_REGISTRY,
            forbidden_roots=(settings.mediacrawler_profile_dir,),
        )
        executable = BrowserExecutableResolver().resolve(
            settings.content_bot_cbce_browser_executable_path
            or settings.content_bot_coccoc_executable_path
        )
    except (ValueError, BrowserExecutableNotFound):
        reason_code = "BROWSER_EXECUTABLE_OR_PROFILE_INVALID"
    dependency_ready = find_spec("playwright") is not None
    if not dependency_ready:
        reason_code = reason_code or "BROWSER_EXTRA_NOT_INSTALLED"
    return {
        "ready": bool(executable and dependency_ready and reason_code is None),
        "reason_code": reason_code,
        "dependency": "playwright",
        "executable_configured": executable is not None,
        "profile_namespace": "v2",
    }
