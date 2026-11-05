"""02번 통합 시험: 인증, MFA, 기기 등록·토큰, API 키, 감사 로그 해시 체인."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_B_COLLECTOR, ORG_A, sub
from tests.conftest import _KEY, in_future, user_token

pytestmark = pytest.mark.integration


@pytest.mark.covers("T01-d")
def test_user_token_validation_and_audit(ctx) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = [
        {},
        {"Authorization": "Bearer " + user_token(sub(101), exp_s=-10)},          # 만료
        {"Authorization": "Bearer " + user_token(sub(101), aud="other")},        # 대상
        {"Authorization": "Bearer " + user_token(sub(101), iss="http://evil")},  # 발급자
        {"Authorization": "Bearer " + user_token(sub(101), key=other)},          # 서명
        {"Authorization": "Bearer " + user_token(sub(999))},                     # 모르는 사용자
        {"Authorization": "Bearer " + user_token(sub(101), org="org_" + "9" * 26)},  # 다른 기관 클레임
    ]
    for h in bad:
        r = ctx.client.get("/v1/me", headers=h)
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED", h
    assert ctx.audit_actions().count("AUTH_FAILED") == len(bad)
    r = ctx.client.get("/v1/me", headers={"Authorization": "Bearer " + user_token(sub(101), org=ORG_A)})
    assert r.status_code == 200
    assert r.json() == {"principal_kind": "USER", "organization_id": ORG_A, "org_type": "PROVIDER",
                        "roles": ["ORG_ADMIN"], "mfa": False, "user_id": "usr_" + "0" * 23 + "101"}
    assert _KEY  # 시험 키 사용 확인


@pytest.mark.covers("T01-c")
def test_organization_id_in_query_or_header_is_rejected(ctx) -> None:
    h = ctx.as_user(101)
    assert ctx.client.get(f"/v1/devices?organization_id={ORG_A}", headers=h).json()["code"] == "VALIDATION_ERROR"
    r = ctx.client.get("/v1/devices", headers={**h, "X-Organization-Id": ORG_A})
    assert r.status_code == 400
    r = ctx.client.post("/v1/sessions", headers=ctx.as_user(102), json={
        "project_id": "prj_" + "0" * 23 + "201", "label": "x", "title": "x", "starts_at": "2026-11-02T00:00:00Z",
        "organization_id": "org_" + "0" * 25 + "2"})
    assert r.status_code == 400  # 계약에 없는 필드


def test_mfa_required_for_high_risk_actions(ctx) -> None:
    r = ctx.client.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes", headers=ctx.as_user(101))
    assert r.status_code == 403 and r.json()["code"] == "MFA_REQUIRED"
    r = ctx.client.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes", headers=ctx.as_user(102, mfa=True))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    assert "PERMISSION_DENIED" in ctx.audit_actions()


def _enroll(ctx, device_id: str = DEV_A_COLLECTOR, admin: int = 101) -> dict:
    r = ctx.client.post(f"/v1/devices/{device_id}/enrollment-codes", headers=ctx.as_user(admin, mfa=True))
    assert r.status_code == 201, r.text
    code = r.json()["code"]
    r = ctx.client.post("/v1/agents/enroll", json={"enrollment_code": code, "agent_version": "0.1.0",
                                                   "os": "linux"})
    assert r.status_code == 201, r.text
    return {"code": code, **r.json()}


def _token(ctx, client_id: str, secret: str, scope: str | None = None):
    data = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret}
    if scope:
        data["scope"] = scope
    return ctx.client.post("/v1/auth/token", data=data)


def test_device_enrollment_token_and_revocation(ctx) -> None:
    e = _enroll(ctx)
    assert e["device_id"] == DEV_A_COLLECTOR and e["token_endpoint"] == "/v1/auth/token"
    # 등록 코드는 1회용
    r = ctx.client.post("/v1/agents/enroll", json={"enrollment_code": e["code"], "agent_version": "0.1.0",
                                                   "os": "linux"})
    assert r.status_code == 410 and r.json()["code"] == "GONE"
    # 서버에는 해시만 저장
    with ctx.c.engine.connect() as conn:
        stored = conn.execute(text("SELECT secret_hash FROM device_credentials")).scalar_one()
    assert e["client_secret"] not in stored
    assert _token(ctx, e["client_id"], "wrong").status_code == 401
    t = _token(ctx, e["client_id"], e["client_secret"])
    assert t.status_code == 200 and t.json()["expires_in"] == 300 and t.json()["token_type"] == "Bearer"
    dh = {"Authorization": "Bearer " + t.json()["access_token"]}
    me = ctx.client.get("/v1/me", headers=dh).json()
    assert me["principal_kind"] == "DEVICE" and me["device_id"] == DEV_A_COLLECTOR
    # 기기는 매핑 범위 밖 동작 불가
    assert ctx.client.get("/v1/devices", headers=dh).status_code == 403
    assert ctx.client.get("/v1/sessions", headers=dh).status_code == 403  # 회차 목록은 기기 동작이 아니다 (요구사항 5.5)
    # 폐기 후 즉시 401
    r = ctx.client.post(f"/v1/devices/{DEV_A_COLLECTOR}/credentials/revoke", headers=ctx.as_user(101, mfa=True))
    assert r.status_code == 200 and r.json()["revoked_credentials"] == 1
    assert ctx.client.get("/v1/me", headers=dh).status_code == 401
    assert _token(ctx, e["client_id"], e["client_secret"]).status_code == 401


def test_enrollment_code_only_for_own_collectors(ctx) -> None:
    h = ctx.as_user(101, mfa=True)
    assert ctx.client.post(f"/v1/devices/{DEV_B_COLLECTOR}/enrollment-codes", headers=h).status_code == 404
    r = ctx.client.post(f"/v1/devices/{DEV_A_CAMERA}/enrollment-codes", headers=h)
    assert r.status_code == 422 and r.json()["unmet_conditions"][0]["condition"] == "kind=COLLECTOR"
    assert ctx.client.post("/v1/agents/enroll", json={"enrollment_code": "x" * 24, "agent_version": "1",
                                                      "os": "linux"}).status_code == 401


def test_api_key_lifecycle_and_scope(ctx) -> None:
    h = ctx.as_user(101, mfa=True)
    r = ctx.client.post("/v1/api-keys", headers=h, json={"name": "erp", "scopes": ["status:read"],
                                                          "expires_at": in_future(days=30)})
    assert r.status_code == 201, r.text
    k = r.json()
    assert k["client_id"].startswith("key_")
    assert _token(ctx, k["client_id"], k["client_secret"], scope="upload:write").status_code == 400
    t = _token(ctx, k["client_id"], k["client_secret"], scope="status:read")
    kh = {"Authorization": "Bearer " + t.json()["access_token"]}
    assert ctx.client.get("/v1/me", headers=kh).json()["principal_kind"] == "API_KEY"
    assert ctx.client.post("/v1/sessions", headers=kh, json={}).status_code == 403
    listed = ctx.client.get("/v1/api-keys", headers=ctx.as_user(101)).json()["items"]
    assert [i["api_key_id"] for i in listed] == [k["api_key_id"]] and "client_secret" not in listed[0]
    assert ctx.client.get("/v1/api-keys", headers=ctx.as_user(103)).json()["items"] == []
    assert ctx.client.post(f"/v1/api-keys/{k['api_key_id']}/revoke", headers=ctx.as_user(103)).status_code == 404
    assert ctx.client.post(f"/v1/api-keys/{k['api_key_id']}/revoke", headers=ctx.as_user(101)).status_code == 200
    assert ctx.client.get("/v1/me", headers=kh).status_code == 401  # 즉시 거부
    r = ctx.client.post("/v1/api-keys", headers=h, json={"name": "x", "scopes": ["admin"],
                                                          "expires_at": in_future(days=1)})
    assert r.status_code == 400


def test_audit_chain_verification_and_tamper_detection(ctx, db_url) -> None:
    for _ in range(3):
        ctx.client.get("/v1/me")
    report = ctx.c.audit.verify_chain()
    assert report.ok and report.checked >= 3
    # 앱 계정 권한과 무관하게 트리거가 수정을 막는다
    with pytest.raises(Exception, match="추가 전용"):
        with ctx.c.engine.begin() as conn:
            conn.execute(text("UPDATE audit_log SET result='X'"))
    # 소유자가 트리거를 끄고 조작하면 검증이 끊긴 위치를 보고한다
    owner = create_engine(db_url)
    with owner.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update"))
        seq = conn.execute(text("SELECT seq FROM audit_log ORDER BY seq LIMIT 1 OFFSET 1")).scalar_one()
        conn.execute(text("UPDATE audit_log SET reason='조작' WHERE seq=:s"), {"s": seq})
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update"))
    owner.dispose()
    report = ctx.c.audit.verify_chain()
    assert not report.ok and report.broken_at_seq == seq


def test_org_admin_sees_only_own_audit_records(ctx) -> None:
    ctx.client.post(f"/v1/devices/{DEV_B_COLLECTOR}/enrollment-codes", headers=ctx.as_user(103, mfa=True))
    ctx.client.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes", headers=ctx.as_user(101, mfa=True))
    a = ctx.client.get("/v1/audit-logs?limit=500", headers=ctx.as_user(101)).json()["items"]
    assert any(i["target_id"] == DEV_A_COLLECTOR for i in a)
    assert not any(i["target_id"] == DEV_B_COLLECTOR for i in a)
    op = ctx.client.get("/v1/audit-logs?limit=500", headers=ctx.as_user(106)).json()["items"]
    assert {DEV_A_COLLECTOR, DEV_B_COLLECTOR} <= {i["target_id"] for i in op}
    assert ctx.client.get("/v1/audit-logs", headers=ctx.as_user(102)).status_code == 403
