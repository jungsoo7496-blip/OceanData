"""심사 처리에서 전이 주체가 아닌 역할의 403도 권한 거부 감사를 남긴다 (계약 점검 S14, security.md 감사 로그).

share:review 권한(Permission_Table)은 있지만 그 전이의 주체(domain.md 5장 share_status 표)가 아닌 경우다.
감사는 요청이 실패해도 남아야 하므로 요청 트랜잭션과 따로 확정한다 (ADR-0053).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Asset, AuditLog
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
ADMIN, MEMBER, OP = 101, 102, 106


def _original(ctx: Ctx, share_status: str) -> str:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        a.data_role, a.sensitivity_class, a.share_status = "BYPRODUCT", "INTERNAL", share_status
        db.add(a)
        db.flush()
        return a.asset_id


def _rows(ctx: Ctx, response) -> list[AuditLog]:
    with ctx.c.sessionmaker() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.request_id == response.headers["x-request-id"])).all())


@pytest.mark.parametrize(("user", "start", "action", "target"), [
    (MEMBER, "CANDIDATE", "START_LEGAL_REVIEW", "LEGAL_REVIEW"),
    (ADMIN, "QUALITY_REVIEW", "APPROVE", "APPROVED"),
])
def test_actor_outside_the_transition_is_audited(ctx: Ctx, user: int, start: str, action: str, target: str) -> None:
    aid = _original(ctx, start)
    r = ctx.client.post(f"/v1/assets/{aid}/share-review", json={"action": action},
                        headers=ctx.as_user(user, mfa=True))
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "FORBIDDEN"
    check(r, "POST", "/v1/assets/{asset_id}/share-review")
    rows = _rows(ctx, r)
    assert [(x.action, x.result, x.actor_id, x.organization_id, x.target_type, x.target_id) for x in rows] == [
        ("PERMISSION_DENIED", "DENIED", fid("usr", user), ORG_A, "Asset", aid)]
    assert rows[0].details == {"action": "share:review", "review_action": action, "from": start, "to": target}
    with ctx.c.sessionmaker() as db:
        assert db.execute(select(Asset.share_status).where(Asset.asset_id == aid)).scalar_one() == start


def test_missing_transition_is_not_a_permission_denial(ctx: Ctx) -> None:
    aid = _original(ctx, "CANDIDATE")
    r = ctx.client.post(f"/v1/assets/{aid}/share-review", json={"action": "APPROVE"}, headers=ctx.as_user(OP))
    assert r.status_code == 409 and r.json()["code"] == "INVALID_STATE_TRANSITION"
    assert _rows(ctx, r) == []
