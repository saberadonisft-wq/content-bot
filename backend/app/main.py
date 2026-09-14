from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import insights, items, runs, subtitles, videos
from .api.catalog import build_catalog_router
from .api.comments import build_comments_router
from .api.crawler_data import build_crawler_data_router
from .api.crawler_profiles import build_crawler_profiles_router
from .api.credentials import router as credentials_router
from .api.health import build_health_router
from .api.live_wall import build_live_wall_router
from .api.tiktok_auth import build_tiktok_auth_router
from .api.voiceover import build_voiceover_router
from .application_services import AppServices
from .config import settings
from .services.http_pool import PoolScope, use_pool_scope
from .services.subtitle_jobs import (
    SubtitleJobQueueFull,
)

logger = logging.getLogger(__name__)


async def job_queue_full_handler(_request, _error):
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Hàng đợi xử lý đang đầy hoặc ứng dụng đang đóng. Vui lòng thử lại sau."
        },
        headers={"Retry-After": "2"},
    )


async def verify_auth_token_middleware(request, call_next):
    if (
        settings.content_bot_auth_enabled
        and request.method != "OPTIONS"
        and not any(
            request.url.path.startswith(prefix)
            for prefix in (
                "/health",
                "/api/v1/health",
                "/api/v1/ready",
                "/api/v1/version",
                "/api/v1/update",
                "/docs",
                "/openapi.json",
                "/redoc",
            )
        )
    ):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Yêu cầu đăng nhập để sử dụng tính năng này."},
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = auth_header.split(" ", 1)[1].strip()
        try:
            from .middleware.auth import verify_token

            payload = await verify_token(token)
            if payload.get("status") != "approved":
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "Tài khoản của bạn đang chờ phê duyệt từ quản trị viên."
                    },
                )
            request.state.user = payload
        except HTTPException as he:
            return JSONResponse(
                status_code=he.status_code,
                content={"detail": he.detail},
                headers=he.headers,
            )
        except Exception as e:
            return JSONResponse(
                status_code=401,
                content={"detail": f"Xác thực thất bại: {e!s}"},
            )

    return await call_next(request)


def create_app(services_factory=None, *, scheduler: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        with use_pool_scope(application.state.http_pool_scope):
            services = await asyncio.to_thread(application.state.services_factory)
            application.state.services = services
            try:
                await services.start(scheduler=scheduler)
                yield
            finally:
                try:
                    await services.shutdown()
                finally:
                    application.state.services = None

    application = FastAPI(title="Content Bot API", version="0.1.0", lifespan=lifespan)
    application.state.services = None
    application.state.services_factory = services_factory or AppServices.create
    application.state.http_pool_scope = PoolScope()

    async def request_http_scope(request, call_next):
        with use_pool_scope(application.state.http_pool_scope):
            return await call_next(request)

    def resource(name):
        services = application.state.services
        if services is None:
            raise HTTPException(503, "Application is starting or stopping")
        return getattr(services, name)

    application.add_exception_handler(SubtitleJobQueueFull, job_queue_full_handler)
    application.middleware("http")(verify_auth_token_middleware)
    application.middleware("http")(request_http_scope)
    for api_router in (
        runs.router,
        items.router,
        insights.router,
        subtitles.router,
        videos.router,
    ):
        application.include_router(api_router)
    application.include_router(
        build_health_router(
            lambda: resource("store"), lambda: len(resource("connectors"))
        )
    )
    application.include_router(
        build_catalog_router(
            lambda: resource("store"),
            lambda: resource("connectors"),
            lambda: resource("run_manager"),
        )
    )
    application.include_router(build_crawler_data_router(lambda: resource("store")))
    application.include_router(
        build_comments_router(lambda: resource("store"), lambda: resource("connectors"))
    )
    application.include_router(
        build_crawler_profiles_router(lambda: resource("crawler_login_manager"))
    )
    application.include_router(
        build_tiktok_auth_router(lambda: resource("tiktok_oauth_service"))
    )
    application.include_router(
        build_live_wall_router(
            lambda: resource("live_wall_manager"), lambda: resource("store")
        )
    )
    application.include_router(credentials_router)
    application.include_router(
        build_voiceover_router(lambda: resource("voiceover_manager"),
            sync_service_provider=lambda: resource("gemini_subtitle_service"))
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip()
            for origin in settings.content_bot_cors_origins.split(",")
            if origin.strip()
        ],
        # Vite may move to the next local port when 5173 is occupied. Keep the
        # local-only API usable from that preview/dev port without allowing remote
        # browser origins.
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
        expose_headers=[
            "X-Sprite-Frames",
            "X-Sprite-Frame-Width",
            "X-Sprite-Frame-Height",
            "X-Cache-Hit",
        ],
    )
    return application


app = create_app()
