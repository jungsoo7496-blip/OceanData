"""데이터셋·카탈로그·이용 신청·권한·배포·이용 이력·DCAT (13번 요구사항 5~12)."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from sqlalchemy import DateTime, Integer, String, Text, cast, func, join, literal, null, select, tuple_, union_all
from sqlalchemy.orm import Session

from app.api.deps import DB, client_ip, container, page_params, require
from app.api.pagination import decode_cursor, encode_cursor, paginate
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
from app.domain.ids import id_pattern, new_id
from app.domain.normalize import parse_rfc3339
from app.domain.principal import Principal
from app.errors import ProblemError, duplicate_items
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


def _not_found(db: Session, c: Container, request: Request, p: Principal, model: type[Grant] | type[AccessRequest],
               ident: str) -> ProblemError:
    """역할별 조회(_requests_for·_grants_for)에서 빠진 행. 다른 기관 행이면 교차 기관 시도로 감사한다.

    load_scoped와 같은 기록이다 (security.md, 02번 요구사항 2.2). 행의 기관은 이용기관(organization_id)이고,
    같은 기관인데 역할 범위 밖이면 교차 기관이 아니다(tenant.get_scoped와 같은 규칙). 응답은 없는 행과 같은
    404이고, 감사는 되돌려지는 요청 트랜잭션과 따로 확정한다 (ADR-0053)."""
    pk = model.__mapper__.primary_key[0]
    owner = db.execute(select(model.organization_id).where(pk == ident)).scalar_one_or_none()
    if owner is not None and owner != p.organization_id:
        c.audit.record("CROSS_ORG_ACCESS_ATTEMPT", result="DENIED", principal=p, organization_id=owner,
                       target_type=model.__name__, target_id=ident, source_ip=client_ip(request),
                       details={"path": request.url.path})
    return ProblemError("NOT_FOUND")


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


def _visible(db: Session, p: Principal, ds: Dataset) -> list[DatasetVersion]:
    """이 주체에게 보이는 버전. 운영자는 모두, 이용기관은 게시·L3 이상·READY(요구사항 7), 제공기관은 자기 기관 자산이 든
    버전만이다 — 메타데이터·바이너리는 자기 기관만(domain.md 2장)이고 계보 조회와 같은 규칙이다. 데이터 카드와 샘플도
    이 목록에서 고른다 (통합 검토 SHARING-2, ADR-0088)."""
    vs = datasets.visible_versions(db, ds, consumer=_consumer(p))
    if _operator(p) or _consumer(p):
        return vs
    return [v for v in vs if p.organization_id in v.provider_organization_ids]


def _view(db: Session, c: Container, p: Principal, ds: Dataset) -> dict:
    vs = _visible(db, p, ds)
    return _with_samples(c, datasets.dataset_view(db, ds, vs), vs)


class DatasetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)


@router.post("/v1/datasets", status_code=201)
def create_dataset(body: DatasetCreate, request: Request, db: Session = DB,
                   c: Container = Depends(container), p: Principal = Depends(require("dataset:write"))) -> dict:
    ds = Dataset(dataset_id=new_id("dataset"), title=body.title, published=False, created_by=p.principal_id,
                 created_at=c.clock.now())
    db.add(ds)
    db.flush()
    c.audit.record("DATASET_CREATED", principal=p, target_type="Dataset", target_id=ds.dataset_id,
                   source_ip=client_ip(request))
    return datasets.dataset_view(db, ds, [])


def _dataset_page(db: Session, stmt: Any, p: Principal, limit: int,
                  cursor: str | None) -> tuple[list[tuple[Dataset, list[DatasetVersion]]], str | None]:
    """dataset_id 순으로 한 쪽 (api-conventions.md 커서). 커서는 마지막으로 돌려준 데이터셋 ID다.

    이용기관에게 보이는 버전(게시·L3 이상·READY)은 visible_versions가 파이썬에서 고르므로, 보이는 버전이 없는
    데이터셋은 건너뛰며 쪽이 찰 때까지 limit+1개씩 더 읽는다 — 쪽이 짧거나 다음 쪽이 비지 않게.
    운영자 말고는 보이는 버전(_visible)이 없는 데이터셋을 건너뛴다."""
    after = decode_cursor(cursor) if cursor else None
    out: list[tuple[Dataset, list[DatasetVersion]]] = []
    while True:
        q = stmt if after is None else stmt.where(Dataset.dataset_id > after)
        rows = list(db.execute(q.order_by(Dataset.dataset_id).limit(limit + 1)).scalars())
        for ds in rows:
            vs = _visible(db, p, ds)
            if not vs and not _operator(p):
                continue
            if len(out) == limit:  # 다음 쪽에 보일 것이 있다
                return out, encode_cursor(out[-1][0].dataset_id)
            out.append((ds, vs))
        if len(rows) <= limit:
            return out, None
        after = rows[-1].dataset_id


@router.get("/v1/datasets")
def list_datasets(db: Session = DB, c: Container = Depends(container),
                  p: Principal = Depends(require("dataset:read")), page: dict = Depends(page_params)) -> dict:
    stmt = select(Dataset)
    if _consumer(p):
        stmt = stmt.where(Dataset.published.is_(True))
    elif not _operator(p):
        stmt = stmt.where(Dataset.dataset_id.in_(_versions_with_org(p.organization_id)))
    rows, nxt = _dataset_page(db, stmt, p, page["limit"], page["cursor"])
    return {"items": [_with_samples(c, datasets.dataset_view(db, ds, vs), vs) for ds, vs in rows], "next_cursor": nxt}


@router.get("/v1/datasets/{dataset_id}")
def get_dataset(dataset_id: str, db: Session = DB, c: Container = Depends(container),
                p: Principal = Depends(require("dataset:read"))) -> dict:
    return _view(db, c, p, _load_dataset(db, p, dataset_id))


ASSET_ID = id_pattern("asset")  # common.schema.json#/$defs/asset_id


class VersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    item_asset_ids: list[Annotated[str, Field(pattern=ASSET_ID)]] = Field(min_length=1, max_length=20000)
    data_card: dict[str, Any]


@router.post("/v1/datasets/{dataset_id}/versions", status_code=201)
def create_version(dataset_id: str, body: VersionCreate, request: Request, db: Session = DB,
                   c: Container = Depends(container), p: Principal = Depends(require("dataset:write")),
                   idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    dup = duplicate_items("item_asset_ids", body.item_asset_ids, "자산 ID")
    if dup:  # 조용히 합치지 않는다. 멱등 기록을 남기기 전에 거부한다
        raise ProblemError("VALIDATION_ERROR", errors=dup)

    def create() -> tuple[int, dict]:
        ds = db.get(Dataset, dataset_id, with_for_update=True)
        if ds is None:
            raise ProblemError("NOT_FOUND")
        if db.execute(select(DatasetVersion).where(DatasetVersion.dataset_id == ds.dataset_id,
                                                   DatasetVersion.version == body.version)).scalar() is not None:
            raise ProblemError("CONFLICT", detail="같은 버전이 이미 있습니다 (버전은 바꿀 수 없고 새 번호로 만든다)")
        ids = sorted(body.item_asset_ids)  # 중복은 위에서 400
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
def get_version_lineage(dataset_id: str, dataset_version_id: str, db: Session = DB,
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
def publish_dataset(dataset_id: str, request: Request, db: Session = DB,
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

    @field_validator("start_date", "end_date")
    @classmethod
    def _calendar_date(cls, value: str, info: ValidationInfo) -> str:
        """형식만 맞고 달력에 없는 날짜(2월 30일·13월)는 400 (계약 format: date). 문자열 그대로 두어 멱등 지문을
        바꾸지 않는다. 이용 기간 끝은 끝 날짜 다음 날 0시라 date.max는 끝 날짜가 될 수 없다 (계약 점검 S07)."""
        try:
            day = date.fromisoformat(value)
        except ValueError:
            raise ValueError("달력에 있는 날짜여야 합니다") from None
        if info.field_name == "end_date" and day == date.max:
            raise ValueError("끝 날짜는 9999-12-30까지입니다 (이용 기간은 끝 날짜 다음 날 0시에 끝납니다)")
        return value


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
def create_access_request(body: AccessCreate, request: Request, db: Session = DB,
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
    stmt = select(AccessRequest)
    if _operator(p):
        return stmt
    if _consumer(p):
        return stmt.where(AccessRequest.organization_id == p.organization_id)
    vids = select(DatasetVersion.dataset_version_id).where(
        DatasetVersion.provider_organization_ids.any(p.organization_id))  # type: ignore[arg-type]
    return stmt.where(AccessRequest.dataset_version_id.in_(vids))


@router.get("/v1/access-requests")
def list_access_requests(db: Session = DB, p: Principal = Depends(require("access_request:read")),
                         page: dict = Depends(page_params)) -> dict:
    """access_request_id(ULID라 만든 순서) 순으로 limit개씩 (api-conventions.md, 통합 검토 SHARING-3, ADR-0088)."""
    rows, nxt = paginate(db, _requests_for(db, p), AccessRequest.access_request_id, page["limit"], page["cursor"])
    return {"items": [datasets.request_view(db, r) for r in rows], "next_cursor": nxt}


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVE", "DENY"]
    reason: str = Field(min_length=2, max_length=1000)


@router.post("/v1/access-requests/{access_request_id}/decisions")
def decide_access_request(access_request_id: str, body: Decision, request: Request, db: Session = DB,
                          c: Container = Depends(container),
                          p: Principal = Depends(require("access_request:decide"))) -> dict:
    req = db.execute(_requests_for(db, p).where(AccessRequest.access_request_id == access_request_id)
                     .with_for_update()).scalar_one_or_none()
    if req is None:
        raise _not_found(db, c, request, p, AccessRequest, access_request_id)
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
    stmt = select(Grant)
    if _operator(p):
        return stmt
    if _consumer(p):
        return stmt.where(Grant.organization_id == p.organization_id)
    vids = select(DatasetVersion.dataset_version_id).where(
        DatasetVersion.provider_organization_ids.any(p.organization_id))  # type: ignore[arg-type]
    return stmt.where(Grant.dataset_version_id.in_(vids))


@router.get("/v1/grants")
def list_grants(db: Session = DB, p: Principal = Depends(require("grant:read")),
                page: dict = Depends(page_params)) -> dict:
    """grant_id(ULID라 만든 순서) 순으로 limit개씩 (api-conventions.md, 통합 검토 SHARING-3, ADR-0088)."""
    rows, nxt = paginate(db, _grants_for(p), Grant.grant_id, page["limit"], page["cursor"])
    return {"items": [grant_view(g) for g in rows], "next_cursor": nxt}


class Revoke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=2, max_length=1000)


@router.post("/v1/grants/{grant_id}/revoke")
def revoke_grant(grant_id: str, body: Revoke, request: Request, db: Session = DB,
                 c: Container = Depends(container), p: Principal = Depends(require("grant:revoke"))) -> dict:
    g = db.execute(_grants_for(p).where(Grant.grant_id == grant_id).with_for_update()).scalar_one_or_none()
    if g is None:
        raise _not_found(db, c, request, p, Grant, grant_id)
    datasets.revoke(c, db, g, set(p.roles), p.principal_id, body.reason)
    c.audit.record("GRANT_REVOKED", principal=p, organization_id=g.organization_id, target_type="Grant",
                   target_id=g.grant_id, reason=body.reason, source_ip=client_ip(request))
    return grant_view(g)


@router.post("/v1/grants/{grant_id}/destruction-confirmation")
def confirm_destruction(grant_id: str, request: Request, db: Session = DB,
                        c: Container = Depends(container),
                        p: Principal = Depends(require("grant:confirm_destruction"))) -> dict:
    g = db.execute(_grants_for(p).where(Grant.grant_id == grant_id).with_for_update()).scalar_one_or_none()
    if g is None:
        raise _not_found(db, c, request, p, Grant, grant_id)
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
def create_distribution(body: DistributionCreate, request: Request, db: Session = DB,
                        c: Container = Depends(container),
                        p: Principal = Depends(require("distribution:create"))) -> dict:
    g = db.execute(_grants_for(p).where(Grant.grant_id == body.grant_id)).scalar_one_or_none()
    if g is None:
        raise _not_found(db, c, request, p, Grant, body.grant_id)
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


USAGE_KINDS = frozenset({"REQUESTED", "DECIDED", "GRANTED", "DOWNLOAD_ISSUED", "REVOKED", "EXPIRED"})


def _usage_events(org: str) -> Any:
    """제공기관 이용 이력의 사건을 한 줄씩 모은 하위 질의 (통합 검토 SHARING-3, ADR-0088).

    열은 초 단위 시각(at)·종류(kind)·사건 ID(event_id), 신청의 버전·이용기관·목적, 설명을 만들 값(text·until·files)이다.
    사건 ID는 종류 안에서 겹치지 않으므로 (at, kind, event_id)가 쪽을 나누는 열쇠다. 신청마다 따로 읽지 않는다."""
    vids = select(DatasetVersion.dataset_version_id).where(
        DatasetVersion.provider_organization_ids.any(org))  # type: ignore[arg-type]
    r = select(AccessRequest).where(AccessRequest.dataset_version_id.in_(vids)).subquery("r")
    ts = DateTime(timezone=True)

    def row(kind: Any, at: Any, event_id: Any, text: Any = None, until: Any = None, files: Any = None) -> Any:
        return select(cast(kind, String).label("kind"), func.date_trunc("second", at, type_=ts).label("at"),
                      cast(event_id, String).label("event_id"), r.c.access_request_id, r.c.dataset_version_id,
                      r.c.organization_id, r.c.purpose, cast(null() if text is None else text, Text).label("text"),
                      cast(null() if until is None else until, ts).label("until"),
                      cast(null() if files is None else files, Integer).label("files"))

    g, d, dl = Grant, AccessDecision, Distribution
    by_request = r.c.access_request_id
    return union_all(
        row(literal("REQUESTED"), r.c.created_at, by_request, r.c.purpose_detail).select_from(r),
        row(literal("DECIDED"), d.decided_at, d.access_request_id + ":" + d.organization_id,
            d.role + " " + d.decision + ": " + d.reason).select_from(join(d, r, by_request == d.access_request_id)),
        row(literal("GRANTED"), g.created_at, g.grant_id, until=g.expires_at)
        .select_from(join(g, r, by_request == g.access_request_id)),
        row(literal("DOWNLOAD_ISSUED"), dl.created_at, dl.distribution_id, files=dl.file_count)
        .select_from(join(dl, g, g.grant_id == dl.grant_id).join(r, by_request == g.access_request_id)),
        row(g.status, func.coalesce(g.revoked_at, g.expires_at), g.grant_id, func.coalesce(g.revoke_reason, ""))
        .select_from(join(g, r, by_request == g.access_request_id)).where(g.status.in_(("REVOKED", "EXPIRED"))),
    ).subquery("ev")


def _usage_cursor(cursor: str) -> tuple[datetime, str, str]:
    """이용 이력 커서 = 앞 쪽 마지막 사건의 [at(초 단위 RFC 3339), kind, 사건 ID] (ADR-0088)."""
    try:
        value = json.loads(decode_cursor(cursor))
        if not isinstance(value, list) or len(value) != 3 or not all(isinstance(v, str) for v in value):
            raise ValueError
        if value[1] not in USAGE_KINDS:
            raise ValueError
        return parse_rfc3339(value[0]), value[1], value[2]
    except (ValueError, OverflowError):
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/query/cursor", "code": "invalid",
                                                        "message": "커서 형식이 올바르지 않습니다"}]) from None


@router.get("/v1/usage-history")
def usage_history(db: Session = DB, p: Principal = Depends(require("usage_history:read")),
                  page: dict = Depends(page_params)) -> dict:
    """제공기관: 자기 자산이 든 데이터셋 버전의 신청·결정·권한·다운로드 이력.

    사건 하나가 항목 하나이고 (at 초 단위, kind, 사건 ID) 순으로 limit개씩 나눈다. 쪽을 이어 붙이면 전에 한 번에
    주던 순서(at·kind)와 같다 (api-conventions.md, 통합 검토 SHARING-3, ADR-0088)."""
    ev, limit = _usage_events(p.organization_id), page["limit"]
    stmt = select(ev)
    if page["cursor"]:
        at, kind, event_id = _usage_cursor(page["cursor"])
        stmt = stmt.where(tuple_(ev.c.at, ev.c.kind, ev.c.event_id) > tuple_(
            literal(at, DateTime(timezone=True)), literal(kind, String), literal(event_id, String)))
    rows = db.execute(stmt.order_by(ev.c.at, ev.c.kind, ev.c.event_id).limit(limit + 1)).all()
    orgs = {x.organization_id for x in rows[:limit]}
    names: dict[str, str] = dict(db.execute(select(Organization.organization_id, Organization.name).where(
        Organization.organization_id.in_(orgs))).tuples().all()) if orgs else {}
    items: list[dict[str, Any]] = []
    for x in rows[:limit]:
        detail = {"GRANTED": f"~ {iso(x.until)}", "DOWNLOAD_ISSUED": f"파일 {x.files}개"}.get(x.kind, x.text or "")
        items.append({"dataset_version_id": x.dataset_version_id, "access_request_id": x.access_request_id,
                      "consumer_organization": names.get(x.organization_id, x.organization_id),
                      "purpose": x.purpose, "kind": x.kind, "at": iso(x.at), "detail": detail})
    last = rows[limit - 1] if len(rows) > limit else None
    nxt = encode_cursor(json.dumps([iso(last.at), last.kind, last.event_id])) if last is not None else None
    return {"items": items, "next_cursor": nxt}


@router.get("/v1/catalog/dcat")
def dcat(db: Session = DB, c: Container = Depends(container)) -> JSONResponse:
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

