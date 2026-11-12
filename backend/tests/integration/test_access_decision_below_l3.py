"""운영자 최종 승인 때 데이터셋 버전 등급이 L3 아래면 422 RULE_VIOLATION — 계약에 적힌 응답 (계약 점검 S09).

제공기관 승인 뒤 자산 등급을 KEEP_UNTIL_EXPIRY로 낮추면 버전 등급도 내려간다 (ADR-0019, domain.md 4장).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import AccessDecision, AccessRequest, Grant
from tests.acceptance.test_t08_t15_sharing import access, decide, share
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract
from tests.integration.test_grant_access_request_tenant_audit import ADMIN_A, OP, published_version

pytestmark = pytest.mark.integration
PATH = "/v1/access-requests/{access_request_id}/decisions"


def test_operator_approval_below_l3_is_documented_422(ictx: Ctx) -> None:
    ctx = ictx
    a, vid = published_version(ctx)
    rid = access(ctx, vid, "AI_TRAINING").json()["access_request_id"]
    assert decide(ctx, ADMIN_A, rid, "APPROVE").json()["status"] == "PROVIDER_APPROVED"
    down = share(ctx, ADMIN_A, a["deid"], "SANDBOX_PARTNERS", existing_grant_handling="KEEP_UNTIL_EXPIRY")
    assert down.json()["state"] == "APPLIED", down.text

    r = decide(ctx, OP, rid, "APPROVE")
    assert r.status_code == 422, r.text
    assert_matches_contract(r, "POST", PATH)
    assert r.json()["code"] == "RULE_VIOLATION"
    assert r.json()["unmet_conditions"] == [{"condition": "데이터셋 버전 등급 ≥ L3", "actual": "SANDBOX_PARTNERS"}]
    # 요청 트랜잭션을 되돌려 운영자 결정·이용 권한이 남지 않는다 (ADR-0053)
    with ctx.c.sessionmaker() as db:
        assert db.get(AccessRequest, rid).status == "PROVIDER_APPROVED"
        assert list(db.scalars(select(AccessDecision.role).where(AccessDecision.access_request_id == rid))) == [
            "PROVIDER"]
        assert db.scalar(select(Grant.grant_id).where(Grant.access_request_id == rid)) is None
