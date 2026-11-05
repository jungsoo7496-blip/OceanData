"""수집 상태·장애·감시 규칙·하드웨어 신호·정기 점검 (11번)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.api.deps import DB, client_ip, container, page_params, require
from app.api.pagination import paginate
from app.container import Container
from app.db.models import Device, HardwareSignalState, Incident, Inspection, WatchRule
from app.domain import inspections as tpl
from app.domain.ids import new_id, new_ulid
from app.domain.normalize import parse_rfc3339
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import inspections, monitoring
from app.services.tenant import scope_filter
from app.timefmt import iso

router = APIRouter(tags=["status"])
Severity = Literal["INFO", "WARNING", "CRITICAL"]


def _operator(p: Principal) -> bool:
    return not p.is_machine and p.has_role("PLATFORM_OPERATOR")


def _bad(pointer: str, message: str, code: str = "invalid") -> ProblemError:
    return ProblemError("VALIDATION_ERROR", errors=[{"pointer": pointer, "code": code, "message": message}])


def _time(value: str | None, pointer: str) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_rfc3339(value)
    except ValueError:
        raise _bad(pointer, "RFC 3339 UTC 시각이어야 합니다") from None


# ------------------------------------------------------------------ 수집 상태 (요구사항 1, 5)


@router.get("/v1/ingest-status")
def get_ingest_status(db: Session = DB, c: Container = Depends(container),
                      p: Principal = Depends(require("ingest_status:read"))) -> dict:
    """기관 범위는 인증 정보로만 정한다 (security.md). 운영자는 전체를 보고 화면에서 기관별로 거른다."""
    now = c.clock.now()
    stmt = select(Device).where(scope_filter(Device, p), Device.status != "REVOKED")
    org: str | None = None if _operator(p) else p.organization_id
    devices = list(db.execute(stmt.order_by(Device.organization_id, Device.device_id)).scalars())
    inc = select(Incident).where(Incident.status != "RESOLVED")
    if org:
        inc = inc.where(Incident.organization_id == org)
    incidents = db.execute(inc.order_by(Incident.opened_at.desc()).limit(100)).scalars()
    return {"generated_at": iso(now), "devices": monitoring.device_health(db, devices, now),
            "open_incidents": [monitoring.incident_dict(i) for i in incidents],
            "top_failure_reasons": monitoring.top_failure_reasons(db, now, org)}


# ------------------------------------------------------------------ 장애 기록 (요구사항 4)


@router.get("/v1/incidents")
def list_incidents(status: Literal["OPEN", "ACKNOWLEDGED", "RESOLVED"] | None = None, device_id: str | None = None,
                   from_: str | None = Query(None, alias="from"),
                   to: str | None = None, page: dict = Depends(page_params), db: Session = DB,
                   p: Principal = Depends(require("incident:read"))) -> dict:
    stmt = select(Incident)
    if not _operator(p):
        stmt = stmt.where(Incident.organization_id == p.organization_id)
    if status:
        stmt = stmt.where(Incident.status == status)
    if device_id:
        stmt = stmt.where(Incident.device_id == device_id)
    start, end = _time(from_, "/query/from"), _time(to, "/query/to")
    if start:
        stmt = stmt.where(Incident.opened_at >= start)
    if end:
        stmt = stmt.where(Incident.opened_at < end)
    rows, nxt = paginate(db, stmt, Incident.incident_id, page["limit"], page["cursor"])
    return {"items": [monitoring.incident_dict(i) for i in rows], "next_cursor": nxt}


class IncidentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ACKNOWLEDGED", "RESOLVED"] | None = None
    assignee: str | None = Field(None, max_length=120)
    memo: str | None = Field(None, max_length=2000)


@router.patch("/v1/incidents/{incident_id}")
def update_incident(incident_id: str, body: IncidentPatch, request: Request, db: Session = DB,
                    c: Container = Depends(container), p: Principal = Depends(require("incident:update"))) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise _bad("", "바꿀 값이 없습니다", "minProperties")
    inc = db.get(Incident, incident_id, with_for_update=True)
    if inc is None or (not _operator(p) and inc.organization_id != p.organization_id):
        if inc is not None and inc.organization_id:
            c.audit.record("CROSS_ORG_ACCESS_ATTEMPT", result="DENIED", principal=p,
                           organization_id=inc.organization_id, target_type="Incident", target_id=incident_id,
                           source_ip=client_ip(request), details={"path": request.url.path})
        raise ProblemError("NOT_FOUND")
    now = c.clock.now()
    new = fields.get("status")
    if inc.status == "RESOLVED" and new is not None:
        raise ProblemError("INVALID_STATE_TRANSITION", detail="해결된 장애는 다시 열거나 확인할 수 없습니다")
    if new == "ACKNOWLEDGED" and inc.status == "OPEN":
        inc.status, inc.acknowledged_at = "ACKNOWLEDGED", now
    elif new == "RESOLVED":
        inc.status, inc.resolved_at, inc.resolved_by = "RESOLVED", now, p.principal_id
        inc.acknowledged_at = inc.acknowledged_at or now
    if "assignee" in fields:
        inc.assignee = body.assignee
    if "memo" in fields:
        inc.memo = body.memo
    c.audit.record("INCIDENT_UPDATED", principal=p, organization_id=inc.organization_id, target_type="Incident",
                   target_id=inc.incident_id, source_ip=client_ip(request),
                   details={"status": inc.status, "fields": sorted(fields)})
    return monitoring.incident_dict(inc)


# ------------------------------------------------------------------ 감시 규칙 (요구사항 2)


@router.get("/v1/watch-rules")
def list_watch_rules(db: Session = DB, _: Principal = Depends(require("watch_rule:read"))) -> dict:
    rows = db.execute(select(WatchRule).order_by(WatchRule.rule_code)).scalars()
    return {"items": [monitoring.rule_dict(r) for r in rows]}


class RulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    severity: Severity | None = None
    params: dict | None = None
    notify_org: bool | None = None
    notify_operator: bool | None = None
    channels: list[Literal["EMAIL", "WEBHOOK"]] | None = None
    realert_interval_s: int | None = Field(None, ge=60)


@router.patch("/v1/watch-rules/{rule_code}")
def update_watch_rule(rule_code: str, body: RulePatch, request: Request, db: Session = DB,
                      c: Container = Depends(container), p: Principal = Depends(require("watch_rule:update"))) -> dict:
    rule = db.get(WatchRule, rule_code, with_for_update=True)
    if rule is None:
        raise ProblemError("NOT_FOUND")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise _bad("", "바꿀 값이 없습니다", "minProperties")
    if "params" in fields:
        errors = monitoring.validate_params(rule_code, fields["params"] or {})
        if errors:
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/params", "code": "invalid", "message": e}
                                                           for e in errors])
        fields["params"] = {**rule.params, **(fields["params"] or {})}
    if "channels" in fields:
        fields["channels"] = sorted(set(fields["channels"] or []))
    before = monitoring.rule_dict(rule)
    for k, v in fields.items():
        if v is not None:
            setattr(rule, k, v)
    rule.updated_at, rule.updated_by = c.clock.now(), p.principal_id
    c.audit.record("WATCH_RULE_UPDATED", principal=p, target_type="WatchRule", target_id=rule_code,
                   source_ip=client_ip(request),
                   details={"changed": {k: [before.get(k), getattr(rule, k)] for k in fields}})
    return monitoring.rule_dict(rule)


# ------------------------------------------------------------------ 하드웨어 신호 (요구사항 7)


class Signal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(min_length=1, max_length=120)
    kind: Literal["DISK", "SMART", "TEMPERATURE", "POWER", "UPS", "OTHER"]
    component: str | None = Field(None, max_length=120)
    value: float | None = None
    unit: str | None = Field(None, max_length=32)
    status: Literal["OK", "WARNING", "CRITICAL"]
    detail: str | None = Field(None, max_length=500)
    observed_at: str


class SignalBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signals: list[Signal] = Field(min_length=1, max_length=500)


def store_signals(db: Session, signals: list[Signal], now: datetime) -> int:
    for i, s in enumerate(signals):
        at = _time(s.observed_at, f"/signals/{i}/observed_at")
        if at is None or at > now + timedelta(minutes=10):
            raise _bad(f"/signals/{i}/observed_at", "미래 시각입니다")
        values = {"value": s.value, "unit": s.unit, "status": s.status, "detail": s.detail, "observed_at": at,
                  "received_at": now}
        stmt = insert(HardwareSignalState).values(host=s.host, kind=s.kind, component=s.component or "", **values)
        db.execute(stmt.on_conflict_do_update(index_elements=["host", "kind", "component"], set_=values,
                                              where=HardwareSignalState.observed_at <= at))
    return len(signals)


@router.post("/v1/hardware-signals", status_code=202)
def post_hardware_signals(body: SignalBatch, db: Session = DB, c: Container = Depends(container),
                          _: Principal = Depends(require("hardware_signal:write"))) -> dict:
    return {"accepted": store_signals(db, body.signals, c.clock.now())}


# ------------------------------------------------------------------ 정기 점검 (요구사항 6)


@router.get("/v1/inspections/templates")
def list_templates(_: Principal = Depends(require("inspection:read"))) -> dict:
    return {"items": [{"cycle": t.cycle, "item_code": t.item_code, "title": t.title, "auto": t.auto}
                      for t in tpl.TEMPLATES]}


Cycle = Literal["DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY"]


@router.get("/v1/inspections")
def list_inspections(cycle: Cycle | None = None, page: dict = Depends(page_params), db: Session = DB,
                     _: Principal = Depends(require("inspection:read"))) -> dict:
    stmt = select(Inspection)
    if cycle:
        stmt = stmt.where(Inspection.cycle == cycle)
    rows, nxt = paginate(db, stmt, Inspection.inspection_id, page["limit"], page["cursor"])
    return {"items": [inspections.view(i) for i in rows], "next_cursor": nxt}


class InspectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cycle: Cycle
    period_key: str = Field(max_length=12)
    assignee: str | None = Field(None, max_length=120)


@router.post("/v1/inspections", status_code=201)
def create_inspection(body: InspectionCreate, request: Request, db: Session = DB,
                      c: Container = Depends(container), p: Principal = Depends(require("inspection:write"))) -> dict:
    try:
        tpl.period_last_day(body.cycle, body.period_key)
    except ValueError as exc:
        raise _bad("/period_key", str(exc)) from None
    if db.execute(select(Inspection.inspection_id).where(Inspection.cycle == body.cycle,
                                                         Inspection.period_key == body.period_key)).scalar():
        raise ProblemError("CONFLICT", detail="이 기간의 점검 기록이 이미 있습니다")
    row = inspections.create(c, db, cycle=body.cycle, period_key=body.period_key, assignee=body.assignee,
                             created_by=p.principal_id, inspection_id=new_id("inspection"))
    db.flush()
    c.audit.record("INSPECTION_CREATED", principal=p, target_type="Inspection", target_id=row.inspection_id,
                   source_ip=client_ip(request), details={"cycle": body.cycle, "period_key": body.period_key})
    return inspections.view(row)


def _inspection(db: Session, inspection_id: str, lock: bool = False) -> Inspection:
    row = db.get(Inspection, inspection_id, with_for_update=lock)
    if row is None:
        raise ProblemError("NOT_FOUND")
    return row


@router.get("/v1/inspections/{inspection_id}")
def get_inspection(inspection_id: str, db: Session = DB,
                   _: Principal = Depends(require("inspection:read"))) -> dict:
    return inspections.view(_inspection(db, inspection_id))


class ItemResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_code: str
    result: Literal["PASS", "FAIL", "NOT_APPLICABLE", "AUTO_UNAVAILABLE"]
    note: str | None = Field(None, max_length=2000)


class InspectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ItemResult] | None = None
    note: str | None = Field(None, max_length=4000)
    complete: bool | None = None


@router.patch("/v1/inspections/{inspection_id}")
def update_inspection(inspection_id: str, body: InspectionPatch, request: Request, db: Session = DB,
                      c: Container = Depends(container), p: Principal = Depends(require("inspection:write"))) -> dict:
    row = _inspection(db, inspection_id, lock=True)
    if row.status == "COMPLETED":
        raise ProblemError("CONFLICT", detail="완료된 점검 기록은 바꿀 수 없습니다")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise _bad("", "바꿀 값이 없습니다", "minProperties")
    items = [dict(x) for x in row.items]
    index = {x["item_code"]: x for x in items}
    for n, it in enumerate(body.items or []):
        target = index.get(it.item_code)
        if target is None:
            raise _bad(f"/items/{n}/item_code", f"이 점검에 없는 항목입니다: {it.item_code}")
        if it.result == "AUTO_UNAVAILABLE":
            raise _bad(f"/items/{n}/result", "AUTO_UNAVAILABLE은 서버만 기록합니다")
        target["result"], target["note"] = it.result, it.note
    if "note" in fields:
        row.note = body.note
    if body.complete:
        missing = [x["item_code"] for x in items if x["result"] in (None, "AUTO_UNAVAILABLE")]
        if missing:
            raise ProblemError("VALIDATION_ERROR", detail="결과가 없는 항목이 있습니다",
                               errors=[{"pointer": "/items", "code": "required", "message": m} for m in missing])
        row.status, row.completed_at, row.completed_by = "COMPLETED", c.clock.now(), p.principal_id
    row.items = items
    c.audit.record("INSPECTION_UPDATED", principal=p, target_type="Inspection", target_id=row.inspection_id,
                   source_ip=client_ip(request), details={"status": row.status})
    return inspections.view(row)


class EvidenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=200)
    byte_size: int = Field(ge=1, le=100 * 1024 * 1024)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_code: str | None = None


@router.post("/v1/inspections/{inspection_id}/evidence", status_code=201)
def add_evidence(inspection_id: str, body: EvidenceCreate, request: Request, db: Session = DB,
                 c: Container = Depends(container), p: Principal = Depends(require("inspection:write"))) -> dict:
    row = _inspection(db, inspection_id, lock=True)
    if row.status == "COMPLETED":
        raise ProblemError("CONFLICT", detail="완료된 점검 기록에는 증빙을 더할 수 없습니다")
    if body.item_code and body.item_code not in {x["item_code"] for x in row.items}:
        raise _bad("/item_code", "이 점검에 없는 항목입니다")
    evidence_id = new_ulid()
    key = f"ops/inspections/{row.inspection_id}/{evidence_id}"
    ttl = c.settings.presign_upload_ttl_s
    url = c.storage.presign_put(c.settings.bucket_evidence, key, ttl)
    row.evidence = [*row.evidence, {"evidence_id": evidence_id, "item_code": body.item_code,
                                    "filename": body.filename, "byte_size": body.byte_size,
                                    "checksum_sha256": body.checksum_sha256, "object_key": key,
                                    "added_by": p.principal_id, "added_at": iso(c.clock.now())}]
    c.audit.record("INSPECTION_EVIDENCE_ADDED", principal=p, target_type="Inspection", target_id=row.inspection_id,
                   source_ip=client_ip(request), details={"evidence_id": evidence_id, "byte_size": body.byte_size})
    return {"evidence_id": evidence_id, "upload_url": url,
            "expires_at": iso(c.clock.now() + timedelta(seconds=ttl))}
