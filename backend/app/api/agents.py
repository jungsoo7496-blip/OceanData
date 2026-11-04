"""에이전트 하트비트·원격 상태 (04번 요구사항 11.2, 11.3). 지연 판정·경보 규칙은 11번 스펙."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.api.deps import container, get_db, load_scoped, require
from app.container import Container
from app.db.models import AgentStatus, Asset, Device, DevicePolicy, IntegrityLogEntry
from app.domain.normalize import parse_rfc3339
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import contracts
from app.timefmt import iso

router = APIRouter(tags=["agents"])
STALE_FACTOR = 3  # 하트비트 주기의 3배가 지나면 STALE (11번에서 정책값으로 옮긴다)


@router.post("/v1/agents/heartbeat", status_code=204)
def post_heartbeat(body: dict = Body(...), db: Session = Depends(get_db), c: Container = Depends(container),
                   p: Principal = Depends(require("agent:heartbeat"))) -> Response:
    problems, rejected = contracts.validate_document("agent-heartbeat", body)
    if rejected:
        raise ProblemError("UNSUPPORTED_SCHEMA_VERSION", detail=problems[0].message)
    errors = [x for x in problems if x.severity == "ERROR"]
    if errors:
        raise ProblemError("VALIDATION_ERROR", errors=[{"pointer": x.pointer, "code": x.code, "message": x.message}
                                                       for x in errors])
    if body["collector_device_id"] != p.device_id:  # 자기 수집기만 보고한다
        raise ProblemError("NOT_FOUND", detail="수집기를 찾을 수 없습니다")
    now = c.clock.now()
    last_success = parse_rfc3339(body["last_success_at"]) if body.get("last_success_at") else None
    values = {"organization_id": p.organization_id, "last_heartbeat_at": now, "agent_version": body["agent_version"],
              "policy_version": body["policy_version"], "last_success_at": last_success,
              "last_error_code": body.get("last_error_code"), "queue_items": body["queue"]["items"],
              "queue_bytes": body["queue"]["bytes"], "heartbeat": body}
    db.execute(insert(AgentStatus).values(collector_device_id=p.device_id, **values)
               .on_conflict_do_update(index_elements=["collector_device_id"], set_=values))
    device = db.get(Device, p.device_id)
    if device is not None:
        device.last_heartbeat_at = now
    return Response(status_code=204)


@router.get("/v1/agents/{device_id}/health")
def get_agent_health(device_id: str, request: Request, db: Session = Depends(get_db),
                     c: Container = Depends(container), p: Principal = Depends(require("agent_health:read"))) -> dict:
    if p.kind == "DEVICE" and device_id != p.device_id:
        raise ProblemError("NOT_FOUND")
    device = load_scoped(db, c, request, Device, device_id, p)
    if device.kind != "COLLECTOR":
        raise ProblemError("NOT_FOUND", detail="원격 상태는 수집기 단위입니다")
    now = c.clock.now()
    st = db.get(AgentStatus, device.device_id)
    policy = db.execute(select(DevicePolicy).where(DevicePolicy.device_id == device.device_id)
                        .order_by(DevicePolicy.version.desc()).limit(1)).scalar_one_or_none()
    current = (policy.policy if policy else {}).get("policy_version", "pv-0")
    interval = int((policy.policy if policy else {}).get("heartbeat_interval_s", 60))
    sources = select(Device.device_id).where((Device.collector_device_id == device.device_id) |
                                             (Device.device_id == device.device_id))
    last_registered: datetime | None = db.execute(
        select(func.max(IntegrityLogEntry.registered_at)).join(Asset, Asset.asset_id == IntegrityLogEntry.asset_id)
        .where(Asset.source_system_id.in_(sources))).scalar()
    if st is None:
        status = "NEVER_SEEN"
    elif st.last_error_code:
        status = "ERROR"
    elif (now - st.last_heartbeat_at).total_seconds() > STALE_FACTOR * interval:
        status = "STALE"
    else:
        status = "OK"
    return {
        "device_id": device.device_id, "status": status,
        "last_heartbeat_at": iso(st.last_heartbeat_at) if st else None,
        "last_success_at": iso(st.last_success_at) if st else None,
        "queue_items": st.queue_items if st else 0, "queue_bytes": st.queue_bytes if st else 0,
        "last_error_code": st.last_error_code if st else None,
        "policy_version_applied": st.policy_version if st else None, "policy_version_current": current,
        "open_alerts": 0, "last_registered_at": iso(last_registered), "last_observation_at": None,
        "expected_interval_s": device.expected_interval_s,
        "lag_s": int((now - last_registered).total_seconds()) if last_registered else None,
    }
