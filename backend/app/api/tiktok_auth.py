from __future__ import annotations

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from ..crawlers.runtime import CrawlerFailure
from ..services.tiktok_oauth import TikTokOAuthService, oauth_cookie_matches

OAUTH_COOKIE = "content_bot_tiktok_oauth_state"


def build_tiktok_auth_router(service: TikTokOAuthService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth/tiktok", tags=["TikTok OAuth"])

    @router.get("/status")
    async def status():
        return service.status().public()

    @router.get("/start")
    async def start(
        request: Request,
        username: str = Query(min_length=2, max_length=100),
    ):
        try:
            request_origin = f"{request.url.scheme}://{request.url.netloc}"
            if request_origin != service.callback_origin():
                raise ValueError(
                    "TikTok OAuth start must use the same HTTPS origin as the approved callback"
                )
            authorization_url = service.begin(username)
            state = _authorization_state(authorization_url)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        response = RedirectResponse(authorization_url, status_code=302)
        response.set_cookie(
            OAUTH_COOKIE,
            state,
            max_age=service.states.ttl_seconds,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/api/v1/auth/tiktok/callback",
        )
        return response

    @router.get("/callback")
    async def callback(
        code: str = Query(default="", max_length=2_048),
        state: str = Query(min_length=32, max_length=256),
        error: str = Query(default="", max_length=100),
        csrf_cookie: str | None = Cookie(default=None, alias=OAUTH_COOKIE),
    ):
        if not oauth_cookie_matches(csrf_cookie, state):
            raise HTTPException(400, "TikTok OAuth state validation failed")
        outcome = "connected"
        reason = ""
        try:
            if error:
                service.consume_denial(state)
                outcome = "denied"
                reason = "PERMISSION_REQUIRED"
            elif not code:
                service.consume_denial(state)
                outcome = "error"
                reason = "AUTH_REQUIRED"
            else:
                await service.complete(code, state)
        except CrawlerFailure as exc:
            outcome = "error"
            reason = exc.code.value
        except ValueError:
            outcome = "error"
            reason = "STORAGE_ERROR"
        response = RedirectResponse(
            service.frontend_redirect(outcome, reason),
            status_code=303,
        )
        response.delete_cookie(
            OAUTH_COOKIE,
            path="/api/v1/auth/tiktok/callback",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    @router.post("/refresh")
    async def refresh():
        try:
            return (await service.refresh()).public()
        except (CrawlerFailure, ValueError) as exc:
            detail = exc.safe_message if isinstance(exc, CrawlerFailure) else str(exc)
            raise HTTPException(409, detail) from exc

    @router.delete("/connection", status_code=204)
    async def disconnect():
        try:
            await service.disconnect()
        except (CrawlerFailure, ValueError) as exc:
            detail = exc.safe_message if isinstance(exc, CrawlerFailure) else str(exc)
            raise HTTPException(409, detail) from exc

    return router


def _authorization_state(url: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    state = parse_qs(urlsplit(url).query).get("state", [""])[0]
    if not state:
        raise ValueError("TikTok OAuth authorization state is missing")
    return state
