"""04번 요구사항 11: 하트비트·원격 상태."""

from __future__ import annotations

import pytest

from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_B_COLLECTOR
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration


def _device_headers(ctx) -> dict[str, str]:
    code = ctx.client.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes",
                           headers=ctx.as_user(101, mfa=True)).json()["code"]
    cred = ctx.client.post("/v1/agents/enroll", json={"enrollment_code": code, "agent_version": "0.1.0",
                                                      "os": "windows"}).json()
    tok = ctx.client.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": cred["client_id"],
                                                  "client_secret": cred["client_secret"]}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def hb(**kw) -> dict:
    return {"schema_version": "1.0.0", "collector_device_id": DEV_A_COLLECTOR, "agent_version": "0.1.0",
            "policy_version": "pv-0", "sent_at": "2026-11-04T00:00:00Z", "queue": {"items": 3, "bytes": 1024},
            "disk_free_bytes": 10**10, **kw}


def test_heartbeat_and_health(ctx) -> None:
    h = ctx.as_user(102)
    r = ctx.client.get(f"/v1/agents/{DEV_A_COLLECTOR}/health", headers=h)
    check(r, "GET", "/v1/agents/{device_id}/health")
    assert r.json()["status"] == "NEVER_SEEN"
    dh = _device_headers(ctx)
    assert ctx.client.post("/v1/agents/heartbeat", json=hb(), headers=dh).status_code == 204
    r = ctx.client.get(f"/v1/agents/{DEV_A_COLLECTOR}/health", headers=h)
    check(r, "GET", "/v1/agents/{device_id}/health")
    body = r.json()
    assert (body["status"], body["queue_items"], body["policy_version_applied"]) == ("OK", 3, "pv-0")
    assert ctx.client.post("/v1/agents/heartbeat", json=hb(last_error_code="DISK_FULL"), headers=dh).status_code == 204
    assert ctx.client.get(f"/v1/agents/{DEV_A_COLLECTOR}/health", headers=h).json()["status"] == "ERROR"
    # 다른 수집기 이름으로 보고 불가, 형식 오류 400, 사용자는 하트비트를 보낼 수 없다
    assert ctx.client.post("/v1/agents/heartbeat", json=hb(collector_device_id=DEV_B_COLLECTOR),
                           headers=dh).status_code == 404
    assert ctx.client.post("/v1/agents/heartbeat", json={"queue": 1}, headers=dh).status_code == 400
    assert ctx.client.post("/v1/agents/heartbeat", json=hb(), headers=h).status_code == 403
    # 기관 범위 (T01)
    assert ctx.client.get(f"/v1/agents/{DEV_A_COLLECTOR}/health", headers=ctx.as_user(104)).status_code == 404
    assert ctx.client.get(f"/v1/agents/{DEV_A_CAMERA}/health", headers=h).status_code == 404
    # 기기는 자기 정책을 받는다
    assert ctx.client.get(f"/v1/devices/{DEV_A_COLLECTOR}/policy", headers=dh).status_code == 200
    assert ctx.client.get(f"/v1/devices/{DEV_B_COLLECTOR}/policy", headers=dh).status_code == 404
