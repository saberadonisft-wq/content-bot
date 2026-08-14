"""TikTok Login Kit orchestration with one-time state and encrypted tokens."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..crawlers.adapters.tiktok import (
    TikTokOAuthClient,
    TikTokOAuthConfig,
    TikTokTargetKind,
    TikTokTokenBundle,
    TikTokTokenVault,
    parse_tiktok_target,
)
from ..crawlers.runtime import CrawlerErrorCode, CrawlerFailure

_REQUIRED_SCOPES = frozenset(
    {"user.info.basic", "user.info.profile", "video.list"}
)


@dataclass(frozen=True, slots=True)
class TikTokOAuthStatus:
    state: str
    configured: bool
    connected: bool
    detail: str
    authorized_username: str = ""
    scopes: tuple[str, ...] = ()
    access_expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None

    def public(self) -> dict[str, object]:
        return {
            "state": self.state,
            "configured": self.configured,
            "connected": self.connected,
            "detail": self.detail,
            "authorized_username": self.authorized_username,
            "scopes": list(self.scopes),
            "access_expires_at": self.access_expires_at,
            "refresh_expires_at": self.refresh_expires_at,
        }


class TikTokOAuthStateStore:
    """Bounded in-memory replay guard; the browser cookie supplies client binding."""

    def __init__(self, ttl_seconds: int = 600, max_pending: int = 32) -> None:
        if not 60 <= ttl_seconds <= 1_800 or not 1 <= max_pending <= 256:
            raise ValueError("TikTok OAuth state-store limits are invalid")
        self.ttl_seconds = ttl_seconds
        self.max_pending = max_pending
        self._pending: dict[str, tuple[float, str]] = {}

    def issue(self, state: str, username: str) -> None:
        self._prune()
        if len(self._pending) >= self.max_pending:
            oldest = min(self._pending, key=lambda key: self._pending[key][0])
            self._pending.pop(oldest, None)
        self._pending[self._digest(state)] = (
            monotonic() + self.ttl_seconds,
            username,
        )

    def consume(self, state: str) -> str:
        self._prune()
        record = self._pending.pop(self._digest(state), None)
        if record is None or record[0] < monotonic():
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "TikTok OAuth state is missing, expired or already used.",
            )
        return record[1]

    def _prune(self) -> None:
        now = monotonic()
        for key, (expires_at, _) in tuple(self._pending.items()):
            if expires_at < now:
                self._pending.pop(key, None)

    @staticmethod
    def _digest(state: str) -> str:
        if not 32 <= len(state) <= 256:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "TikTok OAuth state is invalid.",
            )
        try:
            encoded = state.encode("ascii", errors="strict")
        except UnicodeEncodeError as exc:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "TikTok OAuth state is invalid.",
            ) from exc
        return hashlib.sha256(encoded).hexdigest()


class TikTokOAuthService:
    def __init__(
        self,
        vault: TikTokTokenVault,
        config_factory: Callable[[], TikTokOAuthConfig],
        frontend_url: str,
        *,
        client_factory: Callable[[TikTokOAuthConfig], TikTokOAuthClient] = TikTokOAuthClient,
        states: TikTokOAuthStateStore | None = None,
    ) -> None:
        self.vault = vault
        self._config_factory = config_factory
        self._client_factory = client_factory
        self.states = states or TikTokOAuthStateStore()
        self.frontend_url = self._validate_frontend_url(frontend_url)

    def status(self) -> TikTokOAuthStatus:
        try:
            self._config_factory()
        except ValueError:
            return TikTokOAuthStatus(
                "setup_required",
                False,
                False,
                "Configure the approved TikTok client key, secret and HTTPS redirect URI.",
            )
        try:
            credential = self.vault.load()
        except ValueError:
            return TikTokOAuthStatus(
                "invalid_vault",
                True,
                False,
                "The encrypted TikTok credential vault is invalid and must be reconnected.",
            )
        if credential is None:
            return TikTokOAuthStatus(
                "disconnected",
                True,
                False,
                "TikTok Login Kit is configured but no creator has authorized access.",
            )
        if not _REQUIRED_SCOPES.issubset(credential.scopes):
            state = "permission_required"
            detail = (
                "TikTok authorization is missing user.info.basic, "
                "user.info.profile or video.list."
            )
            connected = False
        elif credential.refresh_expired:
            state = "authorization_expired"
            detail = "TikTok authorization expired; connect the creator account again."
            connected = False
        elif credential.access_expired:
            state = "refresh_required"
            detail = "TikTok access is ready to be refreshed server-side."
            connected = True
        else:
            state = "connected"
            detail = "TikTok is connected for the explicitly authorized creator account."
            connected = True
        return TikTokOAuthStatus(
            state,
            True,
            connected,
            detail,
            credential.authorized_username,
            tuple(sorted(credential.scopes)),
            credential.access_expires_at,
            credential.refresh_expires_at,
        )

    def begin(self, username: str) -> str:
        config = self._config_factory()
        normalized_username = self._validate_username(username)
        client = self._client_factory(config)
        state = client.new_state()
        self.states.issue(state, normalized_username)
        return client.authorization_url(state)

    def callback_origin(self) -> str:
        parsed = urlsplit(self._config_factory().redirect_uri)
        return f"{parsed.scheme}://{parsed.netloc}"

    def consume_denial(self, state: str) -> None:
        self.states.consume(state)

    async def complete(self, code: str, state: str) -> TikTokOAuthStatus:
        username = self.states.consume(state)
        client = self._client_factory(self._config_factory())
        try:
            bundle = await client.exchange_code(code)
            await self._require_display_scopes(client, bundle)
            profile = await client.user_profile(bundle.access_token)
            if profile.open_id != bundle.open_id:
                await self._revoke_rejected_grant(
                    client,
                    bundle,
                    CrawlerErrorCode.AUTH_REQUIRED,
                    "TikTok profile identity did not match the authorization.",
                )
            if profile.username.casefold() != username:
                await self._revoke_rejected_grant(
                    client,
                    bundle,
                    CrawlerErrorCode.PERMISSION_REQUIRED,
                    "TikTok authorization used a different creator account than requested.",
                )
            try:
                self.vault.save(bundle, authorized_username=profile.username)
            except ValueError as exc:
                try:
                    await client.revoke(bundle.access_token)
                finally:
                    raise CrawlerFailure(
                        CrawlerErrorCode.STORAGE_ERROR,
                        "TikTok authorization could not be stored securely.",
                    ) from exc
        finally:
            await client.close()
        return self.status()

    async def refresh(self) -> TikTokOAuthStatus:
        credential = self.vault.load()
        if credential is None or credential.refresh_expired:
            raise CrawlerFailure(
                CrawlerErrorCode.AUTH_REQUIRED,
                "TikTok authorization is missing or expired.",
            )
        client = self._client_factory(self._config_factory())
        try:
            bundle = await client.refresh(credential.refresh_token)
            if bundle.open_id != credential.open_id:
                raise CrawlerFailure(
                    CrawlerErrorCode.AUTH_REQUIRED,
                    "TikTok refresh returned a different account.",
                )
            await self._require_display_scopes(client, bundle)
            self.vault.save(
                bundle,
                authorized_username=credential.authorized_username,
            )
        finally:
            await client.close()
        return self.status()

    async def disconnect(self) -> None:
        credential = self.vault.load()
        if credential is None:
            return
        client = self._client_factory(self._config_factory())
        try:
            access_token = credential.access_token
            if credential.access_expired:
                if credential.refresh_expired:
                    self.vault.delete()
                    return
                bundle = await client.refresh(credential.refresh_token)
                if bundle.open_id != credential.open_id:
                    raise CrawlerFailure(
                        CrawlerErrorCode.AUTH_REQUIRED,
                        "TikTok refresh returned a different account.",
                    )
                access_token = bundle.access_token
            await client.revoke(access_token)
            self.vault.delete()
        finally:
            await client.close()

    def frontend_redirect(self, outcome: str, reason: str = "") -> str:
        parsed = urlsplit(self.frontend_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.update(
            {
                "view": "library",
                "tab": "connections",
                "tiktok_oauth": outcome,
            }
        )
        if reason:
            query["tiktok_reason"] = reason[:80]
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(query), "")
        )

    @staticmethod
    async def _require_display_scopes(
        client: TikTokOAuthClient, bundle: TikTokTokenBundle
    ) -> None:
        if _REQUIRED_SCOPES.issubset(bundle.scopes):
            return
        try:
            await client.revoke(bundle.access_token)
        finally:
            raise CrawlerFailure(
                CrawlerErrorCode.PERMISSION_REQUIRED,
                "TikTok did not grant user.info.basic, user.info.profile and video.list.",
            )

    @staticmethod
    async def _revoke_rejected_grant(
        client: TikTokOAuthClient,
        bundle: TikTokTokenBundle,
        code: CrawlerErrorCode,
        message: str,
    ) -> None:
        try:
            await client.revoke(bundle.access_token)
        finally:
            raise CrawlerFailure(code, message)

    @staticmethod
    def _validate_username(username: str) -> str:
        value = username.strip().lstrip("@").casefold()
        try:
            target = parse_tiktok_target(f"https://www.tiktok.com/@{value}")
        except ValueError as exc:
            raise ValueError("TikTok creator username is invalid") from exc
        if target.kind is not TikTokTargetKind.CREATOR:
            raise ValueError("TikTok creator username is invalid")
        return target.external_id.casefold()

    @staticmethod
    def _validate_frontend_url(value: str) -> str:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("Frontend URL is invalid")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def oauth_cookie_matches(cookie_state: str | None, callback_state: str) -> bool:
    if cookie_state is None or not callback_state:
        return False
    return secrets.compare_digest(cookie_state, callback_state)
