"""공유 후보·심사·등급 변경·분류 지정 (13번 요구사항 1~4, 10)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import DB, client_ip, container, load_scoped, require
from app.container import Container
from app.db.models import Asset, ShareRequest, ShareReviewEvent, TrialSession
from app.domain import sharing_rules as rules
from app.domain import states
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import sharing
from app.services.idempotency import run_idempotent
from app.timefmt import iso

router = APIRouter(tags=["sharing"])
Scope = Literal["PRIVATE", "PROGRAM_REVIEW", "SANDBOX_PARTNERS", "APPROVED_PARTNERS", "PUBLIC"]
Handling = Literal["REVOKE_NOW", "KEEP_UNTIL_EXPIRY"]


class ShareRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_scope: Scope
    reason: str = Field(min_length=2, max_length=1000)
    existing_grant_handling: Handling | None = None


@router.get("/v1/assets/{asset_id}/share-requests")
def list_share_requests(asset_id: str, request: Request, db: Session = DB,
                        c: Container = Depends(container), p: Principal = Depends(require("share:read"))) -> dict:
    a = load_scoped(db, c, request, Asset, asset_id, p)
    rows = db.execute(select(ShareRequest).where(ShareRequest.asset_id == a.asset_id)
                      .order_by(ShareRequest.requested_at, ShareRequest.share_request_id)).scalars()
    return {"items": [sharing.request_view(r, a.share_status) for r in rows]}


@router.post("/v1/assets/{asset_id}/share-requests", status_code=201)
def create_share_request(asset_id: str, body: ShareRequestCreate, request: Request, db: Session = DB,
                         c: Container = Depends(container), p: Principal = Depends(require("share:request")),
                         idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    """상향은 Sharing_Rules, 하향은 언제든 (ORG_ADMIN만, 요구사항 3.8)."""
    def create() -> tuple[int, dict]:
        a = load_scoped(db, c, request, Asset, asset_id, p)
        db.refresh(a, with_for_update=True)
        before = a.sharing_scope
        req = sharing.request_scope(c, db, a, body.target_scope, body.reason, body.existing_grant_handling,
                                    p.principal_id)
        db.flush()
        c.audit.record("SHARE_SCOPE_REQUESTED", principal=p, organization_id=a.organization_id, target_type="Asset",
                       target_id=a.asset_id, reason=body.reason, source_ip=client_ip(request),
                       details={"from": before, "to": body.target_scope, "state": req.state,
                                "existing_grant_handling": body.existing_grant_handling})
        return 201, sharing.request_view(req, a.share_status)

    status, resp, replayed = run_idempotent(db, c.clock, c.settings.idempotency_retention_s, p,
                                            f"POST /v1/assets/{asset_id}/share-requests", idempotency_key,
                                            body.model_dump(mode="json"), create)
    return JSONResponse(resp, status_code=status, headers={"Idempotent-Replayed": "true"} if replayed else None)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVE", "DENY"]
    reason: str = Field(min_length=2, max_length=1000)


@router.post("/v1/share-requests/{share_request_id}/decisions")
def decide_share_request(share_request_id: str, body: Decision, request: Request, db: Session = DB,
                         c: Container = Depends(container), p: Principal = Depends(require("share:decide"))) -> dict:
    req = db.get(ShareRequest, share_request_id, with_for_update=True)
    if req is None:
        raise ProblemError("NOT_FOUND")
    a = sharing.decide_request(c, db, req, body.decision == "APPROVE", body.reason, p.principal_id)
    c.audit.record("SHARE_SCOPE_DECIDED", principal=p, organization_id=a.organization_id, target_type="Asset",
                   target_id=a.asset_id, reason=body.reason, source_ip=client_ip(request),
                   details={"share_request_id": req.share_request_id, "decision": body.decision,
                            "scope": a.sharing_scope})
    return sharing.request_view(req, a.share_status)


class Candidates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_ids: list[str] | None = Field(None, max_length=1000)
    session_id: str | None = None


@router.post("/v1/share-candidates")
def mark_candidates(body: Candidates, request: Request, db: Session = DB,
                    c: Container = Depends(container), p: Principal = Depends(require("share:candidate"))) -> dict:
    if not body.asset_ids and not body.session_id:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "", "code": "minProperties",
                                                        "message": "asset_ids나 session_id가 필요합니다"}])
    assets = [load_scoped(db, c, request, Asset, x, p) for x in sorted(set(body.asset_ids or []))]
    if body.session_id:
        s = load_scoped(db, c, request, TrialSession, body.session_id, p)
        assets += list(db.execute(select(Asset).where(Asset.session_id == s.session_id,
                                                      Asset.organization_id == p.organization_id,
                                                      Asset.deleted_at.is_(None))
                                  .order_by(Asset.asset_id)).scalars())
    seen: dict[str, Asset] = {a.asset_id: a for a in assets}
    done, excluded = sharing.mark_candidates(db, list(seen.values()), set(p.roles), c.clock.now())
    if done:
        c.audit.record("SHARE_CANDIDATES_MARKED", principal=p, organization_id=p.organization_id, target_type="Asset",
                       target_id=done[0], source_ip=client_ip(request),
                       details={"count": len(done), "excluded": len(excluded)})
    return {"candidates": done, "excluded": excluded}


def review_view(db: Session, c: Container, a: Asset) -> dict:
    kids = [d for d in sharing.descendants(db, a.asset_id) if d.variant in sharing.DERIVED_FOR_SHARING]
    best = "PRIVATE"
    for d in kids:
        m = rules.max_scope_for(sharing.facts_for(db, d, c.clock.now()))
        best = m if rules.LEVEL[m] > rules.LEVEL[best] else best
    events = db.execute(select(ShareReviewEvent).where(ShareReviewEvent.asset_id == a.asset_id)
                        .order_by(ShareReviewEvent.event_id)).scalars()
    return {"asset_id": a.asset_id, "share_status": a.share_status, "legal_basis_ref": a.legal_basis_ref,
            "max_scope": best,
            "derived": [{"asset_id": d.asset_id, "variant": d.variant, "share_status": d.share_status,
                         "review_result": d.review_result, "privacy_status": d.privacy_status,
                         "sharing_scope": d.sharing_scope} for d in sorted(kids, key=lambda x: x.asset_id)],
            "events": [{"from_status": e.from_status, "to_status": e.to_status, "actor": e.actor, "reason": e.reason,
                        "legal_basis_ref": e.legal_basis_ref, "at": iso(e.at)} for e in events]}


@router.get("/v1/assets/{asset_id}/share-review")
def get_share_review(asset_id: str, request: Request, db: Session = DB,
                     c: Container = Depends(container), p: Principal = Depends(require("share:read"))) -> dict:
    return review_view(db, c, load_scoped(db, c, request, Asset, asset_id, p))


class ReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["START_LEGAL_REVIEW", "PASS_LEGAL_REVIEW", "APPROVE", "REQUEST_REPROCESS", "REJECT", "RESUBMIT",
                    "WITHDRAW"]
    reason: str | None = Field(None, max_length=1000)
    legal_basis_ref: str | None = Field(None, max_length=200)
    existing_grant_handling: Handling | None = None


@router.post("/v1/assets/{asset_id}/share-review")
def act_share_review(asset_id: str, body: ReviewAction, request: Request, db: Session = DB,
                     c: Container = Depends(container), p: Principal = Depends(require("share:review"))) -> dict:
    a = load_scoped(db, c, request, Asset, asset_id, p)
    db.refresh(a, with_for_update=True)
    target = sharing.ACTIONS[body.action]
    allowed = states.SHARE.get((a.share_status, target))
    if allowed is not None and not allowed & set(p.roles):  # 전이는 있지만 이 역할의 일이 아니다
        raise ProblemError("FORBIDDEN", detail=f"{a.share_status} → {target}은 {', '.join(sorted(allowed))}의 일입니다")
    before = a.share_status
    changed = sharing.review(c, db, a, body.action, set(p.roles), p.principal_id, body.reason, body.legal_basis_ref,
                             body.existing_grant_handling)
    c.audit.record("SHARE_REVIEW_" + body.action, principal=p, organization_id=a.organization_id,
                   target_type="Asset", target_id=a.asset_id, reason=body.reason, source_ip=client_ip(request),
                   details={"from": before, "to": a.share_status, "derived": changed,
                            "legal_basis_ref": body.legal_basis_ref})
    db.flush()
    return review_view(db, c, a)


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sensitivity_class: Literal["INTERNAL", "RESTRICTED", "SECURITY"] | None = None
    privacy_status: Literal["NOT_APPLICABLE", "ANONYMIZED_VERIFIED"] | None = None
    basis_ref: str = Field(min_length=2, max_length=200)
    reason: str = Field(min_length=2, max_length=1000)


@router.patch("/v1/assets/{asset_id}/classification")
def classify_asset(asset_id: str, body: Classification, request: Request, db: Session = DB,
                   c: Container = Depends(container), p: Principal = Depends(require("asset:classify"))) -> dict:
    """운영자가 근거(점검 기록 참조)와 함께 지정 (요구사항 4.6). 더 엄격하게 바꾸면 파생본 등급은 규칙으로 다시 본다."""
    if body.sensitivity_class is None and body.privacy_status is None:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "", "code": "minProperties",
                                                        "message": "sensitivity_class나 privacy_status가 필요합니다"}])
    a = load_scoped(db, c, request, Asset, asset_id, p)
    if body.privacy_status == "ANONYMIZED_VERIFIED":  # 14번 요구사항 5.3: 운영자 승인 + legal_basis_ref
        if not a.legal_basis_ref:
            raise ProblemError("RULE_VIOLATION", detail="ANONYMIZED_VERIFIED에는 legal_basis_ref가 필요합니다",
                               unmet_conditions=[{"condition": "legal_basis_ref 존재", "actual": None}])
        if a.variant == "ORIGINAL":
            raise ProblemError("RULE_VIOLATION", detail="처리 결과는 파생본에만 기록합니다",
                               unmet_conditions=[{"condition": "variant ≠ ORIGINAL", "actual": a.variant}])
    before = {"sensitivity_class": a.sensitivity_class, "privacy_status": a.privacy_status}
    if body.sensitivity_class:
        a.sensitivity_class = body.sensitivity_class
    if body.privacy_status:
        a.privacy_status = body.privacy_status
    a.updated_at = c.clock.now()
    c.audit.record("ASSET_CLASSIFIED", principal=p, organization_id=a.organization_id, target_type="Asset",
                   target_id=a.asset_id, reason=body.reason, source_ip=client_ip(request),
                   details={"before": before, "basis_ref": body.basis_ref,
                            "after": {"sensitivity_class": a.sensitivity_class, "privacy_status": a.privacy_status}})
    return {"asset_id": a.asset_id, "sensitivity_class": a.sensitivity_class, "privacy_status": a.privacy_status}

