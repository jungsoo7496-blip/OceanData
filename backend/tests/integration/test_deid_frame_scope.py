"""운영자의 검수 프레임 범위 (계약 점검 S17, domain.md 2장 PLATFORM_OPERATOR 바이너리 열).

- 원본 프레임: 다른 기관 자산은 긴급 열람 승인으로만 본다. 거부도 PERMISSION_DENIED로 감사한다 (security.md 권한 거부).
합성 자산만 쓴다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from app.db.models import AssetLineage, DeidReview
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A, SES_A, fid
from tests.integration.test_catalog import _asset

pytestmark = [pytest.mark.integration]
OP, REVIEWER = 106, 108
T0 = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)


def _pair(db: Any, share: str, data: bytes = b"") -> tuple[str, str, str]:
    """공유 심사 상태가 share인 BYPRODUCT 원본, 그 DEID 파생본, 검수자 108에게 배정된 검수."""
    o = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
    o.data_role, o.sensitivity_class, o.share_status, o.legal_basis_ref = "BYPRODUCT", "INTERNAL", share, "LB"
    o.storage_zone, o.storage_key = "RAW", f"{ORG_A}/raw/{o.asset_id}.mp4"
    ch = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="DEID")
    ch.storage_zone, ch.storage_key = "PROCESSED", f"{ORG_A}/proc/{ch.asset_id}.mp4"
    if data:
        ch.byte_size = len(data)  # 체크섬은 자산마다 다르게 둔다 (같은 영상을 여러 파생본에 올린다)
    db.add_all([o, ch])
    db.flush()
    db.add(AssetLineage(child_asset_id=ch.asset_id, parent_asset_id=o.asset_id, relation="DEID",
                        tool_version="t", created_at=T0))
    rv = DeidReview(review_id=new_id("review"), organization_id=ORG_A, session_id=SES_A,
                    original_asset_id=o.asset_id, deid_asset_id=ch.asset_id, reviewer_id=fid("usr", REVIEWER),
                    risk_level="LOW", samples=[0.5], recheck=False, status="ASSIGNED", missed=[], manual_regions=[],
                    created_at=T0)
    db.add(rv)
    db.flush()
    return o.asset_id, ch.asset_id, rv.review_id


def _audit(ctx: Any, r: Any) -> list[tuple[str, str, str, dict]]:
    """그 요청 ID로 남은 감사 행 (거절된 요청이라도 남아야 한다)."""
    with ctx.c.engine.connect() as conn:
        return [(a, res, t, d) for a, res, t, d in conn.execute(text(
            "SELECT action, result, target_id, details FROM audit_log WHERE request_id=:r ORDER BY seq"),
            {"r": r.headers["x-request-id"]})]


def _frame(ctx: Any, rid: str, user: int, which: str = "DEID") -> Any:
    return ctx.client.get(f"/v1/deid-reviews/{rid}/frame", params={"t": 0.5, "which": which},
                          headers=ctx.as_user(user))


def test_operator_original_frame_without_grant_is_audited(ctx) -> None:
    """S17: 승인 없는 운영자의 원본 프레임 403에도 PERMISSION_DENIED가 남는다 (요청은 실패해도)."""
    with ctx.c.sessionmaker.begin() as db:
        oid, _, rid = _pair(db, "QUALITY_REVIEW")
    r = _frame(ctx, rid, OP, "ORIGINAL")
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    rows = _audit(ctx, r)
    assert [(a, res, t) for a, res, t, _ in rows] == [("PERMISSION_DENIED", "DENIED", oid)]
    assert rows[0][3] == {"action": "deid_review:read", "which": "ORIGINAL", "reason": "no_break_glass_grant",
                          "review_id": rid}
