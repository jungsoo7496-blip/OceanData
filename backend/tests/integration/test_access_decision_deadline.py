"""응답 기한이 지난 이용 신청의 승인은 409 — 워커 정비 주기를 기다리지 않는다 (13번 요구사항 8.3, 계약 점검 S10).

기한 판단은 정비 주기(`datasets.expire`)와 같은 비교(`deadline_at < 지금`)를 쓴다. 기한 뒤 거절은 받는다
(끝 상태가 기본값 거부와 같다).
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.adapters.system_clock import FixedClock
from app.db.models import AccessDecision, AccessRequest, DatasetVersion, Grant
from app.seed import ORG_A, fid
from app.services import datasets
from tests.acceptance.test_t08_t15_sharing import access, decide
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract
from tests.integration.test_grant_access_request_tenant_audit import ADMIN_A, OP, published_version

pytestmark = pytest.mark.integration
PATH = "/v1/access-requests/{access_request_id}/decisions"


def _past_deadline(ctx: Ctx, rid: str) -> None:
    """응답 기한은 지났고 정비 주기는 아직 돌지 않은 상태 (앱 시계 기준)."""
    with ctx.c.sessionmaker.begin() as db:
        db.execute(update(AccessRequest).where(AccessRequest.access_request_id == rid)
                   .values(deadline_at=ctx.c.clock.now() - timedelta(minutes=5)))


def _roles(db, rid: str) -> list[str]:
    return list(db.scalars(select(AccessDecision.role).where(AccessDecision.access_request_id == rid)
                           .order_by(AccessDecision.decided_at)))


def test_approvals_after_deadline_are_refused(ictx: Ctx) -> None:
    ctx = ictx
    _, vid = published_version(ctx)
    on_time = access(ctx, vid, "AI_TRAINING").json()["access_request_id"]  # 기한이 가장 이르다 (먼저 만듦)
    late = access(ctx, vid, "AI_TRAINING").json()["access_request_id"]
    waiting = access(ctx, vid, "AI_TRAINING").json()["access_request_id"]
    refused = access(ctx, vid, "AI_TRAINING").json()["access_request_id"]
    assert decide(ctx, ADMIN_A, waiting, "APPROVE").json()["status"] == "PROVIDER_APPROVED"

    # 기한 그 시각의 결정은 받는다 — 정비 주기도 그 시각에는 거부하지 않는다
    with ctx.c.sessionmaker.begin() as db:
        req = db.get(AccessRequest, on_time, with_for_update=True)
        at = dataclasses.replace(ctx.c, clock=FixedClock(req.deadline_at))
        assert datasets.expire(at, db)["denied"] == 0
        v = db.get(DatasetVersion, req.dataset_version_id)
        datasets.decide(at, db, req, v, ORG_A, {"ORG_ADMIN"}, True, "기한 시각 승인", fid("usr", ADMIN_A))
        assert req.status == "PROVIDER_APPROVED"

    for rid in (late, waiting, refused):
        _past_deadline(ctx, rid)
    for user, rid in ((ADMIN_A, late), (OP, waiting)):
        r = decide(ctx, user, rid, "APPROVE")
        assert r.status_code == 409, r.text
        assert_matches_contract(r, "POST", PATH)
        assert r.json()["code"] == "INVALID_STATE_TRANSITION"
        assert "응답 기한" in r.json()["detail"]
    r = decide(ctx, ADMIN_A, refused, "DENY")
    assert r.status_code == 200 and r.json()["status"] == "DENIED", r.text

    with ctx.c.sessionmaker() as db:
        assert {rid: db.get(AccessRequest, rid).status for rid in (late, waiting)} == {
            late: "REQUESTED", waiting: "PROVIDER_APPROVED"}
        assert (_roles(db, late), _roles(db, waiting)) == ([], ["PROVIDER"])
        assert db.scalar(select(func.count()).select_from(Grant)) == 0
    # 정비 주기가 돌면 기본값 거부 (요구사항 8.3)
    with ctx.c.sessionmaker.begin() as db:
        assert datasets.expire(ctx.c, db) == {"denied": 2, "expired": 0}
    with ctx.c.sessionmaker() as db:
        assert {rid: db.get(AccessRequest, rid).status for rid in (on_time, late, waiting, refused)} == {
            on_time: "PROVIDER_APPROVED", late: "DENIED", waiting: "DENIED", refused: "DENIED"}
