"""기기 관측 매핑 조회의 기관 격리와 실패 감사 (02번 요구사항 2.2, 5.5)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import AuditLog, Device, ObservationMapping
from app.seed import DEV_A_COLLECTOR, DEV_A_SENSOR, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, fid
from tests.conftest import Ctx
from tests.integration.test_device_reach import _device_headers

pytestmark = pytest.mark.integration


def test_device_mapping_cross_org_denial_is_audited(ctx: Ctx) -> None:
    unmapped = fid("dev", 398)
    mapping = {"format": "csv", "columns": [{"field": "temp", "metric": "sea_water_temperature"}]}
    now = ctx.c.clock.now()
    with ctx.c.sessionmaker.begin() as db:
        db.add(Device(device_id=unmapped, organization_id=ORG_A, project_id=PRJ_A,
                      name="합성 미매핑 관측 장비", kind="SOURCE", device_type="SENSOR",
                      status="ACTIVE", created_at=now))
        db.flush()
        db.add_all([
            ObservationMapping(device_id=device_id, version=1, organization_id=org,
                               mapping=mapping, created_by="synthetic", created_at=now)
            for device_id, org in ((DEV_A_SENSOR, ORG_A), (unmapped, ORG_A), (DEV_B_CAMERA, ORG_B))
        ])

    headers = _device_headers(ctx)
    path = "/v1/devices/{device_id}/observation-mapping"
    own = ctx.client.get(path.format(device_id=DEV_A_SENSOR), headers=headers)
    assert own.status_code == 200 and own.json()["mapping"] == mapping
    for device_id in (unmapped, fid("dev", 399)):
        hidden = ctx.client.get(path.format(device_id=device_id), headers=headers)
        assert hidden.status_code == 404 and hidden.json()["code"] == "NOT_FOUND"
    assert "CROSS_ORG_ACCESS_ATTEMPT" not in ctx.audit_actions()

    denied = ctx.client.get(path.format(device_id=DEV_B_CAMERA), headers=headers)
    assert denied.status_code == 404 and denied.json()["code"] == "NOT_FOUND"
    with ctx.c.sessionmaker() as db:
        attempts = db.execute(select(
            AuditLog.actor_kind, AuditLog.actor_id, AuditLog.actor_organization_id,
            AuditLog.organization_id, AuditLog.target_type, AuditLog.target_id,
            AuditLog.result, AuditLog.request_id,
        ).where(AuditLog.action == "CROSS_ORG_ACCESS_ATTEMPT")).all()
    assert attempts == [("DEVICE", DEV_A_COLLECTOR, ORG_A, ORG_B, "Device", DEV_B_CAMERA,
                         "DENIED", denied.json()["request_id"])]
