"""인증 기관의 이벤트 하위코드 등록 (03번 요구사항 8.3)."""

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import OrgEventCode
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import contracts


def register_subcode(db: Session, principal: Principal, code: str, label: str, created_at: datetime) -> None:
    parent = code.split(".", 1)[0]
    if not any(item["code"] == parent for item in contracts.code_table("event-categories")["items"]):
        raise ProblemError(
            "RULE_VIOLATION", detail="공통 상위분류에 없는 이벤트 코드입니다",
            unmet_conditions=[{"condition": "event_code 상위분류 ∈ event-categories", "actual": parent}],
        )
    stmt = (
        insert(OrgEventCode)
        .values(organization_id=principal.organization_id, code=code, label=label,
                created_by=principal.principal_id, created_at=created_at)
        .on_conflict_do_nothing(index_elements=[OrgEventCode.organization_id, OrgEventCode.code])
        .returning(OrgEventCode.code)
    )
    if db.execute(stmt).scalar_one_or_none() is None:
        raise ProblemError("CONFLICT", detail="같은 기관에 이미 등록된 이벤트 하위코드입니다")
