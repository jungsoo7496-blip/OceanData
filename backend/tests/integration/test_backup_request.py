"""08번 요구사항 6.4: 현장 사본이 없을 수 있는 장비의 자산 백업 신청 → PROTECTED·예상 용량·사용량 반영."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from app.db.models import Asset, Device, OrgQuota
from app.seed import DEV_A_CAMERA, DEV_A_SENSOR, DEV_B_CAMERA, ORG_A, PRJ_A, SES_A
from app.services import backup
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import T0, _asset

pytestmark = pytest.mark.integration
ADMIN_A, MEMBER_A = 101, 102
PATH = "/v1/backup-requests"


def _raw(ctx, *, size: int, session=SES_A, dev=DEV_A_CAMERA, **kw) -> str:  # type: ignore[no-untyped-def]
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, dev, session=session)
        a.storage_zone, a.storage_key, a.byte_size = "RAW", f"{ORG_A}/{a.asset_id}/ORIGINAL", size
        a.checksum_sha256 = hashlib.sha256(a.asset_id.encode()).hexdigest()
        for k, v in kw.items():
            setattr(a, k, v)
        db.add(a)
        return a.asset_id


def test_backup_request_dry_run_apply_quota_and_delete(ictx) -> None:
    ctx = ictx
    admin = ctx.as_user(ADMIN_A)
    with ctx.c.sessionmaker.begin() as db:
        db.get(Device, DEV_A_CAMERA).local_copy_policy = "ROTATING"  # NVR 순환 녹화
        db.get(Device, DEV_A_SENSOR).local_copy_policy = "RETAINED"
        q = db.get(OrgQuota, ORG_A)
        if q is None:
            db.add(OrgQuota(organization_id=ORG_A, quota_bytes=10_000, used_bytes=1_000, quarantine_bytes=0,
                            reserved_bytes=0, backup_bytes=0, updated_at=T0))
        else:
            q.quota_bytes, q.used_bytes, q.quarantine_bytes, q.reserved_bytes, q.backup_bytes = 10_000, 1_000, 0, 0, 0
    a1, a2 = _raw(ctx, size=3_000), _raw(ctx, size=2_000)
    other_session = _raw(ctx, size=500, session=None)
    _raw(ctx, size=700, backup_class="PROTECTED")          # 이미 보호됨 (증빙 인용 등) → 대상 아님
    _raw(ctx, size=900, ingest_status="QUARANTINED")       # 격리 → 대상 아님

    # 권한·대상 장비 규칙
    assert ctx.client.post(PATH, json={"device_id": DEV_A_CAMERA}, headers=ctx.as_user(MEMBER_A)).status_code == 403
    assert ctx.client.post(PATH, json={"device_id": DEV_B_CAMERA}, headers=admin).status_code == 404  # 다른 기관
    r = ctx.client.post(PATH, json={"device_id": DEV_A_SENSOR}, headers=admin)
    assert r.status_code == 422 and r.json()["code"] == "RULE_VIOLATION"  # 현장 원본 보관 장비
    assert ctx.client.post(PATH, json={"device_id": DEV_A_CAMERA, "captured_from": "2026-11-01T00:00:00"},
                           headers=admin).status_code == 400

    # 예상 (기본 dry_run): 바꾸지 않는다
    r = ctx.client.post(PATH, json={"device_id": DEV_A_CAMERA, "session_id": SES_A}, headers=admin)
    check(r, "POST", "/v1/backup-requests")
    est = r.json()
    assert (est["asset_count"], est["total_bytes"], est["applied"]) == (2, 5_000, False)
    assert est["usage"] == {"quota_bytes": 10_000, "used_total_bytes": 1_000, "after_bytes": 6_000}
    with ctx.c.sessionmaker() as db:
        assert db.get(Asset, a1).backup_class == "NONE"

    # 쿼터를 넘으면 거부
    with ctx.c.sessionmaker.begin() as db:
        db.get(OrgQuota, ORG_A).quota_bytes = 5_500
    r = ctx.client.post(PATH, json={"device_id": DEV_A_CAMERA, "session_id": SES_A, "dry_run": False}, headers=admin)
    assert r.status_code == 422 and "쿼터" in r.json()["detail"]
    with ctx.c.sessionmaker.begin() as db:
        db.get(OrgQuota, ORG_A).quota_bytes = 10_000

    # 적용: PROTECTED + 신청 시각, 백업 항목에 용량, 다음 백업 회차 대상, 감사
    r = ctx.client.post(PATH, json={"device_id": DEV_A_CAMERA, "session_id": SES_A, "dry_run": False}, headers=admin)
    check(r, "POST", "/v1/backup-requests")
    assert r.json()["applied"] is True and r.json()["asset_count"] == 2
    with ctx.c.sessionmaker() as db:
        for aid in (a1, a2):
            a = db.get(Asset, aid)
            assert a.backup_class == "PROTECTED" and a.backup_requested_at is not None
        assert db.get(Asset, other_session).backup_class == "NONE"  # 회차 밖
        assert db.get(OrgQuota, ORG_A).backup_bytes == 5_000
        planned = {i["asset_id"] for i in backup.plan(ctx.c, db) if i["asset_id"]}
        assert {a1, a2} <= planned and other_session not in planned
    assert "BACKUP_REQUESTED_BY_ORG" in ctx.audit_actions()
    usage = ctx.client.get("/v1/usage", headers=admin).json()["items"][0]
    assert usage["breakdown"]["backup_bytes"] == 5_000 and usage["usage_bytes"] == 6_000
    again = ctx.client.post(PATH, json={"device_id": DEV_A_CAMERA, "session_id": SES_A, "dry_run": False},
                            headers=admin).json()
    assert (again["asset_count"], again["applied"]) == (0, False)  # 이미 신청한 것은 다시 세지 않는다

    # 삭제하면 신청 백업 용량도 빠지고, 삭제 증명에 백업 사본 소멸일
    r = ctx.client.post(f"/v1/assets/{a1}/deletion", json={"reason": "시험 삭제"},
                        headers=ctx.as_user(ADMIN_A, mfa=True))
    assert r.status_code == 200 and r.json()["backup_copies_expire_at"]
    with ctx.c.sessionmaker() as db:
        assert db.get(OrgQuota, ORG_A).backup_bytes == 2_000
        assert db.execute(select(Asset.backup_requested_at).where(Asset.asset_id == a2)).scalar() is not None
