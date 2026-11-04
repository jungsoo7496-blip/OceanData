"""08번 통합 시험: 보존 정책 계산, dry-run·만료 삭제, 법적 보류, 삭제 요청·증명, 쿼터 알림·사용량, 일일 대조."""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text

from app.db.models import Asset, OrgQuota
from app.seed import DEV_A_CAMERA, DEV_A_SENSOR, ORG_A, SES_A
from app.services import retention
from app.workers import main as worker
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_pipeline import synth_video
from tests.integration.test_pipeline import upload as upload_video

pytestmark = pytest.mark.integration


def upload_csv(ctx, data: bytes, *, session: str | None = SES_A, sha: str | None = None) -> str:
    h = ctx.as_user(102)
    m = {"schema_version": "1.0.0", "source_system_id": DEV_A_SENSOR, "data_type": "SENSOR", "variant": "ORIGINAL",
         "timestamp_quality": {"source": "UNKNOWN", "verified": False},
         "file": {"original_filename": "a.csv", "byte_size": len(data), "mime_type": "text/csv",
                  "checksum_sha256": sha or hashlib.sha256(data).hexdigest()}, "client": {"kind": "API", "version": "t"}}
    if session:
        m["session_id"] = session
    created = ctx.client.post("/v1/uploads", json=m, headers=h).json()
    if created["status"] != "UPLOAD_CREATED":
        return created
    r = httpx.put(created["parts"][0]["url"], content=data)
    ctx.client.post(f"/v1/uploads/{created['upload_id']}/complete",
                    json={"parts": [{"part_number": 1, "etag": r.headers["etag"]}]}, headers=h)
    while worker.run_once(ctx.c, "t"):
        pass
    return created["asset_id"]


def asset(ctx, asset_id: str) -> Asset:
    with ctx.c.sessionmaker() as db:
        return db.get(Asset, asset_id)


def test_retention_policies_applied(ictx) -> None:
    ok = upload_csv(ictx, b"t,v\n1,2\n")
    a = asset(ictx, ok)
    assert a.retention_policy_id == "raw_default"
    assert abs((a.retention_until - datetime.now(UTC)).days - 3650) <= 1
    damaged = upload_csv(ictx, b"t,v\n3,4\n", sha=hashlib.sha256(b"other").hexdigest())
    assert asset(ictx, damaged).retention_policy_id == "quarantine_damaged"
    unknown = upload_csv(ictx, b"t,v\n5,6\n", session=None)
    u = asset(ictx, unknown)
    assert (u.quarantine_reason, u.retention_policy_id, u.retention_until.year) == (
        "UNKNOWN_SESSION", "quarantine_unknown_session", 9999)  # 회차 미상은 자동 삭제하지 않는다


@pytest.mark.covers("T15-a")
def test_dry_run_legal_hold_and_expiry_execution(ictx) -> None:
    a1, a2 = upload_csv(ictx, b"t,v\n1,1\n"), upload_csv(ictx, b"t,v\n2,2\n")
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE assets SET retention_until = now() - interval '1 day' WHERE asset_id IN (:a, :b)"),
                     {"a": a1, "b": a2})
    op = ictx.as_user(106, mfa=True)
    assert ictx.client.post(f"/v1/assets/{a2}/legal-hold", json={"hold": True, "reason": "소송 대응"},
                            headers=ictx.as_user(106)).json()["code"] == "MFA_REQUIRED"
    assert ictx.client.post(f"/v1/assets/{a2}/legal-hold", json={"hold": True, "reason": "소송 대응"},
                            headers=ictx.as_user(101, mfa=True)).status_code == 403
    r = ictx.client.post(f"/v1/assets/{a2}/legal-hold", json={"hold": True, "reason": "소송 대응"}, headers=op)
    check(r, "POST", "/v1/assets/{asset_id}/legal-hold")
    assert "LEGAL_HOLD_SET" in ictx.audit_actions()
    assert ictx.client.post("/v1/retention/dry-run", headers=ictx.as_user(101)).status_code == 403
    dr = ictx.client.post("/v1/retention/dry-run", headers=ictx.as_user(106))
    check(dr, "POST", "/v1/retention/dry-run")
    body = dr.json()
    assert (body["total_count"], body["excluded_legal_hold"]) == (1, 1)
    assert body["targets"] == [{"target": "RAW", "count": 1, "bytes": 8}]
    assert asset(ictx, a1).deleted_at is None  # dry-run은 지우지 않는다
    with ictx.c.sessionmaker.begin() as db:
        certs = retention.execute_expired(ictx.c, db, ictx.c.clock.now())
    assert len(certs) == 1 and certs[0].scope == "RETENTION" and certs[0].objects[0]["asset_id"] == a1
    gone = asset(ictx, a1)
    assert gone.deleted_at is not None and gone.storage_key is None
    assert ictx.client.get(f"/v1/assets/{a1}", headers=ictx.as_user(102)).status_code == 404
    assert asset(ictx, a2).deleted_at is None  # 법적 보류 자산은 남는다
    r = ictx.client.post(f"/v1/assets/{a2}/deletion", json={"reason": "정리"}, headers=ictx.as_user(101, mfa=True))
    assert r.status_code == 409 and r.json()["affected"][0]["reason"] == "LEGAL_HOLD"


@pytest.mark.covers("T15-b")
def test_asset_deletion_with_derivatives_certificate_and_reupload(ictx, tmp_path) -> None:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg가 필요합니다")
    data = synth_video(tmp_path, seconds=2)
    vid = upload_video(ictx, data)
    while worker.run_once(ictx.c, "t"):
        pass
    with ictx.c.sessionmaker() as db:
        before = db.get(OrgQuota, ORG_A).used_bytes
        keys = [(a.storage_zone, a.storage_key) for a in db.execute(select(Asset)).scalars()]
    assert len(keys) == 3  # 원본 + 프록시 + 썸네일
    admin = ictx.as_user(101, mfa=True)
    assert ictx.client.post(f"/v1/assets/{vid}/deletion", json={"reason": "요청"},
                            headers=ictx.as_user(101)).json()["code"] == "MFA_REQUIRED"
    assert ictx.client.post(f"/v1/assets/{vid}/deletion", json={"reason": "요청"},
                            headers=ictx.as_user(102)).status_code == 403
    r = ictx.client.post(f"/v1/assets/{vid}/deletion", json={"reason": "기업 요청으로 삭제"}, headers=admin)
    check(r, "POST", "/v1/assets/{asset_id}/deletion")
    cert = r.json()
    assert cert["scope"] == "ASSET" and len(cert["objects"]) == 3
    assert cert["objects"][0]["checksum_sha256"] and cert["backup_copies_expire_at"] is None
    for zone, key in keys:  # 활성 객체가 모두 지워졌다
        assert ictx.c.storage.head_object(ictx.c.settings.buckets[zone], key) is None
    with ictx.c.sessionmaker() as db:
        assert db.get(OrgQuota, ORG_A).used_bytes == before - sum(o["byte_size"] for o in cert["objects"])
    g = ictx.client.get(f"/v1/deletion-certificates/{cert['certificate_id']}", headers=ictx.as_user(101))
    check(g, "GET", "/v1/deletion-certificates/{certificate_id}")
    assert ictx.client.get(f"/v1/deletion-certificates/{cert['certificate_id']}",
                           headers=ictx.as_user(103)).status_code == 404  # T01
    assert "ASSET_DELETED" in ictx.audit_actions()
    # 삭제 뒤 같은 파일을 다시 받을 수 있다 (멱등 키는 살아 있는 자산에만)
    again = upload_video(ictx, data)
    assert again != vid


def test_session_deletion_and_cross_org(ictx) -> None:
    a1, a2 = upload_csv(ictx, b"t,v\n7,7\n"), upload_csv(ictx, b"t,v\n8,8\n")
    assert ictx.client.post(f"/v1/sessions/{SES_A}/deletion", json={"reason": "다른 기관 시도"},
                            headers=ictx.as_user(103, mfa=True)).status_code == 404
    r = ictx.client.post(f"/v1/sessions/{SES_A}/deletion", json={"reason": "회차 폐기"},
                         headers=ictx.as_user(101, mfa=True))
    check(r, "POST", "/v1/sessions/{session_id}/deletion")
    assert {o["asset_id"] for o in r.json()["objects"]} == {a1, a2}
    assert ictx.client.get("/v1/assets", headers=ictx.as_user(102)).json()["items"] == []


def test_quota_alerts_usage_and_reconcile(ictx) -> None:
    data = b"x" * 1000
    upload_csv(ictx, b"t,v\n9,9\n")  # 쿼터 행 생성
    with ictx.c.sessionmaker.begin() as db:
        q = db.get(OrgQuota, ORG_A)
        q.quota_bytes = q.used_bytes + 1200
    m = {"schema_version": "1.0.0", "source_system_id": DEV_A_CAMERA, "data_type": "VIDEO", "variant": "CLIP",
         "session_id": SES_A, "timestamp_quality": {"source": "UNKNOWN", "verified": False},
         "file": {"original_filename": "c.mp4", "byte_size": len(data), "mime_type": "video/mp4",
                  "checksum_sha256": hashlib.sha256(data).hexdigest()}, "client": {"kind": "API", "version": "t"}}
    assert ictx.client.post("/v1/uploads", json=m, headers=ictx.as_user(102)).status_code == 201
    alerts = [n for n in ictx.c.notifier.sent if n.kind == "quota.threshold"]
    assert [n.data["threshold"] for n in alerts] == [80] and alerts[0].recipients == ("admin-a@example.invalid",)
    u = ictx.client.get("/v1/usage", headers=ictx.as_user(102))
    check(u, "GET", "/v1/usage")
    item = u.json()["items"][0]
    assert item["organization_id"] == ORG_A and item["breakdown"]["reserved_bytes"] == 1000
    assert ictx.client.get("/v1/usage", headers=ictx.as_user(104)).json()["items"][0]["organization_id"] != ORG_A
    # 일일 대조: 스토리지에서 객체가 사라지면 차이를 알린다 (하루 한 번만 실행)
    assert retention.run_daily(ictx.c, "quota_reconcile", lambda db: {"diffs": retention.reconcile(ictx.c, db)}) \
        == {"diffs": []}
    assert retention.run_daily(ictx.c, "quota_reconcile", lambda db: {"diffs": ["again"]}) is None
    with ictx.c.sessionmaker() as db:
        a = db.execute(select(Asset).where(Asset.storage_key.isnot(None))).scalars().first()
    ictx.c.storage.delete_object(ictx.c.settings.buckets[a.storage_zone], a.storage_key)
    with ictx.c.sessionmaker.begin() as db:
        diffs = retention.reconcile(ictx.c, db)
    assert diffs and diffs[0]["organization_id"] == ORG_A
    assert any(n.kind == "quota.reconcile_mismatch" for n in ictx.c.notifier.sent)


def test_expiry_notification(ictx) -> None:
    a = upload_csv(ictx, b"t,v\n10,10\n")
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE assets SET retention_until = now() + interval '5 days' WHERE asset_id = :a"), {"a": a})
    with ictx.c.sessionmaker.begin() as db:
        assert retention.notify_expiring(ictx.c, db, datetime.now(UTC)) == 1
    n = [x for x in ictx.c.notifier.sent if x.kind == "retention.expiring"][-1]
    assert n.data["count"] == 1 and n.organization_id == ORG_A
    assert timedelta(days=1)
