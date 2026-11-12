"""운영자의 다운로드 URL 거부 감사 (02번 요구사항 7.1, 8, 09번 요구사항 7.3, 계약 점검 S28).

권한 표는 운영자에게 asset:download를 주고, 처리기가 유효한 긴급 열람 승인이 없으면 403으로 거부한다.
승인이 없을 때, 요청만 하고 승인되지 않았을 때, 승인한 운영자가 남의 승인으로 받으려 할 때, 승인이 만료됐을 때
모두 403과 함께 권한 거부(PERMISSION_DENIED) 감사가 한 줄 남아야 한다 (security.md '권한 거부·긴급 열람').
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.db.models import AuditLog, BreakGlassGrant
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
VIEWER, OP, OP2 = 105, 106, 107
DOWNLOAD = "/v1/assets/{asset_id}/download-url"


def _org_a_asset(ctx: Ctx) -> str:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        a.location = None
        a.storage_zone, a.storage_key = "RAW", f"{ORG_A}/raw/{a.asset_id}"
        db.add(a)
        return a.asset_id


def _denied(ctx: Ctx, user: int, asset_id: str) -> list[AuditLog]:
    r = ctx.client.post(DOWNLOAD.format(asset_id=asset_id), headers=ctx.as_user(user, mfa=True))
    assert r.status_code == 403, r.text
    check(r, "POST", DOWNLOAD)
    problem = r.json()
    assert problem["code"] == "FORBIDDEN"
    with ctx.c.sessionmaker() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.request_id == problem["request_id"])))


def _assert_operator_denial(rows: list[AuditLog], user: int, asset_id: str) -> None:
    assert [(x.action, x.result) for x in rows] == [("PERMISSION_DENIED", "DENIED")], \
        "운영자의 긴급 열람 없는 원본 요청에 권한 거부 감사가 없습니다"
    audit = rows[0]
    assert (audit.actor_kind, audit.actor_id) == ("USER", fid("usr", user))
    assert (audit.organization_id, audit.target_type, audit.target_id) == (ORG_A, "Asset", asset_id)
    assert audit.details == {"action": "asset:download", "reason": "no_active_break_glass"}


def test_operator_download_without_valid_break_glass_is_audited(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctx.c.settings, "break_glass_single_approval", False)
    asset_id = _org_a_asset(ctx)

    _assert_operator_denial(_denied(ctx, OP, asset_id), OP, asset_id)  # 승인 없음

    r = ctx.client.post("/v1/break-glass-grants", headers=ctx.as_user(OP, mfa=True), json={
        "target_type": "ASSET", "target_id": asset_id, "reason": "장애 INC-합성-077 원본 손상 확인", "duration_s": 3600})
    assert r.status_code == 201, r.text
    grant_id = r.json()["grant_id"]
    _assert_operator_denial(_denied(ctx, OP, asset_id), OP, asset_id)  # 요청만 하고 승인 전 (REQUESTED)

    d = ctx.client.post(f"/v1/break-glass-grants/{grant_id}/decision", json={"decision": "APPROVE"},
                        headers=ctx.as_user(OP2, mfa=True))
    assert d.status_code == 200 and d.json()["status"] == "ACTIVE", d.text
    _assert_operator_denial(_denied(ctx, OP2, asset_id), OP2, asset_id)  # 승인자는 남의 승인으로 받지 못한다

    with ctx.c.sessionmaker.begin() as db:
        db.execute(update(BreakGlassGrant).where(BreakGlassGrant.grant_id == grant_id)
                   .values(expires_at=ctx.c.clock.now() - timedelta(seconds=1)))
    _assert_operator_denial(_denied(ctx, OP, asset_id), OP, asset_id)  # 만료

    assert "BREAK_GLASS_ACCESS" not in ctx.audit_actions() and "DOWNLOAD_URL_ISSUED" not in ctx.audit_actions()


def test_viewer_table_denial_is_still_audited_once(ctx: Ctx) -> None:
    """권한 표에서 막히는 관리기관 열람자는 전처럼 require()의 감사 한 줄만 남는다 (두 번 남기지 않는다)."""
    asset_id = _org_a_asset(ctx)
    rows = _denied(ctx, VIEWER, asset_id)
    assert [(x.action, x.result, x.details["action"]) for x in rows] == [
        ("PERMISSION_DENIED", "DENIED", "asset:download")]
