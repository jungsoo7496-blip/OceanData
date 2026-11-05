"""검토 2 (커밋 시점): 모든 경로가 DB 세션을 function 범위로 받아 응답 전에 커밋하는지 확인한다.

request 범위(FastAPI 기본)로 받는 곳이 하나라도 있으면 그 경로는 응답을 보낸 뒤 커밋하고, 같은 요청 안에서도
FastAPI 의존성 캐시 키가 달라 세션이 둘로 갈린다.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from fastapi.routing import APIRoute

import app.api
from app.api.deps import get_db


def _routes() -> list[APIRoute]:
    out: list[APIRoute] = []
    for info in pkgutil.iter_modules(app.api.__path__):
        module = importlib.import_module(f"app.api.{info.name}")
        router = getattr(module, "router", None)
        if router is not None:
            out += [r for r in router.routes if isinstance(r, APIRoute)]
    return out


def _db_scopes(dependant: Any) -> list[str | None]:
    found: list[str | None] = []
    for sub in dependant.dependencies:
        if sub.call is get_db:
            found.append(sub.scope)
        found += _db_scopes(sub)
    return found


def test_every_route_commits_before_response() -> None:
    routes = _routes()
    assert len(routes) > 100  # 라우터를 모두 훑었다
    wrong = {f"{sorted(r.methods)} {r.path}": s for r in routes for s in _db_scopes(r.dependant) if s != "function"}
    assert wrong == {}
    uses_db = [r for r in routes if _db_scopes(r.dependant)]
    assert len(uses_db) > 100
