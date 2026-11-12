"""수집기 자격증명 폐기로 원천 장비 상태를 바꿀 수 없다 (계약 점검 27번)."""

from __future__ import annotations

import pytest

from app.seed import DEV_A_CAMERA
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract

pytestmark = pytest.mark.integration


def test_source_device_credential_revoke_is_rejected_without_changes(ctx: Ctx) -> None:
    admin = ctx.as_user(101, mfa=True)
    device_url = f"/v1/devices/{DEV_A_CAMERA}"
    before = ctx.client.get(device_url, headers=admin)
    assert before.status_code == 200, before.text
    assert (before.json()["kind"], before.json()["status"]) == ("SOURCE", "ACTIVE")
    revoked_before = ctx.audit_actions().count("DEVICE_CREDENTIAL_REVOKED")

    response = ctx.client.post(f"{device_url}/credentials/revoke", headers=admin)
    assert response.status_code == 422, response.text
    assert_matches_contract(response, "POST", "/v1/devices/{device_id}/credentials/revoke")
    assert response.json()["code"] == "RULE_VIOLATION"

    after = ctx.client.get(device_url, headers=admin)
    assert after.status_code == 200, after.text
    assert after.json()["status"] == "ACTIVE"
    assert ctx.audit_actions().count("DEVICE_CREDENTIAL_REVOKED") == revoked_before
