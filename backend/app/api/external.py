"""공공 Open API 소스 등록·조정·보충 수집 (12번 요구사항 1, 2). 운영자 전용."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import client_ip, container, get_db, require
from app.container import Container
from app.db.models import ExternalSource
from app.domain.normalize import parse_rfc3339
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services.observations import metrics_table
from app.timefmt import iso

router = APIRouter(tags=["status"])
SOURCE_ID = r"^[a-z][a-z0-9_-]{1,59}$"


class Station(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(max_length=60)
    name: str = Field(max_length=120)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class MetricMap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    unit: str


class FieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    station_path: str
    time_path: str
    metrics: dict[str, MetricMap]


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(pattern=SOURCE_ID)
    name: str = Field(min_length=1, max_length=200)
    adapter_kind: Literal["MOCK"]  # 명세가 확인된 실제 어댑터만 추가한다 (요구사항 1.2)
    endpoint: str | None = Field(None, max_length=400)
    secret_ref: str | None = Field(None, max_length=120)
    interval_s: int = Field(ge=60)
    max_window_s: int = Field(86400, ge=600)
    rate_limit_per_min: int | None = Field(None, ge=1)
    supports_backfill: bool = True
    stations: list[Station] = Field(min_length=1, max_length=500)
    field_mapping: FieldMapping
    attribution: str = Field(min_length=1, max_length=500)
    redistribution_allowed: bool
    options: dict[str, Any] | None = None


def view(s: ExternalSource) -> dict[str, Any]:
    return {"source_id": s.source_id, "name": s.name, "adapter_kind": s.adapter_kind, "spec_status": s.spec_status,
            "endpoint": s.endpoint, "secret_ref": s.secret_ref, "interval_s": s.interval_s,
            "max_window_s": s.max_window_s, "rate_limit_per_min": s.rate_limit_per_min,
            "supports_backfill": s.supports_backfill,
            "stations": [{k: st[k] for k in ("code", "name", "lat", "lon")} for st in s.stations],
            "field_mapping": s.field_mapping, "attribution": s.attribution,
            "redistribution_allowed": s.redistribution_allowed, "options": s.options or {}, "enabled": s.enabled,
            "collected_until": iso(s.collected_until), "last_success_at": iso(s.last_success_at),
            "consecutive_failures": s.consecutive_failures, "last_error": s.last_error}


@router.get("/v1/external-sources")
def list_sources(db: Session = Depends(get_db), _: Principal = Depends(require("external_source:read"))) -> dict:
    return {"items": [view(s) for s in db.execute(select(ExternalSource).order_by(ExternalSource.source_id)).scalars()]}


@router.post("/v1/external-sources", status_code=201)
def create_source(body: SourceCreate, request: Request, db: Session = Depends(get_db),
                  c: Container = Depends(container), p: Principal = Depends(require("external_source:write"))) -> dict:
    if db.get(ExternalSource, body.source_id) is not None:
        raise ProblemError("CONFLICT", detail="같은 source_id가 있습니다")
    known = set(metrics_table())
    bad = [m for m in body.field_mapping.metrics if m not in known]
    if bad:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/field_mapping/metrics", "code": "unknown_metric",
                                                        "message": f"관측 항목 코드표에 없습니다: {', '.join(bad)}"}])
    s = ExternalSource(**body.model_dump(exclude={"stations", "field_mapping"}),
                       stations=[x.model_dump() for x in body.stations], field_mapping=body.field_mapping.model_dump(),
                       spec_status="NEEDS_CONFIRMATION",  # 모의 어댑터 = 실제 명세 확인 전
                       enabled=True, consecutive_failures=0, created_by=p.principal_id, created_at=c.clock.now())
    db.add(s)
    db.flush()
    c.audit.record("EXTERNAL_SOURCE_CREATED", principal=p, target_type="ExternalSource", target_id=s.source_id,
                   source_ip=client_ip(request), details={"adapter_kind": s.adapter_kind})
    return view(s)


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    interval_s: int | None = Field(None, ge=60)
    rate_limit_per_min: int | None = Field(None, ge=1)
    attribution: str | None = Field(None, min_length=1, max_length=500)
    redistribution_allowed: bool | None = None


def _source(db: Session, source_id: str) -> ExternalSource:
    s = db.get(ExternalSource, source_id, with_for_update=True)
    if s is None:
        raise ProblemError("NOT_FOUND")
    return s


@router.patch("/v1/external-sources/{source_id}")
def update_source(source_id: str, body: SourcePatch, request: Request, db: Session = Depends(get_db),
                  c: Container = Depends(container), p: Principal = Depends(require("external_source:write"))) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "", "code": "minProperties",
                                                        "message": "바꿀 값이 없습니다"}])
    s = _source(db, source_id)
    for k, v in fields.items():
        setattr(s, k, v)
    c.audit.record("EXTERNAL_SOURCE_UPDATED", principal=p, target_type="ExternalSource", target_id=s.source_id,
                   source_ip=client_ip(request), details={"fields": sorted(fields)})
    return view(s)


class Backfill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_: str = Field(alias="from")
    to: str


@router.post("/v1/external-sources/{source_id}/backfill", status_code=202)
def backfill(source_id: str, body: Backfill, request: Request, db: Session = Depends(get_db),
             c: Container = Depends(container), p: Principal = Depends(require("external_source:write"))) -> dict:
    s = _source(db, source_id)
    if not s.supports_backfill:
        raise ProblemError("CONFLICT", detail="이 소스는 과거 조회를 지원하지 않습니다")
    try:
        start, end = parse_rfc3339(body.from_), parse_rfc3339(body.to)
    except ValueError:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/from", "code": "format",
                                                        "message": "RFC 3339 UTC 시각"}]) from None
    if end <= start or end > c.clock.now() or end - start > timedelta(days=31):
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/to", "code": "range",
                                                        "message": "과거 31일 이내 구간이어야 합니다"}])
    jobs, cur = 0, start
    while cur < end:  # 한 작업은 max_window_s까지
        nxt = min(end, cur + timedelta(seconds=s.max_window_s))
        c.queue.enqueue("collect_source", {"source_id": s.source_id, "since": iso(cur), "until": iso(nxt)},
                        dedupe_key=f"collect:{s.source_id}:{int(cur.timestamp())}:{int(nxt.timestamp())}")
        jobs += 1
        cur = nxt
    c.audit.record("EXTERNAL_SOURCE_BACKFILL", principal=p, target_type="ExternalSource", target_id=s.source_id,
                   source_ip=client_ip(request), details={"from": body.from_, "to": body.to, "jobs": jobs})
    return {"jobs": jobs}
