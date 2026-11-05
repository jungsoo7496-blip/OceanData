"""T15-f: 회차 미상(UNKNOWN_SESSION) 격리 자산은 30일이 지나도 자동 삭제되지 않는다 — ORG_ADMIN이 폐기를 확정할 때만 (domain.md 9장).

같은 30일이 지난 손상 격리(CORRUPT_FILE)는 보존 정책(quarantine_corrupt, 30일 DELETE)대로 지워져 대조가 된다. 값은 모두 합성이다.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db.models import Asset
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A
from app.services import retention
from app.services.retention import apply_retention
from tests.integration.test_catalog import T0, _asset

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]
ADMIN_A, MEMBER_A, OP = 101, 102, 106


def _quarantined(db, reason: str) -> Asset:  # type: ignore[no-untyped-def]
    a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, session=None)
    a.ingest_status, a.quarantine_reason, a.storage_zone = "QUARANTINED", reason, "QUARANTINE"
    a.storage_key = f"{ORG_A}/{PRJ_A}/_unassigned/{DEV_A_CAMERA}/{a.asset_id}/ORIGINAL"
    apply_retention(db, a, T0)  # 격리된 시각 기준 보존 기한 (정책 표)
    db.add(a)
    return a


@pytest.mark.covers("T15-f")
def test_t15f_unknown_session_quarantine_is_never_auto_deleted(ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        unknown, corrupt = _quarantined(db, "UNKNOWN_SESSION"), _quarantined(db, "CORRUPT_FILE")
        ids = (unknown.asset_id, corrupt.asset_id)
        assert unknown.retention_policy_id == "quarantine_unknown_session"
        assert corrupt.retention_until == T0 + timedelta(days=30)
    later = T0 + timedelta(days=31)

    # 31일 뒤: 만료 dry-run·실행 모두 손상 격리만 다룬다
    with ctx.c.sessionmaker() as db:
        dr = retention.dry_run(db, later)
    assert [t["target"] for t in dr["targets"]] == ["QUARANTINE:CORRUPT_FILE"] and dr["total_count"] == 1
    with ctx.c.sessionmaker.begin() as db:
        certs = retention.execute_expired(ctx.c, db, later)
    assert len(certs) == 1
    with ctx.c.sessionmaker() as db:
        assert db.get(Asset, ids[1]).deleted_at is not None  # 손상 격리는 정책대로 삭제
        assert db.get(Asset, ids[0]).deleted_at is None  # 회차 미상은 그대로 (손상 없는 원본일 수 있음)

    # 운영자가 정책으로 자동 삭제를 켤 수도 없다 (ADR-0038: 만료 동작 고정)
    r = ctx.client.patch("/v1/retention-policies/QUARANTINE:UNKNOWN_SESSION", headers=ctx.as_user(OP, mfa=True),
                         json={"expiry_action": "DELETE", "retention_days": 30, "reason": "자동 삭제 시도"})
    assert r.status_code == 409

    # 폐기는 ORG_ADMIN이 다중 인증으로 확정할 때만 (구성원은 못 함)
    path = f"/v1/assets/{ids[0]}/deletion"
    assert ctx.client.post(path, json={"reason": "폐기 확정"}, headers=ctx.as_user(MEMBER_A, mfa=True)).status_code == 403
    r = ctx.client.post(path, json={"reason": "회차를 찾을 수 없어 폐기 확정 (합성)"}, headers=ctx.as_user(ADMIN_A, mfa=True))
    assert r.status_code == 200, r.text
    with ctx.c.sessionmaker() as db:
        assert db.get(Asset, ids[0]).deleted_at is not None
