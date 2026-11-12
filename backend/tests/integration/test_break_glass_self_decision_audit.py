"""긴급 열람 자기 결정 거부 감사 (02번 요구사항 7.1, 8.1)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import AuditLog, BreakGlassGrant
from app.seed import ORG_A, ORG_OPERATOR, SES_A, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("decision", ["APPROVE", "DENY"])
def test_break_glass_self_decision_denial_is_audited(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch, decision: str,
) -> None:
    monkeypatch.setattr(ctx.c.settings, "break_glass_single_approval", False)
    operator = 106
    headers = ctx.as_user(operator, mfa=True)
    path = "/v1/break-glass-grants"
    created = ctx.client.post(path, headers=headers, json={
        "target_type": "SESSION", "target_id": SES_A, "reason": "합성 긴급 열람 감사 시험", "duration_s": 600,
    })
    assert created.status_code == 201, created.text
    grant_id = created.json()["grant_id"]
    assert created.json()["status"] == "REQUESTED"

    decision_path = path + "/{grant_id}/decision"
    denied = ctx.client.post(decision_path.format(grant_id=grant_id), headers=headers, json={"decision": decision})
    assert denied.status_code == 403, denied.text
    problem = denied.json()
    assert problem["code"] == "FORBIDDEN"
    check(denied, "POST", decision_path)

    with ctx.c.sessionmaker() as db:
        grant = db.get(BreakGlassGrant, grant_id)
        assert grant is not None
        assert grant.status == "REQUESTED"
        assert grant.approved_by is None and grant.starts_at is None and grant.expires_at is None
        audit = db.scalars(select(AuditLog).where(AuditLog.request_id == problem["request_id"])).one_or_none()
        assert audit is not None, "자기 결정 요청에 권한 거부 감사가 없습니다"
        assert (audit.action, audit.result) == ("PERMISSION_DENIED", "DENIED")
        assert (audit.actor_kind, audit.actor_id, audit.actor_organization_id) == (
            "USER", fid("usr", operator), ORG_OPERATOR,
        )
        assert (audit.organization_id, audit.target_type, audit.target_id) == (ORG_A, "BreakGlassGrant", grant_id)
        assert audit.details["action"] == "break_glass:decide"
