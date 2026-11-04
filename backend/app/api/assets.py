"""자산 조회·수정·영수증 (04번 요구사항 5.3, 7.1, 7.3). 응답은 asset-metadata.schema.json 형식."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import client_ip, container, get_db, load_scoped, page_params, require
from app.api.pagination import paginate
from app.container import Container
from app.db.models import Asset, TrialSession
from app.domain import states
from app.domain.normalize import generalize_location
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services.ingest import EXT_KEYS, receipt
from app.services.tenant import scoped_select
from app.timefmt import iso

router = APIRouter(tags=["assets"])


def asset_view(a: Asset, p: Principal) -> dict[str, Any]:
    precision = a.location_precision
    if p.organization_id != a.organization_id and precision == "EXACT_RESTRICTED":
        precision = "GENERALIZED"  # 정확한 위치는 소유 기관만 (03번 요구사항 6.4)
    location = generalize_location(a.location, precision)
    out: dict[str, Any] = {
        "schema_version": a.schema_version, "asset_id": a.asset_id, "organization_id": a.organization_id,
        "project_id": a.project_id, "session_id": a.session_id, "source_system_id": a.source_system_id,
        "data_type": a.data_type, "variant": a.variant, "data_role": a.data_role,
        "capture_start_at": iso(a.capture_start_at), "capture_end_at": iso(a.capture_end_at),
        "ingested_at": iso(a.ingested_at), "timestamp_quality": a.timestamp_quality, "mime_type": a.mime_type,
        "byte_size": a.byte_size, "checksum_sha256": a.checksum_sha256, "has_binary": a.has_binary,
        "collection_mode": a.collection_mode, "privacy_status": a.privacy_status,
        "sensitivity_class": a.sensitivity_class, "sharing_scope": a.sharing_scope,
        "retention_policy_id": a.retention_policy_id, "retention_until": iso(a.retention_until),
        "quality_status": a.quality_status, "quality_reasons": list(a.quality_reasons or []),
        "ingest_status": a.ingest_status, "share_status": a.share_status, "backup_class": a.backup_class,
        "provenance": a.provenance,
    }
    optional = {"dataset_id": a.dataset_id, "location": location,
                "location_precision": precision if location else None, "area_code": a.area_code,
                "legal_basis_ref": a.legal_basis_ref, "quarantine_reason": a.quarantine_reason,
                "review_result": a.review_result, "env_context": a.env_context, "tags": a.tags}
    if p.organization_id == a.organization_id and a.source_location:
        optional["source_location"] = a.source_location
    out.update({k: v for k, v in optional.items() if v is not None})
    out.update({k: v for k, v in (a.ext or {}).items() if k in EXT_KEYS})
    return out


@router.get("/v1/assets")
def list_assets(request: Request, session_id: str | None = None, source_system_id: str | None = None,
                data_type: str | None = None, ingest_status: str | None = None, quality_status: str | None = None,
                sharing_scope: str | None = None, variant: str | None = None, captured_from: datetime | None = None,
                captured_to: datetime | None = None, db: Session = Depends(get_db),
                p: Principal = Depends(require("asset:read")), page: dict = Depends(page_params)) -> dict:
    stmt = scoped_select(Asset, p)
    for col, val in ((Asset.session_id, session_id), (Asset.source_system_id, source_system_id),
                     (Asset.data_type, data_type), (Asset.ingest_status, ingest_status),
                     (Asset.quality_status, quality_status), (Asset.sharing_scope, sharing_scope),
                     (Asset.variant, variant)):
        if val is not None:
            stmt = stmt.where(col == val)
    if captured_from is not None:
        stmt = stmt.where(Asset.capture_start_at >= captured_from)
    if captured_to is not None:
        stmt = stmt.where(Asset.capture_start_at < captured_to)
    rows, nxt = paginate(db, stmt, Asset.asset_id, page["limit"], page["cursor"])
    return {"items": [asset_view(a, p) for a in rows], "next_cursor": nxt}


@router.get("/v1/assets/{asset_id}")
def get_asset(asset_id: str, request: Request, db: Session = Depends(get_db), c: Container = Depends(container),
              p: Principal = Depends(require("asset:read"))) -> dict:
    a = load_scoped(db, c, request, Asset, asset_id, p)
    c.audit.record("ASSET_VIEWED", principal=p, organization_id=a.organization_id, target_type="Asset",
                   target_id=a.asset_id, source_ip=client_ip(request))  # 자산 상세 조회 감사 (security.md)
    return asset_view(a, p)


class AssetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str | None = Field(default=None, pattern=r"^ses_[0-9A-HJKMNP-TV-Z]{26}$")
    data_role: Literal["CORE", "BYPRODUCT"] | None = None
    location_precision: Literal["EXACT_RESTRICTED", "GENERALIZED", "NONE"] | None = None
    reason: str | None = Field(default=None, min_length=2, max_length=500)


@router.patch("/v1/assets/{asset_id}")
def patch_asset(asset_id: str, body: AssetPatch, request: Request, db: Session = Depends(get_db),
                c: Container = Depends(container), p: Principal = Depends(require("asset:write"))) -> dict:
    if not body.model_fields_set - {"reason"}:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "", "code": "min_properties",
                                                        "message": "바꿀 항목이 하나 이상 필요합니다"}])
    a = load_scoped(db, c, request, Asset, asset_id, p)
    now = c.clock.now()
    if body.data_role is not None and body.data_role != a.data_role:
        if not p.has_role("ORG_ADMIN"):
            raise ProblemError("FORBIDDEN", detail="data_role 변경은 ORG_ADMIN만 할 수 있습니다")
        if not body.reason:
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/reason", "code": "required",
                                                            "message": "data_role을 바꾸려면 사유가 필요합니다"}])
        if body.data_role == "CORE" and a.sharing_scope not in ("PRIVATE", "PROGRAM_REVIEW"):
            raise ProblemError("RULE_VIOLATION", detail="CORE는 L1을 넘을 수 없습니다. 먼저 공유 등급을 내리세요",
                               unmet_conditions=[{"condition": "sharing_scope ∈ {PRIVATE, PROGRAM_REVIEW}",
                                                  "actual": a.sharing_scope}])
        c.audit.record("DATA_ROLE_CHANGED", principal=p, organization_id=a.organization_id, target_type="Asset",
                       target_id=a.asset_id, reason=body.reason, source_ip=client_ip(request),
                       details={"from": a.data_role, "to": body.data_role})
        a.data_role = body.data_role
    if body.location_precision is not None:
        a.location_precision = body.location_precision if a.location else None
    if body.session_id is not None and body.session_id != a.session_id:
        session = load_scoped(db, c, request, TrialSession, body.session_id, p)
        if session.project_id != a.project_id:
            raise ProblemError("RULE_VIOLATION", detail="회차가 자산과 다른 실증사업에 속합니다",
                               unmet_conditions=[{"condition": "session.project_id=asset.project_id",
                                                  "actual": session.project_id}])
        if a.ingest_status == "QUARANTINED" and a.quarantine_reason in ("UNKNOWN_SESSION",
                                                                         "MISSING_REQUIRED_METADATA"):
            # 보완 후 재검증 (04번 요구사항 5.3, domain.md 5장 QUARANTINED → VALIDATING)
            a.ingest_status = states.transition("ingest_status", a.ingest_status, "VALIDATING", p.roles)
            a.session_id = session.session_id
            db.flush()
            c.queue.enqueue("integrity_verify", {"asset_id": a.asset_id, "revalidate": True},
                            organization_id=a.organization_id, dedupe_key=f"revalidate:{a.asset_id}:{now.timestamp()}")
        elif a.ingest_status in ("UPLOADING", "RECEIVED", "QUARANTINED") or a.session_id is None:
            a.session_id = session.session_id
        else:
            raise ProblemError("CONFLICT", detail="등록된 자산의 회차는 바꿀 수 없습니다")
    a.updated_at = now
    db.flush()
    return asset_view(a, p)


@router.get("/v1/assets/{asset_id}/receipt")
def get_receipt(asset_id: str, request: Request, db: Session = Depends(get_db), c: Container = Depends(container),
                p: Principal = Depends(require("asset:receipt"))) -> dict:
    a = load_scoped(db, c, request, Asset, asset_id, p)
    r = receipt(db, a)
    if r is None:
        raise ProblemError("NOT_FOUND", detail="아직 등록(REGISTERED)되지 않은 자산입니다")
    return r


class ReprocessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[Literal["META", "PROXY", "THUMBNAIL"]] = Field(min_length=1)
    reason: str = Field(min_length=2, max_length=500)


@router.post("/v1/assets/{asset_id}/reprocess", status_code=202)
def reprocess_asset(asset_id: str, body: ReprocessRequest, request: Request, db: Session = Depends(get_db),
                    c: Container = Depends(container), p: Principal = Depends(require("asset:reprocess"))) -> dict:
    """처리 단계 재실행 (05번 요구사항 7.2). 새 파생 자산은 계보에 연결된다."""
    from app.domain.ids import new_ulid
    from app.services.pipeline import JOB_FOR_STEP

    if len(set(body.steps)) != len(body.steps):
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/steps", "code": "unique",
                                                        "message": "단계가 중복되었습니다"}])
    a = load_scoped(db, c, request, Asset, asset_id, p)
    if not a.has_binary or a.data_type != "VIDEO" or a.variant not in ("ORIGINAL", "CLIP"):
        raise ProblemError("CONFLICT", detail="바이너리가 있는 원 영상 자산만 재처리합니다")
    if a.ingest_status == "FAILED":  # 실패 직전 단계로 (domain.md 5장: FAILED → 이전 단계, PLATFORM_OPERATOR)
        back = (a.ingest_context or {}).get("failed_from", "REGISTERED")
        a.ingest_status = states.transition("ingest_status", "FAILED", back, p.roles)
    elif a.ingest_status not in ("REGISTERED", "PROXIED", "READY_PRIVATE"):
        raise ProblemError("CONFLICT", detail=f"{a.ingest_status} 상태에서는 재처리할 수 없습니다")
    order = [s for s in ("META", "PROXY", "THUMBNAIL") if s in body.steps]
    jobs = [JOB_FOR_STEP[s] for s in order]
    run = new_ulid()
    db.flush()
    job_id = c.queue.enqueue(jobs[0], {"asset_id": a.asset_id, "chain": jobs[1:], "run": run, "reprocess": True},
                             organization_id=a.organization_id, dedupe_key=f"{jobs[0]}:{a.asset_id}:{run}")
    c.audit.record("REPROCESS_REQUESTED", principal=p, organization_id=a.organization_id, target_type="Asset",
                   target_id=a.asset_id, reason=body.reason, source_ip=client_ip(request), details={"steps": order})
    return {"asset_id": a.asset_id, "job_ids": [job_id]}
