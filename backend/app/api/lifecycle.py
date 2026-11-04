"""사용량·보존 dry-run·법적 보류·삭제·삭제 증명 (08번)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, container, get_db, load_scoped, require
from app.container import Container
from app.db.models import Asset, DeletionCertificate, OrgQuota, TrialSession
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import retention

router = APIRouter()


@router.get("/v1/usage", tags=["status"])
def get_usage(db: Session = Depends(get_db), c: Container = Depends(container),
              p: Principal = Depends(require("usage:read"))) -> dict:
    stmt = select(OrgQuota).order_by(OrgQuota.organization_id)
    if not p.has_role("PLATFORM_OPERATOR"):
        stmt = stmt.where(OrgQuota.organization_id == p.organization_id)
    rows = list(db.execute(stmt).scalars())
    if not rows and not p.has_role("PLATFORM_OPERATOR"):  # 아직 업로드가 없는 기관
        rows = [OrgQuota(organization_id=p.organization_id, quota_bytes=c.settings.default_org_quota_bytes,
                         used_bytes=0, quarantine_bytes=0, reserved_bytes=0, backup_bytes=0)]
    return {"items": [retention.usage_view(q) for q in rows]}


@router.post("/v1/retention/dry-run", tags=["status"])
def retention_dry_run(db: Session = Depends(get_db), c: Container = Depends(container),
                      _: Principal = Depends(require("retention:run"))) -> dict:
    return retention.dry_run(db, c.clock.now())


class LegalHold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hold: bool
    reason: str = Field(min_length=2, max_length=500)


@router.post("/v1/assets/{asset_id}/legal-hold", tags=["assets"])
def set_legal_hold(asset_id: str, body: LegalHold, request: Request, db: Session = Depends(get_db),
                   c: Container = Depends(container), p: Principal = Depends(require("legal_hold:set"))) -> dict:
    a = load_scoped(db, c, request, Asset, asset_id, p)
    a.legal_hold, a.legal_hold_reason, a.updated_at = body.hold, body.reason if body.hold else None, c.clock.now()
    c.audit.record("LEGAL_HOLD_SET" if body.hold else "LEGAL_HOLD_RELEASED", principal=p,
                   organization_id=a.organization_id, target_type="Asset", target_id=a.asset_id, reason=body.reason,
                   source_ip=client_ip(request))  # 운영자만, 사유와 함께 (요구사항 4.2)
    return {"asset_id": a.asset_id, "legal_hold": a.legal_hold}


class DeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=2, max_length=500)
    confirm_affected: bool = False


def _delete(c: Container, db: Session, request: Request, p: Principal, assets: list[Asset], scope: str,
            target_id: str, body: DeletionRequest) -> dict:
    roots = retention.with_descendants(db, assets)
    try:
        cert = retention.delete_assets(c, db, organization_id=p.organization_id, assets=roots, scope=scope,
                                       target_id=target_id, reason=body.reason, requested_by=p.principal_id,
                                       confirm_affected=body.confirm_affected)
    except retention.DeletionRefused as exc:
        raise ProblemError("CONFLICT", detail=exc.detail, extra={"affected": exc.affected}) from None
    c.audit.record("ASSET_DELETED", principal=p, organization_id=p.organization_id, target_type=scope.title(),
                   target_id=target_id, reason=body.reason, source_ip=client_ip(request),
                   details={"certificate_id": cert.certificate_id, "assets": len(roots)})
    return retention.certificate_view(cert)


@router.post("/v1/assets/{asset_id}/deletion", tags=["assets"])
def delete_asset(asset_id: str, body: DeletionRequest, request: Request, db: Session = Depends(get_db),
                 c: Container = Depends(container), p: Principal = Depends(require("asset:delete"))) -> dict:
    a = load_scoped(db, c, request, Asset, asset_id, p)
    return _delete(c, db, request, p, [a], "ASSET", a.asset_id, body)


@router.post("/v1/sessions/{session_id}/deletion", tags=["context"])
def delete_session_assets(session_id: str, body: DeletionRequest, request: Request, db: Session = Depends(get_db),
                          c: Container = Depends(container), p: Principal = Depends(require("asset:delete"))) -> dict:
    s = load_scoped(db, c, request, TrialSession, session_id, p)
    assets = list(db.execute(select(Asset).where(Asset.session_id == s.session_id,
                                                 Asset.organization_id == p.organization_id,
                                                 Asset.deleted_at.is_(None))).scalars())
    return _delete(c, db, request, p, assets, "SESSION", s.session_id, body)


@router.get("/v1/deletion-certificates/{certificate_id}", tags=["assets"])
def get_certificate(certificate_id: str, db: Session = Depends(get_db),
                    p: Principal = Depends(require("deletion_certificate:read"))) -> dict:
    cert = db.get(DeletionCertificate, certificate_id)
    if cert is None or (cert.organization_id != p.organization_id and not p.has_role("PLATFORM_OPERATOR")):
        raise ProblemError("NOT_FOUND")
    return retention.certificate_view(cert)
