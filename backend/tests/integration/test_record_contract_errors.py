"""회차 기록 API 오류 응답과 근거 교차 기관 감사 (계약 점검 S39~S42)."""

from __future__ import annotations

import pytest

from app.seed import SES_A
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration

MEMBER_A = 102
UPDATE = "/v1/records/{record_id}"


def _new_record(ctx: Ctx) -> str:
    r = ctx.client.post(f"/v1/sessions/{SES_A}/records", json={"kind": "LOG", "title": "합성 일지"},
                        headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 201, r.text
    return str(r.json()["record_id"])


def _latest_version(ctx: Ctx, record_id: str) -> int:
    r = ctx.client.get(f"/v1/records/{record_id}", headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 200, r.text
    return int(r.json()["latest_version"])


# ------------------------------------------------------------------ S39·S42: null을 받을 수 없는 항목


@pytest.mark.parametrize("field", ["title", "kind", "evidence_refs"])
def test_patch_null_on_non_nullable_field_is_400_at_that_field(ctx: Ctx, field: str) -> None:
    record_id = _new_record(ctx)
    r = ctx.client.patch(f"/v1/records/{record_id}", json={field: None}, headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 400, r.text
    check(r, "PATCH", UPDATE)
    assert r.json()["code"] == "VALIDATION_ERROR"
    assert [e["pointer"] for e in r.json()["errors"]] == [f"/{field}"]
    assert _latest_version(ctx, record_id) == 1  # 새 버전을 만들지 않는다


def test_patch_null_on_nullable_text_field_still_makes_new_version(ctx: Ctx) -> None:
    record_id = _new_record(ctx)
    r = ctx.client.patch(f"/v1/records/{record_id}", json={"note": None, "result": None},
                         headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 200, r.text
    check(r, "PATCH", UPDATE)
    assert (r.json()["version"], r.json()["note"], r.json()["evidence_refs"]) == (2, None, [])
