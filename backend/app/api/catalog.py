"""스키마·코드표·사전 검증·장비 정책·계보·품질 집계 (03번 요구사항 1.3, 2, 8, 10, 11, 13)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text, true
from sqlalchemy.orm import Session

from app.api.deps import DB, client_ip, container, load_scoped, require
from app.container import Container
from app.db.models import Asset, Device, OrgEventCode, TrialSession
from app.domain.normalize import parse_rfc3339
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import contracts, device_policy
from app.services.idempotency import run_idempotent
from app.services.org_event_codes import register_subcode
from app.services.tenant import scope_filter
from app.timefmt import iso

router = APIRouter()
_ORG_EVENT_SUBCODES = "/v1/codes/event-categories/subcodes"


@router.get("/v1/schemas", tags=["contracts"])
def list_schemas(_: Principal = Depends(require("schema:read"))) -> dict:
    version = contracts.supported_schema_versions()[-1]
    return {"items": [{"name": n, "version": version} for n in sorted(contracts.schema_files())]}


@router.get("/v1/schemas/{name}/{version}", tags=["contracts"])
def get_schema(name: str, version: str, _: Principal = Depends(require("schema:read"))) -> JSONResponse:
    if name not in contracts.schema_files() or version not in contracts.supported_schema_versions():
        raise ProblemError("NOT_FOUND")
    return JSONResponse(contracts.bundle(name), media_type="application/schema+json")


@router.get("/v1/codes/{table}", tags=["contracts"])
def get_code_table(table: str, db: Session = DB,
                   p: Principal = Depends(require("code:read"))) -> dict:
    if table not in contracts.CODE_TABLES:
        raise ProblemError("NOT_FOUND")
    out = dict(contracts.code_table(table))
    if table == "event-categories":  # 공통 상위분류 + 자기 기관 하위코드 (요구사항 8.3)
        subs = db.execute(select(OrgEventCode).where(OrgEventCode.organization_id == p.organization_id)
                          .order_by(OrgEventCode.code)).scalars()
        out["items"] = [*out["items"], *({"code": s.code, "label": s.label, "organization_specific": True}
                                         for s in subs)]
    return out


class OrgEventSubcodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(max_length=80, pattern=r"^[A-Z][A-Z0-9_]{1,39}\.[a-z0-9_]{1,40}$")
    label: str = Field(min_length=1, max_length=120, pattern=r"^[^\x00-\x1f\x7f]*[^\s\x00-\x1f\x7f][^\x00-\x1f\x7f]*$")


@router.post(_ORG_EVENT_SUBCODES, status_code=201, tags=["contracts"])
def create_org_event_subcode(
    body: OrgEventSubcodeCreate, request: Request, db: Session = DB,
    c: Container = Depends(container), p: Principal = Depends(require("org_event_code:create")),
    idempotency_key: str | None = Header(default=None, min_length=8, max_length=128),
) -> JSONResponse:
    method_path = f"POST {_ORG_EVENT_SUBCODES}"

    def create() -> tuple[int, dict]:
        register_subcode(db, p, body.code, body.label, c.clock.now())
        return 201, {**body.model_dump(), "organization_specific": True}

    status, response, replayed = run_idempotent(
        db, c.clock, c.settings.idempotency_retention_s, p, method_path, idempotency_key, body.model_dump(), create,
    )
    if not replayed:
        c.audit.record("ORG_EVENT_CODE_CREATED", principal=p, organization_id=p.organization_id,
                       target_type="OrgEventCode", source_ip=client_ip(request), details={"code": body.code},
                       connection=db.connection())
    return JSONResponse(response, status_code=status, headers={"Idempotent-Replayed": "true"} if replayed else None)


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract: str
    document: dict


@router.post("/v1/contracts/validate", tags=["contracts"])
def validate_contract(body: ValidateRequest, _: Principal = Depends(require("contract:validate"))) -> dict:
    """저장하지 않고 검사한다. 운영 업로드와 같은 검증 함수를 쓴다 (요구사항 13.2)."""
    if body.contract not in ("upload-manifest", "observation-batch", "event"):
        msg = "upload-manifest, observation-batch, event 중 하나"
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/contract", "code": "enum", "message": msg}])
    problems, _version_rejected = contracts.validate_document(body.contract, body.document)
    return {"valid": not any(p.severity == "ERROR" for p in problems),
            "problems": [p.as_dict() for p in problems]}


# ---- 장비 정책 (에이전트가 주기적으로 받는다). 기본 정책·새 버전 저장은 services/device_policy (09번 초안 적용도)
@router.get("/v1/devices/{device_id}/policy", tags=["agents"])
def get_device_policy(device_id: str, request: Request, db: Session = DB,
                      c: Container = Depends(container), p: Principal = Depends(require("device_policy:read"))) -> dict:
    device = load_scoped(db, c, request, Device, device_id, p)
    if p.kind == "DEVICE" and device_id != p.device_id:  # 수집기는 자기 정책만
        raise ProblemError("NOT_FOUND")
    if device.kind != "COLLECTOR":
        raise ProblemError("NOT_FOUND", detail="정책은 수집기 단위로 배포합니다")
    policy, stored_version = device_policy.current_policy(db, device)
    problems = device_policy.policy_schema_problems(policy, stored=stored_version is not None)
    if problems:
        raise ProblemError("RULE_VIOLATION", detail="현재 계약을 충족하는 장비 정책을 배포해야 합니다",
                           unmet_conditions=[{"condition": f"장비 정책 계약 {p.pointer or '/'}", "actual": p.code}
                                             for p in problems[:20]])
    if stored_version is not None:  # 저장된 정책에도 현재 매핑 버전을 채운다
        policy["sources"] = [{**src, "observation_mapping_version": device_policy.mapping_version(db, src["device_id"])
                              or src.get("observation_mapping_version")} for src in policy["sources"]]
    return policy


# ---- 계보
_LINEAGE_SQL = {
    "ancestors": """
        WITH RECURSIVE up(asset_id, relation, tool_version, model_ref, created_at, depth) AS (
          SELECT parent_asset_id, relation, tool_version, model_ref, created_at, 1
            FROM asset_lineage WHERE child_asset_id = :id
          UNION
          SELECT l.parent_asset_id, l.relation, l.tool_version, l.model_ref, l.created_at, up.depth + 1
            FROM asset_lineage l JOIN up ON l.child_asset_id = up.asset_id WHERE up.depth < 20)
        SELECT DISTINCT asset_id, relation, tool_version, model_ref, created_at FROM up""",
    "descendants": """
        WITH RECURSIVE down(asset_id, relation, tool_version, model_ref, created_at, depth) AS (
          SELECT child_asset_id, relation, tool_version, model_ref, created_at, 1
            FROM asset_lineage WHERE parent_asset_id = :id
          UNION
          SELECT l.child_asset_id, l.relation, l.tool_version, l.model_ref, l.created_at, down.depth + 1
            FROM asset_lineage l JOIN down ON l.parent_asset_id = down.asset_id WHERE down.depth < 20)
        SELECT DISTINCT asset_id, relation, tool_version, model_ref, created_at FROM down""",
}


_QUALITY_STATUSES = ("PENDING", "PASSED", "WARNING", "FAILED")  # domain.md 8장 quality_status
_TOP_REASONS = 10


def _report_time(value: str | None, pointer: str) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_rfc3339(value)
    except ValueError:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": pointer, "code": "invalid",
                                                        "message": "RFC 3339 UTC 시각이어야 합니다"}]) from None


@router.get("/v1/quality-reports", tags=["status"])
def get_quality_report(request: Request, session_id: str | None = None, from_: str | None = Query(None, alias="from"),
                       to: str | None = None, db: Session = DB, c: Container = Depends(container),
                       p: Principal = Depends(require("quality_report:read"))) -> dict:
    """원본·삭제되지 않은 자산의 품질 상태 수와 상위 사유 (03번 요구사항 10.1, STD-03).

    기관 범위는 인증 정보로만 정한다(scope_filter). 기간은 ingested_at 기준 [from, to)."""
    start, end = _report_time(from_, "/query/from"), _report_time(to, "/query/to")
    if start is not None and end is not None and start >= end:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/query/to", "code": "range",
                                                        "message": "from < to 이어야 합니다"}])
    conds = [scope_filter(Asset, p), Asset.variant == "ORIGINAL"]
    if session_id is not None:
        load_scoped(db, c, request, TrialSession, session_id, p)  # 보이지 않는 회차는 404
        conds.append(Asset.session_id == session_id)
    if start is not None:
        conds.append(Asset.ingested_at >= start)
    if end is not None:
        conds.append(Asset.ingested_at < end)
    counts = dict.fromkeys(_QUALITY_STATUSES, 0)
    for status, n in db.execute(select(Asset.quality_status, func.count()).where(*conds)
                                .group_by(Asset.quality_status)).all():
        counts[status] = int(n)
    reason = func.jsonb_array_elements_text(Asset.quality_reasons).table_valued("value").render_derived(name="r")
    n_assets = func.count()
    rows = db.execute(select(reason.c.value, n_assets).select_from(Asset).join(reason, true()).where(*conds)
                      .group_by(reason.c.value).order_by(n_assets.desc(), reason.c.value).limit(_TOP_REASONS)).all()
    out: dict = {"counts_by_quality_status": counts,
                 "top_reasons": [{"reason": r, "count": int(n)} for r, n in rows]}
    for key, value in (("session_id", session_id), ("from", start), ("to", end)):
        if value is not None:
            out[key] = value if isinstance(value, str) else iso(value)
    return out


@router.get("/v1/assets/{asset_id}/lineage", tags=["assets"])
def get_lineage(asset_id: str, request: Request, db: Session = DB, c: Container = Depends(container),
                p: Principal = Depends(require("asset:read"))) -> dict:
    load_scoped(db, c, request, Asset, asset_id, p)
    out: dict = {"asset_id": asset_id}
    for key, sql in _LINEAGE_SQL.items():
        rows = db.execute(text(sql), {"id": asset_id}).mappings().all()
        ids = [r["asset_id"] for r in rows]
        # 권한 범위 밖 자산은 계보에서도 보이지 않는다 (요구사항 11.2)
        visible = set(db.execute(select(Asset.asset_id).where(Asset.asset_id.in_(ids or [""]),
                                                              scope_filter(Asset, p))).scalars())
        edges = []
        for r in sorted(rows, key=lambda r: r["asset_id"]):
            if r["asset_id"] in visible:
                e = {"asset_id": r["asset_id"], "relation": r["relation"], "created_at": iso(r["created_at"])}
                if r["tool_version"]:
                    e["tool_version"] = r["tool_version"]
                if r["model_ref"]:
                    e["model_ref"] = r["model_ref"]
                edges.append(e)
        out[key] = edges
    return out
