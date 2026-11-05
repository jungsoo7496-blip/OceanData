"""FastAPI 의존성: DB 세션, 인증 주체, 권한 확인, 기관 범위 조회."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, TypeVar

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


def get_db(c: Container = Depends(container)) -> Iterator[Session]:
    db = c.sessionmaker()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def get_principal(request: Request, db: Session = Depends(get_db), c: Container = Depends(container)) -> Principal:
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
    if scheme.lower() != "bearer" or not token.strip():
        c.audit.record("AUTH_FAILED", result="DENIED", source_ip=client_ip(request),
                       details={"reason": "missing_token", "path": request.url.path})
        raise ProblemError("UNAUTHENTICATED", headers={"WWW-Authenticate": "Bearer"})
    try:
        principal = c.authenticator.authenticate(db, token.strip())
    except AuthFailed as exc:
        c.audit.record("AUTH_FAILED", result="DENIED", source_ip=client_ip(request),
                       details={"reason": exc.reason, "path": request.url.path})
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
                *, purpose: str = "metadata") -> T:
    try:
        return get_scoped(db, model, ident, principal, purpose=purpose)
    except NotFoundInScope as exc:
        if exc.cross_org:
            c.audit.record("CROSS_ORG_ACCESS_ATTEMPT", result="DENIED", principal=principal,
                           organization_id=exc.owner_org, target_type=exc.model, target_id=ident,
                           source_ip=client_ip(request), details={"path": request.url.path})
        raise ProblemError("NOT_FOUND") from None


def page_params(limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    if limit < 1 or limit > 500:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/query/limit", "code": "range",
                                                        "message": "limit은 1~500입니다"}])
    return {"limit": limit, "cursor": cursor}
