"""긴급 열람 철회의 상태별 계약과 응답 (ADR-0029)."""

from __future__ import annotations

import re

import pytest
import yaml

from app.seed import SES_A
from app.services.contracts import contracts_dir
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

REVOKE_PATH = "/v1/break-glass-grants/{grant_id}/revoke"
TRANSITIONS = (("REQUESTED", "DENIED"), ("ACTIVE", "EXPIRED"))


def test_break_glass_revoke_contract_documents_both_status_transitions() -> None:
    spec = yaml.safe_load((contracts_dir() / "openapi.yaml").read_text(encoding="utf-8"))
    operation = spec["paths"][REVOKE_PATH]["post"]
    documentation = "\n".join((operation.get("summary", ""), operation.get("description", "")))
    for before, after in TRANSITIONS:
        assert re.search(rf"\b{before}\b[^.;\n]*\b{after}\b", documentation), (
            f"철회 계약의 summary 또는 description에 {before}에서 {after}로 바뀐다고 명시해야 합니다"
        )


@pytest.mark.integration
@pytest.mark.parametrize(("before", "after"), TRANSITIONS)
def test_break_glass_revoke_returns_status_for_grant_state(ctx: Ctx, before: str, after: str) -> None:
    requester = ctx.as_user(106, mfa=True)
    created = ctx.client.post("/v1/break-glass-grants", headers=requester, json={
        "target_type": "SESSION", "target_id": SES_A, "reason": "합성 긴급 열람 철회 시험", "duration_s": 600,
    })
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "REQUESTED"
    grant_id = created.json()["grant_id"]
    if before == "ACTIVE":
        approved = ctx.client.post(f"/v1/break-glass-grants/{grant_id}/decision",
                                   headers=ctx.as_user(107, mfa=True), json={"decision": "APPROVE"})
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "ACTIVE"

    revoked = ctx.client.post(REVOKE_PATH.format(grant_id=grant_id), headers=requester, json={"reason": "열람 종료"})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == after
    check(revoked, "POST", REVOKE_PATH)
