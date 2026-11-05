"""기관 약정·데이터 연계 카드 (03번 요구사항 1.4·1.5). 화면은 09번 온보딩."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import DB, client_ip, container, load_scoped, require
from app.container import Container
from app.db.models import Agreement, Device, IntegrationCard, Organization
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import onboarding
from app.services.contracts import schema_files
from app.services.tenant import scope_filter, scoped_select

router = APIRouter(tags=["context"])


def _purposes() -> set[str]:
    return set(schema_files()["common"]["$defs"]["purpose"]["enum"])


def _aware(field: str, value: datetime | None) -> None:
    if value is not None and value.tzinfo is None:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": f"/{field}", "code": "timezone",
                                                        "message": "시간대가 있는 RFC 3339 시각이어야 합니다"}])


# ------------------------------------------------------------------ 약정


class AgreementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: str
    tier: Literal["TIER1", "TIER2"]
    reference: str = Field(min_length=2, max_length=200)
    valid_from: datetime
    valid_until: datetime | None = None
    allowed_purposes: list[str] = Field(default_factory=list, max_length=10)


class AgreementEnd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=2, max_length=500)
    effective_at: datetime | None = None


@router.get("/v1/agreements")
def list_agreements(db: Session = DB, c: Container = Depends(container),
                    p: Principal = Depends(require("agreement:read"))) -> dict:
    rows = db.execute(scoped_select(Agreement, p).order_by(Agreement.organization_id, Agreement.valid_from)).scalars()
    now = c.clock.now()
    return {"items": [onboarding.agreement_view(a, now) for a in rows]}


@router.post("/v1/agreements", status_code=201)
def create_agreement(body: AgreementCreate, request: Request, db: Session = DB,
                     c: Container = Depends(container), p: Principal = Depends(require("agreement:write"))) -> dict:
    _aware("valid_from", body.valid_from)
    _aware("valid_until", body.valid_until)
    bad = sorted(set(body.allowed_purposes) - _purposes())
    if bad or len(set(body.allowed_purposes)) != len(body.allowed_purposes):
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/allowed_purposes", "code": "enum",
                                                        "message": f"purpose 코드표에 없거나 중복: {bad}"}])
    if db.get(Organization, body.organization_id) is None:
        raise ProblemError("NOT_FOUND", detail="기관을 찾을 수 없습니다")
    now = c.clock.now()
    a = onboarding.create_agreement(db, organization_id=body.organization_id, tier=body.tier, reference=body.reference,
                                    valid_from=body.valid_from, valid_until=body.valid_until,
                                    purposes=body.allowed_purposes, by=p.principal_id, now=now)
    c.audit.record("AGREEMENT_RECORDED", principal=p, organization_id=a.organization_id, target_type="Agreement",
                   target_id=a.agreement_id, source_ip=client_ip(request),
                   details={"tier": a.tier, "allowed_purposes": a.allowed_purposes,
                            "valid_until": body.valid_until.isoformat() if body.valid_until else None})
    return onboarding.agreement_view(a, now)


@router.post("/v1/agreements/{agreement_id}/end")
def end_agreement(agreement_id: str, body: AgreementEnd, request: Request, db: Session = DB,
                  c: Container = Depends(container), p: Principal = Depends(require("agreement:write"))) -> dict:
    _aware("effective_at", body.effective_at)
    a = db.execute(select(Agreement).where(Agreement.agreement_id == agreement_id).with_for_update()) \
        .scalar_one_or_none()
    if a is None:
        raise ProblemError("NOT_FOUND")
    now = c.clock.now()
    before = onboarding.end_agreement(a, body.effective_at or now)
    if before is None:
        raise ProblemError("CONFLICT", detail="이미 그 시각 전에 끝난 약정입니다")
    c.audit.record("AGREEMENT_ENDED", principal=p, organization_id=a.organization_id, target_type="Agreement",
                   target_id=a.agreement_id, reason=body.reason, source_ip=client_ip(request),
                   details={"valid_until": a.valid_until.isoformat() if a.valid_until else None})
    return onboarding.agreement_view(a, now)


# ------------------------------------------------------------------ 연계 카드


class CardWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str | None = None
    content: dict[str, Any]


def load_card(db: Session, c: Container, request: Request, p: Principal, card_id: str,
              version: int | None = None) -> IntegrationCard:
    """범위 안의 카드 (version이 없으면 최신). 없거나 다른 기관이면 404 (교차 기관 시도는 감사). 09번 초안도 쓴다."""
    q = select(IntegrationCard).where(IntegrationCard.integration_card_id == card_id,
                                      scope_filter(IntegrationCard, p))
    q = q.where(IntegrationCard.version == version) if version else q.order_by(IntegrationCard.version.desc())
    card = db.execute(q.limit(1)).scalar_one_or_none()
    if card is not None:
        return card
    owner = db.execute(select(IntegrationCard.organization_id).where(
        IntegrationCard.integration_card_id == card_id).limit(1)).scalar_one_or_none()
    if owner is not None and owner != p.organization_id:
        c.audit.record("CROSS_ORG_ACCESS_ATTEMPT", result="DENIED", principal=p, organization_id=owner,
                       target_type="IntegrationCard", target_id=card_id, source_ip=client_ip(request),
                       details={"path": request.url.path})
    raise ProblemError("NOT_FOUND")


def _write(db: Session, c: Container, request: Request, p: Principal, body: CardWrite,
           card_id: str | None) -> dict:
    device = load_scoped(db, c, request, Device, body.device_id, p) if body.device_id else None
    try:
        card, changed = onboarding.write_card(db, organization_id=p.organization_id, card_id=card_id, device=device,
                                              content=body.content, by=p.principal_id, now=c.clock.now())
    except onboarding.CardInvalid as exc:
        raise ProblemError("VALIDATION_ERROR", errors=exc.errors) from None
    c.audit.record("INTEGRATION_CARD_RECORDED", principal=p, organization_id=p.organization_id,
                   target_type="IntegrationCard", target_id=card.integration_card_id, source_ip=client_ip(request),
                   details={"version": card.version, "device_id": card.device_id})
    if changed:  # 장비의 현장 사본 정책이 바뀌면 따로 남긴다 (백업 대상·유일본 안내에 영향, domain.md 9장)
        c.audit.record("DEVICE_LOCAL_COPY_POLICY_CHANGED", principal=p, organization_id=p.organization_id,
                       target_type="Device", target_id=changed["device_id"], source_ip=client_ip(request),
                       details={"from": changed["from"], "to": changed["to"],
                                "integration_card_id": card.integration_card_id, "version": card.version})
    return onboarding.card_view(db, card)


@router.get("/v1/integration-cards")
def list_cards(db: Session = DB, p: Principal = Depends(require("integration_card:read"))) -> dict:
    return {"items": [onboarding.card_view(db, x) for x in
                      onboarding.latest_versions(db, scope_filter(IntegrationCard, p))]}


@router.post("/v1/integration-cards", status_code=201)
def create_card(body: CardWrite, request: Request, db: Session = DB, c: Container = Depends(container),
                p: Principal = Depends(require("integration_card:write"))) -> dict:
    return _write(db, c, request, p, body, None)


@router.get("/v1/integration-cards/{integration_card_id}")
def get_card(integration_card_id: str, request: Request, version: int | None = None, db: Session = DB,
             c: Container = Depends(container), p: Principal = Depends(require("integration_card:read"))) -> dict:
    if version is not None and version < 1:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/query/version", "code": "range",
                                                        "message": "version은 1 이상입니다"}])
    return onboarding.card_view(db, load_card(db, c, request, p, integration_card_id, version))


@router.post("/v1/integration-cards/{integration_card_id}/versions", status_code=201)
def add_card_version(integration_card_id: str, body: CardWrite, request: Request, db: Session = DB,
                     c: Container = Depends(container),
                     p: Principal = Depends(require("integration_card:write"))) -> dict:
    load_card(db, c, request, p, integration_card_id)
    return _write(db, c, request, p, body, integration_card_id)
