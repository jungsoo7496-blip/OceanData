"""데이터셋·카탈로그·이용 신청·권한·배포·이용 이력·DCAT (13번 요구사항 5~12)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, container, get_db, require
from app.container import Container
from app.db.models import (
    AccessDecision,
    AccessRequest,
    Dataset,
    DatasetVersion,
    Distribution,
    Grant,
    Organization,
)
from app.domain.ids import new_id
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import contracts, datasets
from app.services.idempotency import run_idempotent
from app.timefmt import iso

router = APIRouter(tags=["datasets"])


def _operator(p: Principal) -> bool:
    return not p.is_machine and p.has_role("PLATFORM_OPERATOR")


def _consumer(p: Principal) -> bool:
    return not p.is_machine and p.has_role("CONSUMER") and not _operator(p)


def _versions_with_org(org: str) -> Any:
    return select(DatasetVersion.dataset_id).where(DatasetVersion.provider_organization_ids.any(org))  # type: ignore[arg-type]


def _load_dataset(db: Session, p: Principal, dataset_id: str) -> Dataset:
    ds = db.get(Dataset, dataset_id)
    if ds is None:
        raise ProblemError("NOT_FOUND")
    if _operator(p):
        return ds
    if _consumer(p):
        if not datasets.visible_versions(db, ds, consumer=True):
            raise ProblemError("NOT_FOUND")  # 게시·L3 이상이 아니면 존재를 드러내지 않는다 (요구사항 7.2)
        return ds
    if db.execute(_versions_with_org(p.organization_id).where(DatasetVersion.dataset_id == ds.dataset_id)
                  .limit(1)).scalar() is None:
        raise ProblemError("NOT_FOUND")
    return ds


def _with_samples(c: Container, view: dict, versions: list[DatasetVersion]) -> dict:
    """최신 READY 버전의 샘플 썸네일 단기 URL (요구사항 7.1). 보이는 버전에서만 고른다."""
    ready = [v for v in versions if v.status == "READY"]
    samples = datasets.sample_urls(c, ready[-1] if ready else None)
    return {**view, "samples": samples} if samples else view


def _view(db: Session, c: Container, p: Principal, ds: Dataset) -> dict:
    vs = datasets.visible_versions(db, ds, consumer=_consumer(p))
    return _with_samples(c, datasets.dataset_view(db, ds, vs), vs)


class DatasetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)


@router.post("/v1/datasets", status_code=201)
def create_dataset(body: DatasetCreate, request: Request, db: Session = Depends(get_db),
                   c: Container = Depends(container), p: Principal = Depends(require("dataset:write"))) -> dict:
    ds = Dataset(dataset_id=new_id("dataset"), title=body.title, published=False, created_by=p.principal_id,
                 created_at=c.clock.now())
    db.add(ds)
    db.flush()
    c.audit.record("DATASET_CREATED", principal=p, target_type="Dataset", target_id=ds.dataset_id,
                   source_ip=client_ip(request))
    return datasets.dataset_view(db, ds, [])


@router.get("/v1/datasets")
def list_datasets(db: Session = Depends(get_db), c: Container = Depends(container),
                  p: Principal = Depends(require("dataset:read"))) -> dict:
    stmt = select(Dataset).order_by(Dataset.dataset_id)
    if _consumer(p):
        stmt = stmt.where(Dataset.published.is_(True))
    elif not _operator(p):
        stmt = stmt.where(Dataset.dataset_id.in_(_versions_with_org(p.organization_id)))
    out = []
    for ds in db.execute(stmt).scalars():
        vs = datasets.visible_versions(db, ds, consumer=_consumer(p))
        if _consumer(p) and not vs:
            continue
        out.append(_with_samples(c, datasets.dataset_view(db, ds, vs), vs))
    return {"items": out, "next_cursor": None}


@router.get("/v1/datasets/{dataset_id}")
def get_dataset(dataset_id: str, db: Session = Depends(get_db), c: Container = Depends(container),
                p: Principal = Depends(require("dataset:read"))) -> dict:
    return _view(db, c, p, _load_dataset(db, p, dataset_id))


class VersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    item_asset_ids: list[str] = Field(min_length=1, max_length=20000)
    data_card: dict[str, Any]


@router.post("/v1/datasets/{dataset_id}/versions", status_code=201)
def create_version(dataset_id: str, body: VersionCreate, request: Request, db: Session = Depends(get_db),
                   c: Container = Depends(container), p: Principal = Depends(require("dataset:write")),
                   idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    def create() -> tuple[int, dict]:
        ds = db.get(Dataset, dataset_id, with_for_update=True)
        if ds is None:
            raise ProblemError("NOT_FOUND")
        if db.execute(select(DatasetVersion).where(DatasetVersion.dataset_id == ds.dataset_id,
                                                   DatasetVersion.version == body.version)).scalar() is not None:
            raise ProblemError("CONFLICT", detail="같은 버전이 이미 있습니다 (버전은 바꿀 수 없고 새 번호로 만든다)")
        ids = sorted(set(body.item_asset_ids))
        if len(ids) > c.settings.dataset_sync_max_items:  # 큰 버전은 작업 큐에서 (13번 작업 32)
            bv, job_id = datasets.start_build(c, db, ds, body.version, ids, body.data_card, p.principal_id)
            c.audit.record("DATASET_VERSION_BUILD_STARTED", principal=p, target_type="DatasetVersion",
                           target_id=bv.dataset_version_id, source_ip=client_ip(request),
                           details={"items": bv.item_count, "job_id": job_id})
            return 202, {"dataset_version_id": bv.dataset_version_id, "version": bv.version, "status": "BUILDING",
                         "item_count": bv.item_count, "job_id": job_id}
        v = datasets.build_version(c, db, ds, body.version, ids, body.data_card, p.principal_id)
        c.audit.record("DATASET_VERSION_CREATED", principal=p, target_type="DatasetVersion",
                       target_id=v.dataset_version_id, source_ip=client_ip(request),
                       details={"items": v.item_count, "manifest_sha256": v.manifest_sha256})
        return 201, {"dataset_version_id": v.dataset_version_id, "version": v.version,
                     "scope": datasets.version_scope(db, v), "manifest_sha256": v.manifest_sha256,
                     "manifest": v.manifest}

    status, resp, replayed = run_idempotent(db, c.clock, c.settings.idempotency_retention_s, p,
                                            f"POST /v1/datasets/{dataset_id}/versions", idempotency_key,
                                            body.model_dump(mode="json"), create)
    return JSONResponse(resp, status_code=status, headers={"Idempotent-Replayed": "true"} if replayed else None)


@router.get("/v1/datasets/{dataset_id}/versions/{dataset_version_id}/lineage")
def get_version_lineage(dataset_id: str, dataset_version_id: str, db: Session = Depends(get_db),
                        p: Principal = Depends(require("dataset:lineage"))) -> dict:
    """항목별 원본·처리 모델·검수자 (13번 요구사항 5.3, 14번 요구사항 3.1). 제공기관은 자기 항목만."""
    ds = _load_dataset(db, p, dataset_id)
    v = db.get(DatasetVersion, dataset_version_id)
    org = None if _operator(p) else p.organization_id
    if v is None or v.dataset_id != ds.dataset_id or (org is not None and org not in v.provider_organization_ids):
        raise ProblemError("NOT_FOUND")
    if v.status != "READY":
        raise ProblemError("CONFLICT", detail=f"아직 다 만들지 못한 버전입니다 ({v.status})")
    return datasets.version_lineage(db, v, org)


@router.post("/v1/datasets/{dataset_id}/publish")
def publish_dataset(dataset_id: str, request: Request, db: Session = Depends(get_db),
                    c: Container = Depends(container), p: Principal = Depends(require("dataset:write"))) -> dict:
    ds = db.get(Dataset, dataset_id, with_for_update=True)
    if ds is None:
        raise ProblemError("NOT_FOUND")
    vs = datasets.visible_versions(db, ds, consumer=False)
    ready = [v for v in vs if v.status == "READY"]  # 다 만든 버전 기준 (13번 작업 32)
    if not ready:
        raise ProblemError("RULE_VIOLATION", detail="다 만든 버전이 없는 데이터셋은 게시할 수 없습니다",
                           unmet_conditions=[{"condition": "READY 데이터셋 버전 1개 이상", "actual": "0"}])
    problems = contracts.schema_problems("dataset-version-manifest", ready[-1].manifest)
    if problems:  # 데이터 카드 필수 항목 (요구사항 6.2)
        raise ProblemError("RULE_VIOLATION", detail="데이터 카드 필수 항목이 비어 있습니다",
                           unmet_conditions=[{"condition": x.pointer, "actual": x.message} for x in problems[:20]])
    ds.published, ds.published_at = True, c.clock.now()
    c.audit.record("DATASET_PUBLISHED", principal=p, target_type="Dataset", target_id=ds.dataset_id,
                   source_ip=client_ip(request))
    return datasets.dataset_view(db, ds, vs)


# ------------------------------------------------------------------ 이용 신청 (요구사항 8)


class Period(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class AccessCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_version_id: str
    purpose: Literal["JOINT_RESEARCH", "AI_TRAINING", "MODEL_IMPROVEMENT", "COMMERCIAL_SERVICE", "EXTERNAL_SALE"]
    purpose_detail: str = Field(min_length=10, max_length=2000)
    period: Period
    security_measures: str = Field(min_length=10, max_length=2000)
    agreement_accepted: Literal[True]
    agreement_version: str | None = Field(None, max_length=32)


@router.post("/v1/access-requests", status_code=201)
def create_access_request(body: AccessCreate, request: Request, db: Session = Depends(get_db),
                          c: Container = Depends(container), p: Principal = Depends(require("access_request:create")),
                          idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    def create() -> tuple[int, dict]:
        v = db.get(DatasetVersion, body.dataset_version_id)
        ds = db.get(Dataset, v.dataset_id) if v else None
        if v is None or ds is None or v not in datasets.visible_versions(db, ds, consumer=True):
            raise ProblemError("NOT_FOUND")
        req = datasets.create_request(c, db, v, p.organization_id, p.principal_id, body.model_dump(mode="json"))
        db.flush()
        c.audit.record("ACCESS_REQUESTED", principal=p, organization_id=p.organization_id,
                       target_type="AccessRequest", target_id=req.access_request_id, source_ip=client_ip(request),
                       details={"dataset_version_id": v.dataset_version_id, "purpose": body.purpose})
        return 201, datasets.request_view(db, req)

    status, resp, replayed = run_idempotent(db, c.clock, c.settings.idempotency_retention_s, p,
                                            "POST /v1/access-requests", idempotency_key,
                                            body.model_dump(mode="json"), create)
    return JSONResponse(resp, status_code=status, headers={"Idempotent-Replayed": "true"} if replayed else None)


def _requests_for(db: Session, p: Principal) -> Any:
    stmt = select(AccessRequest).order_by(AccessRequest.created_at, AccessRequest.access_request_id)
    if _operator(p):
        return stmt
    if _consumer(p):
        return stmt.where(AccessRequest.organization_id == p.organization_id)
    vids = select(DatasetVersion.dataset_version_id).where(
        DatasetVersion.provider_organization_ids.any(p.organization_id))  # type: ignore[arg-type]
    return stmt.where(AccessRequest.dataset_version_id.in_(vids))


@router.get("/v1/access-requests")
def list_access_requests(db: Session = Depends(get_db), p: Principal = Depends(require("access_request:read"))) -> dict:
    return {"items": [datasets.request_view(db, r) for r in db.execute(_requests_for(db, p)).scalars()]}


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVE", "DENY"]
    reason: str = Field(min_length=2, max_length=1000)


@router.post("/v1/access-requests/{access_request_id}/decisions")
def decide_access_request(access_request_id: str, body: Decision, request: Request, db: Session = Depends(get_db),
                          c: Container = Depends(container),
                          p: Principal = Depends(require("access_request:decide"))) -> dict:
    req = db.execute(_requests_for(db, p).where(AccessRequest.access_request_id == access_request_id)
                     .with_for_update()).scalar_one_or_none()
    if req is None:
        raise ProblemError("NOT_FOUND")
    v = db.get(DatasetVersion, req.dataset_version_id)
    assert v is not None  # noqa: S101 - FK
    try:
        g = datasets.decide(c, db, req, v, p.organization_id, set(p.roles), body.decision == "APPROVE", body.reason,
                            p.principal_id)
    except PermissionError:
        raise ProblemError("FORBIDDEN") from None
    c.audit.record("ACCESS_DECIDED", principal=p, organization_id=p.organization_id, target_type="AccessRequest",
                   target_id=req.access_request_id, reason=body.reason, source_ip=client_ip(request),
                   details={"decision": body.decision, "status": req.status, "grant_id": g.grant_id if g else None})
    db.flush()
    return datasets.request_view(db, req)


# ------------------------------------------------------------------ 권한·배포 (요구사항 9, 10)


def grant_view(g: Grant) -> dict:
    out = {"grant_id": g.grant_id, "access_request_id": g.access_request_id, "dataset_version_id": g.dataset_version_id,
           "organization_id": g.organization_id, "status": g.status, "starts_at": iso(g.starts_at),
           "expires_at": iso(g.expires_at), "revoked_at": iso(g.revoked_at), "revoke_reason": g.revoke_reason,
           "destruction_confirmed_at": iso(g.destruction_confirmed_at)}
    if g.status == "REVOKED":
        out["notice"] = datasets.NO_RECALL
    return out


def _grants_for(p: Principal) -> Any:
    stmt = select(Grant).order_by(Grant.created_at, Grant.grant_id)
    if _operator(p):
        return stmt
    if _consumer(p):
        return stmt.where(Grant.organization_id == p.organization_id)
    vids = select(DatasetVersion.dataset_version_id).where(
        DatasetVersion.provider_organization_ids.any(p.organization_id))  # type: ignore[arg-type]
    return stmt.where(Grant.dataset_version_id.in_(vids))


@router.get("/v1/grants")
def list_grants(db: Session = Depends(get_db), p: Principal = Depends(require("grant:read"))) -> dict:
    return {"items": [grant_view(g) for g in db.execute(_grants_for(p)).scalars()]}


class Revoke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=2, max_length=1000)


@router.post("/v1/grants/{grant_id}/revoke")
def revoke_grant(grant_id: str, body: Revoke, request: Request, db: Session = Depends(get_db),
                 c: Container = Depends(container), p: Principal = Depends(require("grant:revoke"))) -> dict:
    g = db.execute(_grants_for(p).where(Grant.grant_id == grant_id).with_for_update()).scalar_one_or_none()
    if g is None:
        raise ProblemError("NOT_FOUND")
    datasets.revoke(c, db, g, set(p.roles), p.principal_id, body.reason)
    c.audit.record("GRANT_REVOKED", principal=p, organization_id=g.organization_id, target_type="Grant",
                   target_id=g.grant_id, reason=body.reason, source_ip=client_ip(request))
    return grant_view(g)


@router.post("/v1/grants/{grant_id}/destruction-confirmation")
def confirm_destruction(grant_id: str, request: Request, db: Session = Depends(get_db),
                        c: Container = Depends(container),
                        p: Principal = Depends(require("grant:confirm_destruction"))) -> dict:
    g = db.execute(_grants_for(p).where(Grant.grant_id == grant_id).with_for_update()).scalar_one_or_none()
    if g is None:
        raise ProblemError("NOT_FOUND")
    if g.status not in ("REVOKED", "EXPIRED") or g.destruction_confirmed_at is not None:
        raise ProblemError("CONFLICT", detail="철회·만료된 권한에 한 번만 제출합니다")
    g.destruction_confirmed_at = c.clock.now()
    c.audit.record("GRANT_DESTRUCTION_CONFIRMED", principal=p, organization_id=g.organization_id, target_type="Grant",
                   target_id=g.grant_id, source_ip=client_ip(request))
    return grant_view(g)


class DistributionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant_id: str
    paths: list[str] | None = Field(None, max_length=1000)


@router.post("/v1/distributions", status_code=201)
def create_distribution(body: DistributionCreate, request: Request, db: Session = Depends(get_db),
                        c: Container = Depends(container),
                        p: Principal = Depends(require("distribution:create"))) -> dict:
    g = db.execute(_grants_for(p).where(Grant.grant_id == body.grant_id)).scalar_one_or_none()
    if g is None:
        raise ProblemError("NOT_FOUND")
    try:
        out = datasets.distribute(c, db, g, p.principal_id, body.paths)
    except datasets.GrantInactive as exc:
        raise ProblemError("GONE", detail=f"이용 권한이 유효하지 않습니다 ({exc})") from None
    # URL은 기록하지 않는다. 제공기관 이용 이력에 남는다 (요구사항 9.2)
    c.audit.record("DISTRIBUTION_ISSUED", principal=p, organization_id=g.organization_id, target_type="Grant",
                   target_id=g.grant_id, source_ip=client_ip(request),
                   details={"distribution_id": out["distribution_id"], "files": len(out["files"])})
    return out


# ------------------------------------------------------------------ 이용 이력·DCAT (요구사항 11, 12)


@router.get("/v1/usage-history")
def usage_history(db: Session = Depends(get_db), p: Principal = Depends(require("usage_history:read"))) -> dict:
    """제공기관: 자기 자산이 든 데이터셋 버전의 신청·결정·권한·다운로드 이력."""
    vids = select(DatasetVersion.dataset_version_id).where(
        DatasetVersion.provider_organization_ids.any(p.organization_id))  # type: ignore[arg-type]
    names: dict[str, str] = {o: n for o, n in db.execute(select(Organization.organization_id, Organization.name)).all()}
    items: list[dict[str, Any]] = []
    for r in db.execute(select(AccessRequest).where(AccessRequest.dataset_version_id.in_(vids))).scalars():
        base = {"dataset_version_id": r.dataset_version_id, "access_request_id": r.access_request_id,
                "consumer_organization": names.get(r.organization_id, r.organization_id), "purpose": r.purpose}
        items.append({**base, "kind": "REQUESTED", "at": iso(r.created_at), "detail": r.purpose_detail})
        for d in db.execute(select(AccessDecision).where(
                AccessDecision.access_request_id == r.access_request_id)).scalars():
            items.append({**base, "kind": "DECIDED", "at": iso(d.decided_at),
                          "detail": f"{d.role} {d.decision}: {d.reason}"})
        g = db.execute(select(Grant).where(Grant.access_request_id == r.access_request_id)).scalar_one_or_none()
        if g is None:
            continue
        items.append({**base, "kind": "GRANTED", "at": iso(g.created_at), "detail": f"~ {iso(g.expires_at)}"})
        for dl in db.execute(select(Distribution).where(Distribution.grant_id == g.grant_id)).scalars():
            items.append({**base, "kind": "DOWNLOAD_ISSUED", "at": iso(dl.created_at),
                          "detail": f"파일 {dl.file_count}개"})
        if g.status in ("REVOKED", "EXPIRED"):
            items.append({**base, "kind": g.status, "at": iso(g.revoked_at or g.expires_at),
                          "detail": g.revoke_reason or ""})
    return {"items": sorted(items, key=lambda x: (x["at"] or "", x["kind"]))}


@router.get("/v1/catalog/dcat")
def dcat(db: Session = Depends(get_db), c: Container = Depends(container)) -> JSONResponse:
    """모든 항목이 L4인 게시 데이터셋만 DCAT 3 JSON-LD로 (요구사항 12). 광역 허브 프로파일은 확인 필요."""
    out = []
    for ds in db.execute(select(Dataset).where(Dataset.published.is_(True)).order_by(Dataset.dataset_id)).scalars():
        vs = [v for v in datasets.visible_versions(db, ds, consumer=False)
              if datasets.version_scope(db, v) == "PUBLIC"]
        if not vs:
            continue
        v = vs[-1]
        card = v.data_card
        out.append({
            "@type": "dcat:Dataset", "@id": f"urn:marine:dataset:{ds.dataset_id}", "dct:identifier": ds.dataset_id,
            "dct:title": card.get("title", ds.title), "dct:issued": iso(ds.published_at),
            "dcat:version": v.version, "dct:modified": iso(v.created_at),
            "dct:temporal": {"@type": "dct:PeriodOfTime",
                             "dcat:startDate": card.get("source_summary", {}).get("period_start"),
                             "dcat:endDate": card.get("source_summary", {}).get("period_end")},
            "dct:spatial": card.get("source_summary", {}).get("sea_areas_generalized", []),
            "dct:rightsHolder": card.get("rights_holders", []),
            "dct:accessRights": "PUBLIC",
        })
    doc = {"@context": {"dcat": "http://www.w3.org/ns/dcat#", "dct": "http://purl.org/dc/terms/"},
           "@type": "dcat:Catalog", "dct:title": "해양 규제자유특구 실증데이터 공개 카탈로그", "dcat:dataset": out}
    return JSONResponse(doc, media_type="application/ld+json")

