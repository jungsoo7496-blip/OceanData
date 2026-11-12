"""같은 회차 재검수 표시 (14번 요구사항 4.4, 계약 점검 S20, ADR-0075).

미탐 결정은 같은 회차의 다른 통과 비식별본을 재검수 대상으로 '표시'한다. 이미 열린 검수가 있는 비식별본에 또 만들지 않고,
결정 중인 비식별본 자신과 이미 불합격(FAILED)인 비식별본은 다시 열지 않는다. 합성 자산만 쓴다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import Asset, AssetLineage, DeidReview
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A, SES_A, fid
from tests.integration.test_catalog import _asset

pytestmark = [pytest.mark.integration]
REVIEWER = 108
T0 = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
MISSED = {"result": "MISSED", "missed": [{"cls": "FACE", "t": 0.5}]}


def _mk(db: Any, status: str) -> tuple[str, str]:
    """QUALITY_REVIEW 원본의 DEID 파생본과 회차 SES_A의 검수 (status=PASSED면 파생본도 PASSED)."""
    o = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
    o.data_role, o.sensitivity_class, o.share_status, o.legal_basis_ref = "BYPRODUCT", "INTERNAL", "QUALITY_REVIEW", "LB"
    o.storage_zone, o.storage_key = "RAW", f"{ORG_A}/raw/{o.asset_id}.mp4"
    ch = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="DEID")
    ch.storage_zone, ch.storage_key = "PROCESSED", f"{ORG_A}/proc/{ch.asset_id}.mp4"
    ch.review_result = "PASSED" if status == "PASSED" else None
    db.add_all([o, ch])
    db.flush()
    db.add(AssetLineage(child_asset_id=ch.asset_id, parent_asset_id=o.asset_id, relation="DEID",
                        tool_version="t", created_at=T0))
    rv = DeidReview(review_id=new_id("review"), organization_id=ORG_A, session_id=SES_A,
                    original_asset_id=o.asset_id, deid_asset_id=ch.asset_id, reviewer_id=fid("usr", REVIEWER),
                    risk_level="LOW", samples=[0.5], recheck=False, status=status, missed=[], manual_regions=[],
                    created_at=T0)
    db.add(rv)
    db.flush()
    return ch.asset_id, rv.review_id


def _decide(ctx: Any, rid: str) -> Any:
    r = ctx.client.post(f"/v1/deid-reviews/{rid}/decision", json=MISSED, headers=ctx.as_user(REVIEWER))
    assert r.status_code == 200, r.text
    return r


def _reviews(ctx: Any, child: str) -> list[tuple[str, str, bool]]:
    with ctx.c.sessionmaker() as db:
        return [(r.review_id, r.status, r.recheck) for r in db.execute(
            select(DeidReview).where(DeidReview.deid_asset_id == child)
            .order_by(DeidReview.created_at, DeidReview.review_id)).scalars()]


def _result(ctx: Any, child: str) -> str | None:
    with ctx.c.sessionmaker() as db:
        a = db.get(Asset, child)
        assert a is not None
        return a.review_result


def test_second_missed_reuses_open_recheck(ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        a_child, a_rid = _mk(db, "PASSED")
        _, b_rid = _mk(db, "ASSIGNED")
        _, c_rid = _mk(db, "ASSIGNED")
    first = _decide(ctx, b_rid).json()["recheck_review_ids"]
    second = _decide(ctx, c_rid).json()["recheck_review_ids"]
    rows = _reviews(ctx, a_child)
    opened = [rid for rid, status, _ in rows if status == "ASSIGNED"]
    assert len(opened) == 1, f"같은 비식별본에 열린 재검수 {len(opened)}건"
    assert [(s, rc) for _, s, rc in rows] == [("PASSED", False), ("ASSIGNED", True)]
    assert first == second == opened  # 두 번째 결정도 이미 표시된 재검수를 가리킨다
    assert _result(ctx, a_child) is None


def test_missed_recheck_does_not_reopen_itself_or_failed(ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        a_child, a_rid = _mk(db, "PASSED")
        _, b_rid = _mk(db, "ASSIGNED")
        _, c_rid = _mk(db, "ASSIGNED")
    (recheck,) = _decide(ctx, b_rid).json()["recheck_review_ids"]
    # 재검수에서 A의 비식별본도 미탐 → A는 불합격. 자기 자신의 옛 PASSED 검수로 다시 재검수를 열지 않는다
    assert _decide(ctx, recheck).json()["recheck_review_ids"] == []
    assert [s for _, s, _ in _reviews(ctx, a_child)] == ["PASSED", "MISSED"]
    assert _result(ctx, a_child) == "FAILED"
    # 같은 회차의 다음 미탐도 이미 불합격인 A를 재검수 대기(None)로 되돌리지 않는다
    assert _decide(ctx, c_rid).json()["recheck_review_ids"] == []
    assert [s for _, s, _ in _reviews(ctx, a_child)] == ["PASSED", "MISSED"]
    assert _result(ctx, a_child) == "FAILED"
