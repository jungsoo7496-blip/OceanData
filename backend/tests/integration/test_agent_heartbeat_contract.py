"""하트비트 범위 거부·실패 감사·스키마 버전 응답."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import AgentStatus, AuditLog, Device
from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_B_COLLECTOR, ORG_A, ORG_B, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_agents import _device_headers, hb

pytestmark = pytest.mark.integration


def test_heartbeat_collector_scope_denials_are_indistinguishable_and_audited(ctx: Ctx) -> None:
    headers = _device_headers(ctx)
    path = "/v1/agents/heartbeat"
    own = hb()
    accepted = ctx.client.post(path, headers=headers, json=own)
    assert accepted.status_code == 204, accepted.text
    check(accepted, "POST", path)

    problems = {}
    for device_id in (fid("dev", 399), DEV_A_CAMERA, DEV_B_COLLECTOR):
        denied = ctx.client.post(path, headers=headers, json=hb(collector_device_id=device_id))
        assert denied.status_code == 404, denied.text
        assert denied.json()["code"] == "NOT_FOUND"
        check(denied, "POST", path)
        problems[device_id] = denied.json()

    cross_org_request_id = problems[DEV_B_COLLECTOR]["request_id"]
    public_problems = [{key: value for key, value in problem.items() if key != "request_id"}
                       for problem in problems.values()]
    assert public_problems[0] == public_problems[1] == public_problems[2]

    with ctx.c.sessionmaker() as db:
        stored = db.scalars(select(AgentStatus)).one()
        assert (stored.collector_device_id, stored.heartbeat) == (DEV_A_COLLECTOR, own)
        collector = db.get(Device, DEV_A_COLLECTOR)
        assert collector is not None and collector.last_heartbeat_at == stored.last_heartbeat_at
        audits = db.execute(select(
            AuditLog.request_id, AuditLog.actor_kind, AuditLog.actor_id, AuditLog.actor_organization_id,
            AuditLog.result, AuditLog.organization_id, AuditLog.target_type, AuditLog.target_id,
        ).where(AuditLog.action == "CROSS_ORG_ACCESS_ATTEMPT")).all()
    assert audits == [(cross_org_request_id, "DEVICE", DEV_A_COLLECTOR, ORG_A, "DENIED",
                       ORG_B, "Device", DEV_B_COLLECTOR)]


def test_heartbeat_unsupported_schema_version_has_contract_response(ctx: Ctx) -> None:
    headers = _device_headers(ctx)
    path = "/v1/agents/heartbeat"
    for version in ("2.0.0", "1.999.0"):
        denied = ctx.client.post(path, headers=headers, json=hb(schema_version=version))
        assert denied.status_code == 422, denied.text
        assert denied.json()["code"] == "UNSUPPORTED_SCHEMA_VERSION"
        check(denied, "POST", path)
    with ctx.c.sessionmaker() as db:
        assert db.get(AgentStatus, DEV_A_COLLECTOR) is None
