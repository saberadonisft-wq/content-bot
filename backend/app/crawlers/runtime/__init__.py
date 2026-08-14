"""Provider-neutral crawler runtime primitives."""

from .auth import (
    AuthState,
    AuthStateMachine,
    AuthTransition,
    SessionProbe,
    SessionStatus,
)
from .browser import (
    BrowserDriver,
    BrowserExecutableNotFound,
    BrowserExecutableResolver,
    BrowserHandle,
    BrowserLaunchRequest,
    BrowserSession,
    BrowserSessionState,
)
from .contracts import (
    CancellationToken,
    CommentRecord,
    ContentRecord,
    CrawlerEvent,
    CrawlerEventType,
    RunBudgets,
    RunContext,
)
from .dom_structure import (
    DomLandmark,
    DomStructureObservation,
    observe_dom_structure,
)
from .errors import CrawlerErrorCode, CrawlerFailure
from .limiter import AdaptiveRateLimiter, RatePolicy
from .media_downloader import (
    BoundedMediaDownloader,
    MediaArtifact,
    MediaDownloadPolicy,
)
from .observation import ContractObservation, sanitize_http_observation
from .paginator import Page, PageBatch, bounded_pages
from .playwright_driver import (
    PlaywrightBrowserHandle,
    PlaywrightPersistentDriver,
    PlaywrightUnavailable,
)
from .privacy import IdentityPseudonymizer, PseudonymKeyStore
from .profiles import ProfileInUse, ProfileLock, ProfileNamespace, ProfileRef
from .protocol import (
    BoundedMessageBuffer,
    SequenceTracker,
    WorkerEnvelope,
    WorkerMessageKind,
    decode_message,
    encode_message,
    safe_diagnostic,
    sanitized_payload,
)
from .shadow import (
    ShadowAcceptancePolicy,
    ShadowCollector,
    ShadowReport,
    ShadowSnapshot,
    compare_shadow,
)
from .supervisor import CrawlerSupervisor, SupervisorResult
from .worker_process import (
    WorkerControl,
    WorkerExited,
    WorkerProcessResult,
    WorkerProcessSpec,
    WorkerProcessSupervisor,
    WorkerProtocolError,
    safe_worker_environment,
)

__all__ = [
    "AdaptiveRateLimiter",
    "AuthState",
    "AuthStateMachine",
    "AuthTransition",
    "BoundedMediaDownloader",
    "BoundedMessageBuffer",
    "BrowserDriver",
    "BrowserExecutableNotFound",
    "BrowserExecutableResolver",
    "BrowserHandle",
    "BrowserLaunchRequest",
    "BrowserSession",
    "BrowserSessionState",
    "CancellationToken",
    "CommentRecord",
    "ContentRecord",
    "ContractObservation",
    "CrawlerErrorCode",
    "CrawlerEvent",
    "CrawlerEventType",
    "CrawlerFailure",
    "CrawlerSupervisor",
    "DomLandmark",
    "DomStructureObservation",
    "IdentityPseudonymizer",
    "MediaArtifact",
    "MediaDownloadPolicy",
    "Page",
    "PageBatch",
    "PlaywrightBrowserHandle",
    "PlaywrightPersistentDriver",
    "PlaywrightUnavailable",
    "ProfileInUse",
    "ProfileLock",
    "ProfileNamespace",
    "ProfileRef",
    "PseudonymKeyStore",
    "RatePolicy",
    "RunBudgets",
    "RunContext",
    "SequenceTracker",
    "SessionProbe",
    "SessionStatus",
    "ShadowAcceptancePolicy",
    "ShadowCollector",
    "ShadowReport",
    "ShadowSnapshot",
    "SupervisorResult",
    "WorkerControl",
    "WorkerEnvelope",
    "WorkerExited",
    "WorkerMessageKind",
    "WorkerProcessResult",
    "WorkerProcessSpec",
    "WorkerProcessSupervisor",
    "WorkerProtocolError",
    "bounded_pages",
    "compare_shadow",
    "decode_message",
    "encode_message",
    "observe_dom_structure",
    "safe_diagnostic",
    "safe_worker_environment",
    "sanitize_http_observation",
    "sanitized_payload",
]
