from __future__ import annotations

import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .routes.admin import router as admin_router
from .routes.google import router as google_router
from .routes.login import router as login_router
from .routes.register import router as register_router
from .routes.token import router as token_router
from .routes.update import router as update_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("auth_server")

app = FastAPI(
    title="Content Bot Auth Server",
    version="1.0.0",
    description="Authentication and Role-Based Access Control Server for Content Bot",
)

# Configure CORS
origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
if "*" in origins or not origins:
    allow_origins = ["*"]
else:
    allow_origins = origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routes
app.include_router(register_router)
app.include_router(login_router)
app.include_router(google_router)
app.include_router(token_router)
app.include_router(admin_router)
app.include_router(update_router)


@app.get("/")
def root() -> JSONResponse:
    return JSONResponse({
        "service": "Content Bot Auth Server",
        "version": "1.0.0",
        "status": "healthy",
    })


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "auth_ready": True,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=True)
