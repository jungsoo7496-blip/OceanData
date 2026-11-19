"""통합 검토 VALIDATE-3: 사람이 아닌 주체의 이벤트 확인 표시(human_verified) 거부 403은 PERMISSION_DENIED 감사를 남긴다.

07번 요구사항 7.3(확인 표시는 사람만)으로 수집기·API 키의 확인 표시는 등록(POST /v1/events)과 PATCH 모두 403이다.
security.md는 권한 거부를 반드시 감사하라고 한다. 거부 기록은 요청이 실패해도 남도록 따로 확정한다(ADR-0053).
시험 데이터는 합성 값이다.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import AuditLog, Event
from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, ORG_A, SES_A
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract

pytestmark = pytest.mark.integration
DENIAL = {"action": "event:write", "reason": "human_verified_requires_user"}


def _device_headers(ctx: Ctx) -> dict[str, str]:
    code = ctx.client.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes",
                           headers=ctx.as_user(101, mfa=True)).json()["code"]
    cred = ctx.client.post("/v1/agents/enroll", json={"enrollment_code": code, "agent_version": "0.1.0",
                                                      "os": "linux"}).json()
    tok = ctx.client.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": cred["client_id"],
                                                  "client_secret": cred["client_secret"]}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _event(**kw: Any) -> dict[str, Any]:
    return {"schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL",
            "started_at": "2026-11-02T00:00:00Z", "timestamp_quality": {"source": "DEVICE", "verified": True},
            "source": "DEVICE", "source_system_id": DEV_A_CAMERA, "note": "합성 확인 거부 시험", **kw}


def _audits(ctx: Ctx, request_id: str) -> list[AuditLog]:
    with ctx.c.sessionmaker() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.request_id == request_id).order_by(AuditLog.seq)))


def test_machine_human_verified_event_creation_denial_is_audited(ctx: Ctx) -> None:
    dh = _device_headers(ctx)
    r = ctx.client.post("/v1/events", json=_event(human_verified=True), headers=dh)
    assert r.status_code == 403, r.text
    assert_matches_contract(r, "POST", "/v1/events")
    assert r.json()["code"] == "FORBIDDEN"
    rows = _audits(ctx, r.headers["x-request-id"])
    assert [(x.action, x.result, x.actor_kind, x.organization_id, x.target_type, x.target_id) for x in rows] == [
        ("PERMISSION_DENIED", "DENIED", "DEVICE", ORG_A, "Event", None)]
    assert rows[0].details == DENIAL
    with ctx.c.sessionmaker() as db:  # 거부된 요청은 아무것도 만들지 않는다
        assert db.scalar(select(Event.event_id).where(Event.note == "합성 확인 거부 시험")) is None


def test_machine_human_verified_patch_denial_is_audited(ctx: Ctx) -> None:
    dh = _device_headers(ctx)
    created = ctx.client.post("/v1/events", json=_event(), headers=dh)
    assert created.status_code == 201, created.text
    event_id = created.json()["event_id"]
    r = ctx.client.patch(f"/v1/events/{event_id}", json={"human_verified": True}, headers=dh)
    assert r.status_code == 403, r.text
    assert_matches_contract(r, "PATCH", "/v1/events/{event_id}")
    assert r.json()["code"] == "FORBIDDEN"
    rows = _audits(ctx, r.headers["x-request-id"])
    assert [(x.action, x.result, x.actor_kind, x.organization_id, x.target_type, x.target_id) for x in rows] == [
        ("PERMISSION_DENIED", "DENIED", "DEVICE", ORG_A, "Event", event_id)]
    assert rows[0].details == DENIAL
    with ctx.c.sessionmaker() as db:  # 확인 표시는 바뀌지 않는다
        assert db.get(Event, event_id).human_verified is False


def test_human_verification_still_records_no_denial(ctx: Ctx) -> None:
    """사람의 확인 표시는 전처럼 된다. 거부 감사가 붙지 않는다."""
    created = ctx.client.post("/v1/events", json=_event(source="MANUAL"), headers=ctx.as_user(102))
    assert created.status_code == 201, created.text
    r = ctx.client.patch(f"/v1/events/{created.json()['event_id']}", json={"human_verified": True},
                         headers=ctx.as_user(102))
    assert r.status_code == 200, r.text
    assert [x.action for x in _audits(ctx, r.headers["x-request-id"])] == ["EVENT_VERIFIED"]
