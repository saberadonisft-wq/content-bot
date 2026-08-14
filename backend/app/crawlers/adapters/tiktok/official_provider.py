"""Permission-gated TikTok Login Kit and Display API read providers.

Only official ``open.tiktokapis.com`` endpoints are used. The Display API is
limited to the TikTok account that explicitly authorized ``video.list``; it is
not a public keyword-search or arbitrary-creator crawler.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import httpx

from ...runtime import (
    CancellationToken,
    ContentRecord,
    CrawlerErrorCode,
    CrawlerFailure,
    IdentityPseudonymizer,
    Page,
    RunContext,
)
from .targets import TikTokTargetKind, parse_tiktok_target

_OFFICIAL_API_ROOT = "https://open.tiktokapis.com"
_AUTHORIZE_ROOT = "https://www.tiktok.com/v2/auth/authorize/"
_VIDEO_FIELDS = (
    "id,create_time,share_url,video_description,duration,height,width,title,"
    "like_count,comment_count,share_count,view_count"
)
_VIDEO_ID = re.compile(r"[1-9][0-9]{5,24}")
_CLIENT_KEY = re.compile(r"[A-Za-z0-9._-]{4,200}")
_OPEN_ID = re.compile(r"[A-Za-z0-9._-]{4,200}")
_STATE = re.compile(r"[A-Za-z0-9_-]{32,256}")
_SCOPE = re.compile(r"[a-z][a-z0-9_.]{1,79}")
_REQUIRED_OAUTH_SCOPES = frozenset(
    {"user.info.basic", "user.info.profile", "video.list"}
)


@dataclass(frozen=True, slots=True)
class TikTokOAuthConfig:
    client_key: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    scopes: frozenset[str] = _REQUIRED_OAUTH_SCOPES

    def __post_init__(self) -> None:
        parsed = urlsplit(self.redirect_uri)
        if not _CLIENT_KEY.fullmatch(self.client_key):
            raise ValueError("TikTok client key is invalid")
        if not self.client_secret.strip() or len(self.client_secret) > 1_024:
            raise ValueError("TikTok client secret is invalid")
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(self.redirect_uri) >= 512
        ):
            raise ValueError(
                "TikTok redirect URI must be an approved static HTTPS URL"
            )
        scopes = frozenset(self.scopes)
        if (
            not _REQUIRED_OAUTH_SCOPES.issubset(scopes)
            or len(scopes) > 20
            or any(not _SCOPE.fullmatch(scope) for scope in scopes)
        ):
            raise ValueError("TikTok OAuth scopes are invalid")
        object.__setattr__(self, "scopes", scopes)


@dataclass(frozen=True, slots=True)
class TikTokTokenBundle:
    open_id: str
    scopes: frozenset[str]
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int
    refresh_expires_in: int

    def __post_init__(self) -> None:
        if not _OPEN_ID.fullmatch(self.open_id):
            raise ValueError("TikTok open ID is invalid")
        if (
            not self.access_token.strip()
            or len(self.access_token) > 4_096
            or not self.refresh_token.strip()
            or len(self.refresh_token) > 4_096
            or self.expires_in <= 0
            or self.refresh_expires_in <= 0
        ):
            raise ValueError("TikTok OAuth token bundle is invalid")
        scopes = frozenset(self.scopes)
        if not scopes or any(not _SCOPE.fullmatch(scope) for scope in scopes):
            raise ValueError("TikTok granted scopes are invalid")
        object.__setattr__(self, "scopes", scopes)


@dataclass(frozen=True, slots=True)
class TikTokUserProfile:
    open_id: str
    username: str

    def __post_init__(self) -> None:
        if not _OPEN_ID.fullmatch(self.open_id):
            raise ValueError("TikTok profile open ID is invalid")
        try:
            target = parse_tiktok_target(f"https://www.tiktok.com/@{self.username}")
        except ValueError as exc:
            raise ValueError("TikTok profile username is invalid") from exc
        if target.kind is not TikTokTargetKind.CREATOR:
            raise ValueError("TikTok profile username is invalid")
        object.__setattr__(self, "username", target.external_id.casefold())


@dataclass(frozen=True, slots=True)
class TikTokDisplayConfig:
    open_id: str
    granted_scopes: frozenset[str]
    access_token: str = field(repr=False)
    base_url: str = _OFFICIAL_API_ROOT

    def __post_init__(self) -> None:
        if not _OPEN_ID.fullmatch(self.open_id):
            raise ValueError("TikTok Display open ID is invalid")
        if not self.access_token.strip() or len(self.access_token) > 4_096:
            raise ValueError("TikTok Display access token is invalid")
        scopes = frozenset(self.granted_scopes)
        if "video.list" not in scopes:
            raise ValueError("TikTok Display API requires the video.list scope")
        if any(not _SCOPE.fullmatch(scope) for scope in scopes):
            raise ValueError("TikTok Display granted scopes are invalid")
        if self.base_url != _OFFICIAL_API_ROOT:
            raise ValueError("TikTok Display API host is not approved")
        object.__setattr__(self, "granted_scopes", scopes)


@dataclass(frozen=True, slots=True)
class TikTokVideo:
    video_id: str
    share_url: str
    title: str = ""
    description: str = ""
    created_at: datetime | None = None
    duration_seconds: int = 0
    width: int = 0
    height: int = 0
    metrics: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TikTokVideoPage:
    items: tuple[TikTokVideo, ...]
    next_cursor_ms: int | None


@dataclass(frozen=True, slots=True)
class TikTokDisplayCursor:
    cursor_ms: int | None = None

    def __post_init__(self) -> None:
        if self.cursor_ms is not None and not 1 <= self.cursor_ms <= 9_999_999_999_999:
            raise ValueError("TikTok Display cursor is invalid")

    def encode(self) -> str:
        return f"v1d:{self.cursor_ms or 0}"

    @classmethod
    def decode(cls, value: str | None) -> TikTokDisplayCursor:
        if value is None:
            return cls()
        match = re.fullmatch(r"v1d:([0-9]{1,13})", value)
        if not match:
            raise _parse_failure("TikTok Display checkpoint is invalid.")
        cursor = int(match.group(1))
        return cls(cursor or None)


class TikTokDisplayProvider(Protocol):
    async def open(
        self, context: RunContext, cancellation: CancellationToken
    ) -> None: ...

    async def list_videos(
        self, *, cursor_ms: int | None, max_count: int
    ) -> TikTokVideoPage: ...

    async def query_videos(self, video_ids: tuple[str, ...]) -> tuple[TikTokVideo, ...]: ...

    async def close(self) -> None: ...


class TikTokOAuthClient:
    """Server-side Login Kit token lifecycle; tokens never enter URL queries."""

    def __init__(
        self,
        config: TikTokOAuthConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        _validate_client(client)
        self._client = client
        self._owns_client = client is None

    @staticmethod
    def new_state() -> str:
        return secrets.token_urlsafe(32)

    def authorization_url(self, state: str) -> str:
        if not _STATE.fullmatch(state):
            raise ValueError("TikTok OAuth state is invalid")
        return _AUTHORIZE_ROOT + "?" + urlencode(
            {
                "client_key": self.config.client_key,
                "scope": ",".join(sorted(self.config.scopes)),
                "response_type": "code",
                "redirect_uri": self.config.redirect_uri,
                "state": state,
            }
        )

    async def exchange_code(self, code: str) -> TikTokTokenBundle:
        if not code.strip() or len(code) > 2_048:
            raise ValueError("TikTok authorization code is invalid")
        return await self._token_request(
            {
                "client_key": self.config.client_key,
                "client_secret": self.config.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.config.redirect_uri,
            }
        )

    async def refresh(self, refresh_token: str) -> TikTokTokenBundle:
        if not refresh_token.strip() or len(refresh_token) > 4_096:
            raise ValueError("TikTok refresh token is invalid")
        return await self._token_request(
            {
                "client_key": self.config.client_key,
                "client_secret": self.config.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )

    async def user_profile(self, access_token: str) -> TikTokUserProfile:
        if not access_token.strip() or len(access_token) > 4_096:
            raise ValueError("TikTok access token is invalid")
        client = self._ensure_client()
        try:
            response = await client.get(
                "/v2/user/info/",
                params={"fields": "open_id,username"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.TRANSPORT_ERROR,
                "TikTok profile verification failed.",
                retryable=True,
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise _parse_failure("TikTok profile response changed.") from exc
        if not isinstance(payload, Mapping):
            raise _parse_failure("TikTok profile response changed.")
        error = payload.get("error")
        code = str(error.get("code") or "") if isinstance(error, Mapping) else ""
        if response.status_code >= 400 or code not in {"", "ok"}:
            raise _api_failure(response.status_code, code, response.headers)
        data = payload.get("data")
        user = data.get("user") if isinstance(data, Mapping) else None
        if not isinstance(user, Mapping):
            raise _parse_failure("TikTok profile response changed.")
        try:
            return TikTokUserProfile(
                open_id=str(user.get("open_id") or ""),
                username=str(user.get("username") or ""),
            )
        except ValueError as exc:
            raise _parse_failure("TikTok profile identity is invalid.") from exc

    async def revoke(self, access_token: str) -> None:
        if not access_token.strip() or len(access_token) > 4_096:
            raise ValueError("TikTok access token is invalid")
        response = await self._post_form(
            "/v2/oauth/revoke/",
            {
                "client_key": self.config.client_key,
                "client_secret": self.config.client_secret,
                "token": access_token,
            },
        )
        if response.content:
            try:
                payload = response.json()
            except ValueError as exc:
                raise _parse_failure("TikTok revoke response changed.") from exc
            if isinstance(payload, Mapping) and payload.get("error"):
                raise _oauth_failure(payload)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None and self._owns_client:
            await client.aclose()

    async def _token_request(self, data: Mapping[str, str]) -> TikTokTokenBundle:
        response = await self._post_form("/v2/oauth/token/", data)
        try:
            payload = response.json()
        except ValueError as exc:
            raise _parse_failure("TikTok OAuth returned invalid JSON.") from exc
        if not isinstance(payload, Mapping):
            raise _parse_failure("TikTok OAuth response changed.")
        if response.status_code >= 400 or payload.get("error"):
            raise _oauth_failure(payload, status=response.status_code)
        return _parse_token_bundle(payload)

    async def _post_form(
        self, path: str, data: Mapping[str, str]
    ) -> httpx.Response:
        client = self._ensure_client()
        try:
            response = await client.post(
                path,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.TRANSPORT_ERROR,
                "TikTok OAuth request failed.",
                retryable=True,
            ) from exc
        if response.status_code >= 500:
            raise CrawlerFailure(
                CrawlerErrorCode.TRANSPORT_ERROR,
                "TikTok OAuth service is temporarily unavailable.",
                retryable=True,
            )
        return response

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_OFFICIAL_API_ROOT,
                timeout=30,
                follow_redirects=False,
            )
        return self._client


class TikTokDisplayApiProvider:
    def __init__(
        self,
        config: TikTokDisplayConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        _validate_client(client)
        self._client = client
        self._owns_client = client is None
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        cancellation.raise_if_cancelled()
        if context.source_id != "tiktok" or context.operation not in {
            "scan_channel",
            "list_creator",
            "fetch_detail",
        }:
            raise ValueError("TikTok Display provider context is invalid")
        self._cancellation = cancellation
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url,
                headers={"Authorization": f"Bearer {self.config.access_token}"},
                timeout=30,
                follow_redirects=False,
            )

    async def list_videos(
        self, *, cursor_ms: int | None, max_count: int
    ) -> TikTokVideoPage:
        if cursor_ms is not None and not 1 <= cursor_ms <= 9_999_999_999_999:
            raise ValueError("TikTok Display cursor is invalid")
        if not 1 <= max_count <= 20:
            raise ValueError("TikTok Display page size must be between 1 and 20")
        body: dict[str, int] = {"max_count": max_count}
        if cursor_ms is not None:
            body["cursor"] = cursor_ms
        payload = await self._request(
            "POST",
            "/v2/video/list/",
            params={"fields": _VIDEO_FIELDS},
            json=body,
        )
        data = payload.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("videos"), list):
            raise _parse_failure("TikTok Display video-list response changed.")
        items = tuple(
            video for value in data["videos"] if (video := _parse_video(value))
        )
        has_more = data.get("has_more") is True
        next_cursor = _safe_cursor(data.get("cursor")) if has_more else None
        if has_more and next_cursor is None:
            raise _parse_failure("TikTok Display pagination cursor changed.")
        return TikTokVideoPage(items, next_cursor)

    async def query_videos(
        self, video_ids: tuple[str, ...]
    ) -> tuple[TikTokVideo, ...]:
        unique = tuple(dict.fromkeys(video_ids))
        if not 1 <= len(unique) <= 20 or any(
            not _VIDEO_ID.fullmatch(value) for value in unique
        ):
            raise ValueError("TikTok Display video query is invalid")
        payload = await self._request(
            "POST",
            "/v2/video/query/",
            params={"fields": _VIDEO_FIELDS},
            json={"filters": {"video_ids": list(unique)}},
        )
        data = payload.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("videos"), list):
            raise _parse_failure("TikTok Display video-query response changed.")
        return tuple(
            video for value in data["videos"] if (video := _parse_video(value))
        )

    async def close(self) -> None:
        client, self._client = self._client, None
        self._cancellation = None
        if client is not None and self._owns_client:
            await client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str],
        json: Mapping[str, object],
    ) -> Mapping[str, Any]:
        if self._client is None or self._cancellation is None:
            raise RuntimeError("TikTok Display provider is not open")
        self._cancellation.raise_if_cancelled()
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {self.config.access_token}"},
            )
        except httpx.HTTPError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.TRANSPORT_ERROR,
                "TikTok Display API request failed.",
                retryable=True,
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise _parse_failure("TikTok Display API returned invalid JSON.") from exc
        if not isinstance(payload, Mapping):
            raise _parse_failure("TikTok Display API response changed.")
        error = payload.get("error")
        code = str(error.get("code") or "") if isinstance(error, Mapping) else ""
        if response.status_code >= 400 or (code and code != "ok"):
            raise _api_failure(response.status_code, code, response.headers)
        return payload


class TikTokDisplayVideoListAdapter:
    source_id = "tiktok"
    provider_id = "tiktok_display"
    owns_resources = True

    def __init__(
        self,
        provider: TikTokDisplayProvider,
        pseudonymizer: IdentityPseudonymizer,
        open_id: str,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self.open_id = open_id
        self._cancellation: CancellationToken | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        target_open_id = str(context.target.get("open_id") or "")
        if context.operation not in {"scan_channel", "list_creator"}:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "TikTok Display API only lists an authorized creator's videos.",
            )
        if target_open_id and target_open_id != self.open_id:
            raise CrawlerFailure(
                CrawlerErrorCode.PERMISSION_REQUIRED,
                "TikTok creator is not the account that authorized this connection.",
            )
        self._cancellation = cancellation
        await self.provider.open(context, cancellation)

    async def fetch_page(
        self, context: RunContext, cursor: object | None, limit: int
    ) -> Page[ContentRecord, str]:
        if self._cancellation is None:
            raise RuntimeError("TikTok Display adapter is not open")
        if cursor is not None and not isinstance(cursor, str):
            raise _parse_failure("TikTok Display checkpoint shape is invalid.")
        state = TikTokDisplayCursor.decode(cursor)
        provider_page = await self.provider.list_videos(
            cursor_ms=state.cursor_ms,
            max_count=min(limit, 20),
        )
        records = tuple(
            normalize_tiktok_video(video, self.open_id, self.pseudonymizer)
            for video in provider_page.items[:limit]
        )
        next_cursor = (
            TikTokDisplayCursor(provider_page.next_cursor_ms).encode()
            if provider_page.next_cursor_ms is not None
            else None
        )
        return Page(records, next_cursor, next_cursor is not None)

    async def close(self) -> None:
        self._cancellation = None
        await self.provider.close()


class TikTokDisplayDetailAdapter:
    source_id = "tiktok"
    provider_id = "tiktok_display"
    owns_resources = True

    def __init__(
        self,
        provider: TikTokDisplayProvider,
        pseudonymizer: IdentityPseudonymizer,
        open_id: str,
    ) -> None:
        self.provider = provider
        self.pseudonymizer = pseudonymizer
        self.open_id = open_id
        self._video_id: str | None = None

    async def open(self, context: RunContext, cancellation: CancellationToken) -> None:
        target = context.target.get("url") or context.target.get("content_id")
        try:
            parsed = parse_tiktok_target(str(target))
        except ValueError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "TikTok Display detail target is invalid.",
            ) from exc
        if context.operation != "fetch_detail" or parsed.kind is not TikTokTargetKind.VIDEO:
            raise CrawlerFailure(
                CrawlerErrorCode.UNSUPPORTED,
                "TikTok Display detail requires a video URL.",
            )
        self._video_id = parsed.external_id
        await self.provider.open(context, cancellation)

    async def fetch(self) -> ContentRecord:
        if self._video_id is None:
            raise RuntimeError("TikTok Display detail adapter is not open")
        videos = await self.provider.query_videos((self._video_id,))
        video = next((item for item in videos if item.video_id == self._video_id), None)
        if video is None:
            raise CrawlerFailure(
                CrawlerErrorCode.NOT_FOUND,
                "TikTok video was not found for the authorized account.",
            )
        return normalize_tiktok_video(video, self.open_id, self.pseudonymizer)

    async def close(self) -> None:
        self._video_id = None
        await self.provider.close()


def normalize_tiktok_video(
    video: TikTokVideo,
    open_id: str,
    pseudonymizer: IdentityPseudonymizer,
) -> ContentRecord:
    try:
        target = parse_tiktok_target(video.share_url)
    except ValueError as exc:
        raise _parse_failure("TikTok Display share URL is invalid.") from exc
    if target.kind is not TikTokTargetKind.VIDEO or target.external_id != video.video_id:
        raise _parse_failure("TikTok Display video identity is inconsistent.")
    title = video.title.strip() or video.description.strip() or f"TikTok video {video.video_id}"
    metrics = {
        key: value
        for key, value in video.metrics.items()
        if key in {"like_count", "comment_count", "share_count", "view_count"}
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }
    return ContentRecord(
        source_id="tiktok",
        external_id=video.video_id,
        canonical_url=target.canonical_url,
        title=title[:500],
        body=video.description.strip()[:4_000],
        author_pseudonym=pseudonymizer.pseudonym("tiktok", open_id),
        published_at=video.created_at,
        metrics=metrics,
        provenance={
            "provider_id": "tiktok_display",
            "contract_version": "cbce.tiktok.display-video.v1",
            "access_basis": "authorized_creator",
            "coverage": "authorized_account_only",
            "duration_seconds": max(0, video.duration_seconds),
            "width": max(0, video.width),
            "height": max(0, video.height),
        },
    )


def _parse_video(value: Any) -> TikTokVideo | None:
    if not isinstance(value, Mapping):
        return None
    video_id = str(value.get("id") or "")
    share_url = str(value.get("share_url") or "")
    if not _VIDEO_ID.fullmatch(video_id) or not share_url:
        return None
    timestamp = _safe_int(value.get("create_time"))
    created_at = None
    if timestamp > 0:
        try:
            created_at = datetime.fromtimestamp(timestamp, tz=UTC)
        except (OSError, OverflowError, ValueError):
            created_at = None
    return TikTokVideo(
        video_id=video_id,
        share_url=share_url,
        title=str(value.get("title") or "")[:500],
        description=str(value.get("video_description") or "")[:4_000],
        created_at=created_at,
        duration_seconds=_safe_int(value.get("duration")),
        width=_safe_int(value.get("width")),
        height=_safe_int(value.get("height")),
        metrics={
            "like_count": _safe_int(value.get("like_count")),
            "comment_count": _safe_int(value.get("comment_count")),
            "share_count": _safe_int(value.get("share_count")),
            "view_count": _safe_int(value.get("view_count")),
        },
    )


def _parse_token_bundle(payload: Mapping[str, Any]) -> TikTokTokenBundle:
    scopes = frozenset(
        scope.strip() for scope in str(payload.get("scope") or "").split(",") if scope.strip()
    )
    try:
        return TikTokTokenBundle(
            open_id=str(payload.get("open_id") or ""),
            scopes=scopes,
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_in=int(payload.get("expires_in") or 0),
            refresh_expires_in=int(payload.get("refresh_expires_in") or 0),
        )
    except (TypeError, ValueError) as exc:
        raise _parse_failure("TikTok OAuth token response changed.") from exc


def _validate_client(client: httpx.AsyncClient | None) -> None:
    if client is None:
        return
    if str(client.base_url).rstrip("/") != _OFFICIAL_API_ROOT:
        raise ValueError("Injected TikTok client has an unapproved base URL")
    if client.follow_redirects:
        raise ValueError("TikTok API client must not follow redirects")


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_cursor(value: Any) -> int | None:
    cursor = _safe_int(value)
    return cursor if 1 <= cursor <= 9_999_999_999_999 else None


def _api_failure(
    status: int, code: str, headers: Mapping[str, str]
) -> CrawlerFailure:
    if code in {"scope_not_authorized", "scope_permission_missed"}:
        return CrawlerFailure(
            CrawlerErrorCode.PERMISSION_REQUIRED,
            "TikTok user or app has not granted the required Display API scope.",
        )
    if status == 401 or code == "access_token_invalid":
        return CrawlerFailure(
            CrawlerErrorCode.AUTH_REQUIRED,
            "TikTok access token is invalid, expired or revoked.",
        )
    if status == 429 or code == "rate_limit_exceeded":
        return CrawlerFailure(
            CrawlerErrorCode.RATE_LIMITED,
            "TikTok Display API rate limit was exceeded.",
            retryable=True,
            retry_after_seconds=_retry_after(headers.get("Retry-After")),
        )
    if status >= 500 or code == "internal_error":
        return CrawlerFailure(
            CrawlerErrorCode.TRANSPORT_ERROR,
            "TikTok Display API is temporarily unavailable.",
            retryable=True,
        )
    return CrawlerFailure(
        CrawlerErrorCode.PARSE_CHANGED,
        "TikTok Display API rejected the request.",
    )


def _oauth_failure(
    payload: Mapping[str, Any], *, status: int = 400
) -> CrawlerFailure:
    code = str(payload.get("error") or "")
    if status == 401 or code in {"invalid_grant", "invalid_token"}:
        return CrawlerFailure(
            CrawlerErrorCode.AUTH_REQUIRED,
            "TikTok authorization is invalid, expired or revoked.",
        )
    if code in {"access_denied", "scope_not_authorized"}:
        return CrawlerFailure(
            CrawlerErrorCode.PERMISSION_REQUIRED,
            "TikTok authorization or required scope was not granted.",
        )
    if status == 429:
        return CrawlerFailure(
            CrawlerErrorCode.RATE_LIMITED,
            "TikTok OAuth rate limit was exceeded.",
            retryable=True,
        )
    return CrawlerFailure(
        CrawlerErrorCode.AUTH_REQUIRED,
        "TikTok OAuth request was rejected.",
    )


def _retry_after(value: str | None) -> float | None:
    try:
        seconds = float(value or "")
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 86_400 else None


def _parse_failure(message: str) -> CrawlerFailure:
    return CrawlerFailure(CrawlerErrorCode.PARSE_CHANGED, message)
