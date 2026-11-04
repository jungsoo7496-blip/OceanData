"""Tenant_Guard — 저장소 계층에서 기관 범위를 강제한다 (02번 요구사항 2, security.md).

라우터는 이 모듈의 함수로만 기관 소유 테이블을 조회한다. 범위 밖 리소스는 존재 여부를 숨기고 404로 처리하며
교차 기관 시도를 감사 로그에 남긴다.
"""

from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy import ColumnElement, and_, false, select, true
from sqlalchemy.orm import Session

from app.domain.principal import Principal

T = TypeVar("T")


class NotFoundInScope(Exception):
    def __init__(self, model: str, ident: str, cross_org: bool, owner_org: str | None) -> None:
        super().__init__(model)
        self.model, self.ident, self.cross_org, self.owner_org = model, ident, cross_org, owner_org


def scope_filter(model: Any, principal: Principal, *, purpose: str = "metadata") -> ColumnElement[bool]:
    """역할별 데이터 범위 표(domain.md 2장)를 SQL 조건으로 만든다. 표에 없는 경우는 거부(false).

    삭제 표시된 자산(08번)은 누구에게도 보이지 않는다 (삭제 증명으로만 확인)."""
    cond = _role_filter(model, principal, purpose)
    if hasattr(model, "deleted_at"):
        cond = and_(cond, model.deleted_at.is_(None))
    return cond


def _role_filter(model: Any, principal: Principal, purpose: str) -> ColumnElement[bool]:
    org_col = model.organization_id
    if principal.kind in ("DEVICE", "API_KEY") or principal.has_role("ORG_ADMIN", "ORG_MEMBER"):
        return org_col == principal.organization_id
    if principal.has_role("PLATFORM_OPERATOR") and purpose == "metadata":
        return true()  # 운영 목적 메타데이터 열람 (감사 기록은 호출자가 남긴다)
    if principal.has_role("PROGRAM_VIEWER") and purpose == "metadata" and hasattr(model, "project_id"):
        if not principal.assigned_project_ids:
            return false()
        project_col = model.project_id
        cond = project_col.in_(sorted(principal.assigned_project_ids))
        if hasattr(model, "sharing_scope"):  # 자산은 L1(PROGRAM_REVIEW) 이상만 (domain.md 2장)
            cond = and_(cond, model.sharing_scope != "PRIVATE")
        return cond
    return false()


def scoped_select(model: Any, principal: Principal, *, purpose: str = "metadata") -> Any:
    return select(model).where(scope_filter(model, principal, purpose=purpose))


def get_scoped(db: Session, model: type[T], ident: str, principal: Principal, *, purpose: str = "metadata") -> T:
    pk = list(model.__table__.primary_key.columns)[0]  # type: ignore[attr-defined]
    row = db.execute(select(model).where(pk == ident, scope_filter(model, principal, purpose=purpose))) \
        .scalar_one_or_none()
    if row is not None:
        return row
    owner = db.execute(select(model.organization_id).where(pk == ident)).scalar_one_or_none()  # type: ignore[attr-defined]
    raise NotFoundInScope(model.__name__, ident, cross_org=owner is not None and owner != principal.organization_id,
                          owner_org=owner)
