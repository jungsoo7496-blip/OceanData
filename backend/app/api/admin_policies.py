"""운영 콘솔 정책 편집 (09번 요구사항 10.1·10.2, ADR-0038): 기관 쿼터·악성코드 검사 요구, 보존 정책, 작업 우선순위.

모두 PLATFORM_OPERATOR, 바꾸기는 다중 인증·사유 필수. 바뀐 항목의 전후 값과 사유를 감사 로그에 남긴다.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import DB, client_ip, container, require
from app.container import Container
from app.db.models import JobPolicy, JobRow, Organization, OrgQuota, RetentionPolicy
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services.ingest import Quota
from app.services.retention import usage_view
from app.timefmt import iso

router = APIRouter(tags=["status"])
# 만료 동작 KEEP 고정: 증빙·데이터셋 사본(사람 결정 없이 바꾸지 않는다), 회차 미상·필수 메타데이터 누락 격리
# (손상 없는 원본일 수 있어 자동 삭제하지 않고 ORG_ADMIN이 폐기를 확정할 때만 지운다 — domain.md 9장)
LOCKED_ACTION = ("EVIDENCE", "DATASET", "QUARANTINE:UNKNOWN_SESSION", "QUARANTINE:MISSING_REQUIRED_METADATA")


def _need_change(body: BaseModel) -> set[str]:
    sent = body.model_fields_set - {"reason"}
    if not sent:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "", "code": "min_properties",
                                                        "message": "바꿀 항목이 하나 이상 필요합니다"}])
    return sent


def _changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: {"from": before[k], "to": after[k]} for k in after if before.get(k) != after[k]}


# ---------------------------------------------------------------- 기관 정책
class OrganizationPolicyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quota_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1)  # 저장 열(bigint) 상한
    malware_scan_required: bool | None = None
    reason: str = Field(min_length=2, max_length=500)


def _org_view(org: Organization, q: OrgQuota | None, default_quota: int) -> dict[str, Any]:
    usage = usage_view(q)["usage_bytes"] if q is not None else 0
    quota = q.quota_bytes if q is not None else default_quota
    return {"organization_id": org.organization_id, "name": org.name, "org_type": org.org_type, "quota_bytes": quota,
            "usage_bytes": usage, "malware_scan_required": bool(org.malware_scan_required), "over_quota": usage > quota}


@router.get("/v1/organization-policies")
def list_organization_policies(db: Session = DB, c: Container = Depends(container),
                               p: Principal = Depends(require("org_policy:read"))) -> dict:
    quotas = {q.organization_id: q for q in db.execute(select(OrgQuota)).scalars()}
    orgs = db.execute(select(Organization).order_by(Organization.organization_id)).scalars()
    return {"items": [_org_view(o, quotas.get(o.organization_id), c.settings.default_org_quota_bytes) for o in orgs]}


@router.patch("/v1/organization-policies/{organization_id}")
def update_organization_policy(organization_id: str, body: OrganizationPolicyPatch, request: Request,
                               db: Session = DB, c: Container = Depends(container),
                               p: Principal = Depends(require("org_policy:write"))) -> dict:
    sent = _need_change(body)
    org = db.get(Organization, organization_id)
    if org is None:
        raise ProblemError("NOT_FOUND")
    q = Quota(c, db)._row(organization_id) if "quota_bytes" in sent else db.get(OrgQuota, organization_id)
    before = {"quota_bytes": q.quota_bytes if q is not None else c.settings.default_org_quota_bytes,
              "malware_scan_required": bool(org.malware_scan_required)}
    if "quota_bytes" in sent and body.quota_bytes is not None and q is not None:
        q.quota_bytes, q.updated_at = body.quota_bytes, c.clock.now()
    if "malware_scan_required" in sent and body.malware_scan_required is not None:
        org.malware_scan_required = body.malware_scan_required
    after = {"quota_bytes": q.quota_bytes if q is not None else before["quota_bytes"],
             "malware_scan_required": bool(org.malware_scan_required)}
    changes = _changes(before, after)
    db.flush()  # DB가 값을 거부하면(범위 초과 등) 감사 전에 실패한다
    if changes:  # 감사는 변경과 같은 트랜잭션 — 변경이 롤백되면 감사도 남지 않는다
        c.audit.record("ORG_POLICY_CHANGED", principal=p, organization_id=organization_id,
                       target_type="Organization", target_id=organization_id, reason=body.reason,
                       source_ip=client_ip(request), details={"changes": changes}, connection=db.connection())
    return _org_view(org, q, c.settings.default_org_quota_bytes)


# ---------------------------------------------------------------- 보존 정책
class RetentionPolicyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retention_days: int | None = Field(default=None, ge=1, le=36500)
    expiry_action: Literal["DELETE", "KEEP"] | None = None
    notify_days_before: int | None = Field(default=None, ge=0, le=365)
    reason: str = Field(min_length=2, max_length=500)


def _retention_view(r: RetentionPolicy) -> dict[str, Any]:
    return {"target": r.target, "policy_id": r.policy_id, "retention_days": r.retention_days,
            "expiry_action": r.expiry_action, "notify_days_before": r.notify_days_before,
            "updated_at": iso(r.updated_at), "action_locked": r.target in LOCKED_ACTION}


@router.get("/v1/retention-policies")
def list_retention_policies(db: Session = DB,
                            p: Principal = Depends(require("retention_policy:read"))) -> dict:
    rows = db.execute(select(RetentionPolicy).order_by(RetentionPolicy.target)).scalars()
    return {"items": [_retention_view(r) for r in rows]}


@router.patch("/v1/retention-policies/{target}")
def update_retention_policy(target: str, body: RetentionPolicyPatch, request: Request, db: Session = DB,
                            c: Container = Depends(container),
                            p: Principal = Depends(require("retention_policy:write"))) -> dict:
    sent = _need_change(body)
    r = db.execute(select(RetentionPolicy).where(RetentionPolicy.target == target).with_for_update()) \
        .scalar_one_or_none()
    if r is None:  # 표에 없는 대상은 여기서 만들지 않는다
        raise ProblemError("NOT_FOUND")
    for name in ("expiry_action", "notify_days_before"):
        if name in sent and getattr(body, name) is None:
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": f"/{name}", "code": "not_nullable",
                                                            "message": "비울 수 없는 항목입니다"}])
    if r.target in LOCKED_ACTION and body.expiry_action is not None and body.expiry_action != r.expiry_action:
        raise ProblemError("CONFLICT", detail=f"{r.target}의 만료 동작은 바꿀 수 없습니다 (KEEP 고정, domain.md 9장)")
    before = _retention_view(r)
    action = body.expiry_action if body.expiry_action is not None else r.expiry_action
    days = body.retention_days if "retention_days" in sent else r.retention_days
    if action == "DELETE" and days is None:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/retention_days", "code": "required",
                                                        "message": "DELETE에는 보존 일수가 필요합니다"}])
    r.expiry_action, r.retention_days = action, days
    if body.notify_days_before is not None:
        r.notify_days_before = body.notify_days_before
    r.updated_at = c.clock.now()
    after = _retention_view(r)
    keys = ("retention_days", "expiry_action", "notify_days_before")
    changes = _changes({k: before[k] for k in keys}, {k: after[k] for k in keys})
    db.flush()
    if changes:
        c.audit.record("RETENTION_POLICY_CHANGED", principal=p, target_type="RetentionPolicy", target_id=r.target,
                       reason=body.reason, source_ip=client_ip(request), details={"changes": changes},
                       connection=db.connection())
    return after


# ---------------------------------------------------------------- 작업 정책
class JobPolicyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: int | None = Field(default=None, ge=0, le=1000)
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    reason: str = Field(min_length=2, max_length=500)


def _queued(db: Session) -> dict[str, int]:
    rows = db.execute(select(JobRow.job_type, func.count()).where(JobRow.status == "QUEUED")
                      .group_by(JobRow.job_type)).all()
    return {t: int(n) for t, n in rows}


def _job_view(j: JobPolicy, queued: dict[str, int]) -> dict[str, Any]:
    return {"job_type": j.job_type, "priority": j.priority, "resource_class": j.resource_class,
            "gpu_fallback": j.gpu_fallback, "max_attempts": j.max_attempts, "lease_s": j.lease_s,
            "queued_jobs": queued.get(j.job_type, 0)}


@router.get("/v1/job-policies")
def list_job_policies(db: Session = DB, p: Principal = Depends(require("job_policy:read"))) -> dict:
    queued = _queued(db)
    rows = db.execute(select(JobPolicy).order_by(JobPolicy.priority.desc(), JobPolicy.job_type)).scalars()
    return {"items": [_job_view(j, queued) for j in rows]}


@router.patch("/v1/job-policies/{job_type}")
def update_job_policy(job_type: str, body: JobPolicyPatch, request: Request, db: Session = DB,
                      c: Container = Depends(container), p: Principal = Depends(require("job_policy:write"))) -> dict:
    sent = _need_change(body)
    j = db.execute(select(JobPolicy).where(JobPolicy.job_type == job_type).with_for_update()).scalar_one_or_none()
    if j is None:
        raise ProblemError("NOT_FOUND")
    for name in sent:
        if getattr(body, name) is None:
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": f"/{name}", "code": "not_nullable",
                                                            "message": "비울 수 없는 항목입니다"}])
    before = {"priority": j.priority, "max_attempts": j.max_attempts}
    if body.priority is not None:
        j.priority = body.priority
        # 대기 중인 작업에도 바로 적용 (작업 행의 우선순위는 등록할 때 정책에서 복사된다)
        db.execute(update(JobRow).where(JobRow.job_type == job_type, JobRow.status == "QUEUED")
                   .values(priority=body.priority))
    if body.max_attempts is not None:
        j.max_attempts = body.max_attempts
    changes = _changes(before, {"priority": j.priority, "max_attempts": j.max_attempts})
    db.flush()
    if changes:
        c.audit.record("JOB_POLICY_CHANGED", principal=p, target_type="JobPolicy", target_id=job_type,
                       reason=body.reason, source_ip=client_ip(request), details={"changes": changes},
                       connection=db.connection())
    return _job_view(j, _queued(db))
