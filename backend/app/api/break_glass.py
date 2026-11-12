"""긴급 열람 요청·승인·조기 종료·조회 (02번 요구사항 8). 원본 접근 자체는 assets의 download-url에서 판단한다."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, container, get_db, require
from app.container import Container
from app.db.models import BreakGlassGrant
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import break_glass as bg
from app.services.tenant import scoped_select

router = APIRouter(tags=["system"])


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_type: Literal["ASSET", "SESSION"]
    target_id: str = Field(max_length=64)
    reason: str = Field(min_length=10, max_length=1000)
    duration_s: int = Field(ge=60, le=bg.MAX_DURATION_S)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVE", "DENY"]
    reason: str | None = Field(None, max_length=1000)


class Revoke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=2, max_length=1000)


def _locked(db: Session, grant_id: str) -> BreakGlassGrant:
    g = db.execute(select(BreakGlassGrant).where(BreakGlassGrant.grant_id == grant_id).with_for_update()) \
        .scalar_one_or_none()
    if g is None:
        raise ProblemError("NOT_FOUND")
    return g


@router.get("/v1/break-glass-grants")
def list_grants(db: Session = Depends(get_db), c: Container = Depends(container),
                p: Principal = Depends(require("break_glass:read"))) -> dict:
    rows = db.execute(scoped_select(BreakGlassGrant, p).order_by(BreakGlassGrant.created_at.desc())
                      .limit(200)).scalars()
    now = c.clock.now()
    return {"items": [bg.view(g, now) for g in rows]}


@router.post("/v1/break-glass-grants", status_code=201)
def request_grant(body: GrantRequest, request: Request, db: Session = Depends(get_db),
                  c: Container = Depends(container), p: Principal = Depends(require("break_glass:request"))) -> dict:
    now = c.clock.now()
    try:
        g = bg.request(db, by=p.principal_id, target_type=body.target_type, target_id=body.target_id,
                       reason=body.reason, duration_s=body.duration_s, now=now)
    except bg.Refused as exc:
        raise ProblemError(exc.code, detail=exc.detail) from None
    c.audit.record("BREAK_GLASS_REQUESTED", principal=p, organization_id=g.organization_id,
                   target_type="BreakGlassGrant", target_id=g.grant_id, reason=body.reason,
                   source_ip=client_ip(request), details={"target_type": g.target_type, "target_id": g.target_id,
                                                          "duration_s": g.duration_s})
    return bg.view(g, now)


@router.post("/v1/break-glass-grants/{grant_id}/decision")
def decide_grant(grant_id: str, body: Decision, request: Request, db: Session = Depends(get_db),
                 c: Container = Depends(container), p: Principal = Depends(require("break_glass:decide"))) -> dict:
    g = _locked(db, grant_id)
    now = c.clock.now()
    try:
        bg.decide(c, db, g, by=p.principal_id, approve=body.decision == "APPROVE", now=now)
    except bg.Refused as exc:
        if exc.code == "FORBIDDEN":
            c.audit.record("PERMISSION_DENIED", result="DENIED", principal=p, organization_id=g.organization_id,
                           target_type="BreakGlassGrant", target_id=g.grant_id, source_ip=client_ip(request),
                           details={"action": "break_glass:decide", "reason": "self_decision"})
        raise ProblemError(exc.code, detail=exc.detail) from None
    c.audit.record("BREAK_GLASS_APPROVED" if body.decision == "APPROVE" else "BREAK_GLASS_DENIED", principal=p,
                   organization_id=g.organization_id, target_type="BreakGlassGrant", target_id=g.grant_id,
                   reason=body.reason, source_ip=client_ip(request),
                   details={"expires_at": g.expires_at.isoformat() if g.expires_at else None})
    return bg.view(g, now)


@router.post("/v1/break-glass-grants/{grant_id}/revoke")
def revoke_grant(grant_id: str, body: Revoke, request: Request, db: Session = Depends(get_db),
                 c: Container = Depends(container), p: Principal = Depends(require("break_glass:decide"))) -> dict:
    g = _locked(db, grant_id)
    now = c.clock.now()
    try:
        bg.revoke(g, now)
    except bg.Refused as exc:
        raise ProblemError(exc.code, detail=exc.detail) from None
    c.audit.record("BREAK_GLASS_REVOKED", principal=p, organization_id=g.organization_id,
                   target_type="BreakGlassGrant", target_id=g.grant_id, reason=body.reason,
                   source_ip=client_ip(request))
    return bg.view(g, now)
