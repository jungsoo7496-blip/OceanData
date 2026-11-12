"""수집기 원격 상태·정책 조회의 기관 격리와 실패 감사 (02번 요구사항 2.2)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import AuditLog, Device
from app.seed import DEV_A_COLLECTOR, DEV_B_COLLECTOR, ORG_A, ORG_B, PRJ_A, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_agents import _device_headers

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("path", ["/v1/agents/{device_id}/health", "/v1/devices/{device_id}/policy"])
def test_agent_read_cross_org_denial_is_audited(ctx: Ctx, path: str) -> None:
    other_local = fid("dev", 398)
    with ctx.c.sessionmaker.begin() as db:
        db.add(Device(device_id=other_local, organization_id=ORG_A, project_id=PRJ_A,
                      name="합성 수집기", kind="COLLECTOR", device_type="AGENT_PC",
                      status="ACTIVE", created_at=ctx.c.clock.now()))

    headers = _device_headers(ctx)
    own = ctx.client.get(path.format(device_id=DEV_A_COLLECTOR), headers=headers)
    assert own.status_code == 200
    check(own, "GET", path)
    for device_id in (other_local, fid("dev", 399)):
        hidden = ctx.client.get(path.format(device_id=device_id), headers=headers)
        assert hidden.status_code == 404 and hidden.json()["code"] == "NOT_FOUND"
    assert "CROSS_ORG_ACCESS_ATTEMPT" not in ctx.audit_actions()

    denied = ctx.client.get(path.format(device_id=DEV_B_COLLECTOR), headers=headers)
    assert denied.status_code == 404 and denied.json()["code"] == "NOT_FOUND"
    check(denied, "GET", path)
    with ctx.c.sessionmaker() as db:
        attempts = db.execute(select(
            AuditLog.actor_kind, AuditLog.actor_id, AuditLog.actor_organization_id,
            AuditLog.organization_id, AuditLog.target_type, AuditLog.target_id,
            AuditLog.result, AuditLog.request_id,
        ).where(AuditLog.action == "CROSS_ORG_ACCESS_ATTEMPT")).all()
    assert attempts == [("DEVICE", DEV_A_COLLECTOR, ORG_A, ORG_B, "Device", DEV_B_COLLECTOR,
                         "DENIED", denied.json()["request_id"])]
