"""Privacy-preserving comparison helpers for crawler provider cutovers.

Shadow samples are deliberately ephemeral.  Raw content is reduced immediately
to keyed identity digests, aggregate field counters and timestamp bounds.  The
serializable report never contains an external ID, URL, title, body or author.
"""

from __future__ import annotations

import hashlib
import hmac
from collections import Counter
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

DEFAULT_FIELDS = (
    "external_id",
    "canonical_url",
    "title",
    "body_snippet",
    "author",
    "published_at",
)


class ShadowItem(Protocol):
    external_id: str
    canonical_url: str
    title: str
    body_snippet: str
    author: str
    published_at: datetime | None
    metrics: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ShadowSnapshot:
    """Non-serializable comparison state for one provider invocation."""

    item_count: int
    id_digests: frozenset[str] = field(repr=False)
    url_digests: frozenset[str] = field(repr=False)
    field_counts: Mapping[str, int]
    metric_counts: Mapping[str, int]
    earliest_published_at: datetime | None
    latest_published_at: datetime | None

    @property
    def unique_id_count(self) -> int:
        return len(self.id_digests)

    @property
    def duplicate_id_count(self) -> int:
        return max(0, self.item_count - self.unique_id_count)


@dataclass(frozen=True, slots=True)
class ShadowReport:
    schema_version: str
    baseline_count: int
    candidate_count: int
    baseline_unique_ids: int
    candidate_unique_ids: int
    baseline_duplicate_ids: int
    candidate_duplicate_ids: int
    id_overlap_count: int
    url_overlap_count: int
    baseline_id_recall: float
    candidate_id_precision: float
    baseline_url_recall: float
    candidate_url_precision: float
    count_delta_ratio: float
    baseline_field_completeness: Mapping[str, float]
    candidate_field_completeness: Mapping[str, float]
    baseline_metric_completeness: Mapping[str, float]
    candidate_metric_completeness: Mapping[str, float]
    baseline_timestamp_range: tuple[str | None, str | None]
    candidate_timestamp_range: tuple[str | None, str | None]

    def as_dict(self) -> dict[str, object]:
        """Return the only representation intended for logs or artifacts."""

        return {
            "schema_version": self.schema_version,
            "baseline_count": self.baseline_count,
            "candidate_count": self.candidate_count,
            "baseline_unique_ids": self.baseline_unique_ids,
            "candidate_unique_ids": self.candidate_unique_ids,
            "baseline_duplicate_ids": self.baseline_duplicate_ids,
            "candidate_duplicate_ids": self.candidate_duplicate_ids,
            "id_overlap_count": self.id_overlap_count,
            "url_overlap_count": self.url_overlap_count,
            "baseline_id_recall": self.baseline_id_recall,
            "candidate_id_precision": self.candidate_id_precision,
            "baseline_url_recall": self.baseline_url_recall,
            "candidate_url_precision": self.candidate_url_precision,
            "count_delta_ratio": self.count_delta_ratio,
            "baseline_field_completeness": dict(self.baseline_field_completeness),
            "candidate_field_completeness": dict(self.candidate_field_completeness),
            "baseline_metric_completeness": dict(self.baseline_metric_completeness),
            "candidate_metric_completeness": dict(self.candidate_metric_completeness),
            "baseline_timestamp_range": list(self.baseline_timestamp_range),
            "candidate_timestamp_range": list(self.candidate_timestamp_range),
        }


@dataclass(frozen=True, slots=True)
class ShadowAcceptancePolicy:
    min_baseline_id_recall: float = 0.5
    max_count_delta_ratio: float = 0.5
    min_required_field_completeness: float = 0.95
    required_candidate_fields: tuple[str, ...] = (
        "external_id",
        "canonical_url",
        "title",
    )

    def __post_init__(self) -> None:
        for value in (
            self.min_baseline_id_recall,
            self.max_count_delta_ratio,
            self.min_required_field_completeness,
        ):
            if not 0 <= value <= 1:
                raise ValueError("Shadow acceptance ratios must be between 0 and 1")

    def evaluate(self, report: ShadowReport) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        if report.baseline_count <= 0:
            reasons.append("BASELINE_EMPTY")
        if report.candidate_count <= 0:
            reasons.append("CANDIDATE_EMPTY")
        if report.baseline_id_recall < self.min_baseline_id_recall:
            reasons.append("ID_OVERLAP_BELOW_THRESHOLD")
        if report.count_delta_ratio > self.max_count_delta_ratio:
            reasons.append("COUNT_DELTA_ABOVE_THRESHOLD")
        for field_name in self.required_candidate_fields:
            ratio = report.candidate_field_completeness.get(field_name, 0.0)
            if ratio < self.min_required_field_completeness:
                reasons.append(f"FIELD_INCOMPLETE:{field_name}")
        return not reasons, tuple(reasons)


class ShadowCollector:
    """Reduce one item stream without retaining its content."""

    def __init__(
        self,
        comparison_key: bytes,
        *,
        allowed_metric_keys: tuple[str, ...] = (),
    ) -> None:
        if len(comparison_key) < 16:
            raise ValueError("Shadow comparison key must contain at least 16 bytes")
        self._key = bytes(comparison_key)
        self._allowed_metrics = frozenset(allowed_metric_keys)

    async def collect(
        self,
        items: AsyncIterable[ShadowItem],
        *,
        max_items: int,
    ) -> ShadowSnapshot:
        if max_items <= 0:
            raise ValueError("Shadow sample size must be positive")
        item_count = 0
        ids: set[str] = set()
        urls: set[str] = set()
        fields: Counter[str] = Counter()
        metrics: Counter[str] = Counter()
        earliest: datetime | None = None
        latest: datetime | None = None
        async for item in items:
            if item_count >= max_items:
                break
            item_count += 1
            external_id = str(item.external_id or "").strip()
            canonical_url = str(item.canonical_url or "").strip()
            if external_id:
                ids.add(self._digest("id", external_id))
                fields["external_id"] += 1
            if canonical_url:
                urls.add(self._digest("url", canonical_url))
                fields["canonical_url"] += 1
            for name in ("title", "body_snippet", "author"):
                if str(getattr(item, name, "") or "").strip():
                    fields[name] += 1
            published_at = _utc(item.published_at)
            if published_at is not None:
                fields["published_at"] += 1
                earliest = (
                    published_at if earliest is None else min(earliest, published_at)
                )
                latest = published_at if latest is None else max(latest, published_at)
            for key, value in item.metrics.items():
                if (
                    key in self._allowed_metrics
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    metrics[key] += 1
        return ShadowSnapshot(
            item_count=item_count,
            id_digests=frozenset(ids),
            url_digests=frozenset(urls),
            field_counts=dict(fields),
            metric_counts=dict(metrics),
            earliest_published_at=earliest,
            latest_published_at=latest,
        )

    def _digest(self, namespace: str, value: str) -> str:
        return hmac.new(
            self._key,
            f"{namespace}\x00{value}".encode(),
            hashlib.sha256,
        ).hexdigest()


def compare_shadow(
    baseline: ShadowSnapshot,
    candidate: ShadowSnapshot,
) -> ShadowReport:
    id_overlap = len(baseline.id_digests & candidate.id_digests)
    url_overlap = len(baseline.url_digests & candidate.url_digests)
    field_names = (
        set(DEFAULT_FIELDS) | set(baseline.field_counts) | set(candidate.field_counts)
    )
    metric_names = set(baseline.metric_counts) | set(candidate.metric_counts)
    return ShadowReport(
        schema_version="cbce.shadow.aggregate.v1",
        baseline_count=baseline.item_count,
        candidate_count=candidate.item_count,
        baseline_unique_ids=baseline.unique_id_count,
        candidate_unique_ids=candidate.unique_id_count,
        baseline_duplicate_ids=baseline.duplicate_id_count,
        candidate_duplicate_ids=candidate.duplicate_id_count,
        id_overlap_count=id_overlap,
        url_overlap_count=url_overlap,
        baseline_id_recall=_ratio(id_overlap, baseline.unique_id_count),
        candidate_id_precision=_ratio(id_overlap, candidate.unique_id_count),
        baseline_url_recall=_ratio(url_overlap, len(baseline.url_digests)),
        candidate_url_precision=_ratio(url_overlap, len(candidate.url_digests)),
        count_delta_ratio=(
            abs(candidate.item_count - baseline.item_count)
            / max(1, baseline.item_count)
        ),
        baseline_field_completeness={
            name: _ratio(baseline.field_counts.get(name, 0), baseline.item_count)
            for name in sorted(field_names)
        },
        candidate_field_completeness={
            name: _ratio(candidate.field_counts.get(name, 0), candidate.item_count)
            for name in sorted(field_names)
        },
        baseline_metric_completeness={
            name: _ratio(baseline.metric_counts.get(name, 0), baseline.item_count)
            for name in sorted(metric_names)
        },
        candidate_metric_completeness={
            name: _ratio(candidate.metric_counts.get(name, 0), candidate.item_count)
            for name in sorted(metric_names)
        },
        baseline_timestamp_range=_timestamp_range(baseline),
        candidate_timestamp_range=_timestamp_range(candidate),
    )


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator > 0 else 0.0


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp_range(snapshot: ShadowSnapshot) -> tuple[str | None, str | None]:
    def encode(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    return encode(snapshot.earliest_published_at), encode(snapshot.latest_published_at)
