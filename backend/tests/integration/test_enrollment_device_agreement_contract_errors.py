"""등록·장비·약정 API 오류 응답 계약 (계약 점검 21~24)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.models import Agreement, Device
from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_B_COLLECTOR, ORG_A, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration

DEVICE_CREATE = {"name": "합성 관측 장비", "kind": "SOURCE", "device_type": "SENSOR", "local_copy_policy": "RETAINED"}


def test_enrollment_unknown_code_has_401_contract_response(ctx: Ctx) -> None:
    path = "/v1/agents/enroll"
    denied = ctx.client.post(path, json={
        "enrollment_code": "unissued-test-code", "agent_version": "0.3.0", "os": "linux",
    })
    assert denied.status_code == 401, denied.text
    assert denied.json()["code"] == "UNAUTHENTICATED"
    check(denied, "POST", path)


@pytest.mark.parametrize(("device_id", "status", "code"), [
    pytest.param(DEV_A_CAMERA, 422, "RULE_VIOLATION", id="source"),
    pytest.param(DEV_A_COLLECTOR, 409, "CONFLICT", id="revoked-collector"),
])
def test_enrollment_code_issuance_errors_match_contract(ctx: Ctx, device_id: str, status: int, code: str) -> None:
    if device_id == DEV_A_COLLECTOR:
        with ctx.c.sessionmaker.begin() as db:
            collector = db.get(Device, device_id)
            assert collector is not None
            collector.status = "REVOKED"

    path = "/v1/devices/{device_id}/enrollment-codes"
    denied = ctx.client.post(path.format(device_id=device_id), headers=ctx.as_user(101, mfa=True))
    assert denied.status_code == status, denied.text
    problem = denied.json()
    assert problem["code"] == code
    if status == 422:
        assert problem["unmet_conditions"] == [{"condition": "kind=COLLECTOR", "actual": "SOURCE"}]
    check(denied, "POST", path)


@pytest.mark.parametrize(("references", "status", "code"), [
    pytest.param({"collector_device_id": DEV_B_COLLECTOR}, 404, "NOT_FOUND", id="cross-org-collector"),
    pytest.param({"project_id": fid("prj", 299)}, 404, "NOT_FOUND", id="missing-project"),
    pytest.param({"collector_device_id": DEV_A_CAMERA}, 422, "RULE_VIOLATION", id="source-as-collector"),
])
def test_device_registration_reference_errors_match_contract(
    ctx: Ctx, references: dict[str, str], status: int, code: str,
) -> None:
    path = "/v1/devices"
    denied = ctx.client.post(path, headers=ctx.as_user(101, mfa=True), json={**DEVICE_CREATE, **references})
    assert denied.status_code == status, denied.text
    problem = denied.json()
    assert problem["code"] == code
    if status == 422:
        assert problem["unmet_conditions"] == [{"condition": "collector.kind=COLLECTOR", "actual": "SOURCE"}]
    check(denied, "POST", path)


def test_device_registration_idempotency_conflict_has_409_contract_response(ctx: Ctx) -> None:
    path = "/v1/devices"
    headers = {**ctx.as_user(101, mfa=True), "Idempotency-Key": "device-contract-test"}
    created = ctx.client.post(path, headers=headers, json=DEVICE_CREATE)
    assert created.status_code == 201, created.text
    denied = ctx.client.post(path, headers=headers, json={**DEVICE_CREATE, "name": "변경된 합성 관측 장비"})
    assert denied.status_code == 409, denied.text
    assert denied.json()["code"] == "IDEMPOTENCY_CONFLICT"
    check(denied, "POST", path)


@pytest.mark.parametrize("explicit_end", [False, True], ids=["default-end", "explicit-end"])
def test_end_agreement_before_start_has_422_contract_response(ctx: Ctx, explicit_end: bool) -> None:
    headers = ctx.as_user(106, mfa=True)
    now = ctx.c.clock.now()
    created = ctx.client.post("/v1/agreements", headers=headers, json={
        "organization_id": ORG_A, "tier": "TIER1", "reference": "합성 미래 시작 약정",
        "valid_from": (now + timedelta(days=1)).isoformat(),
    })
    assert created.status_code == 201, created.text
    agreement_id = created.json()["agreement_id"]
    body = {"reason": "합성 약정 종료"}
    if explicit_end:
        body["effective_at"] = now.isoformat()
    path = "/v1/agreements/{agreement_id}/end"
    denied = ctx.client.post(path.format(agreement_id=agreement_id), headers=headers, json=body)
    assert denied.status_code == 422, denied.text
    problem = denied.json()
    assert problem["code"] == "RULE_VIOLATION"
    assert problem["unmet_conditions"][0]["condition"] == "effective_at >= valid_from"
    with ctx.c.sessionmaker() as db:
        agreement = db.get(Agreement, agreement_id)
        assert agreement is not None and agreement.valid_until is None
    check(denied, "POST", path)
