"""장비 등록 권한 거부는 감사 기록에 남긴다 (계약 점검 25번)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import AuditLog, Device
from app.seed import ORG_OPERATOR, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract

pytestmark = pytest.mark.integration


def test_operator_device_creation_denial_is_audited(ctx: Ctx) -> None:
    name = "합성 운영자 등록 거부 시험"
    response = ctx.client.post("/v1/devices", headers=ctx.as_user(106, mfa=True), json={
        "name": name, "kind": "SOURCE", "device_type": "CAMERA", "local_copy_policy": "RETAINED",
    })
    assert response.status_code == 403, response.text
    assert_matches_contract(response, "POST", "/v1/devices")
    assert response.json()["code"] == "FORBIDDEN"

    with ctx.c.sessionmaker() as db:
        assert db.scalar(select(Device.device_id).where(Device.name == name)) is None
        audits = db.scalars(select(AuditLog).where(AuditLog.request_id == response.headers["x-request-id"])).all()
        assert [(row.action, row.result, row.actor_id, row.organization_id) for row in audits] == [
            ("PERMISSION_DENIED", "DENIED", fid("usr", 106), ORG_OPERATOR),
        ]
        assert audits[0].details["action"] == "device:write"
