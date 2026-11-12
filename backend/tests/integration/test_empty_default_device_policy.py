"""수집기 등록 직후에도 유효한 기본 정책을 내려준다 (계약 점검 18번)."""

from __future__ import annotations

import pytest

from app.db.models import Device
from app.seed import DEV_A_COLLECTOR, PRJ_A
from app.services.device_policy import default_policy, policy_problems
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract
from tests.integration.test_identity import _enroll, _token

pytestmark = pytest.mark.integration
POLICY_PATH = "/v1/devices/{device_id}/policy"


def test_enrolled_collector_gets_default_policy_before_and_after_mapping(ctx: Ctx) -> None:
    admin = ctx.as_user(101, mfa=True)
    collector = ctx.client.post("/v1/devices", headers=admin, json={
        "name": "합성 초기 수집기", "kind": "COLLECTOR", "device_type": "AGENT_PC",
        "project_id": PRJ_A, "local_copy_policy": "RETAINED",
    })
    assert collector.status_code == 201, collector.text
    collector_id = collector.json()["device_id"]
    enrolled = _enroll(ctx, collector_id)
    token = _token(ctx, enrolled["client_id"], enrolled["client_secret"])
    assert token.status_code == 200, token.text
    device_headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
    policy_url = POLICY_PATH.format(device_id=collector_id)

    response = ctx.client.get(policy_url, headers=device_headers)
    assert response.status_code == 200, response.text
    assert_matches_contract(response, "GET", POLICY_PATH)
    empty = response.json()
    assert (empty["collector_device_id"], empty["policy_version"]) == (collector_id, "pv-0")
    assert empty["sources"] == empty["watch_rules"] == []

    source = ctx.client.post("/v1/devices", headers=admin, json={
        "name": "합성 원천 카메라", "kind": "SOURCE", "device_type": "CAMERA",
        "collector_device_id": collector_id, "project_id": PRJ_A, "local_copy_policy": "RETAINED",
    })
    assert source.status_code == 201, source.text

    response = ctx.client.get(policy_url, headers=device_headers)
    assert response.status_code == 200, response.text
    assert_matches_contract(response, "GET", POLICY_PATH)
    mapped = response.json()
    assert (mapped["collector_device_id"], mapped["policy_version"]) == (collector_id, "pv-0")
    assert [(row["device_id"], row["project_id"]) for row in mapped["sources"]] == [(source.json()["device_id"], PRJ_A)]
    assert mapped["watch_rules"] == []


def test_operator_policy_cannot_store_the_empty_default_shape(ctx: Ctx) -> None:
    with ctx.c.sessionmaker() as db:
        collector = db.get(Device, DEV_A_COLLECTOR)
        assert collector is not None
        doc = {**default_policy(db, collector), "sources": []}
        assert doc["policy_version"] == "pv-0"
        assert policy_problems(db, collector.device_id, doc, integration_card_id=None)
