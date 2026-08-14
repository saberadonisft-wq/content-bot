"""Manual, aggregate-only live canaries for crawler cutover evidence.

The runner intentionally has no database dependency.  It consumes records in
memory, enforces tiny caller budgets, and emits only aggregate observations.
Queries, target URLs, provider payloads, account identifiers and content text
are never included in the report.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ..crawlers import SOURCE_REGISTRY
from ..crawlers.contracts import Operation, PolicyState
from ..crawlers.registry import SourceRegistry
from ..crawlers.runtime import CrawlerErrorCode, CrawlerFailure
from .channel_scans import ChannelUnavailable, normalize_channel, scan_channel
from .connectors import (
    RawContentItem,
    SearchQuery,
    SourceConnector,
    default_connectors,
)

REPORT_SCHEMA = "cbce.live-canary.v1"
_SUPPORTED_OPERATIONS = frozenset({Operation.SEARCH, Operation.SCAN_CHANNEL})
_READY_STATES = frozenset({"ready"})


class CanaryState(StrEnum):
    PASSED = "passed"
    SKIPPED = "skipped"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class CanaryRequest:
    source_id: str
    operation: Operation
    query: str | None = field(default=None, repr=False)
    target_url: str | None = field(default=None, repr=False)
    include_terms: tuple[str, ...] = field(default=(), repr=False)
    max_items: int = 3
    max_requests: int = 5
    deadline_seconds: float = 60
    provider_id: str | None = None
    allow_legacy: bool = False

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("Canary source_id is required")
        if self.operation not in _SUPPORTED_OPERATIONS:
            raise ValueError("Canary operation is not supported by this runner")
        if not 1 <= self.max_items <= 5:
            raise ValueError("Canary max_items must be between 1 and 5")
        if not 1 <= self.max_requests <= 10:
            raise ValueError("Canary max_requests must be between 1 and 10")
        if not 1 <= self.deadline_seconds <= 300:
            raise ValueError("Canary deadline_seconds must be between 1 and 300")
        if self.operation is Operation.SEARCH and not str(self.query or "").strip():
            raise ValueError("Search canary requires a query")
        if self.operation is Operation.SCAN_CHANNEL and not str(
            self.target_url or ""
        ).strip():
            raise ValueError("Channel canary requires target_url")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> CanaryRequest:
        allowed = {
            "source_id",
            "operation",
            "query",
            "target_url",
            "include_terms",
            "max_items",
            "max_requests",
            "deadline_seconds",
            "provider_id",
            "allow_legacy",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError("Canary request contains unsupported fields")
        terms = payload.get("include_terms") or ()
        if not isinstance(terms, (list, tuple)) or not all(
            isinstance(term, str) for term in terms
        ):
            raise ValueError("Canary include_terms must be a string array")
        try:
            operation = Operation(str(payload.get("operation") or ""))
        except ValueError as exc:
            raise ValueError("Canary operation is invalid") from exc
        return cls(
            source_id=str(payload.get("source_id") or ""),
            operation=operation,
            query=(
                str(payload["query"])
                if payload.get("query") is not None
                else None
            ),
            target_url=(
                str(payload["target_url"])
                if payload.get("target_url") is not None
                else None
            ),
            include_terms=tuple(str(term) for term in terms),
            max_items=int(payload.get("max_items", 3)),
            max_requests=int(payload.get("max_requests", 5)),
            deadline_seconds=float(payload.get("deadline_seconds", 60)),
            provider_id=(
                str(payload["provider_id"])
                if payload.get("provider_id") is not None
                else None
            ),
            allow_legacy=bool(payload.get("allow_legacy", False)),
        )


@dataclass(slots=True)
class CanaryReport:
    canary_id: str
    source_id: str
    provider_id: str | None
    operation: str
    state: CanaryState
    started_at: str
    completed_at: str
    elapsed_ms: int
    input_digest: str
    max_items: int
    max_requests: int
    deadline_seconds: float
    items_observed: int = 0
    unique_items_observed: int = 0
    earliest_published_at: str | None = None
    latest_published_at: str | None = None
    completeness: dict[str, int] = field(default_factory=dict)
    metric_keys: tuple[str, ...] = ()
    warning_codes: tuple[str, ...] = ()
    error_code: str | None = None
    safe_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA,
            "canary_id": self.canary_id,
            "source_id": self.source_id,
            "provider_id": self.provider_id,
            "operation": self.operation,
            "state": self.state.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_ms": self.elapsed_ms,
            "input_digest": self.input_digest,
            "budgets": {
                "max_items": self.max_items,
                "max_requests": self.max_requests,
                "deadline_seconds": self.deadline_seconds,
            },
            "observation": {
                "items": self.items_observed,
                "unique_items": self.unique_items_observed,
                "earliest_published_at": self.earliest_published_at,
                "latest_published_at": self.latest_published_at,
                "completeness": dict(self.completeness),
                "metric_keys": list(self.metric_keys),
            },
            "warning_codes": list(self.warning_codes),
            "error_code": self.error_code,
            "safe_message": self.safe_message,
            "persisted": False,
        }


class _Observation:
    def __init__(self) -> None:
        self.count = 0
        self.identities: set[str] = set()
        self.timestamps: list[datetime] = []
        self.completeness = {
            "title": 0,
            "body": 0,
            "author": 0,
            "published_at": 0,
            "metrics": 0,
        }
        self.metric_keys: set[str] = set()

    def observe(self, source_id: str, item: RawContentItem) -> None:
        self.count += 1
        identity = f"{source_id}\0{item.external_id}".encode()
        self.identities.add(hashlib.sha256(identity).hexdigest())
        self.completeness["title"] += int(bool(item.title.strip()))
        self.completeness["body"] += int(bool(item.body_snippet.strip()))
        self.completeness["author"] += int(bool(item.author.strip()))
        self.completeness["metrics"] += int(bool(item.metrics))
        self.metric_keys.update(str(key) for key in item.metrics)
        if item.published_at is not None:
            stamp = item.published_at
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            self.timestamps.append(stamp.astimezone(UTC))
            self.completeness["published_at"] += 1


class CrawlerCanaryRunner:
    def __init__(
        self,
        *,
        connectors: Mapping[str, SourceConnector] | None = None,
        registry: SourceRegistry = SOURCE_REGISTRY,
        channel_scanner: Callable[
            [dict[str, Any], SearchQuery], AsyncIterator[RawContentItem]
        ] = scan_channel,
    ) -> None:
        self.connectors = dict(connectors or default_connectors())
        self.registry = registry
        self.channel_scanner = channel_scanner

    async def run(self, request: CanaryRequest) -> CanaryReport:
        started_wall = datetime.now(UTC)
        started_clock = time.monotonic()
        canary_id = f"canary-{uuid.uuid4().hex}"
        source_id = self.registry.resolve_id(request.source_id)
        provider_id: str | None = request.provider_id
        warning_codes: set[str] = set()
        observation = _Observation()
        state = CanaryState.FAILED
        error_code: str | None = None
        safe_message: str | None = None

        try:
            manifest = self.registry.require(source_id)
            provider_id, operation_spec = self.registry.executable_provider(
                source_id,
                request.operation,
                preferred_provider_id=request.provider_id,
            )
            provider = next(
                provider
                for provider in manifest.providers
                if provider.id == provider_id
            )
            if provider.policy_state is PolicyState.LEGACY_ONLY and not request.allow_legacy:
                state = CanaryState.SKIPPED
                error_code = "LEGACY_PROVIDER_REQUIRES_OPT_IN"
                safe_message = "Legacy provider canary requires allow_legacy=true."
                return self._report(
                    request,
                    canary_id,
                    source_id,
                    provider_id,
                    state,
                    started_wall,
                    started_clock,
                    observation,
                    warning_codes,
                    error_code,
                    safe_message,
                )
            if not operation_spec.handler_key:
                raise ValueError("Canary operation has no registered handler")
            connector = self.connectors.get(source_id)
            if connector is None:
                state = CanaryState.SKIPPED
                error_code = "HANDLER_NOT_BOUND"
                safe_message = "Canary connector is not bound."
                return self._report(
                    request,
                    canary_id,
                    source_id,
                    provider_id,
                    state,
                    started_wall,
                    started_clock,
                    observation,
                    warning_codes,
                    error_code,
                    safe_message,
                )
            health = await connector.healthcheck()
            if health.state not in _READY_STATES:
                state = CanaryState.SKIPPED
                error_code = health.reason_code or "PROVIDER_NOT_READY"
                safe_message = "Provider prerequisites are not ready."
                return self._report(
                    request,
                    canary_id,
                    source_id,
                    provider_id,
                    state,
                    started_wall,
                    started_clock,
                    observation,
                    warning_codes,
                    error_code,
                    safe_message,
                )

            async def warning(code: str, _message: str) -> None:
                warning_codes.add(str(code)[:100])

            query = SearchQuery(
                keyword_id=0,
                name=str(request.query or "live-canary"),
                include_terms=list(request.include_terms),
                max_items=request.max_items,
                request_budget=request.max_requests,
                deadline_seconds=request.deadline_seconds,
                warning_callback=warning,
            )
            iterator = self._iterator(request, connector, query, source_id)
            async with aclosing(iterator):
                async with asyncio.timeout(request.deadline_seconds):
                    async for item in iterator:
                        observation.observe(source_id, item)
                        if observation.count >= request.max_items:
                            break
            state = CanaryState.PASSED
        except TimeoutError:
            state = CanaryState.TIMED_OUT
            error_code = CrawlerErrorCode.DEADLINE_EXCEEDED.value
            safe_message = "Canary deadline was exceeded."
        except CrawlerFailure as exc:
            state = CanaryState.FAILED
            error_code = exc.code.value
            safe_message = exc.safe_message
        except ChannelUnavailable:
            state = CanaryState.SKIPPED
            error_code = "CHANNEL_UNAVAILABLE"
            safe_message = "Channel scan prerequisites are unavailable."
        except (KeyError, ValueError):
            state = CanaryState.SKIPPED
            error_code = "OPERATION_UNAVAILABLE"
            safe_message = "Requested canary operation is unavailable."
        except Exception:
            state = CanaryState.FAILED
            error_code = "UNEXPECTED_FAILURE"
            safe_message = "Canary failed without exposing provider diagnostics."

        return self._report(
            request,
            canary_id,
            source_id,
            provider_id,
            state,
            started_wall,
            started_clock,
            observation,
            warning_codes,
            error_code,
            safe_message,
        )

    async def _iterator(
        self,
        request: CanaryRequest,
        connector: SourceConnector,
        query: SearchQuery,
        source_id: str,
    ) -> AsyncIterator[RawContentItem]:
        if request.operation is Operation.SEARCH:
            iterator = connector.search(query, checkpoint=None)
            async with aclosing(iterator):
                async for item in iterator:
                    yield item
            return
        channel = normalize_channel(
            {
                "id": f"canary-{uuid.uuid4().hex}",
                "url": request.target_url,
                "label": "Live canary",
                "enabled": True,
            }
        )
        if channel["source_id"] != source_id:
            raise ValueError("Canary target does not belong to the requested source")
        iterator = self.channel_scanner(channel, query)
        async with aclosing(iterator):
            async for item in iterator:
                yield item

    @staticmethod
    def _report(
        request: CanaryRequest,
        canary_id: str,
        source_id: str,
        provider_id: str | None,
        state: CanaryState,
        started_wall: datetime,
        started_clock: float,
        observation: _Observation,
        warning_codes: set[str],
        error_code: str | None,
        safe_message: str | None,
    ) -> CanaryReport:
        completed = datetime.now(UTC)
        input_material = json.dumps(
            {
                "source_id": source_id,
                "operation": request.operation.value,
                "query": request.query,
                "target_url": request.target_url,
                "include_terms": request.include_terms,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        timestamps = observation.timestamps
        return CanaryReport(
            canary_id=canary_id,
            source_id=source_id,
            provider_id=provider_id,
            operation=request.operation.value,
            state=state,
            started_at=started_wall.isoformat(),
            completed_at=completed.isoformat(),
            elapsed_ms=max(0, int((time.monotonic() - started_clock) * 1_000)),
            input_digest=hashlib.sha256(input_material).hexdigest(),
            max_items=request.max_items,
            max_requests=request.max_requests,
            deadline_seconds=request.deadline_seconds,
            items_observed=observation.count,
            unique_items_observed=len(observation.identities),
            earliest_published_at=(min(timestamps).isoformat() if timestamps else None),
            latest_published_at=(max(timestamps).isoformat() if timestamps else None),
            completeness=dict(observation.completeness),
            metric_keys=tuple(sorted(observation.metric_keys)),
            warning_codes=tuple(sorted(warning_codes)),
            error_code=error_code,
            safe_message=safe_message,
        )
