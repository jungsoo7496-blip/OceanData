"""스키마·코드표·사전 검증·장비 정책·계보 (03번 요구사항 1.3, 2, 8, 11, 13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.deps import container, get_db, load_scoped, require
from app.container import Container
from app.db.models import Asset, Device, DevicePolicy, OrgEventCode
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import contracts
from app.services.tenant import scope_filter
from app.timefmt import iso

router = APIRouter()


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
def get_code_table(table: str, db: Session = Depends(get_db),
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


# ---- 장비 정책 (에이전트가 주기적으로 받는다)
def default_policy(db: Session, collector: Device) -> dict:
    sources = db.execute(select(Device).where(Device.collector_device_id == collector.device_id,
                                              Device.organization_id == collector.organization_id,
                                              Device.status != "REVOKED").order_by(Device.device_id)).scalars()
    return {
        "policy_version": "pv-0",
        "collector_device_id": collector.device_id,
        "sources": [{"device_id": d.device_id, "project_id": d.project_id, "collection_mode": d.collection_mode,
                     "local_copy_policy": d.local_copy_policy, "local_retention_days": d.local_retention_days,
                     "allowed_data_roles": ["CORE"], "expected_interval_s": d.expected_interval_s,
                     "source_timezone": d.source_timezone, "observation_mapping_version": None}
                    for d in sources if d.project_id],
        "watch_rules": [],
        "timezone": "UTC",
        "heartbeat_interval_s": 60,
        "size_stable_s": 30,
    }


@router.get("/v1/devices/{device_id}/policy", tags=["agents"])
def get_device_policy(device_id: str, request: Request, db: Session = Depends(get_db),
                      c: Container = Depends(container), p: Principal = Depends(require("device_policy:read"))) -> dict:
    if p.kind == "DEVICE" and device_id != p.device_id:  # 수집기는 자기 정책만
        raise ProblemError("NOT_FOUND")
    device = load_scoped(db, c, request, Device, device_id, p)
    if device.kind != "COLLECTOR":
        raise ProblemError("NOT_FOUND", detail="정책은 수집기 단위로 배포합니다")
    stored = db.execute(select(DevicePolicy).where(DevicePolicy.device_id == device.device_id)
                        .order_by(DevicePolicy.version.desc()).limit(1)).scalar_one_or_none()
    return stored.policy if stored else default_policy(db, device)


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


@router.get("/v1/assets/{asset_id}/lineage", tags=["assets"])
def get_lineage(asset_id: str, request: Request, db: Session = Depends(get_db), c: Container = Depends(container),
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
