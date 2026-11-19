"""data_role CORE 변경이 원 자산·파생본 행을 잠그는 때와 순서 (통합 검토 ASSETS-1·ASSETS-3, ADR-0074·ADR-0076).

- ASSETS-1: CORE 변경은 파생본의 공유 등급을 내리면서 그 행을 먼저 잠그고(하향 뒤 첫 조회의 autoflush), 원 자산 행은
  마지막 flush에서야 잠갔다. 분류 지정(sharing.lock_family)·삭제·검수 결정은 원 자산 → 파생본 순이라 서로 교착해
  한쪽이 계약에 없는 409 CONFLICT를 받았다.
- ASSETS-3: CORE 변경은 파생본을 잠그지 않고 읽은 값으로 내릴지 정했다. 그사이 L1 파생본을 L2로 올리는 신청이
  확정되면(그 신청은 원 자산의 바꾸기 전 BYPRODUCT를 본다) CORE 원본의 파생본이 L2에 남았다 (domain.md 3장 위반).
이제 CORE 변경은 판단하기 전에 sharing.lock_family로 원 자산 → 파생본 순으로 잠그고 다시 읽는다.
시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.api import assets as assets_api
from app.db.models import Asset
from app.services import sharing
from tests.conftest import Ctx
from tests.integration.test_asset_patch_audit import _byproduct_with_shared_deid, _scope_and_grant
from tests.integration.test_classification_lowers_scope import _shared_deid
from tests.integration.test_sharing import share

pytestmark = pytest.mark.integration
ADMIN = 101
CORE = {"data_role": "CORE", "reason": "핵심 영상으로 재분류"}


def _lock_waiters(ctx: Ctx) -> int:
    with ctx.c.engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                                     "AND wait_event_type = 'Lock'")).scalar_one())


def _wait_blocked_or_done(ctx: Ctx, t: threading.Thread) -> None:
    deadline = time.monotonic() + 10
    while t.is_alive() and not _lock_waiters(ctx) and time.monotonic() < deadline:
        time.sleep(0.05)


def _row_free(ctx: Ctx, asset_id: str) -> bool:
    """다른 트랜잭션이 그 자산 행을 쥐고 있지 않은가 (기다리지 않고 잠가 보고 곧 놓는다)."""
    try:
        with ctx.c.engine.begin() as conn:
            conn.execute(text("SELECT asset_id FROM assets WHERE asset_id = :a FOR UPDATE NOWAIT"), {"a": asset_id})
    except DBAPIError:  # 55P03 lock_not_available
        return False
    return True


def test_core_change_takes_the_original_before_its_derivatives(ctx: Ctx) -> None:
    """분류 지정처럼 원 자산 → 파생본 순으로 잠그는 쪽이 원 자산을 쥔 동안 CORE 변경이 들어와 기다리게 한다.
    그때 파생본 행은 비어 있어야 하고, 이어서 파생본까지 잠근 쪽과 CORE 변경이 모두 성공해야 한다
    (전: CORE 변경이 파생본을 쥔 채 원 자산을 기다려 교착 → CORE 변경 409 CONFLICT)."""
    ids = _byproduct_with_shared_deid(ctx)  # 원본 share NONE·PRIVATE, 파생본 SANDBOX_PARTNERS + 이용 권한
    main, race = threading.get_ident(), {}

    def patch() -> None:
        race["response"] = ctx.client.patch(f"/v1/assets/{ids['orig']}", json=CORE,
                                            headers=ctx.as_user(ADMIN, mfa=True))

    def after_first_lock(_conn: Any, _cur: Any, statement: str, *_a: Any) -> None:
        if threading.get_ident() != main or "FOR UPDATE" not in statement or "thread" in race:
            return
        race["thread"] = t = threading.Thread(target=patch)
        t.start()
        _wait_blocked_or_done(ctx, t)
        race["patch_waited"] = t.is_alive() and _lock_waiters(ctx) > 0
        race["derivative_free"] = _row_free(ctx, ids["deid"])

    event.listen(ctx.c.engine, "after_cursor_execute", after_first_lock)
    try:
        with ctx.c.sessionmaker.begin() as db:
            orig = db.get(Asset, ids["orig"])
            assert orig is not None
            sharing.lock_family(db, orig)  # 분류 지정과 같은 잠금 (원 자산 → 파생본)
        family = "ok"
    except OperationalError as exc:  # 교착에서 이쪽이 중단된 경우
        family = f"aborted: {exc.orig!r}"
    finally:
        event.remove(ctx.c.engine, "after_cursor_execute", after_first_lock)
    race["thread"].join(timeout=30)
    assert race["patch_waited"], "CORE 변경이 행 잠금을 기다리지 않았다"
    assert race["derivative_free"], "CORE 변경이 원 자산을 기다리면서 파생본 행을 이미 쥐고 있다 (잠금 순서 뒤집힘)"
    r = race["response"]
    assert (family, r.status_code) == ("ok", 200), r.text
    assert _scope_and_grant(ctx, ids) == ("PROGRAM_REVIEW", "REVOKED")


def test_upgrade_overlapping_a_core_change_cannot_leave_the_derivative_shared(ctx: Ctx, monkeypatch) -> None:
    """CORE 변경이 판단과 flush를 마친 뒤(감사 직전) 같은 기관 ORG_ADMIN의 L1 → L2 상향 신청이 들어온다. 그 신청은
    CORE 변경의 확정을 기다린 뒤 'CORE 원본의 파생본'이라 422여야 하고, 파생본은 L1에 남아야 한다. 전에는 CORE 변경이
    파생본을 잠그지 않아 신청이 바뀌기 전 BYPRODUCT를 보고 곧바로 적용됐다 — 끝 상태가 CORE 원본 + L2 파생본."""
    ids = _shared_deid(ctx)  # 정상 흐름으로 비식별본 SANDBOX_PARTNERS
    down = share(ctx, ids["deid"], ADMIN, target_scope="PROGRAM_REVIEW", existing_grant_handling="KEEP_UNTIL_EXPIRY")
    assert down.json()["state"] == "APPLIED", down.text
    real, race = assets_api._audit_reclassified, {}

    def upgrade() -> None:
        race["upgrade"] = share(ctx, ids["deid"], ADMIN, target_scope="SANDBOX_PARTNERS")

    def overlap_then_audit(*a: Any, **k: Any) -> None:
        race["thread"] = t = threading.Thread(target=upgrade)
        t.start()
        _wait_blocked_or_done(ctx, t)  # 상향이 끝났거나(전) CORE 변경이 쥔 파생본 행을 기다린다(후)
        real(*a, **k)

    monkeypatch.setattr(assets_api, "_audit_reclassified", overlap_then_audit)
    r = ctx.client.patch(f"/v1/assets/{ids['orig']}", json=CORE, headers=ctx.as_user(ADMIN, mfa=True))
    race["thread"].join(timeout=30)
    assert r.status_code == 200, r.text
    with ctx.c.sessionmaker() as db:
        final = (db.get(Asset, ids["orig"]).data_role, db.get(Asset, ids["deid"]).sharing_scope)
    assert final == ("CORE", "PROGRAM_REVIEW"), "CORE 원본의 파생본이 L1을 넘었다 (겹친 상향이 바뀌기 전 분류를 봤다)"
    up = race["upgrade"]
    assert (up.status_code, up.json()["code"]) == (422, "RULE_VIOLATION"), up.text
