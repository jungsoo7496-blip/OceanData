"""11번 통합 시험: 감시 규칙 조정, 하드웨어 모의 신호, 처리·업로드·격리·용량 규칙, 정기 점검, 웹훅 본문."""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.adapters.system_clock import FixedClock
from app.db.models import Inspection, JobRow, OrgQuota, Upload, WatchRule
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, ORG_A, ORG_B, PRJ_A
from app.services import inspections, monitoring
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
OP = 106


def run_watch(ctx, at: datetime | None = None) -> monitoring.EvalResult:
    c = dataclasses.replace(ctx.c, clock=FixedClock(at or datetime.now(UTC)))
    with c.sessionmaker.begin() as db:
        return monitoring.evaluate(c, db)


def test_watch_rules_api(ctx) -> None:
    r = ctx.client.get("/v1/watch-rules", headers=ctx.as_user(OP))
    check(r, "GET", "/v1/watch-rules")
    rules = {x["rule_code"]: x for x in r.json()["items"]}
    assert len(rules) == 13 and all(r["data_source_ready"] for r in rules.values())  # 12번에서 Open API 신호원 연결
    assert rules["HEARTBEAT_MISSING"]["params"] == {"factor": 3, "min_silence_s": 300}
    assert ctx.client.get("/v1/watch-rules", headers=ctx.as_user(101)).status_code == 403
    path = "/v1/watch-rules/HEARTBEAT_MISSING"
    assert ctx.client.patch(path, json={"params": {"factor": 5}}, headers=ctx.as_user(OP)).status_code == 403  # MFA
    h = ctx.as_user(OP, mfa=True)
    r = ctx.client.patch(path, json={"params": {"factor": 5}, "severity": "WARNING"}, headers=h)
    check(r, "PATCH", "/v1/watch-rules/{rule_code}")
    assert r.json()["params"] == {"factor": 5, "min_silence_s": 300} and r.json()["severity"] == "WARNING"
    for bad in ({"params": {"nope": 1}}, {"params": {"factor": -1}}, {"params": {"min_silence_s": "x"}},
                {"realert_interval_s": 30}, {}, {"channels": ["SMS"]}):
        assert ctx.client.patch(path, json=bad, headers=h).status_code == 400, bad
    assert ctx.client.patch("/v1/watch-rules/NOPE", json={"enabled": False}, headers=h).status_code == 404
    assert "WATCH_RULE_UPDATED" in ctx.audit_actions()


def test_hardware_signal_mock_and_rule_disable(ctx) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    sig = {"host": "storage-01", "kind": "TEMPERATURE", "component": "cpu0", "value": 92.5, "unit": "degC",
           "status": "CRITICAL", "observed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert ctx.client.post("/v1/hardware-signals", json={"signals": [sig]}, headers=ctx.as_user(101)).status_code == 403
    r = ctx.client.post("/v1/hardware-signals", json={"signals": [sig]}, headers=ctx.as_user(OP))
    check(r, "POST", "/v1/hardware-signals")
    assert r.json() == {"accepted": 1}
    future = {**sig, "observed_at": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    assert ctx.client.post("/v1/hardware-signals", json={"signals": [future]}, headers=ctx.as_user(OP)).status_code == 400
    res = run_watch(ctx, now + timedelta(seconds=5))
    assert "HARDWARE_SIGNAL" in res.opened_rules
    # 기관 없는 운영 경보: 운영자만 본다
    op_items = ctx.client.get("/v1/incidents", params={"status": "OPEN"}, headers=ctx.as_user(OP)).json()["items"]
    hw = next(i for i in op_items if i["rule_code"] == "HARDWARE_SIGNAL")
    assert hw["organization_id"] is None and "92.5degC" in hw["summary"]
    org_items = ctx.client.get("/v1/incidents", headers=ctx.as_user(101)).json()["items"]
    assert all(i["organization_id"] == ORG_A for i in org_items)
    assert ctx.client.patch(f"/v1/incidents/{hw['incident_id']}", json={"memo": "x"},
                            headers=ctx.as_user(101)).status_code == 404
    # 규칙을 끄면 해제 알림 없이 종료
    ctx.c.notifier.sent.clear()
    assert ctx.client.patch("/v1/watch-rules/HARDWARE_SIGNAL", json={"enabled": False},
                            headers=ctx.as_user(OP, mfa=True)).status_code == 200
    res = run_watch(ctx, now + timedelta(seconds=10))
    assert hw["incident_id"] not in res.resolved
    assert not [n for n in ctx.c.notifier.sent if n.data.get("rule_code") == "HARDWARE_SIGNAL"]
    inc = ctx.client.get("/v1/incidents", params={"status": "RESOLVED"}, headers=ctx.as_user(OP)).json()["items"]
    assert any(i["incident_id"] == hw["incident_id"] and "비활성화" in i["memo"] for i in inc)
    # 해결된 장애는 다시 확인할 수 없다
    assert ctx.client.patch(f"/v1/incidents/{hw['incident_id']}", json={"status": "ACKNOWLEDGED"},
                            headers=ctx.as_user(OP)).status_code == 409


def _upload(org: str, status: str, error: str | None, at: datetime) -> Upload:
    return Upload(upload_id=new_id("upload"), organization_id=org, asset_id=None, source_system_id=DEV_A_CAMERA,
                  created_by="t", bucket="raw", object_key="k", s3_upload_id="u", part_size=1, part_count=1,
                  declared_size=1, reserved_bytes=0, status=status, error_code=error, expires_at=at, created_at=at,
                  updated_at=at)


def test_processing_upload_quarantine_storage_rules(ctx) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    old = now - timedelta(hours=2)
    with ctx.c.sessionmaker.begin() as db:
        db.add(JobRow(job_id=new_id("job"), job_type="make_proxy", payload={}, status="QUEUED", priority=0, attempts=0,
                      max_attempts=5, run_after=old, resource_class="GPU", created_at=old, updated_at=old))
        for i in range(6):
            db.add(_upload(ORG_A, "EXPIRED" if i < 3 else "RECEIVED", None, now - timedelta(minutes=10)))
        for _ in range(12):  # 최근 1시간 격리 12건, 평소 0건
            a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, session=None)
            a.ingest_status, a.quarantine_reason, a.ingested_at = "QUARANTINED", "CHECKSUM_MISMATCH", now
            a.byte_size = 10**9
            db.add(a)
        db.add(OrgQuota(organization_id=ORG_A, quota_bytes=20 * 10**9, used_bytes=12 * 10**9, quarantine_bytes=0,
                        reserved_bytes=0, backup_bytes=0, updated_at=now))
    res = run_watch(ctx, now)
    assert {"PROCESSING_BACKLOG", "UPLOAD_FAILURE_RATE", "QUARANTINE_SURGE", "STORAGE_FORECAST"} <= set(res.opened_rules)
    # 처리 대기는 운영자에게만
    pb = next(n for n in ctx.c.notifier.sent if n.data.get("rule_code") == "PROCESSING_BACKLOG")
    assert pb.recipients == ("operator2@example.invalid", "operator@example.invalid")
    # 수집 상태: 실패 원인 상위 목록 (요구사항 5.1)
    r = ctx.client.get("/v1/ingest-status", headers=ctx.as_user(OP))
    check(r, "GET", "/v1/ingest-status")
    top = r.json()["top_failure_reasons"]
    assert {"source": "QUARANTINE", "code": "CHECKSUM_MISMATCH", "count": 12} in top
    # 기관 범위는 인증 정보로만: 쿼리로 바꿀 수 없다 (security.md)
    assert ctx.client.get("/v1/ingest-status", params={"organization_id": ORG_B},
                          headers=ctx.as_user(OP)).status_code == 400
    r = ctx.client.get("/v1/ingest-status", headers=ctx.as_user(103))
    assert all(i["organization_id"] == ORG_B for i in r.json()["open_incidents"]) and r.json()["top_failure_reasons"] == []


def test_volume_drop(ctx) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    with ctx.c.sessionmaker.begin() as db:
        for d in range(1, 8):  # 지난 7일 하루 20건, 최근 24시간 0건
            for _ in range(20):
                a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
                a.ingested_at = now - timedelta(days=d, hours=2)
                db.add(a)
    res = run_watch(ctx, now)
    assert "VOLUME_DROP" in res.opened_rules


def test_webhook_channel_body(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_post(url, content, headers, timeout):  # noqa: ANN001
        calls.append({"url": url, "body": content, "headers": headers})
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(monitoring.httpx, "post", fake_post)
    settings = ctx.c.settings.model_copy(update={"alert_webhook_url": "https://ops.example.invalid/hook"})
    c = dataclasses.replace(ctx.c, settings=settings, clock=FixedClock(datetime.now(UTC)))
    with c.sessionmaker.begin() as db:
        db.execute(update(WatchRule).where(WatchRule.rule_code == "SESSION_NO_DATA")
                   .values(channels=["EMAIL", "WEBHOOK"]))
        monitoring.evaluate(c, db)
    assert calls and calls[0]["url"] == "https://ops.example.invalid/hook"
    body = calls[0]["body"].decode()
    assert '"type":"watch.opened"' in body and "http" not in body and "@" not in body
    assert "X-Signature" not in calls[0]["headers"]  # 비밀값이 없으면 서명 없음


def test_inspections_flow(ictx) -> None:
    ctx = ictx
    h = ctx.as_user(OP)
    r = ctx.client.get("/v1/inspections/templates", headers=h)
    check(r, "GET", "/v1/inspections/templates")
    assert {t["cycle"] for t in r.json()["items"]} == {"DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY"}
    assert ctx.client.get("/v1/inspections/templates", headers=ctx.as_user(101)).status_code == 403
    assert ctx.client.post("/v1/inspections", json={"cycle": "DAILY", "period_key": "2026-W45"},
                           headers=h).status_code == 400
    r = ctx.client.post("/v1/inspections", json={"cycle": "DAILY", "period_key": "2026-11-03", "assignee": "운영1"},
                        headers=h)
    check(r, "POST", "/v1/inspections")
    ins = r.json()
    items = {i["item_code"]: i for i in ins["items"]}
    assert items["D_BACKUP"]["result"] == "AUTO_UNAVAILABLE"  # 08번 백업 미구현
    assert items["D_INGEST"]["result"] in ("PASS", "FAIL") and items["D_INGEST"]["auto_detail"] is not None
    assert ins["due_at"] == "2026-11-03T15:00:00Z"  # KST 11-04 00:00
    assert ctx.client.post("/v1/inspections", json={"cycle": "DAILY", "period_key": "2026-11-03"},
                           headers=h).status_code == 409
    path = f"/v1/inspections/{ins['inspection_id']}"
    r = ctx.client.patch(path, json={"complete": True}, headers=h)
    assert r.status_code == 400  # 결과 없는 항목
    data = b"synthetic evidence"
    r = ctx.client.post(path + "/evidence", headers=h, json={
        "filename": "daily.pdf", "byte_size": len(data), "checksum_sha256": hashlib.sha256(data).hexdigest(),
        "item_code": "D_BACKUP"})
    check(r, "POST", "/v1/inspections/{inspection_id}/evidence")
    assert httpx.put(r.json()["upload_url"], content=data, timeout=30).status_code == 200
    results = [{"item_code": c, "result": "PASS"} for c in items]
    results[3] = {"item_code": "D_BACKUP", "result": "NOT_APPLICABLE", "note": "백업 장비 도입 전"}
    assert ctx.client.patch(path, json={"items": [{"item_code": "X", "result": "PASS"}]}, headers=h).status_code == 400
    r = ctx.client.patch(path, json={"items": results, "note": "이상 없음", "complete": True}, headers=h)
    check(r, "PATCH", "/v1/inspections/{inspection_id}")
    assert r.json()["status"] == "COMPLETED" and len(r.json()["evidence"]) == 1
    assert ctx.client.patch(path, json={"note": "x"}, headers=h).status_code == 409
    r = ctx.client.get("/v1/inspections", params={"cycle": "DAILY"}, headers=h)
    check(r, "GET", "/v1/inspections")
    assert [i["inspection_id"] for i in r.json()["items"]] == [ins["inspection_id"]]
    # 기한 지난 점검 알림: 완료한 2026-11-03은 빠진다
    at = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)  # KST 11-04 12:00
    with ctx.c.sessionmaker.begin() as db:
        overdue = inspections.notify_overdue(ctx.c, db, at)
        assert db.execute(select(Inspection.status)).scalar() == "COMPLETED"
    assert "DAILY:2026-11-03" not in overdue and "WEEKLY:2026-W44" in overdue and "YEARLY:2025" in overdue
    n = next(x for x in ctx.c.notifier.sent if x.kind == "inspection.overdue")
    assert n.recipients == ("operator2@example.invalid", "operator@example.invalid")
