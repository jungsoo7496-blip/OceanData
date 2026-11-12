"""회차 기록 API 오류 응답과 근거 교차 기관 감사 (계약 점검 S39~S42)."""

from __future__ import annotations

from typing import Any

import pytest

from app.seed import DEV_A_SENSOR, SES_A, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration

MEMBER_A = 102
CREATE = "/v1/sessions/{session_id}/records"
UPDATE = "/v1/records/{record_id}"


def _obs(dev: str, frm: str = "2026-10-01T00:00:00Z", to: str = "2026-10-02T00:00:00Z") -> dict[str, Any]:
    return {"kind": "OBSERVATION_RANGE", "source_system_id": dev, "metric": "sea_water_temperature",
            "from": frm, "to": to}


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


# ------------------------------------------------------------------ S40: 형식은 맞지만 없는 시각


@pytest.mark.parametrize(("route", "device", "field", "value"), [
    pytest.param("POST", DEV_A_SENSOR, "from", "2026-02-30T00:00:00Z", id="post-feb30-from"),
    pytest.param("POST", DEV_A_SENSOR, "to", "2026-10-01T25:00:00Z", id="post-hour25-to"),
    pytest.param("POST", DEV_A_SENSOR, "from", "2026-06-30T23:59:60Z", id="post-leap-second"),
    pytest.param("POST", fid("dev", 999), "from", "2026-13-01T00:00:00Z", id="post-unknown-device-month13"),
    pytest.param("PATCH", DEV_A_SENSOR, "to", "2026-10-01T25:00:00Z", id="patch-hour25-to"),
])
def test_observation_range_impossible_timestamp_is_400(ctx: Ctx, route: str, device: str, field: str,
                                                       value: str) -> None:
    hm = ctx.as_user(MEMBER_A)
    refs = [_obs(device, **({"frm": value} if field == "from" else {"to": value}))]
    if route == "POST":
        r = ctx.client.post(f"/v1/sessions/{SES_A}/records", json={"kind": "LOG", "title": "x", "evidence_refs": refs},
                            headers=hm)
        path = CREATE
    else:
        record_id = _new_record(ctx)
        r = ctx.client.patch(f"/v1/records/{record_id}", json={"evidence_refs": refs}, headers=hm)
        path = UPDATE
    assert r.status_code == 400, r.text
    check(r, route, path)
    assert r.json()["code"] == "VALIDATION_ERROR"
    pointers = [e["pointer"] for e in r.json()["errors"]]
    assert len(pointers) == 1 and pointers[0].startswith("/evidence_refs/0/") and pointers[0].endswith(f"/{field}")
    if route == "PATCH":
        assert _latest_version(ctx, record_id) == 1


def test_observation_range_valid_timestamp_still_accepted(ctx: Ctx) -> None:
    r = ctx.client.post(f"/v1/sessions/{SES_A}/records", headers=ctx.as_user(MEMBER_A), json={
        "kind": "LOG", "title": "x", "evidence_refs": [_obs(DEV_A_SENSOR, to="2026-10-01T23:59:59.5Z")]})
    assert r.status_code == 201, r.text
    check(r, "POST", CREATE)
