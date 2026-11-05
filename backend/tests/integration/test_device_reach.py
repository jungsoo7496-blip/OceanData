"""기기(수집기) 토큰이 할 수 있는 일 — 02번 요구사항 5.5, domain.md 2장 DEVICE 행.

기기 범위(status:read·upload:write)가 보고서·실증 기록·관측값·이벤트·사용량·장애 조회와 실증 기록 작성까지
넓어지지 않는지, 매핑되지 않은 원천 장비의 자원이 보이지 않는지 확인한다. 합성 데이터만 쓴다.
"""

from __future__ import annotations

from app.domain import permissions
from app.domain.principal import Principal
from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, ORG_A, SES_A
from tests.conftest import in_future


def _device_headers(ctx) -> dict[str, str]:
    code = ctx.client.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes",
                           headers=ctx.as_user(101, mfa=True)).json()["code"]
    cred = ctx.client.post("/v1/agents/enroll", json={"enrollment_code": code, "agent_version": "0.1.0",
                                                      "os": "linux"}).json()
    tok = ctx.client.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": cred["client_id"],
                                                  "client_secret": cred["client_secret"]}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _unmapped_source_with_observation(ctx) -> str:
    admin, member = ctx.as_user(101), ctx.as_user(102)
    dev = ctx.client.post("/v1/devices", json={"name": "합성 미매핑 부이", "kind": "SOURCE", "device_type": "SENSOR",
                                               "local_copy_policy": "RETAINED"}, headers=admin)
    assert dev.status_code == 201, dev.text
    other = dev.json()["device_id"]
    obs = {"schema_version": "1.0.0", "source_system_id": other, "observations": [
        {"metric": "sea_water_temperature", "observed_at": "2026-11-02T00:00:00Z", "value": 18.2, "unit": "degC",
         "quality_flag": "GOOD"}]}
    assert ctx.client.post("/v1/observations", json=obs, headers=member).status_code == 200
    return other


def test_device_token_cannot_read_reports_records_or_org_wide_data(ctx) -> None:
    member = ctx.as_user(102)
    rep = ctx.client.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A},
                          headers=member)
    assert rep.status_code == 201, rep.text
    rid = rep.json()["report_id"]
    other = _unmapped_source_with_observation(ctx)
    dh = _device_headers(ctx)

    # 보고서 초안·실증 기록: 조회도 작성도 안 된다 (위조 증빙 방지)
    assert ctx.client.get("/v1/reports", headers=dh).status_code == 403
    assert ctx.client.get(f"/v1/reports/{rid}", headers=dh).status_code == 403
    w = ctx.client.post(f"/v1/sessions/{SES_A}/records",
                        json={"kind": "TEST", "title": "기기가 쓴 기록", "result": "PASS"}, headers=dh)
    assert w.status_code == 403, w.text
    assert ctx.client.get(f"/v1/sessions/{SES_A}/records", headers=dh).status_code == 403
    # 관측값·이벤트·사용량·장애·회차 조회도 기기 동작이 아니다
    q = {"source_system_id": other, "from": "2026-11-01T00:00:00Z", "to": "2026-11-03T00:00:00Z"}
    assert ctx.client.get("/v1/observations", params=q, headers=dh).status_code == 403
    for path in ("/v1/usage", "/v1/incidents", "/v1/events", "/v1/sessions"):
        assert ctx.client.get(path, headers=dh).status_code == 403, path
    # 매핑되지 않은 원천 장비로는 이벤트를 낼 수 없다 (404, 존재를 숨긴다)
    ev = ctx.client.post("/v1/events", json={
        "schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL", "started_at": "2026-11-02T00:00:00Z",
        "timestamp_quality": {"source": "DEVICE", "verified": True}, "source": "RULE",
        "source_system_id": other}, headers=dh)
    assert ev.status_code == 404, ev.text
    # 에이전트가 쓰는 동작은 그대로: 자기 확인·자기 정책·자기 상태·매핑된 원천 장비 이벤트
    assert ctx.client.get("/v1/me", headers=dh).status_code == 200
    assert ctx.client.get(f"/v1/devices/{DEV_A_COLLECTOR}/policy", headers=dh).status_code == 200
    assert ctx.client.get(f"/v1/agents/{DEV_A_COLLECTOR}/health", headers=dh).status_code == 200
    ok = ctx.client.post("/v1/events", json={
        "schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL", "started_at": "2026-11-02T00:00:00Z",
        "timestamp_quality": {"source": "DEVICE", "verified": True}, "source": "RULE",
        "source_system_id": DEV_A_CAMERA}, headers=dh)
    assert ok.status_code == 201, ok.text


def test_api_key_status_read_keeps_current_reach(ctx) -> None:
    """API 키는 요구사항 6.3·6.4대로 지정 scope 안에서 자기 기관 범위 (이번 수정으로 바뀌지 않는다)."""
    k = ctx.client.post("/v1/api-keys", headers=ctx.as_user(101, mfa=True),
                        json={"name": "합성 상태 조회 키", "scopes": ["status:read"], "expires_at": in_future(days=1)})
    assert k.status_code == 201, k.text
    tok = ctx.client.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": k.json()["client_id"],
                                                  "client_secret": k.json()["client_secret"]}).json()["access_token"]
    kh = {"Authorization": f"Bearer {tok}"}
    assert ctx.client.get("/v1/usage", headers=kh).status_code == 200
    assert ctx.client.get("/v1/events", headers=kh).status_code == 200
    # scope 밖 (upload:write 없음)
    assert ctx.client.get("/v1/sessions", headers=kh).status_code == 403


def test_permission_table_device_allowlist() -> None:
    """기기가 할 수 있는 동작은 요구사항 5.5의 것만 (표의 device 표시)."""
    dev = Principal(kind="DEVICE", organization_id=ORG_A, org_type="PROVIDER", roles=frozenset({"DEVICE"}),
                    device_id=DEV_A_COLLECTOR, scopes=frozenset({"upload:write", "observation:write", "event:write",
                                                                 "status:read", "agent:heartbeat", "agent:policy"}))
    allowed = {a for a in permissions.PERMISSIONS if permissions.allowed(dev, a)}
    assert allowed == {"me:read", "device_policy:read", "agent:heartbeat", "agent_health:read", "upload:write",
                       "upload:read", "asset:receipt", "observation:write", "mapping:read", "event:write"}
