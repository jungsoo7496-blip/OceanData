"""분류 지정이 자신·파생본 행을 잠그는 순서와 비용 (계약 점검 S11 검토 지적, ADR-0074).

- 잠금 순서는 원 자산 → 파생본(계보 단계마다 asset_id 순)이다. 삭제(retention.with_descendants)·클립 기록과 같고, 비식별
  검수 결정(deid.decide)도 원본 → 비식별본으로 잠근다. 반대 순서면 운영자의 분류 지정과 검수자의 결정이 교착해
  한쪽이 계약에 없는 409를 받았다.
- L0·L1 파생본은 분류로 내릴 일이 없으므로(L1 조건은 ORG_ADMIN 승인뿐) 사실을 모으지 않는다. 파생본이 많아도 문 수가
  늘지 않는다. 잠그기는 모두 잠근다 — 같은 때 L0·L1에서 L2 이상으로 올리는 신청이 바뀌기 전 분류를 보고 지나치지 않게.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError

from app.db.models import Asset, AssetLineage, DeidReview
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A
from app.services import deid
from tests.conftest import Ctx
from tests.integration.test_catalog import _asset
from tests.integration.test_classification_lowers_scope import OP, _classify, _scope, _shared_deid

pytestmark = pytest.mark.integration
NOW = datetime.now(UTC)


def _lock_waiters(ctx: Ctx) -> int:
    with ctx.c.engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                                     "AND wait_event_type = 'Lock'")).scalar_one())


def test_review_decision_and_classification_take_rows_in_the_same_order(ctx: Ctx) -> None:
    """검수 결정이 첫 행을 잠근 채 분류 지정이 들어와 기다리게 한 뒤 결정을 끝낸다. 둘 다 성공해야 한다
    (전: 결정은 비식별본 → 원본, 분류는 원본 → 비식별본이라 교착 → 분류 지정 409 CONFLICT)."""
    ids = _shared_deid(ctx)
    rid = new_id("review")
    with ctx.c.sessionmaker.begin() as db:
        db.add(DeidReview(review_id=rid, organization_id=ORG_A, session_id=None, original_asset_id=ids["orig"],
                          deid_asset_id=ids["deid"], reviewer_id="usr_합성검수자", risk_level="LOW", samples=[],
                          status="ASSIGNED", created_at=NOW))
    main, race = threading.get_ident(), {}

    def classify() -> None:
        race["response"] = ctx.client.patch(f"/v1/assets/{ids['orig']}/classification", headers=ctx.as_user(OP),
                                            json={"basis_ref": "점검-2026-11-12-02", "reason": "민감 해역 확인",
                                                  "sensitivity_class": "SECURITY"})

    def after_first_lock(_conn: Any, _cur: Any, statement: str, *_a: Any) -> None:
        if threading.get_ident() != main or "FOR UPDATE" not in statement or "thread" in race:
            return
        race["thread"] = t = threading.Thread(target=classify)
        t.start()
        deadline = time.monotonic() + 10
        while t.is_alive() and not _lock_waiters(ctx) and time.monotonic() < deadline:
            time.sleep(0.05)
        race["classify_waited"] = t.is_alive() and _lock_waiters(ctx) > 0

    event.listen(ctx.c.engine, "after_cursor_execute", after_first_lock)
    try:
        with ctx.c.sessionmaker.begin() as db:
            rv = db.get(DeidReview, rid)
            assert rv is not None
            deid.decide(ctx.c, db, rv, "PASS", [], [], None, {"REVIEWER"}, "usr_합성검수자")
        decided = "ok"
    except OperationalError as exc:  # 교착에서 이쪽이 중단된 경우
        decided = f"aborted: {exc.orig!r}"
    finally:
        event.remove(ctx.c.engine, "after_cursor_execute", after_first_lock)
    race["thread"].join(timeout=30)
    assert race["classify_waited"], "분류 지정이 행 잠금을 기다리지 않았다"
    r = race["response"]
    assert (decided, r.status_code) == ("ok", 200), r.text
    assert _scope(ctx, ids["deid"]) == "PROGRAM_REVIEW"  # 결정 뒤의 상태로 분류 지정이 판단했다


def _family(ctx: Ctx, n: int, scope: str = "PRIVATE") -> str:
    """BYPRODUCT 원본과 그 비식별본 n개 (합성)."""
    with ctx.c.sessionmaker.begin() as db:
        orig = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        orig.data_role = "BYPRODUCT"
        kids = [_asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="DEID", scope=scope) for _ in range(n)]
        db.add_all([orig, *kids])
        db.flush()
        db.add_all([AssetLineage(child_asset_id=k.asset_id, parent_asset_id=orig.asset_id, relation="DEID",
                                 created_at=NOW) for k in kids])
        return orig.asset_id


def _count_statements(ctx: Ctx, asset_id: str) -> int:
    seen: list[str] = []

    def count(_conn: Any, _cur: Any, statement: str, *_a: Any) -> None:
        seen.append(statement)

    event.listen(ctx.c.engine, "before_cursor_execute", count)
    try:
        _classify(ctx, asset_id, sensitivity_class="SECURITY")
    finally:
        event.remove(ctx.c.engine, "before_cursor_execute", count)
    return len(seen)


def test_private_derivatives_do_not_add_statements(ctx: Ctx) -> None:
    """L0·L1 파생본 수에 비례해 사실 수집 문이 늘지 않는다 (전: 파생본 하나에 10문 안팎, 400개면 약 4,000문)."""
    few, many = _family(ctx, 2), _family(ctx, 30)
    assert _count_statements(ctx, many) - _count_statements(ctx, few) <= 3


def test_descendants_are_locked_level_by_level(ctx: Ctx) -> None:
    """원본 → 프록시 → 썸네일: 썸네일 ID가 프록시보다 앞서도 프록시를 먼저 잠근다 (계보 단계 순, 단계 안에서는 ID 순).
    한 문장으로 ID 순 잠금이면 이 원본과 프록시를 동시에 분류 지정할 때 교착한다."""
    with ctx.c.sessionmaker.begin() as db:
        orig, proxy = _asset(ORG_A, PRJ_A, DEV_A_CAMERA), _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="PROXY")
        thumb = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="THUMBNAIL")
        thumb.asset_id = "ast_00000000000000000000000000"  # 합성: 어떤 새 ID보다도 앞선다
        db.add_all([orig, proxy, thumb])
        db.flush()
        db.add_all([AssetLineage(child_asset_id=proxy.asset_id, parent_asset_id=orig.asset_id, relation="PROXY",
                                 created_at=NOW),
                    AssetLineage(child_asset_id=thumb.asset_id, parent_asset_id=proxy.asset_id, relation="THUMBNAIL",
                                 created_at=NOW)])
        ids = (orig.asset_id, proxy.asset_id, thumb.asset_id)
    locked: list[set[str]] = []

    def capture(_conn: Any, _cur: Any, statement: str, params: Any, *_a: Any) -> None:
        if "FOR UPDATE" in statement and "FROM assets" in statement:
            values = params.values() if isinstance(params, dict) else params
            locked.append({v for v in values if isinstance(v, str) and v in ids})

    event.listen(ctx.c.engine, "before_cursor_execute", capture)
    try:
        _classify(ctx, ids[0], sensitivity_class="SECURITY")
    finally:
        event.remove(ctx.c.engine, "before_cursor_execute", capture)
    assert [x for x in locked if x] == [{ids[0]}, {ids[1]}, {ids[2]}]
    with ctx.c.sessionmaker() as db:  # 분류는 지정한 자산에만 (파생본의 행 값은 그대로)
        assert db.execute(select(Asset.sensitivity_class).where(Asset.asset_id == ids[2])).scalar_one() == "RESTRICTED"
