"""재신청(RESUBMIT)은 반려된 BYPRODUCT 원본만 (계약 점검 S12, domain.md 5장 share_status 표).

NONE → CANDIDATE는 공유 후보 지정(`POST /v1/share-candidates`, data_role=BYPRODUCT 조건)으로만 간다. RESUBMIT는
REJECTED → CANDIDATE(보완 후 재신청, ORG_ADMIN)이다. 심사 처리로 CORE 원본이 후보가 되면 안 된다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Asset, AuditLog
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
ADMIN, MEMBER, OP = 101, 102, 106
PATH = "/v1/assets/{asset_id}/share-review"


def _original(ctx: Ctx, *, data_role: str, share_status: str) -> str:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        a.data_role, a.share_status, a.sensitivity_class = data_role, share_status, "INTERNAL"
        db.add(a)
        db.flush()
        return a.asset_id


def _resubmit(ctx: Ctx, asset_id: str, user: int):
    return ctx.client.post(f"/v1/assets/{asset_id}/share-review", json={"action": "RESUBMIT"},
                           headers=ctx.as_user(user))


def _stored(ctx: Ctx, asset_id: str) -> str:
    with ctx.c.sessionmaker() as db:
        return db.execute(select(Asset.share_status).where(Asset.asset_id == asset_id)).scalar_one()


def _audits(ctx: Ctx, response) -> list[str]:
    with ctx.c.sessionmaker() as db:
        return [x.action for x in db.scalars(select(AuditLog).where(
            AuditLog.request_id == response.headers["x-request-id"])).all()]


@pytest.mark.parametrize("user", [MEMBER, ADMIN, OP])
@pytest.mark.parametrize("data_role", ["CORE", "BYPRODUCT"])
def test_resubmit_needs_a_rejected_original(ctx: Ctx, user: int, data_role: str) -> None:
    aid = _original(ctx, data_role=data_role, share_status="NONE")
    r = _resubmit(ctx, aid, user)
    assert r.status_code == 409, r.text  # 표에 없는 전이 (역할과 상관없이)
    assert r.json()["code"] == "INVALID_STATE_TRANSITION"
    check(r, "POST", PATH)
    assert _stored(ctx, aid) == "NONE"
    assert _audits(ctx, r) == []  # 처리 감사(SHARE_REVIEW_RESUBMIT)도, 권한 거부도 아니다


def test_core_original_cannot_reenter_review(ctx: Ctx) -> None:
    aid = _original(ctx, data_role="CORE", share_status="NONE")
    r = ctx.client.post("/v1/share-candidates", json={"asset_ids": [aid]}, headers=ctx.as_user(MEMBER))
    assert r.json()["excluded"] == [{"asset_id": aid, "reason": "CORE"}]
    assert _resubmit(ctx, aid, MEMBER).status_code == 409
    r = ctx.client.post(f"/v1/assets/{aid}/share-review", json={"action": "START_LEGAL_REVIEW"},
                        headers=ctx.as_user(OP))
    assert r.status_code == 409  # 후보가 아니므로 법률 검토도 시작할 수 없다
    assert _stored(ctx, aid) == "NONE"


def test_resubmit_of_a_rejected_core_original_is_a_rule_violation(ctx: Ctx) -> None:
    aid = _original(ctx, data_role="CORE", share_status="REJECTED")
    r = _resubmit(ctx, aid, ADMIN)
    assert r.status_code == 422, r.text
    check(r, "POST", PATH)
    assert r.json()["unmet_conditions"] == [{"condition": "data_role=BYPRODUCT", "actual": "CORE"}]
    assert _stored(ctx, aid) == "REJECTED"
    assert _resubmit(ctx, _original(ctx, data_role="BYPRODUCT", share_status="REJECTED"), ADMIN).json()[
        "share_status"] == "CANDIDATE"
