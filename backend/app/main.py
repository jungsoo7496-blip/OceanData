"""FastAPI 애플리케이션 (Api_Server)."""

from __future__ import annotations

from fastapi import FastAPI

from app import API_VERSION
from app.api import (
    agents,
    assets,
    auth,
    break_glass,
    catalog,
    context,
    datasets,
    deid,
    external,
    lifecycle,
    models_registry,
    monitoring,
    observations,
    onboarding,
    reports,
    sharing,
    system,
    uploads,
    webhooks,
)
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
    from app.services.storage_check import warn_on_startup

    warn_on_startup(c)  # 서버 측 암호화를 요청했는데 지원하지 않으면 기동 경고 (08번 요구사항 1.4)
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
    app.include_router(onboarding.router)
    app.include_router(break_glass.router)
    app.include_router(webhooks.router)
    app.include_router(models_registry.router)
    app.include_router(monitoring.router)
    app.include_router(reports.router)
    app.include_router(sharing.router)
    app.include_router(datasets.router)
    app.include_router(deid.router)
    app.include_router(external.router)
    return app


def app_factory() -> FastAPI:  # uvicorn --factory app.main:app_factory
    return create_app()
