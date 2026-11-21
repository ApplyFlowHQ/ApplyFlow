"""FastAPI application entrypoint for ApplyFlow Global."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_auth, routes_automation, routes_setup, routes_tracker, routes_workspace
from app.core.config import get_settings
from app.core.logging import configure_logging


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "Authorization"],
    )

    _NO_STORE_PREFIXES = (
        "/api/v1/setup",
        "/api/v1/integrations",
        "/api/v1/auth",
        "/api/v1/mailboxes",
        "/api/v1/sheets",
        "/api/v1/account",
        "/api/v1/operator",
    )

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if any(request.url.path.startswith(prefix) for prefix in _NO_STORE_PREFIXES):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

    app.include_router(routes_auth.router, prefix="/api/v1")
    app.include_router(routes_setup.router, prefix="/api/v1")
    app.include_router(routes_workspace.router, prefix="/api/v1")
    app.include_router(routes_automation.router, prefix="/api/v1")
    app.include_router(routes_tracker.router, prefix="/api/v1")

    return app


app = create_app()
