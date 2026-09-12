"""Read-only shadow comparisons used before a provider cutover."""

from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass

from ..crawlers.runtime import (
    ShadowAcceptancePolicy,
    ShadowCollector,
    ShadowReport,
    compare_shadow,
)
from .connector_contracts import SearchQuery, SourceConnector


class ShadowProviderTimeout(TimeoutError):
    def __init__(self, role: str) -> None:
        if role not in {"baseline", "candidate"}:
            raise ValueError("Invalid shadow provider role")
        self.role = role
        self.reason_code = f"{role.upper()}_TIMEOUT"
        super().__init__(self.reason_code)


@dataclass(frozen=True, slots=True)
class ShadowRunResult:
    report: ShadowReport
    accepted: bool
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "report": self.report.as_dict(),
            "acceptance": {
                "accepted": self.accepted,
                "reason_codes": list(self.reason_codes),
            },
        }


async def compare_connectors_without_persistence(
    baseline: SourceConnector,
    candidate: SourceConnector,
    query: SearchQuery,
    *,
    secret_key: bytes,
    allowed_metric_keys: tuple[str, ...],
    policy: ShadowAcceptancePolicy | None = None,
    provider_timeout_seconds: float = 120,
) -> ShadowRunResult:
    """Run providers sequentially and retain only privacy-safe aggregates.

    A fresh query is created for each provider.  Checkpoint trackers and progress
    callbacks are intentionally omitted, so a shadow run cannot advance durable
    state or send raw provider progress into production telemetry.
    """

    if baseline.source_id != candidate.source_id:
        raise ValueError("Shadow providers must belong to the same source")
    if provider_timeout_seconds <= 0:
        raise ValueError("Shadow provider timeout must be positive")
    comparison_key = hmac.digest(secret_key, b"cbce-shadow-comparison-v1", "sha256")
    collector = ShadowCollector(
        comparison_key,
        allowed_metric_keys=allowed_metric_keys,
    )
    try:
        async with asyncio.timeout(provider_timeout_seconds):
            baseline_snapshot = await collector.collect(
                baseline.search(_copy_query(query), checkpoint=None),
                max_items=query.max_items,
            )
    except TimeoutError as exc:
        raise ShadowProviderTimeout("baseline") from exc
    try:
        async with asyncio.timeout(provider_timeout_seconds):
            candidate_snapshot = await collector.collect(
                candidate.search(_copy_query(query), checkpoint=None),
                max_items=query.max_items,
            )
    except TimeoutError as exc:
        raise ShadowProviderTimeout("candidate") from exc
    report = compare_shadow(baseline_snapshot, candidate_snapshot)
    accepted, reasons = (policy or ShadowAcceptancePolicy()).evaluate(report)
    return ShadowRunResult(report, accepted, reasons)


def _copy_query(query: SearchQuery) -> SearchQuery:
    return SearchQuery(
        keyword_id=query.keyword_id,
        name=query.name,
        include_terms=list(query.include_terms),
        max_items=query.max_items,
    )
