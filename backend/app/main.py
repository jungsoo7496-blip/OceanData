"""FastAPI 애플리케이션 (Api_Server)."""

from __future__ import annotations

from fastapi import FastAPI

from app import API_VERSION
from app.api import agents, assets, auth, catalog, context, lifecycle, monitoring, observations, system, uploads
from app.config import get_settings
from app.container import Container, build_container
from app.errors import install_error_handlers
from app.logging_setup import configure_logging
from app.middleware import RequestContextMiddleware


def create_app(c: Container | None = None) -> FastAPI:
    if c is None:
        settings = get_settings()
        configure_logging(settings.log_level)
        c = build_container(settings)
    app = FastAPI(title="해양 실증데이터 플랫폼 API", version=API_VERSION, docs_url="/docs", redoc_url=None,
                  openapi_url="/openapi.json")
    app.state.container = c
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(system.router)
    app.include_router(auth.router)
    app.include_router(context.router)
    app.include_router(catalog.router)
    app.include_router(uploads.router)
    app.include_router(assets.router)
    app.include_router(agents.router)
    app.include_router(observations.router)
    app.include_router(lifecycle.router)
    app.include_router(monitoring.router)
    return app


def app_factory() -> FastAPI:  # uvicorn --factory app.main:app_factory
    return create_app()
