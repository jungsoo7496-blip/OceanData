"""관측값 배치 범위 거부와 감사 (02번 요구사항 2.2, 07번 요구사항 1.1)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.db.models import AuditLog, Device
from app.seed import DEV_A_COLLECTOR, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, SES_B, fid
from app.services.observations import OBS
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_agents import _device_headers
from tests.integration.test_sensor_events import T, batch, z

pytestmark = pytest.mark.integration


def test_device_observation_batch_scope_denials_are_audited(ctx: Ctx) -> None:
    unmapped = fid("dev", 398)
    with ctx.c.sessionmaker.begin() as db:
        db.add(Device(device_id=unmapped, organization_id=ORG_A, project_id=PRJ_A,
                      name="합성 미매핑 관측 장비", kind="SOURCE", device_type="SENSOR",
                      status="ACTIVE", created_at=ctx.c.clock.now()))

    headers = _device_headers(ctx)
    path = "/v1/observations"
    payload = batch([{
        "metric": "sea_water_temperature", "observed_at": z(T), "value": 18.4,
        "unit": "degC", "quality_flag": "GOOD",
    }])
    cases = (
        ({"source_system_id": DEV_B_CAMERA}, "CROSS_ORG_ACCESS_ATTEMPT", ORG_B, "Device", DEV_B_CAMERA),
        ({"session_id": SES_B}, "CROSS_ORG_ACCESS_ATTEMPT", ORG_B, "TrialSession", SES_B),
        ({"source_system_id": unmapped}, "DEVICE_SCOPE_VIOLATION", ORG_A, "Device", unmapped),
    )
    expected_audits = set()
    for overrides, action, organization_id, target_type, target_id in cases:
        denied = ctx.client.post(path, headers=headers, json={**payload, **overrides})
        assert denied.status_code == 404, denied.text
        problem = denied.json()
        assert problem["code"] == "NOT_FOUND"
        check(denied, "POST", path)
        expected_audits.add((problem["request_id"], "DEVICE", DEV_A_COLLECTOR, ORG_A, "DENIED",
                             action, organization_id, target_type, target_id))

    with ctx.c.sessionmaker() as db:
        assert db.scalar(select(func.count()).select_from(OBS)) == 0
        audits = db.execute(select(
            AuditLog.request_id, AuditLog.actor_kind, AuditLog.actor_id, AuditLog.actor_organization_id,
            AuditLog.result, AuditLog.action, AuditLog.organization_id, AuditLog.target_type, AuditLog.target_id,
        ).where(AuditLog.action.in_(("CROSS_ORG_ACCESS_ATTEMPT", "DEVICE_SCOPE_VIOLATION")))).all()
    assert len(audits) == len(expected_audits)
    assert set(audits) == expected_audits
