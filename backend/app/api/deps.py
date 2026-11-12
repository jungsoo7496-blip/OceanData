"""FastAPI 의존성: DB 세션, 인증 주체, 권한 확인, 기관 범위 조회."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

import anyio
import anyio.to_thread
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.container import Container
from app.context import organization_id_var
from app.domain import permissions
from app.domain.principal import Principal
from app.errors import ProblemError
from app.ratelimit import Limiter
from app.services.auth import AuthFailed
from app.services.tenant import NotFoundInScope, get_scoped

T = TypeVar("T")
FORBIDDEN_SCOPE_INPUTS = ("organization_id",)


def container(request: Request) -> Container:
    return request.app.state.container  # type: ignore[no-any-return]


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def get_db(c: Container = Depends(container)) -> AsyncIterator[Session]:
    """요청의 DB 세션. DB 작업 차례(ADR-0062)를 받은 뒤 만들고, 경로 함수가 끝나면 커밋한다.

    차례는 이벤트 루프에서 기다리므로 기다리는 동안 스레드도 연결도 쥐지 않는다. 커밋·되돌리기·닫기는 스레드 풀에서
    돌린다(이벤트 루프를 막지 않는다). 요청이 취소돼도 되돌리기·닫기는 끝까지 한다."""
    async with c.db_slots.hold():
        db = c.sessionmaker()
        try:
            yield db
            await anyio.to_thread.run_sync(db.commit)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(db.rollback)
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(db.close)


# 모든 경로가 DB 세션을 이 표시로 받는다. scope="function"이면 경로 함수가 끝난 직후, 응답을 보내기 전에
# get_db의 마무리(커밋)가 돈다. FastAPI 0.118 이상에서 기본(request 범위)은 응답을 보낸 뒤에 마무리하므로
# 커밋이 실패해도 클라이언트는 이미 2xx를 받고 작업은 없다. 의존성 캐시 키에 범위가 들어가서 한 곳이라도
# `Depends(get_db)`로 받으면 같은 요청 안에서 세션이 둘로 갈린다 — 시험이 모든 경로를 훑는다
# (tests/unit/test_db_dependency_scope.py). ADR-0053.
DB = Depends(get_db, scope="function")


def get_principal(request: Request, db: Session = DB, c: Container = Depends(container)) -> Principal:
    # 요청으로 받은 기관 ID로 범위를 바꾸지 않는다 (02번 요구사항 2.6)
    for name in FORBIDDEN_SCOPE_INPUTS:
        if name in request.query_params:
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": f"/query/{name}", "code": "not_allowed",
                                                            "message": "기관 범위는 인증 정보로만 정합니다"}])
    if request.headers.get("x-organization-id"):
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/header/x-organization-id",
                                                        "code": "not_allowed",
                                                        "message": "기관 범위는 인증 정보로만 정합니다"}])
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    audit_path = getattr(request.scope.get("route"), "path", None) or request.url.path[:100]
    if scheme.lower() != "bearer" or not token.strip():
        c.audit.record("AUTH_FAILED", result="DENIED", source_ip=client_ip(request),
                       details={"reason": "missing_token", "path": audit_path})
        raise ProblemError("UNAUTHENTICATED", headers={"WWW-Authenticate": "Bearer"})
    try:
        principal = c.authenticator.authenticate(db, token.strip())
    except AuthFailed as exc:
        c.audit.record("AUTH_FAILED", result="DENIED", source_ip=client_ip(request),
                       details={"reason": exc.reason, "path": audit_path})
        raise ProblemError("UNAUTHENTICATED", headers={"WWW-Authenticate": "Bearer"}) from None
    organization_id_var.set(principal.organization_id)
    request.state.principal = principal
    limiter = request.app.state.__dict__.setdefault("limiter", Limiter(c.settings.rate_limit_per_minute))
    limiter.check(f"{principal.kind}:{principal.principal_id}")  # 클라이언트별 분당 요청 수 (04번 요구사항 9)
    from app.api.contract_query import check_request_against_contract

    check_request_against_contract(request)  # 조회 조건은 계약 스키마로 (인증 뒤, 04번 요구사항 10.2)
    return principal


def require(action: str) -> Callable[..., Principal]:
    """Permission_Table 확인. 표에 없거나 역할이 없으면 403, 고위험 동작에 MFA가 없으면 403 MFA_REQUIRED."""

    def _dep(request: Request, principal: Principal = Depends(get_principal),
             c: Container = Depends(container)) -> Principal:
        try:
            permissions.check(principal, action)
        except permissions.PermissionDenied as exc:
            c.audit.record("PERMISSION_DENIED", result="DENIED", principal=principal,
                           organization_id=principal.organization_id, source_ip=client_ip(request),
                           details={"action": action, "mfa_required": exc.mfa_required})
            raise ProblemError("MFA_REQUIRED" if exc.mfa_required else "FORBIDDEN") from None
        return principal

    return _dep


def load_scoped(db: Session, c: Container, request: Request, model: type[T], ident: str, principal: Principal,
                *, purpose: str = "metadata", audit_device_scope: bool = False) -> T:
    try:
        return get_scoped(db, model, ident, principal, purpose=purpose)
    except NotFoundInScope as exc:
        if exc.cross_org:
            c.audit.record("CROSS_ORG_ACCESS_ATTEMPT", result="DENIED", principal=principal,
                           organization_id=exc.owner_org, target_type=exc.model, target_id=ident,
                           source_ip=client_ip(request), details={"path": request.url.path})
        elif audit_device_scope and principal.kind == "DEVICE":
            c.audit.record("DEVICE_SCOPE_VIOLATION", result="DENIED", principal=principal,
                           organization_id=principal.organization_id, target_type=exc.model, target_id=ident)
        raise ProblemError("NOT_FOUND") from None


def page_params(limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    if limit < 1 or limit > 500:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/query/limit", "code": "range",
                                                        "message": "limit은 1~500입니다"}])
    return {"limit": limit, "cursor": cursor}
