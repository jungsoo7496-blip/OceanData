"""실증사업·실증회차·장비 (contracts: context 태그). 모든 조회는 Tenant_Guard를 거친다."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import DB, client_ip, container, load_scoped, page_params, require
from app.api.pagination import paginate
from app.container import Container
from app.db.models import Device, Project, SessionDevice, TrialSession
from app.domain.ids import new_id
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services.idempotency import run_idempotent
from app.services.tenant import scoped_select
from app.timefmt import iso

router = APIRouter(tags=["context"])
ID_PATTERN = r"^[a-z]{3}_[0-9A-HJKMNP-TV-Z]{26}$"


def project_view(p: Project) -> dict:
    return {"project_id": p.project_id, "name": p.name, "status": p.status}


def device_view(d: Device) -> dict:
    out = {"device_id": d.device_id, "name": d.name, "kind": d.kind, "device_type": d.device_type,
           "status": d.status, "collection_mode": d.collection_mode, "local_copy_policy": d.local_copy_policy,
           "local_retention_days": d.local_retention_days, "expected_interval_s": d.expected_interval_s,
           "center_copy_may_be_only_copy": d.local_copy_policy in ("ROTATING", "NONE", "UNKNOWN"),
           "last_heartbeat_at": iso(d.last_heartbeat_at)}
    if d.collector_device_id:
        out["collector_device_id"] = d.collector_device_id
    if d.project_id:
        out["project_id"] = d.project_id
    return out


def session_view(s: TrialSession, device_ids: list[str]) -> dict:
    return {"session_id": s.session_id, "project_id": s.project_id, "label": s.label, "title": s.title,
            "starts_at": iso(s.starts_at), "ends_at": iso(s.ends_at), "site": s.site, "note": s.note,
            "device_ids": device_ids, "created_at": iso(s.created_at)}


def _session_devices(db: Session, session_ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {sid: [] for sid in session_ids}
    if session_ids:
        for sid, did in db.execute(select(SessionDevice.session_id, SessionDevice.device_id)
                                   .where(SessionDevice.session_id.in_(session_ids))
                                   .order_by(SessionDevice.device_id)).all():
            out[sid].append(did)
    return out


@router.get("/v1/projects")
def list_projects(db: Session = DB, p: Principal = Depends(require("project:read")),
                  page: dict = Depends(page_params)) -> dict:
    if p.has_role("PROGRAM_VIEWER") and not p.has_role("PLATFORM_OPERATOR"):
        stmt = select(Project).where(Project.project_id.in_(sorted(p.assigned_project_ids) or [""]))
    else:
        stmt = scoped_select(Project, p)
    rows, nxt = paginate(db, stmt, Project.project_id, page["limit"], page["cursor"])
    return {"items": [project_view(r) for r in rows], "next_cursor": nxt}


# ---- 장비
class DeviceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["SOURCE", "COLLECTOR"]
    device_type: Literal["CAMERA", "NVR", "SENSOR", "EXTERNAL_STATION", "AGENT_PC", "EDGE_BOX", "SERVER", "OTHER"]
    collector_device_id: str | None = Field(default=None, pattern=ID_PATTERN)
    project_id: str | None = Field(default=None, pattern=ID_PATTERN)
    local_copy_policy: Literal["RETAINED", "ROTATING", "NONE", "UNKNOWN"]
    # 장비 기록은 기본 정책의 sources 항목이므로 같은 기술 상한을 쓴다 (ADR-0040).
    local_retention_days: int | None = Field(default=None, ge=0, le=24855)
    expected_interval_s: int | None = Field(default=None, ge=1, le=2147483647)


@router.get("/v1/devices")
def list_devices(db: Session = DB, p: Principal = Depends(require("device:read")),
                 page: dict = Depends(page_params)) -> dict:
    rows, nxt = paginate(db, scoped_select(Device, p), Device.device_id, page["limit"], page["cursor"])
    return {"items": [device_view(r) for r in rows], "next_cursor": nxt}


@router.post("/v1/devices", status_code=201)
def create_device(body: DeviceCreate, request: Request, db: Session = DB,
                  c: Container = Depends(container), p: Principal = Depends(require("device:write")),
                  idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    if p.has_role("PLATFORM_OPERATOR") and not p.has_role("ORG_ADMIN"):
        # 운영자는 기관을 본문으로 지정할 수 없다 (organization_id는 인증 정보로만) → 대리 등록은 09번 운영 콘솔에서
        c.audit.record("PERMISSION_DENIED", result="DENIED", principal=p, organization_id=p.organization_id,
                       source_ip=client_ip(request), details={"action": "device:write"})
        raise ProblemError("FORBIDDEN", detail="장비 등록은 해당 기관 ORG_ADMIN이 합니다")

    def create() -> tuple[int, dict]:
        if body.collector_device_id:
            collector = load_scoped(db, c, request, Device, body.collector_device_id, p)
            if collector.kind != "COLLECTOR":
                raise ProblemError("RULE_VIOLATION", detail="collector_device_id는 수집기여야 합니다",
                                   unmet_conditions=[{"condition": "collector.kind=COLLECTOR",
                                                      "actual": collector.kind}])
        if body.project_id:
            load_scoped(db, c, request, Project, body.project_id, p)
        d = Device(device_id=new_id("device"), organization_id=p.organization_id, created_at=c.clock.now(),
                   status="PENDING_ENROLLMENT" if body.kind == "COLLECTOR" else "ACTIVE",
                   **body.model_dump())
        db.add(d)
        db.flush()
        c.audit.record("DEVICE_REGISTERED", principal=p, organization_id=p.organization_id, target_type="Device",
                       target_id=d.device_id, source_ip=client_ip(request))
        return 201, device_view(d)

    status, resp, replayed = run_idempotent(db, c.clock, c.settings.idempotency_retention_s, p,
                                            "POST /v1/devices", idempotency_key, body.model_dump(mode="json"),
                                            create)
    return JSONResponse(resp, status_code=status, headers={"Idempotent-Replayed": "true"} if replayed else None)


@router.get("/v1/devices/{device_id}")
def get_device(device_id: str, request: Request, db: Session = DB, c: Container = Depends(container),
               p: Principal = Depends(require("device:read"))) -> dict:
    return device_view(load_scoped(db, c, request, Device, device_id, p))


# ---- 실증회차
class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(pattern=ID_PATTERN)
    label: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    ends_at: datetime | None = None
    site: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    device_ids: list[str] = Field(default_factory=list, max_length=500)


def _require_tz(field: str, value: datetime | None) -> None:
    if value is not None and value.tzinfo is None:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": f"/{field}", "code": "timezone_required",
                                                        "message": "시각은 UTC(Z) 또는 오프셋을 포함해야 합니다"}])


@router.get("/v1/sessions")
def list_sessions(project_id: str | None = None, label: str | None = None, db: Session = DB,
                  p: Principal = Depends(require("session:read")), page: dict = Depends(page_params)) -> dict:
    stmt = scoped_select(TrialSession, p)
    if project_id:
        stmt = stmt.where(TrialSession.project_id == project_id)
    if label:
        stmt = stmt.where(TrialSession.label == label)
    rows, nxt = paginate(db, stmt, TrialSession.session_id, page["limit"], page["cursor"])
    devs = _session_devices(db, [r.session_id for r in rows])
    return {"items": [session_view(r, devs[r.session_id]) for r in rows], "next_cursor": nxt}


@router.post("/v1/sessions", status_code=201)
def create_session(body: SessionCreate, request: Request, db: Session = DB,
                   c: Container = Depends(container), p: Principal = Depends(require("session:write")),
                   idempotency_key: str | None = Header(default=None)) -> JSONResponse:
    _require_tz("starts_at", body.starts_at)
    _require_tz("ends_at", body.ends_at)
    if body.ends_at and body.ends_at < body.starts_at:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/ends_at", "code": "before_start",
                                                        "message": "종료 시각이 시작 시각보다 빠릅니다"}])

    def create() -> tuple[int, dict]:
        load_scoped(db, c, request, Project, body.project_id, p)
        for did in body.device_ids:
            load_scoped(db, c, request, Device, did, p)
        s = TrialSession(session_id=new_id("session"), organization_id=p.organization_id,
                         created_at=c.clock.now(), **body.model_dump(exclude={"device_ids"}))
        db.add(s)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise ProblemError("CONFLICT", detail="같은 label의 실증회차가 이미 있습니다") from None
        for did in sorted(set(body.device_ids)):
            db.add(SessionDevice(session_id=s.session_id, device_id=did))
        return 201, session_view(s, sorted(set(body.device_ids)))

    status, resp, replayed = run_idempotent(db, c.clock, c.settings.idempotency_retention_s, p,
                                            "POST /v1/sessions", idempotency_key, body.model_dump(mode="json"),
                                            create)
    return JSONResponse(resp, status_code=status, headers={"Idempotent-Replayed": "true"} if replayed else None)


@router.get("/v1/sessions/{session_id}")
def get_session(session_id: str, request: Request, db: Session = DB,
                c: Container = Depends(container), p: Principal = Depends(require("session:read"))) -> dict:
    s = load_scoped(db, c, request, TrialSession, session_id, p)
    return session_view(s, _session_devices(db, [s.session_id])[s.session_id])


class SessionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")  # label·project_id는 바꿀 수 없다 (보내면 400)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    site: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    device_ids: list[str] | None = Field(default=None, max_length=500)


@router.patch("/v1/sessions/{session_id}")
def update_session(session_id: str, body: SessionPatch, request: Request, db: Session = DB,
                   c: Container = Depends(container), p: Principal = Depends(require("session:write"))) -> dict:
    """실증회차 일정·장소·메모·장비 수정 (09번 요구사항 5.1). 바뀐 항목의 전후 값을 감사한다."""
    sent = body.model_fields_set
    if not sent:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "", "code": "min_properties",
                                                        "message": "바꿀 항목이 하나 이상 필요합니다"}])
    for name in ("title", "starts_at", "device_ids"):  # 비울 수 없는 항목
        if name in sent and getattr(body, name) is None:
            raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": f"/{name}", "code": "not_nullable",
                                                            "message": "비울 수 없는 항목입니다"}])
    _require_tz("starts_at", body.starts_at)
    _require_tz("ends_at", body.ends_at)
    s = load_scoped(db, c, request, TrialSession, session_id, p)
    starts = body.starts_at if "starts_at" in sent and body.starts_at is not None else s.starts_at
    ends = body.ends_at if "ends_at" in sent else s.ends_at
    if ends is not None and ends < starts:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": "/ends_at", "code": "before_start",
                                                        "message": "종료 시각이 시작 시각보다 빠릅니다"}])
    before_devices = _session_devices(db, [s.session_id])[s.session_id]
    changes: dict[str, dict[str, Any]] = {}

    def note_change(name: str, old: Any, new: Any) -> None:
        if old != new:
            if name == "note":  # 메모 본문은 감사 로그에 옮기지 않는다 (길이만)
                old, new = (len(old) if old else 0), (len(new) if new else 0)
            changes[name] = {"from": old, "to": new}

    for name in ("title", "site", "note"):
        if name in sent:
            note_change(name, getattr(s, name), getattr(body, name))
            setattr(s, name, getattr(body, name))
    if "starts_at" in sent:
        note_change("starts_at", iso(s.starts_at), iso(starts))
        s.starts_at = starts
    if "ends_at" in sent:
        note_change("ends_at", iso(s.ends_at), iso(ends))
        s.ends_at = ends
    devices = before_devices
    if "device_ids" in sent and body.device_ids is not None:
        devices = sorted(set(body.device_ids))
        for did in devices:
            load_scoped(db, c, request, Device, did, p)  # 자기 기관 장비만 (아니면 404)
        if devices != before_devices:
            note_change("device_ids", before_devices, devices)
            db.execute(delete(SessionDevice).where(SessionDevice.session_id == s.session_id))
            db.add_all([SessionDevice(session_id=s.session_id, device_id=d) for d in devices])
    if changes:
        c.audit.record("SESSION_UPDATED", principal=p, organization_id=s.organization_id, target_type="Session",
                       target_id=s.session_id, source_ip=client_ip(request), details={"changes": changes})
    db.flush()
    return session_view(s, devices)
