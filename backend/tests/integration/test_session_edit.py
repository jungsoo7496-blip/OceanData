"""09번 요구사항 5.1: 실증회차 수정 — 일정·장소·메모·장비, 바뀐 항목 전후 감사, label·실증사업은 바꾸지 않음."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_B_CAMERA, PRJ_A, SES_A
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
ADMIN_A, MEMBER_A, ADMIN_B, VIEWER, OP = 101, 102, 103, 105, 106
PATH = f"/v1/sessions/{SES_A}"


def _audit(ctx) -> list[dict]:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        rows = conn.execute(text("SELECT details FROM audit_log WHERE action = 'SESSION_UPDATED' ORDER BY seq"))
        return [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]


def test_session_edit_changes_schedule_site_note_devices(ctx) -> None:
    before = ctx.client.get(PATH, headers=ctx.as_user(MEMBER_A)).json()
    r = ctx.client.patch(PATH, headers=ctx.as_user(MEMBER_A), json={
        "ends_at": "2026-12-31T09:00:00Z", "site": "부산 남항 시험 해역 (합성)", "note": "기상 악화로 연장 (합성 메모)",
        "device_ids": [DEV_A_CAMERA, DEV_A_CAMERA]})
    check(r, "PATCH", "/v1/sessions/{session_id}")
    assert r.status_code == 200, r.text
    s = r.json()
    assert (s["ends_at"], s["site"], s["device_ids"]) == ("2026-12-31T09:00:00Z", "부산 남항 시험 해역 (합성)", [DEV_A_CAMERA])
    assert (s["label"], s["project_id"], s["title"], s["starts_at"]) == (
        before["label"], before["project_id"], before["title"], before["starts_at"])  # 보내지 않은 항목은 그대로
    assert ctx.client.get(PATH, headers=ctx.as_user(ADMIN_A)).json() == s
    changes = _audit(ctx)[-1]["changes"]
    assert changes["site"] == {"from": before["site"], "to": "부산 남항 시험 해역 (합성)"}
    assert changes["note"]["to"] == len("기상 악화로 연장 (합성 메모)")  # 메모 본문은 감사 로그에 옮기지 않는다
    assert changes["device_ids"] == {"from": before["device_ids"], "to": [DEV_A_CAMERA]}

    # 비우기(null)와 같은 값 다시 보내기: 바뀐 것만 감사
    n = len(_audit(ctx))
    r = ctx.client.patch(PATH, headers=ctx.as_user(MEMBER_A), json={"site": None, "ends_at": None,
                                                                    "device_ids": [DEV_A_CAMERA, DEV_A_COLLECTOR]})
    assert r.status_code == 200 and r.json()["site"] is None and r.json()["ends_at"] is None
    assert r.json()["device_ids"] == sorted([DEV_A_CAMERA, DEV_A_COLLECTOR])
    assert ctx.client.patch(PATH, headers=ctx.as_user(MEMBER_A), json={"title": before["title"]}).status_code == 200
    assert len(_audit(ctx)) == n + 1  # 두 번째 요청은 바뀐 것이 없어 감사하지 않는다


@pytest.mark.parametrize("body, pointer", [
    ({}, ""), ({"label": "새표시값"}, None), ({"project_id": PRJ_A}, None),
    ({"starts_at": None}, "/starts_at"), ({"title": ""}, "/title"),
    ({"ends_at": "2020-01-01T00:00:00Z"}, "/ends_at"), ({"starts_at": "2026-11-01T09:00:00"}, "/starts_at"),
])
def test_session_edit_rejects_invalid(ctx, body: dict, pointer: str | None) -> None:
    r = ctx.client.patch(PATH, headers=ctx.as_user(MEMBER_A), json=body)
    check(r, "PATCH", "/v1/sessions/{session_id}")
    assert r.status_code == 400, (body, r.text)
    if pointer is not None:
        assert pointer in [e["pointer"] for e in r.json()["errors"]], r.text


def test_session_edit_scope_and_roles(ctx) -> None:
    body = {"site": "합성"}
    assert ctx.client.patch(PATH, headers=ctx.as_user(ADMIN_B), json=body).status_code == 404  # 다른 기관
    assert ctx.client.patch(PATH, headers=ctx.as_user(VIEWER), json=body).status_code == 403
    assert ctx.client.patch(PATH, headers=ctx.as_user(OP), json=body).status_code == 403
    r = ctx.client.patch(PATH, headers=ctx.as_user(MEMBER_A), json={"device_ids": [DEV_B_CAMERA]})
    assert r.status_code == 404  # 다른 기관 장비는 회차에 넣을 수 없다
    assert ctx.client.patch("/v1/sessions/ses_" + "Z" * 26, headers=ctx.as_user(MEMBER_A), json=body).status_code == 404
