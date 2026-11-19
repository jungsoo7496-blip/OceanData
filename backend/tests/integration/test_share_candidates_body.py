"""공유 후보 지정 본문의 asset_ids는 1개 이상, 중복 없이 (계약 점검 S16, markShareCandidates minItems·uniqueItems).

빈 목록(session_id와 함께)과 중복 목록은 400 VALIDATION_ERROR이고 아무 자산도 후보가 되지 않는다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Asset
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A, SES_A
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
MEMBER = 102


def _byproduct(ctx: Ctx) -> str:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)  # SES_A 회차
        a.data_role = "BYPRODUCT"
        db.add(a)
        db.flush()
        return a.asset_id


def _post(ctx: Ctx, body: dict):
    return ctx.client.post("/v1/share-candidates", json=body, headers=ctx.as_user(MEMBER))


def _status(ctx: Ctx, asset_id: str) -> str:
    with ctx.c.sessionmaker() as db:
        return db.execute(select(Asset.share_status).where(Asset.asset_id == asset_id)).scalar_one()


@pytest.mark.parametrize(("name", "body", "code"), [
    ("empty_with_session", {"asset_ids": [], "session_id": SES_A}, "too_short"),
    ("empty", {"asset_ids": []}, "too_short"),
    ("duplicate", {"asset_ids": ["DUP", "DUP"]}, "unique"),
])
def test_asset_ids_must_be_non_empty_and_unique(ctx: Ctx, name: str, body: dict, code: str) -> None:
    aid = _byproduct(ctx)
    body = {**body, "asset_ids": [aid if x == "DUP" else x for x in body["asset_ids"]]}
    r = _post(ctx, body)
    assert r.status_code == 400, f"{name}: {r.status_code} {r.text}"
    check(r, "POST", "/v1/share-candidates")
    assert r.json()["code"] == "VALIDATION_ERROR"
    assert [(e["pointer"], e["code"]) for e in r.json()["errors"]] == [("/asset_ids", code)]
    if code == "unique":  # 데이터셋 버전 item_asset_ids와 같은 모양: 배열 자리 하나, 다시 나온 ID는 메시지에 (SHARING-5)
        assert aid in r.json()["errors"][0]["message"]
    assert _status(ctx, aid) == "NONE"  # 회차의 자산도 후보가 되지 않았다


def test_valid_bodies_still_work(ctx: Ctx) -> None:
    r = _post(ctx, {})
    assert r.status_code == 400 and r.json()["errors"][0]["code"] == "minProperties"
    aid = _byproduct(ctx)
    r = _post(ctx, {"asset_ids": [aid]})
    assert r.status_code == 200 and r.json()["candidates"] == [aid]


@pytest.mark.parametrize(("name", "body", "pointer"), [
    ("asset_id", {"asset_ids": ["OK", "not-an-asset-id"]}, "/asset_ids/1"),
    ("session_id", {"asset_ids": ["OK"], "session_id": "ses_bad"}, "/session_id"),
])
def test_malformed_ids_are_validation_errors(ctx: Ctx, name: str, body: dict, pointer: str) -> None:
    """계약의 asset_id·session_id 형식이 아니면 없는 자산의 404가 아니라 400이다 (통합 검토 SHARING-4, 데이터셋 버전
    item_asset_ids와 같은 규칙). 맞는 ID가 함께 있어도 아무 자산도 후보가 되지 않는다."""
    aid = _byproduct(ctx)
    r = _post(ctx, {**body, "asset_ids": [aid if x == "OK" else x for x in body["asset_ids"]]})
    assert r.status_code == 400, f"{name}: {r.status_code} {r.text}"
    check(r, "POST", "/v1/share-candidates")
    assert r.json()["code"] == "VALIDATION_ERROR"
    assert [e["pointer"] for e in r.json()["errors"]] == [pointer]
    assert _status(ctx, aid) == "NONE"
