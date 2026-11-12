"""T01 테넌트 격리 (docs/ACCEPTANCE.md). 새 엔드포인트를 만들면 CROSS_ORG_CASES에 추가한다 (security.md)."""

from __future__ import annotations

import pytest

from app.seed import (
    DEV_A_CAMERA,
    DEV_A_COLLECTOR,
    DEV_B_CAMERA,
    DEV_B_COLLECTOR,
    ORG_B,
    PRJ_A,
    PRJ_B,
    SES_A,
    SES_B,
)
from tests.conftest import in_future

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]

# (메서드, B 기관 리소스 경로, 본문) — A 기관 ORG_ADMIN(MFA)으로 요청하면 모두 404여야 한다
CROSS_ORG_CASES = [
    ("GET", f"/v1/devices/{DEV_B_CAMERA}", None),
    ("GET", f"/v1/devices/{DEV_B_COLLECTOR}", None),
    ("GET", f"/v1/sessions/{SES_B}", None),
    ("POST", f"/v1/devices/{DEV_B_COLLECTOR}/enrollment-codes", None),
    ("POST", f"/v1/devices/{DEV_B_COLLECTOR}/credentials/revoke", None),
    ("POST", "/v1/sessions", {"project_id": PRJ_B, "label": "x", "title": "x", "starts_at": "2026-11-02T00:00:00Z"}),
    ("POST", "/v1/sessions", {"project_id": PRJ_A, "label": "y", "title": "y", "starts_at": "2026-11-02T00:00:00Z",
                              "device_ids": [DEV_B_CAMERA]}),
    ("POST", "/v1/devices", {"name": "x", "kind": "SOURCE", "device_type": "CAMERA", "local_copy_policy": "UNKNOWN",
                             "collector_device_id": DEV_B_COLLECTOR}),
    ("POST", "/v1/policy-drafts", {"device_id": DEV_B_CAMERA}),  # 09번 장비 정책 초안
]


@pytest.mark.parametrize(("method", "path", "body"), CROSS_ORG_CASES)
@pytest.mark.covers("T01-a")
def test_T01_a_cross_org_resources_are_404_and_audited(ctx, method, path, body) -> None:
    r = ctx.client.request(method, path, json=body, headers=ctx.as_user(101, mfa=True))
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "NOT_FOUND"
    assert "CROSS_ORG_ACCESS_ATTEMPT" in ctx.audit_actions()


@pytest.mark.covers("T01-a")
def test_T01_a_cross_org_api_key_revoke(ctx) -> None:
    k = ctx.client.post("/v1/api-keys", headers=ctx.as_user(103, mfa=True),
                        json={"name": "b", "scopes": ["status:read"], "expires_at": in_future(days=1)}).json()
    r = ctx.client.post(f"/v1/api-keys/{k['api_key_id']}/revoke", headers=ctx.as_user(101))
    assert r.status_code == 404


@pytest.mark.covers("T01-b")
def test_T01_b_lists_do_not_reveal_other_org(ctx) -> None:
    h = ctx.as_user(102)
    devices = ctx.client.get("/v1/devices", headers=h).json()
    assert {d["device_id"] for d in devices["items"]} == {DEV_A_COLLECTOR, DEV_A_CAMERA,
                                                          "dev_" + "0" * 23 + "303"}
    assert "total" not in devices and "count" not in devices
    sessions = ctx.client.get(f"/v1/sessions?project_id={PRJ_B}", headers=h).json()
    assert sessions == {"items": [], "next_cursor": None}  # 존재하는 B 사업이어도 빈 결과
    assert ctx.client.get("/v1/sessions?label=B-2026-11-01", headers=h).json()["items"] == []
    projects = ctx.client.get("/v1/projects", headers=h).json()["items"]
    assert [p["project_id"] for p in projects] == [PRJ_A]


@pytest.mark.covers("T01-b")
def test_T01_b_pagination_stays_in_scope(ctx) -> None:
    h = ctx.as_user(102)
    seen, cursor = [], None
    while True:
        url = "/v1/devices?limit=1" + (f"&cursor={cursor}" if cursor else "")
        page = ctx.client.get(url, headers=h).json()
        seen += [d["device_id"] for d in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 3 and DEV_B_CAMERA not in seen


@pytest.mark.covers("T01-c")
def test_T01_c_body_query_header_org_id_do_not_change_scope(ctx) -> None:
    h = ctx.as_user(101)
    assert ctx.client.get(f"/v1/devices?organization_id={ORG_B}", headers=h).status_code == 400
    assert ctx.client.get("/v1/devices", headers={**h, "X-Organization-Id": ORG_B}).status_code == 400
    r = ctx.client.post("/v1/devices", headers=ctx.as_user(101, mfa=True), json={
        "name": "x", "kind": "SOURCE", "device_type": "CAMERA", "local_copy_policy": "UNKNOWN",
        "organization_id": ORG_B})
    assert r.status_code == 400


@pytest.mark.covers("T01-f")
def test_T01_f_program_viewer_scope(ctx) -> None:
    h = ctx.as_user(105)
    assert [p["project_id"] for p in ctx.client.get("/v1/projects", headers=h).json()["items"]] == [PRJ_A]
    assert [s["session_id"] for s in ctx.client.get("/v1/sessions", headers=h).json()["items"]] == [SES_A]
    assert ctx.client.get(f"/v1/sessions/{SES_B}", headers=h).status_code == 404
    assert ctx.client.get("/v1/devices", headers=h).status_code == 403  # 장비 목록은 범위 밖
    assert ctx.client.post("/v1/sessions", headers=h, json={}).status_code == 403


@pytest.mark.covers("T01-g")
def test_T01_g_operator_and_reviewer_cannot_act_as_provider_admin(ctx) -> None:
    for n in (106, 108):
        h = ctx.as_user(n, mfa=True)
        assert ctx.client.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes", headers=h).status_code == 403
        assert ctx.client.post("/v1/api-keys", headers=h, json={"name": "x", "scopes": ["status:read"],
                                                                 "expires_at": in_future(days=1)}).status_code == 403
        assert ctx.client.post("/v1/sessions", headers=h, json={}).status_code == 403


def test_T01_consumer_sees_no_provider_context(ctx) -> None:
    h = ctx.as_user(109)
    for path in ("/v1/projects", "/v1/devices", "/v1/sessions", f"/v1/sessions/{SES_A}"):
        assert ctx.client.get(path, headers=h).status_code == 403, path


@pytest.mark.covers("T01-a")
def test_T01_a_cross_org_incident_update_and_list(ctx) -> None:
    """11번 장애 기록: 다른 기관 장애는 목록에 없고, 바꾸려 하면 404 + 감사."""
    import dataclasses
    from datetime import UTC, datetime

    from app.adapters.system_clock import FixedClock
    from app.services import monitoring

    c = dataclasses.replace(ctx.c, clock=FixedClock(datetime.now(UTC)))
    with c.sessionmaker.begin() as db:  # 시드의 진행 중 회차(B)에 자료가 없어 B 장애가 열린다
        monitoring.evaluate(c, db)
    b_items = ctx.client.get("/v1/incidents", headers=ctx.as_user(103)).json()["items"]
    assert b_items and all(i["organization_id"] == ORG_B for i in b_items)
    assert all(i["organization_id"] != ORG_B for i in
               ctx.client.get("/v1/incidents", headers=ctx.as_user(101)).json()["items"])
    r = ctx.client.patch(f"/v1/incidents/{b_items[0]['incident_id']}", json={"memo": "x"}, headers=ctx.as_user(101))
    assert r.status_code == 404
    assert "CROSS_ORG_ACCESS_ATTEMPT" in ctx.audit_actions()


@pytest.mark.covers("T01-a", "T01-b")
def test_T01_a_cross_org_policy_drafts(ctx) -> None:
    """09번 장비 정책 초안: 다른 기관 초안·카드는 없는 것(404 + 감사), 목록에도 없다."""
    b = ctx.as_user(103)
    card = ctx.client.post("/v1/integration-cards", headers=b, json={"content": {"basic": {"project_name": "B (합성)"}}})
    draft = ctx.client.post("/v1/policy-drafts", headers=b, json={"device_id": DEV_B_CAMERA}).json()
    path = f"/v1/policy-drafts/{draft['policy_draft_id']}"
    a = ctx.as_user(101, mfa=True)
    assert ctx.client.get(path, headers=a).status_code == 404
    r = ctx.client.patch(path, headers=a, json={"revision": 1, "values": {"expected_interval_s": 60}})
    assert r.status_code == 404
    r = ctx.client.post("/v1/policy-drafts", headers=a,
                        json={"device_id": DEV_A_CAMERA, "integration_card_id": card.json()["integration_card_id"]})
    assert r.status_code == 404
    assert ctx.client.get("/v1/policy-drafts", headers=a).json()["items"] == []
    assert ctx.client.get(f"/v1/policy-drafts?device_id={DEV_B_CAMERA}", headers=a).json()["items"] == []
    assert ctx.audit_actions().count("CROSS_ORG_ACCESS_ATTEMPT") == 3
