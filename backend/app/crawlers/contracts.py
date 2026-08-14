"""Stable contracts for the Content Bot crawler registry.

The objects in this module contain declarative metadata only.  Runtime callables,
credentials, cookies, selectors and provider payloads deliberately live outside
the manifest so the catalog can be serialized and validated safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Operation(StrEnum):
    """Operations which a provider may expose to the run planner or UI."""

    SEARCH = "search"
    SCAN_CHANNEL = "scan_channel"
    FETCH_DETAIL = "fetch_detail"
    LIST_CREATOR = "list_creator"
    LIST_COMMENTS = "list_comments"
    LIST_CHILD_COMMENTS = "list_child_comments"
    MEDIA_METADATA = "media_metadata"
    MEDIA_DOWNLOAD = "media_download"
    RENDER_EMBED = "render_embed"


class TargetKind(StrEnum):
    KEYWORD = "keyword"
    FEED = "feed"
    CHANNEL = "channel"
    CREATOR = "creator"
    CONTENT_URL = "content_url"
    CONTENT_ID = "content_id"
    HASHTAG = "hashtag"
    ACCOUNT = "account"
    PAGE = "page"
    SUBREDDIT = "subreddit"


class Coverage(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class ImplementationState(StrEnum):
    IMPLEMENTED = "implemented"
    PLANNED = "planned"


class AvailabilityState(StrEnum):
    READY = "ready"
    SETUP_REQUIRED = "setup_required"
    AUTH_REQUIRED = "auth_required"
    PERMISSION_REQUIRED = "permission_required"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    DISABLED_BY_POLICY = "disabled_by_policy"


class AuthMode(StrEnum):
    NONE = "none"
    API_KEY = "api_key"
    APP_TOKEN = "app_token"
    BEARER_TOKEN = "bearer_token"
    OAUTH = "oauth"
    BROWSER_PROFILE = "browser_profile"


class ExecutionLane(StrEnum):
    IN_PROCESS = "in_process"
    ISOLATED_BROWSER = "isolated_browser"
    EMBED_ONLY = "embed_only"
    WEBHOOK = "webhook"


class SchedulePolicy(StrEnum):
    BACKGROUND_SAFE = "background_safe"
    MANUAL_ONLY = "manual_only"
    EVENT_DRIVEN = "event_driven"
    DISABLED = "disabled"


class PolicyState(StrEnum):
    ALLOWED = "allowed"
    APPROVAL_REQUIRED = "approval_required"
    DISABLED = "disabled"
    LEGACY_ONLY = "legacy_only"


@dataclass(frozen=True, slots=True)
class DomainRule:
    """A normalized DNS root owned by one source.

    ``include_subdomains`` never means a textual suffix match.  Consumers must
    use the DNS-label-aware matcher in :mod:`target_detection`.
    """

    root: str
    include_subdomains: bool = True


@dataclass(frozen=True, slots=True)
class ConfigRequirement:
    """A safe configuration descriptor; ``key`` is never a secret value."""

    key: str
    label: str
    secret: bool = False
    required: bool = True


@dataclass(frozen=True, slots=True)
class MetricSpec:
    id: str
    label: str
    semantics: str
    legacy_key: str | None = None


@dataclass(frozen=True, slots=True)
class CrawlBudgets:
    max_items: int = 500
    max_requests: int = 100
    deadline_seconds: int = 900
    max_root_comments: int = 0
    max_children_per_root: int = 0
    max_total_comments: int = 0
    max_media_files: int = 0
    max_media_bytes: int = 0


@dataclass(frozen=True, slots=True)
class ProviderOperationSpec:
    """Static claim made by one provider about one operation.

    Unsupported operations are omitted from a provider.  The registry can then
    synthesize ``coverage=unsupported`` without presenting a no-op as a real
    implementation.
    """

    operation: Operation
    target_kinds: tuple[TargetKind, ...]
    coverage: Coverage
    implementation: ImplementationState
    auth_modes: tuple[AuthMode, ...]
    schedule_policy: SchedulePolicy
    checkpoint_codec: str | None = None
    handler_key: str | None = None
    content_kinds: tuple[str, ...] = ()
    metric_ids: tuple[str, ...] = ()
    cta: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderManifest:
    id: str
    label: str
    access_basis: str
    execution_lane: ExecutionLane
    policy_state: PolicyState
    operations: tuple[ProviderOperationSpec, ...]
    config_requirements: tuple[ConfigRequirement, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceManifest:
    id: str
    legacy_aliases: tuple[str, ...]
    label: str
    order: int
    group_id: str
    group_label: str
    domain_rules: tuple[DomainRule, ...]
    providers: tuple[ProviderManifest, ...]
    default_provider_id: str
    content_kinds: tuple[str, ...]
    metrics: tuple[MetricSpec, ...]
    primary_operation: Operation
    default_budgets: CrawlBudgets = field(default_factory=CrawlBudgets)
    coverage_disclaimer: str | None = None


@dataclass(frozen=True, slots=True)
class OperationCapability:
    """Runtime view produced by combining a manifest claim and a health probe."""

    source_id: str
    provider_id: str | None
    operation: Operation
    coverage: Coverage
    implementation: ImplementationState
    availability: AvailabilityState | None
    reason_code: str | None = None
    safe_message: str | None = None
    checked_at: datetime | None = None
    quota_remaining: int | None = None
    quota_reset_at: datetime | None = None

    @property
    def enabled(self) -> bool:
        return (
            self.coverage is not Coverage.UNSUPPORTED
            and self.implementation is ImplementationState.IMPLEMENTED
            and self.availability is AvailabilityState.READY
        )


@dataclass(frozen=True, slots=True)
class TargetMatch:
    source_id: str
    normalized_url: str
    host: str
    matched_domain: str
    configured_domain: bool = False
