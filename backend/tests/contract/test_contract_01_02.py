"""01·02번 엔드포인트 응답이 contracts/openapi.yaml과 일치하는지 확인한다."""

from __future__ import annotations

import pytest

from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, PRJ_A, SES_A
from tests.conftest import in_future
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration


def test_system_and_context_responses_match_contract(ctx) -> None:
    cl, h = ctx.client, ctx.as_user(101, mfa=True)
    check(cl.get("/healthz"), "GET", "/healthz")
    check(cl.get("/readyz"), "GET", "/readyz")
    check(cl.get("/v1/system/capabilities", headers=h), "GET", "/v1/system/capabilities")
    check(cl.get("/v1/system/capabilities"), "GET", "/v1/system/capabilities")  # 401 problem
    check(cl.get("/v1/me", headers=h), "GET", "/v1/me")
    check(cl.get("/v1/projects", headers=h), "GET", "/v1/projects")
    check(cl.get("/v1/sessions", headers=h), "GET", "/v1/sessions")
    check(cl.get(f"/v1/sessions/{SES_A}", headers=h), "GET", "/v1/sessions/{session_id}")
    r = cl.post("/v1/sessions", headers=h, json={"project_id": PRJ_A, "label": "C-1", "title": "계약 시험",
                                                  "starts_at": "2026-11-03T00:00:00Z", "device_ids": [DEV_A_CAMERA]})
    check(r, "POST", "/v1/sessions")
    check(cl.post("/v1/sessions", headers=h, json={"title": ""}), "POST", "/v1/sessions")  # 400
    check(cl.get("/v1/devices", headers=h), "GET", "/v1/devices")
    check(cl.get(f"/v1/devices/{DEV_A_CAMERA}", headers=h), "GET", "/v1/devices/{device_id}")
    r = cl.post("/v1/devices", headers=h, json={"name": "선미 카메라", "kind": "SOURCE", "device_type": "CAMERA",
                                                 "collector_device_id": DEV_A_COLLECTOR,
                                                 "local_copy_policy": "ROTATING"})
    check(r, "POST", "/v1/devices")


def test_identity_responses_match_contract(ctx) -> None:
    cl, h = ctx.client, ctx.as_user(101, mfa=True)
    r = cl.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes", headers=h)
    check(r, "POST", "/v1/devices/{device_id}/enrollment-codes")
    e = cl.post("/v1/agents/enroll", json={"enrollment_code": r.json()["code"], "agent_version": "0.1.0",
                                           "os": "windows"})
    check(e, "POST", "/v1/agents/enroll")
    t = cl.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": e.json()["client_id"],
                                        "client_secret": e.json()["client_secret"]})
    check(t, "POST", "/v1/auth/token")
    check(cl.get("/v1/me", headers={"Authorization": "Bearer " + t.json()["access_token"]}), "GET", "/v1/me")
    spare = cl.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes", headers=h)
    assert spare.status_code == 201
    check(cl.post(f"/v1/devices/{DEV_A_COLLECTOR}/credentials/revoke", headers=h),
          "POST", "/v1/devices/{device_id}/credentials/revoke")
    expired = cl.post("/v1/agents/enroll", json={"enrollment_code": spare.json()["code"], "agent_version": "0.1.0",
                                                 "os": "windows"})
    assert expired.status_code == 410 and expired.json()["reason"] == "EXPIRED"
    check(expired, "POST", "/v1/agents/enroll")
    k = cl.post("/v1/api-keys", headers=h, json={"name": "erp", "scopes": ["upload:write", "status:read"],
                                                 "expires_at": in_future(days=7)})
    check(k, "POST", "/v1/api-keys")
    check(cl.get("/v1/api-keys", headers=h), "GET", "/v1/api-keys")
    check(cl.post(f"/v1/api-keys/{k.json()['api_key_id']}/revoke", headers=h), "POST",
          "/v1/api-keys/{api_key_id}/revoke")
    check(cl.get("/v1/audit-logs", headers=h), "GET", "/v1/audit-logs")
    check(cl.post("/v1/api-keys", headers=ctx.as_user(101), json={"name": "x", "scopes": ["status:read"],
                                                                  "expires_at": in_future(days=1)}),
          "POST", "/v1/api-keys")  # 403 MFA_REQUIRED
