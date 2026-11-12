"""검토 2 (커밋 시점): 모든 경로가 DB 세션을 function 범위로 받아 응답 전에 커밋하는지 확인한다 (ADR-0053).

request 범위(FastAPI 기본)로 받는 곳이 하나라도 있으면 그 경로는 응답을 보낸 뒤 커밋하고, 같은 요청 안에서도
FastAPI 의존성 캐시 키가 달라 세션이 둘로 갈린다. FastAPI 0.162는 포함(include_router)마다 경로 사본의 의존성 트리를 만들어
실제로 쓰므로, 모듈의 라우터 객체와 **조립된 앱의 경로**를 둘 다 확인한다 — `include_router(dependencies=[…])`나 `router`가 아닌
이름의 라우터가 세션을 따로 여는 경우를 앞쪽 검사만으로는 놓친다.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Any

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import create_engine

import app.api
from app.api.deps import get_db


def _walk(dependant: Any) -> list[Any]:
    found = []
    for sub in dependant.dependencies:
        found.append(sub)
        found += _walk(sub)
    return found


def _db_scopes(dependant: Any) -> list[str | None]:
    return [sub.scope for sub in _walk(dependant) if sub.call is get_db]


def _is_generator(fn: Any) -> bool:
    return inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn)


def _module_routes() -> list[APIRoute]:
    out: list[APIRoute] = []
    for info in pkgutil.iter_modules(app.api.__path__):
        module = importlib.import_module(f"app.api.{info.name}")
        router = getattr(module, "router", None)
        if router is not None:
            out += [r for r in router.routes if isinstance(r, APIRoute)]
    return out


def test_every_route_commits_before_response() -> None:
    routes = _module_routes()
    assert len(routes) > 100  # 라우터를 모두 훑었다
    wrong = {f"{sorted(r.methods)} {r.path}": s for r in routes for s in _db_scopes(r.dependant) if s != "function"}
    assert wrong == {}
    uses_db = [r for r in routes if _db_scopes(r.dependant)]
    assert len(uses_db) > 100


def test_assembled_app_routes_commit_before_response_and_open_no_other_session() -> None:
    """조립된 앱의 실제 경로: get_db는 모두 function 범위이고, 그 밖에 마무리 단계를 가진(yield) 의존성이 없다."""
    routing = pytest.importorskip("fastapi.routing")
    iter_route_contexts = getattr(routing, "iter_route_contexts", None)
    if iter_route_contexts is None:  # 0.162 미만: 위 시험이 모듈 라우터를 확인한다
        pytest.skip("이 FastAPI에는 iter_route_contexts가 없다")
    from app.adapters.notifier import LogNotifier
    from app.container import build_container
    from app.main import create_app
    from tests.conftest import FakeStorage, StaticIdentity, test_settings

    container = build_container(test_settings(), engine=create_engine("postgresql+pg8000://u:p@127.0.0.1:1/x"),
                                storage=FakeStorage(), identity=StaticIdentity(), notifier=LogNotifier())
    contexts = [rc for rc in iter_route_contexts(create_app(container).router.routes)
                if getattr(rc, "dependant", None) is not None]
    assert len(contexts) > 100

    wrong, other_yield = {}, {}
    for rc in contexts:
        label = f"{sorted(rc.methods or [])} {rc.path}"
        scopes = _db_scopes(rc.dependant)
        if any(s != "function" for s in scopes):
            wrong[label] = scopes
        extra = [getattr(sub.call, "__name__", repr(sub.call)) for sub in _walk(rc.dependant)
                 if _is_generator(sub.call) and sub.call is not get_db]
        if extra:
            other_yield[label] = extra
    assert wrong == {}
    assert other_yield == {}, "세션을 따로 여는 마무리 단계 의존성은 응답 뒤에 커밋·정리할 수 있다"
    assert sum(1 for rc in contexts if _db_scopes(rc.dependant)) > 100
