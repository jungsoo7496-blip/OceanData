"""Model_Registry 등록·조회 (05번 요구사항 8.3·8.4, 14번 ADR-0020). 로드 판단은 adapters/onnx_inference.check_model."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.onnx_inference import ALLOWED_MODEL_LICENSES
from app.api.deps import client_ip, container, get_db, require
from app.container import Container
from app.db.models import ModelRegistryEntry
from app.domain.principal import Principal
from app.errors import ProblemError
from app.timefmt import iso

router = APIRouter(tags=["system"])


class ModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,119}$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,59}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license: str = Field(min_length=1, max_length=60)
    weights_uri: str = Field(min_length=1, max_length=400)
    training_data_terms: str = Field(min_length=2, max_length=4000)


def view(m: ModelRegistryEntry) -> dict:
    return {"name": m.name, "version": m.version, "sha256": m.sha256, "license": m.license,
            "weights_uri": m.weights_uri, "training_data_terms": m.training_data_terms, "approved_by": m.approved_by,
            "loadable": bool(m.license and m.license in ALLOWED_MODEL_LICENSES), "created_at": iso(m.created_at)}


@router.get("/v1/models")
def list_models(db: Session = Depends(get_db), _: Principal = Depends(require("model:read"))) -> dict:
    rows = db.execute(select(ModelRegistryEntry).order_by(ModelRegistryEntry.name, ModelRegistryEntry.version))
    return {"items": [view(m) for m in rows.scalars()], "allowed_licenses": sorted(ALLOWED_MODEL_LICENSES)}


@router.post("/v1/models", status_code=201)
def register_model(body: ModelCreate, request: Request, db: Session = Depends(get_db),
                   c: Container = Depends(container), p: Principal = Depends(require("model:write"))) -> dict:
    if body.license not in ALLOWED_MODEL_LICENSES:  # 로드할 수 없는 모델은 등록하지 않는다 (요구사항 8.4)
        raise ProblemError("RULE_VIOLATION",
                           detail="허용 목록 밖의 모델 라이선스입니다. 예외는 ADR·승인 후 허용 목록에 추가합니다",
                           unmet_conditions=[{"condition": f"license ∈ {sorted(ALLOWED_MODEL_LICENSES)}",
                                              "actual": body.license}])
    if db.get(ModelRegistryEntry, (body.name, body.version)) is not None:
        raise ProblemError("CONFLICT", detail="이미 등록된 이름·버전입니다. 새 버전으로 등록하세요")
    m = ModelRegistryEntry(name=body.name, version=body.version, sha256=body.sha256, license=body.license,
                           weights_uri=body.weights_uri, training_data_terms=body.training_data_terms,
                           approved_by=p.principal_id, created_at=c.clock.now())
    db.add(m)
    db.flush()
    c.audit.record("MODEL_REGISTERED", principal=p, target_type="Model", target_id=f"{m.name}@{m.version}",
                   source_ip=client_ip(request), details={"license": m.license, "sha256": m.sha256})
    return view(m)
