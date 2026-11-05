"""09번 요구사항 10.1·10.2: 운영 콘솔 정책 편집 — 기관 쿼터·악성코드 검사 요구, 보존 정책, 작업 우선순위.

운영자만, 바꾸기는 다중 인증·사유 필수, 바뀐 항목의 전후 값과 사유를 감사 로그에 남긴다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.db.models import Asset, JobRow, OrgQuota
from app.seed import ORG_A
from app.services.retention import apply_retention
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
ADMIN_A, OP = 101, 106


def _audit(ctx, action: str) -> list[tuple[str | None, dict]]:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        rows = conn.execute(text("SELECT reason, details FROM audit_log WHERE action = :a ORDER BY seq"), {"a": action})
        return [(r[0], r[1] if isinstance(r[1], dict) else json.loads(r[1])) for r in rows]


def test_organization_policy_quota_and_malware_flag(ctx) -> None:
    r = ctx.client.get("/v1/organization-policies", headers=ctx.as_user(OP))
    check(r, "GET", "/v1/organization-policies")
    org = next(x for x in r.json()["items"] if x["organization_id"] == ORG_A)
    assert ctx.client.get("/v1/organization-policies", headers=ctx.as_user(ADMIN_A)).status_code == 403
    path = f"/v1/organization-policies/{ORG_A}"
    body = {"quota_bytes": 5 * 1024**4, "reason": "2단계 실증 확대 (합성)"}
    assert ctx.client.patch(path, json=body, headers=ctx.as_user(OP)).json()["code"] == "MFA_REQUIRED"
    assert ctx.client.patch(path, json=body, headers=ctx.as_user(ADMIN_A, mfa=True)).status_code == 403
    r = ctx.client.patch(path, json=body, headers=ctx.as_user(OP, mfa=True))
    check(r, "PATCH", "/v1/organization-policies/{organization_id}")
    assert r.status_code == 200 and r.json()["quota_bytes"] == 5 * 1024**4 and r.json()["over_quota"] is False
    reason, details = _audit(ctx, "ORG_POLICY_CHANGED")[-1]
    assert reason == "2단계 실증 확대 (합성)"
    assert details["changes"] == {"quota_bytes": {"from": org["quota_bytes"], "to": 5 * 1024**4}}

    # 사용량보다 낮추면 over_quota (데이터는 지우지 않는다), 악성코드 검사 요구 켜기
    with ctx.c.sessionmaker.begin() as db:
        db.get(OrgQuota, ORG_A).used_bytes = 500
    r = ctx.client.patch(path, json={"quota_bytes": 100, "malware_scan_required": True, "reason": "시험"},
                         headers=ctx.as_user(OP, mfa=True))
    assert (r.json()["over_quota"], r.json()["malware_scan_required"], r.json()["usage_bytes"]) == (True, True, 500)
    assert set(_audit(ctx, "ORG_POLICY_CHANGED")[-1][1]["changes"]) == {"quota_bytes", "malware_scan_required"}

    for bad in ({"reason": "바꿀 것 없음"}, {"quota_bytes": -1, "reason": "음수"}, {"quota_bytes": 1},
                {"quota_bytes": 1, "reason": "x", "extra": 1}):
        r = ctx.client.patch(path, json=bad, headers=ctx.as_user(OP, mfa=True))
        check(r, "PATCH", "/v1/organization-policies/{organization_id}")
        assert r.status_code == 400, (bad, r.text)
    r = ctx.client.patch("/v1/organization-policies/org_" + "Z" * 26, json=body, headers=ctx.as_user(OP, mfa=True))
    assert r.status_code == 404


def test_retention_policy_edit(ctx) -> None:
    r = ctx.client.get("/v1/retention-policies", headers=ctx.as_user(OP))
    check(r, "GET", "/v1/retention-policies")
    rows = {x["target"]: x for x in r.json()["items"]}
    locked = {t for t, x in rows.items() if x["action_locked"]}
    assert locked == {"EVIDENCE", "DATASET", "QUARANTINE:UNKNOWN_SESSION", "QUARANTINE:MISSING_REQUIRED_METADATA"}
    op = ctx.as_user(OP, mfa=True)
    r = ctx.client.patch("/v1/retention-policies/RAW", json={"retention_days": 1825, "reason": "보존 5년 확정 (합성)"},
                         headers=op)
    check(r, "PATCH", "/v1/retention-policies/{target}")
    assert r.status_code == 200 and r.json()["retention_days"] == 1825
    reason, details = _audit(ctx, "RETENTION_POLICY_CHANGED")[-1]
    assert reason == "보존 5년 확정 (합성)" and details["changes"] == {
        "retention_days": {"from": rows["RAW"]["retention_days"], "to": 1825}}
    with ctx.c.sessionmaker() as db:  # 새로 등록하는 자산부터 새 기간
        a = _asset(ORG_A, "prj_00000000000000000000000201", "dev_00000000000000000000000302")
        base = datetime(2026, 11, 5, tzinfo=UTC)
        apply_retention(db, a, base)
        assert a.retention_until == base + timedelta(days=1825)

    r = ctx.client.patch("/v1/retention-policies/EVIDENCE", json={"expiry_action": "DELETE", "retention_days": 30,
                                                                  "reason": "증빙 정리"}, headers=op)
    check(r, "PATCH", "/v1/retention-policies/{target}")
    assert r.status_code == 409  # 증빙·데이터셋 사본의 KEEP은 바꿀 수 없다
    assert ctx.client.patch("/v1/retention-policies/EVIDENCE", json={"notify_days_before": 10, "reason": "알림 조정"},
                            headers=op).status_code == 200
    assert ctx.client.patch("/v1/retention-policies/RAW", json={"expiry_action": "KEEP", "reason": "보류"},
                            headers=op).json()["expiry_action"] == "KEEP"
    r = ctx.client.patch("/v1/retention-policies/RAW", json={"expiry_action": "DELETE", "retention_days": None,
                                                             "reason": "삭제로 복귀"}, headers=op)
    assert r.status_code == 400 and r.json()["errors"][0]["pointer"] == "/retention_days"
    r = ctx.client.patch("/v1/retention-policies/QUARANTINE:UNKNOWN_SESSION",  # 자동 삭제하지 않는 격리 (domain.md 9장)
                         json={"expiry_action": "DELETE", "retention_days": 30, "reason": "자동 삭제"}, headers=op)
    assert r.status_code == 409
    assert ctx.client.patch("/v1/retention-policies/NOT_A_TARGET", json={"retention_days": 30, "reason": "없음"},
                            headers=op).status_code == 404
    assert ctx.client.patch("/v1/retention-policies/RAW", json={"retention_days": 30, "reason": "x"},
                            headers=ctx.as_user(OP)).json()["code"] == "MFA_REQUIRED"


def test_job_policy_priority_applies_to_queued_jobs(ctx) -> None:
    for i in range(2):
        ctx.c.queue.enqueue("make_proxy", {"asset_id": f"ast_{i}"}, organization_id=ORG_A)
    r = ctx.client.get("/v1/job-policies", headers=ctx.as_user(OP))
    check(r, "GET", "/v1/job-policies")
    proxy = next(x for x in r.json()["items"] if x["job_type"] == "make_proxy")
    assert proxy["queued_jobs"] == 2
    r = ctx.client.patch("/v1/job-policies/make_proxy", json={"priority": 95, "max_attempts": 4,
                                                              "reason": "프록시 적체 해소 (합성)"},
                         headers=ctx.as_user(OP, mfa=True))
    check(r, "PATCH", "/v1/job-policies/{job_type}")
    assert (r.json()["priority"], r.json()["max_attempts"], r.json()["queued_jobs"]) == (95, 4, 2)
    with ctx.c.sessionmaker() as db:  # 대기 중인 작업에도 바로 적용
        assert {j.priority for j in db.query(JobRow).filter(JobRow.job_type == "make_proxy")} == {95}
    new = ctx.c.queue.enqueue("make_proxy", {"asset_id": "ast_new"}, organization_id=ORG_A)
    with ctx.c.sessionmaker() as db:  # 최대 시도는 새로 등록하는 작업부터
        assert db.get(JobRow, new).max_attempts == 4
    reason, details = _audit(ctx, "JOB_POLICY_CHANGED")[-1]
    assert details["changes"] == {"priority": {"from": proxy["priority"], "to": 95},
                                  "max_attempts": {"from": proxy["max_attempts"], "to": 4}}
    for path, body, status in (("/v1/job-policies/nope", {"priority": 1, "reason": "없는 작업"}, 404),
                               ("/v1/job-policies/make_proxy", {"priority": 2000, "reason": "범위 밖"}, 400),
                               ("/v1/job-policies/make_proxy", {"priority": None, "reason": "비우기"}, 400)):
        r = ctx.client.patch(path, json=body, headers=ctx.as_user(OP, mfa=True))
        assert r.status_code == status, (path, body, r.text)
    assert ctx.client.get("/v1/job-policies", headers=ctx.as_user(ADMIN_A)).status_code == 403
    with ctx.c.sessionmaker() as db:
        assert db.get(Asset, "ast_0") is None  # 합성 작업만 넣었다 (자산 없음)


def test_policy_audit_is_written_only_when_the_change_commits(ctx, monkeypatch) -> None:
    """보안 검토 발견 3: 감사가 따로 커밋되어, 실패한 변경도 SUCCESS 감사로 남던 문제."""
    path, h = f"/v1/organization-policies/{ORG_A}", ctx.as_user(OP, mfa=True)
    r = ctx.client.patch(path, json={"quota_bytes": 2**63, "reason": "bigint 넘는 값 (합성)"}, headers=h)
    check(r, "PATCH", "/v1/organization-policies/{organization_id}")
    assert r.status_code == 400 and _audit(ctx, "ORG_POLICY_CHANGED") == []
    max_ok = ctx.client.patch(path, json={"quota_bytes": 2**63 - 1, "reason": "상한 값 (합성)"}, headers=h)
    assert max_ok.status_code == 200, max_ok.text
    assert len(_audit(ctx, "ORG_POLICY_CHANGED")) == 1

    # 감사를 쓴 뒤 요청이 실패하면 변경과 감사가 함께 롤백된다
    from app.api import admin_policies

    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("응답 만들기 실패 (합성)")

    monkeypatch.setattr(admin_policies, "_job_view", boom)
    with ctx.c.sessionmaker() as db:
        before = db.execute(text("SELECT priority FROM job_policies WHERE job_type = 'make_proxy'")).scalar_one()
    r = ctx.client.patch("/v1/job-policies/make_proxy", json={"priority": before + 1, "reason": "롤백 확인 (합성)"},
                         headers=h)
    assert r.status_code == 500
    with ctx.c.sessionmaker() as db:
        assert db.execute(text("SELECT priority FROM job_policies WHERE job_type = 'make_proxy'")).scalar_one() == before
    assert _audit(ctx, "JOB_POLICY_CHANGED") == []
