"""업로드 API (04번 요구사항 1~4, 7.2). 바이너리는 사전 서명 URL로 스토리지에 직접 보낸다."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import DB, container, require
from app.container import Container
from app.domain.principal import Principal
from app.services.idempotency import run_idempotent
from app.services.ingest import IngestService

router = APIRouter(tags=["uploads"])


@router.post("/v1/uploads")
def create_upload(manifest: dict = Body(...), db: Session = DB, c: Container = Depends(container),
                  p: Principal = Depends(require("upload:write")),
                  idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    svc = IngestService(c, db)
    status, body, replayed = run_idempotent(db, c.clock, c.settings.idempotency_retention_s, p, "POST /v1/uploads",
                                            idempotency_key, manifest, lambda: svc.create_upload(p, manifest))
    return JSONResponse(body, status_code=status, headers={"Idempotent-Replayed": "true"} if replayed else None)


@router.get("/v1/uploads/{upload_id}")
def get_upload(upload_id: str, db: Session = DB, c: Container = Depends(container),
               p: Principal = Depends(require("upload:read"))) -> dict:
    svc = IngestService(c, db)
    return svc.upload_status(svc.load_upload(p, upload_id))


@router.delete("/v1/uploads/{upload_id}", status_code=204)
def abort_upload(upload_id: str, db: Session = DB, c: Container = Depends(container),
                 p: Principal = Depends(require("upload:write"))) -> Response:
    svc = IngestService(c, db)
    svc.abort(svc.load_upload(p, upload_id))
    return Response(status_code=204)


@router.get("/v1/uploads/{upload_id}/parts")
def list_parts(upload_id: str, db: Session = DB, c: Container = Depends(container),
               p: Principal = Depends(require("upload:write"))) -> dict:
    svc = IngestService(c, db)
    return svc.list_parts(svc.load_upload(p, upload_id))


class PartRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_number: int = Field(ge=1, le=10000)
    etag: str = Field(min_length=1, max_length=200)


class CompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parts: list[PartRef] = Field(min_length=1, max_length=10000)


@router.post("/v1/uploads/{upload_id}/complete", status_code=202)
def complete_upload(upload_id: str, body: CompleteRequest, db: Session = DB,
                    c: Container = Depends(container), p: Principal = Depends(require("upload:write")),
                    idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    svc = IngestService(c, db)
    up = svc.load_upload(p, upload_id)
    payload = body.model_dump()
    status, resp, replayed = run_idempotent(
        db, c.clock, c.settings.idempotency_retention_s, p, f"POST /v1/uploads/{upload_id}/complete",
        idempotency_key, payload, lambda: (202, svc.complete(p, up, payload["parts"])))
    return JSONResponse(resp, status_code=status, headers={"Idempotent-Replayed": "true"} if replayed else None)
