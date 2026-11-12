"""운영자의 검수 프레임 범위 (계약 점검 S17·S19, domain.md 2장 PLATFORM_OPERATOR 바이너리 열, ADR-0075).

- 원본 프레임: 다른 기관 자산은 긴급 열람 승인으로만 본다. 거부도 PERMISSION_DENIED로 감사한다 (security.md 권한 거부).
- 비식별본 프레임: 원본이 공유 심사 중(PROCESSING·QUALITY_REVIEW·REPROCESS)일 때만 본다. 배정 검수자는 그대로 본다.
합성 영상과 합성 자산만 쓴다.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from app.db.models import Asset, AssetLineage, DeidReview
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A, SES_A, fid
from tests.integration.test_catalog import _asset

pytestmark = [pytest.mark.integration]
ADMIN, OP, REVIEWER = 101, 106, 108
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


def _video(tmp: Path) -> bytes:
    out = tmp / "v.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=160x120:rate=10", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
                   check=True)
    return out.read_bytes()


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


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg")
def test_operator_deid_frame_only_while_share_under_review(ictx, tmp_path: Path) -> None:
    """S19: 공유 철회 뒤에는 운영자가 비식별본 프레임을 받지 못한다. 검수 통과 뒤라도 QUALITY_REVIEW면 본다."""
    ctx = ictx
    data = _video(tmp_path)
    with ctx.c.sessionmaker.begin() as db:
        oid, cid, rid = _pair(db, "QUALITY_REVIEW", data)
        key = db.get(Asset, cid).storage_key
    ctx.c.storage.put_object(ctx.c.settings.bucket_proc, key, data, "video/mp4")

    r = _frame(ctx, rid, OP)
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"  # 검수 중
    assert ctx.client.post(f"/v1/deid-reviews/{rid}/decision", json={"result": "PASS"},
                           headers=ctx.as_user(REVIEWER)).status_code == 200
    assert _frame(ctx, rid, OP).status_code == 200  # 운영자가 QUALITY_REVIEW → APPROVED를 판단할 때 본다
    w = ctx.client.post(f"/v1/assets/{oid}/share-review", json={"action": "WITHDRAW", "reason": "공유 철회"},
                        headers=ctx.as_user(ADMIN, mfa=True))
    assert w.status_code == 200 and w.json()["share_status"] == "NONE"

    r = _frame(ctx, rid, OP)
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    rows = _audit(ctx, r)
    assert [(a, res, t) for a, res, t, _ in rows] == [("PERMISSION_DENIED", "DENIED", cid)]
    assert rows[0][3] == {"action": "deid_review:read", "which": "DEID", "reason": "not_under_review",
                          "review_id": rid}
    assert _frame(ctx, rid, REVIEWER).status_code == 200  # 배정 검수자는 그대로 (domain.md 2장 REVIEWER 행)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg")
def test_operator_deid_frame_by_share_status(ictx, tmp_path: Path) -> None:
    """S19: '심사 중'은 원본의 share_status가 PROCESSING·QUALITY_REVIEW·REPROCESS인 동안이다 (ADR-0075)."""
    ctx = ictx
    data = _video(tmp_path)
    want = {"PROCESSING": 200, "QUALITY_REVIEW": 200, "REPROCESS": 200,
            "NONE": 403, "CANDIDATE": 403, "LEGAL_REVIEW": 403, "APPROVED": 403, "REJECTED": 403}
    rids: dict[str, str] = {}
    with ctx.c.sessionmaker.begin() as db:
        for share in want:
            _, cid, rids[share] = _pair(db, share, data)
            ctx.c.storage.put_object(ctx.c.settings.bucket_proc, db.get(Asset, cid).storage_key, data, "video/mp4")
    got = {share: _frame(ctx, rid, OP).status_code for share, rid in rids.items()}
    assert got == want
    assert {share: _frame(ctx, rid, REVIEWER).status_code for share, rid in rids.items()} == dict.fromkeys(want, 200)
