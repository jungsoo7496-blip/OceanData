"""에이전트 하트비트·원격 상태 (04번 요구사항 11.2, 11.3). 상태 판정은 services/monitoring (11번)."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request, Response
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.api.deps import DB, container, load_scoped, require
from app.container import Container
from app.db.models import AgentStatus, Device
from app.domain.normalize import parse_rfc3339
from app.domain.principal import Principal
from app.errors import ProblemError
from app.services import contracts, monitoring

router = APIRouter(tags=["agents"])


@router.post("/v1/agents/heartbeat", status_code=204)
def post_heartbeat(body: dict = Body(...), db: Session = DB, c: Container = Depends(container),
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
def get_agent_health(device_id: str, request: Request, db: Session = DB,
                     c: Container = Depends(container), p: Principal = Depends(require("agent_health:read"))) -> dict:
    if p.kind == "DEVICE" and device_id != p.device_id:
        raise ProblemError("NOT_FOUND")
    device = load_scoped(db, c, request, Device, device_id, p)
    if device.kind != "COLLECTOR":
        raise ProblemError("NOT_FOUND", detail="원격 상태는 수집기 단위입니다")
    h = monitoring.device_health(db, [device], c.clock.now())[0]
    keys = ("device_id", "status", "last_heartbeat_at", "last_success_at", "queue_items", "queue_bytes",
            "last_error_code", "policy_version_applied", "policy_version_current", "open_alerts",
            "last_registered_at", "last_observation_at", "expected_interval_s", "lag_s", "heartbeat_lag_s",
            "clock_offset_ms", "policy_mismatch", "disk_free_bytes", "next_expected_at")
    return {k: h[k] for k in keys}
