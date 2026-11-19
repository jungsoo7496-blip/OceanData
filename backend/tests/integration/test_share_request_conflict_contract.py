"""공유 등급 변경 신청의 409를 계약에 적는다 (계약 점검 S15).

지금 등급과 같은 목표 등급은 409 INVALID_STATE_TRANSITION, 같은 멱등 키에 다른 본문은 409 IDEMPOTENCY_CONFLICT다.
동작은 그대로 두고 계약(createShareRequest)에 409를 적었다 — 장비 등록(계약 점검 #23)과 같은 처리.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Asset, ShareRequest
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
ADMIN = 101
PATH = "/v1/assets/{asset_id}/share-requests"


def _deid(ctx: Ctx) -> str:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="DEID")
        a.data_role, a.privacy_status, a.sensitivity_class = "BYPRODUCT", "PSEUDONYMIZED", "INTERNAL"
        db.add(a)
        db.flush()
        return a.asset_id


def _post(ctx: Ctx, asset_id: str, body: dict, key: str | None = None):
    headers = {**ctx.as_user(ADMIN), **({"Idempotency-Key": key} if key else {})}
    return ctx.client.post(f"/v1/assets/{asset_id}/share-requests", json={"reason": "공동활용 준비", **body},
                           headers=headers)


def test_target_equal_to_current_scope_is_a_documented_409(ctx: Ctx) -> None:
    aid = _deid(ctx)
    r = _post(ctx, aid, {"target_scope": "PRIVATE"})
    assert r.status_code == 409 and r.json()["code"] == "INVALID_STATE_TRANSITION"
    check(r, "POST", PATH)
    with ctx.c.sessionmaker() as db:
        assert db.scalars(select(ShareRequest).where(ShareRequest.asset_id == aid)).all() == []


def test_same_idempotency_key_with_another_body_is_a_documented_409(ctx: Ctx) -> None:
    aid = _deid(ctx)
    r = _post(ctx, aid, {"target_scope": "PROGRAM_REVIEW"}, key="share-req-0001")
    assert r.status_code == 201 and r.json()["state"] == "APPLIED", r.text
    r = _post(ctx, aid, {"target_scope": "PROGRAM_REVIEW", "reason": "다른 사유"}, key="share-req-0001")
    assert r.status_code == 409 and r.json()["code"] == "IDEMPOTENCY_CONFLICT"
    check(r, "POST", PATH)
    with ctx.c.sessionmaker() as db:
        assert db.execute(select(Asset.sharing_scope).where(Asset.asset_id == aid)).scalar_one() == "PROGRAM_REVIEW"
