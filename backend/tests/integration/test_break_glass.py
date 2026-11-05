"""02번 긴급 열람 (요구사항 8.1~8.4): 요청 → 다른 운영자 승인 → 대상 기관 통지 → 원본 접근 감사 → 만료·조기 종료 회수."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import update

from app.db.models import BreakGlassGrant
from app.seed import ORG_A, SES_A
from app.services import break_glass
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_ingest import MB, drain, manifest, mp4_bytes, upload_all

pytestmark = pytest.mark.integration
ADMIN_A, MEMBER_A, ADMIN_B, OP, OP2 = 101, 102, 103, 106, 107


def _ask(ctx, user: int, target_type: str, target_id: str, **kw):  # type: ignore[no-untyped-def]
    body = {"target_type": target_type, "target_id": target_id, "reason": "장애 INC-합성-001 원본 손상 확인",
            "duration_s": 3600, **kw}
    return ctx.client.post("/v1/break-glass-grants", json=body, headers=ctx.as_user(user, mfa=True))


def _decide(ctx, user: int, grant_id: str, decision: str = "APPROVE"):  # type: ignore[no-untyped-def]
    return ctx.client.post(f"/v1/break-glass-grants/{grant_id}/decision", json={"decision": decision},
                           headers=ctx.as_user(user, mfa=True))


def _download(ctx, user: int, asset_id: str):  # type: ignore[no-untyped-def]
    return ctx.client.post(f"/v1/assets/{asset_id}/download-url", headers=ctx.as_user(user))


def test_break_glass_request_approve_access_expire(ictx) -> None:
    ctx = ictx
    data = mp4_bytes(5 * MB, seed=21)
    asset_id = upload_all(ctx, manifest(data), data, ctx.as_user(MEMBER_A))["asset_id"]
    drain(ctx)
    assert _download(ctx, OP, asset_id).status_code == 403  # 평소 운영자는 원본을 열지 않는다

    # 요청: 운영자·다중 인증, 사유·기간(≤24시간)·대상 확인
    assert ctx.client.post("/v1/break-glass-grants", headers=ctx.as_user(OP), json={
        "target_type": "ASSET", "target_id": asset_id, "reason": "장애 INC-합성-001 원본 손상 확인",
        "duration_s": 3600}).status_code == 403
    assert _ask(ctx, ADMIN_A, "ASSET", asset_id).status_code == 403
    assert _ask(ctx, OP, "ASSET", asset_id, reason="짧음").status_code == 400
    assert _ask(ctx, OP, "ASSET", asset_id, duration_s=86401).status_code == 400
    assert _ask(ctx, OP, "ASSET", "ast_0000000000000000000000ZZZZ").status_code == 404
    r = _ask(ctx, OP, "ASSET", asset_id)
    check(r, "POST", "/v1/break-glass-grants")
    g = r.json()
    assert r.status_code == 201 and (g["status"], g["organization_id"]) == ("REQUESTED", ORG_A)
    assert _download(ctx, OP, asset_id).status_code == 403  # 승인 전

    # 승인: 요청자 본인은 불가, 다른 운영자 승인 → 지금부터 기간만큼, 대상 기관 ORG_ADMIN 통지
    assert _decide(ctx, OP, g["grant_id"]).status_code == 403
    r = _decide(ctx, OP2, g["grant_id"])
    check(r, "POST", "/v1/break-glass-grants/{grant_id}/decision")
    assert r.json()["status"] == "ACTIVE" and r.json()["approved_by"] and r.json()["expires_at"]
    assert _decide(ctx, OP2, g["grant_id"]).status_code == 409
    note = next(n for n in ctx.c.notifier.sent if n.kind == "break_glass.granted")
    assert note.organization_id == ORG_A and note.recipients and all("admin" in x for x in note.recipients)
    assert g["grant_id"] in str(note.data) and "http" not in note.body

    # 유효한 동안: 요청한 운영자만 받는다, 접근마다 감사 (8.3). 승인자·다른 대상은 불가
    r = _download(ctx, OP, asset_id)
    check(r, "POST", "/v1/assets/{asset_id}/download-url")
    assert r.status_code == 200 and httpx.get(r.json()["url"], timeout=30).content == data
    assert _download(ctx, OP2, asset_id).status_code == 403
    other = upload_all(ctx, manifest(mp4_bytes(5 * MB, seed=22), session=None), mp4_bytes(5 * MB, seed=22),
                       ctx.as_user(MEMBER_A))["asset_id"]
    drain(ctx)
    assert _download(ctx, OP, other).status_code == 403  # 회차 없는 다른 자산은 대상 밖
    assert ctx.audit_actions().count("BREAK_GLASS_ACCESS") == 1

    # 조회: 운영자 전체, 대상 기관 ORG_ADMIN은 자기 기관 것, 다른 기관·구성원은 못 본다
    r = ctx.client.get("/v1/break-glass-grants", headers=ctx.as_user(ADMIN_A))
    check(r, "GET", "/v1/break-glass-grants")
    assert [x["grant_id"] for x in r.json()["items"]] == [g["grant_id"]]
    assert ctx.client.get("/v1/break-glass-grants", headers=ctx.as_user(ADMIN_B)).json()["items"] == []
    assert ctx.client.get("/v1/break-glass-grants", headers=ctx.as_user(MEMBER_A)).status_code == 403

    # 만료 (8.4): 만료 시각이 지나면 바로 막히고, 워커가 상태를 EXPIRED로 맞춘다
    with ctx.c.sessionmaker.begin() as db:
        db.execute(update(BreakGlassGrant).where(BreakGlassGrant.grant_id == g["grant_id"])
                   .values(expires_at=ctx.c.clock.now() - timedelta(seconds=1)))
    assert _download(ctx, OP, asset_id).status_code == 403
    assert ctx.client.get("/v1/break-glass-grants", headers=ctx.as_user(OP)).json()["items"][0]["status"] == "EXPIRED"
    with ctx.c.sessionmaker.begin() as db:
        assert [x.grant_id for x in break_glass.expire(db, ctx.c.clock.now())] == [g["grant_id"]]
    with ctx.c.sessionmaker() as db:
        assert db.get(BreakGlassGrant, g["grant_id"]).status == "EXPIRED"

    # 회차 단위 권한 → 그 회차 자산, 조기 종료 → 바로 막힘, 다시 종료는 409
    s = _ask(ctx, OP, "SESSION", SES_A, duration_s=600).json()
    _decide(ctx, OP2, s["grant_id"])
    assert _download(ctx, OP, asset_id).status_code == 200
    r = ctx.client.post(f"/v1/break-glass-grants/{s['grant_id']}/revoke", json={"reason": "확인 끝"},
                        headers=ctx.as_user(OP2, mfa=True))
    check(r, "POST", "/v1/break-glass-grants/{grant_id}/revoke")
    assert r.json()["status"] == "EXPIRED"
    assert _download(ctx, OP, asset_id).status_code == 403
    assert ctx.client.post(f"/v1/break-glass-grants/{s['grant_id']}/revoke", json={"reason": "다시"},
                           headers=ctx.as_user(OP2, mfa=True)).status_code == 409

    # 거절: DENIED, 접근 불가
    d = _ask(ctx, OP, "ASSET", asset_id).json()
    assert _decide(ctx, OP2, d["grant_id"], "DENY").json()["status"] == "DENIED"
    assert _download(ctx, OP, asset_id).status_code == 403
    actions = ctx.audit_actions()
    for action in ("BREAK_GLASS_REQUESTED", "BREAK_GLASS_APPROVED", "BREAK_GLASS_DENIED", "BREAK_GLASS_REVOKED"):
        assert action in actions, action
    assert ctx.client.get("/v1/system/capabilities", headers=ctx.as_user(OP)).json()["features"]["break_glass"] is True


def test_single_approval_setting(ictx) -> None:
    """BREAK_GLASS_SINGLE_APPROVAL=true면 요청자 본인 승인 허용 (요구사항 8.1 설정값)."""
    ctx = ictx
    data = mp4_bytes(5 * MB, seed=23)
    asset_id = upload_all(ctx, manifest(data), data, ctx.as_user(MEMBER_A))["asset_id"]
    g = _ask(ctx, OP, "ASSET", asset_id).json()
    ctx.c.settings.__dict__["break_glass_single_approval"] = True
    try:
        assert _decide(ctx, OP, g["grant_id"]).json()["status"] == "ACTIVE"
    finally:
        ctx.c.settings.__dict__["break_glass_single_approval"] = False
