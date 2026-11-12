"""운영자 기관 사용량에는 아직 쿼터 행이 없는 기관도 들어간다 (계약 점검 11번)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import OrgQuota
from app.seed import ORG_A, ORG_B
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract

pytestmark = pytest.mark.integration
USAGE_PATH = "/v1/usage"


def test_usage_lists_all_orgs_for_operator_without_creating_quotas(ctx: Ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        db.add(OrgQuota(organization_id=ORG_A, quota_bytes=500, used_bytes=100, quarantine_bytes=20,
                        reserved_bytes=30, backup_bytes=50, updated_at=ctx.c.clock.now()))

    operator = ctx.as_user(106)
    policies = ctx.client.get("/v1/organization-policies", headers=operator)
    assert policies.status_code == 200, policies.text
    assert_matches_contract(policies, "GET", "/v1/organization-policies")
    response = ctx.client.get(USAGE_PATH, headers=operator)
    assert response.status_code == 200, response.text
    assert_matches_contract(response, "GET", USAGE_PATH)
    rows = response.json()["items"]
    assert [(r["organization_id"], r["quota_bytes"], r["usage_bytes"]) for r in rows] == [
        (r["organization_id"], r["quota_bytes"], r["usage_bytes"]) for r in policies.json()["items"]
    ]

    by_org = {r["organization_id"]: r for r in rows}
    assert by_org[ORG_A] == {
        "organization_id": ORG_A, "quota_bytes": 500, "usage_bytes": 200, "usage_ratio": 0.4,
        "breakdown": {"stored_bytes": 100, "quarantine_bytes": 20, "reserved_bytes": 30, "backup_bytes": 50},
    }
    assert by_org[ORG_B] == {
        "organization_id": ORG_B, "quota_bytes": ctx.c.settings.default_org_quota_bytes,
        "usage_bytes": 0, "usage_ratio": 0.0,
        "breakdown": {"stored_bytes": 0, "quarantine_bytes": 0, "reserved_bytes": 0, "backup_bytes": 0},
    }
    for user, organization_id in ((101, ORG_A), (103, ORG_B)):
        own = ctx.client.get(USAGE_PATH, headers=ctx.as_user(user))
        assert own.status_code == 200, own.text
        assert_matches_contract(own, "GET", USAGE_PATH)
        assert own.json()["items"] == [by_org[organization_id]]

    with ctx.c.sessionmaker() as db:
        assert db.scalars(select(OrgQuota.organization_id)).all() == [ORG_A]
