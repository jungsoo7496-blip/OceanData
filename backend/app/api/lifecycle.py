"""사용량·보존 dry-run·법적 보류·삭제·삭제 증명 (08번)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, container, get_db, load_scoped, require
from app.container import Container
from app.db.models import Asset, BackupRun, DeletionCertificate, Device, OrgQuota, TrialSession
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import backup, retention

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


@router.get("/v1/backup-runs", tags=["status"])
def list_backup_runs(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db),
                     _: Principal = Depends(require("backup:read"))) -> dict:
    rows = db.execute(select(BackupRun).order_by(BackupRun.started_at.desc()).limit(limit)).scalars()
    return {"items": [backup.run_view(r) for r in rows]}


@router.post("/v1/backup-runs", tags=["status"], status_code=202)
def start_backup_run(request: Request, db: Session = Depends(get_db), c: Container = Depends(container),
                     p: Principal = Depends(require("backup:run"))) -> dict:
    """지금 백업 (요구사항 6.1). 실행 중이면 409. 작업은 워커가 한다."""
    if db.execute(select(BackupRun.backup_run_id).where(BackupRun.status == "RUNNING")).first():
        raise ProblemError("CONFLICT", detail="백업이 이미 실행 중입니다")
    job_id = c.queue.enqueue("backup", {"requested_by": p.principal_id}, dedupe_key="backup")
    c.audit.record("BACKUP_REQUESTED", principal=p, target_type="Job", target_id=job_id, source_ip=client_ip(request))
    return {"job_id": job_id}


class BackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(pattern=r"^dev_[0-9A-HJKMNP-TV-Z]{26}$")
    session_id: str | None = Field(default=None, pattern=r"^ses_[0-9A-HJKMNP-TV-Z]{26}$")
    captured_from: datetime | None = None
    captured_to: datetime | None = None
    dry_run: bool = True


@router.post("/v1/backup-requests", tags=["status"])
def request_backup(body: BackupRequest, request: Request, db: Session = Depends(get_db),
                   c: Container = Depends(container), p: Principal = Depends(require("backup:request"))) -> dict:
    """현장 사본이 없을 수 있는 장비의 자산 백업 신청 (08번 요구사항 6.4). 먼저 dry_run으로 예상 용량을 본다."""
    for field, value in (("captured_from", body.captured_from), ("captured_to", body.captured_to)):
        if value is not None and value.tzinfo is None:
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": f"/{field}", "code": "timezone",
                                                            "message": "시간대가 있는 RFC 3339 시각이어야 합니다"}])
    device = load_scoped(db, c, request, Device, body.device_id, p)
    try:
        out = backup.request_backup(db, organization_id=p.organization_id, device=device, session_id=body.session_id,
                                    captured_from=body.captured_from, captured_to=body.captured_to,
                                    dry_run=body.dry_run, now=c.clock.now())
    except backup.RequestRefused as exc:
        raise ProblemError("RULE_VIOLATION", detail=exc.detail,
                           unmet_conditions=[{"condition": k, "actual": v} for k, v in exc.unmet]) from None
    if out["applied"]:
        c.audit.record("BACKUP_REQUESTED_BY_ORG", principal=p, organization_id=p.organization_id,
                       target_type="Device", target_id=device.device_id, source_ip=client_ip(request),
                       details={"asset_count": out["asset_count"], "total_bytes": out["total_bytes"],
                                "session_id": body.session_id})
    return out


@router.get("/v1/capacity-forecast", tags=["status"])
def capacity_forecast(db: Session = Depends(get_db), c: Container = Depends(container),
                      _: Principal = Depends(require("capacity:read"))) -> dict:
    from app.services import capacity

    return capacity.forecast(c, db, c.clock.now().date())


@router.get("/v1/usage-reports", tags=["status"])
def usage_report(month: str = Query(pattern=r"^(19|20)[0-9]{2}-(0[1-9]|1[0-2])$"), db: Session = Depends(get_db),
                 p: Principal = Depends(require("usage_report:read"))) -> dict:
    """기관별 월간 자원 사용량 (08번 요구사항 10). 기관 범위는 인증 정보로만: 운영자는 전체, ORG_ADMIN은 자기 기관."""
    from app.services import capacity

    org = None if p.has_role("PLATFORM_OPERATOR") else p.organization_id
    return {"month": month, "items": capacity.monthly_report(db, month, org)}


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
    # 원 자산 행을 잠근 뒤 파생본을 찾는다. 파생 작업(구간 클립)이 기록 중이면 끝난 뒤 그 결과까지 지운다 (ADR-0033)
    roots = retention.with_descendants(db, assets, lock=True)
    if scope == "ASSET" and not roots:  # 기다리는 사이 다른 요청이 지웠다 (삭제된 자산은 보이지 않는다)
        raise ProblemError("NOT_FOUND")
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
