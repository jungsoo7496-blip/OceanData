"""다른 기관의 삭제 증명 조회 — 404와 교차 기관 시도 감사 (02번 요구사항 2.2, 7.1, 계약 점검 S27).

증명을 가진 기관의 ORG_ADMIN과 운영자는 지금처럼 200이고, 없는 증명은 감사 없이 404다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import AuditLog
from app.domain.ids import new_id
from app.seed import DEV_B_CAMERA, ORG_B, PRJ_B, SES_B, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
ADMIN_A, ADMIN_B, OP = 101, 103, 106
CERT = "/v1/deletion-certificates/{certificate_id}"


def _audit_rows(ctx: Ctx, request_id: str) -> list[AuditLog]:
    with ctx.c.sessionmaker() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.request_id == request_id)))


def _org_b_certificate(ctx: Ctx) -> str:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_B, PRJ_B, DEV_B_CAMERA, session=SES_B)
        a.location = None
        db.add(a)
        asset_id = a.asset_id
    r = ctx.client.post(f"/v1/assets/{asset_id}/deletion", json={"reason": "합성 삭제 요청"},
                        headers=ctx.as_user(ADMIN_B, mfa=True))
    assert r.status_code == 200, r.text
    return str(r.json()["certificate_id"])


def test_cross_org_certificate_lookup_is_404_and_audited(ctx: Ctx) -> None:
    cert = _org_b_certificate(ctx)
    path = CERT.format(certificate_id=cert)
    for user in (ADMIN_B, OP):  # 증명을 가진 기관과 운영자는 그대로 본다
        r = ctx.client.get(path, headers=ctx.as_user(user))
        assert r.status_code == 200, (user, r.text)
        check(r, "GET", CERT)
        assert r.json()["certificate_id"] == cert

    r = ctx.client.get(path, headers=ctx.as_user(ADMIN_A, mfa=True))
    assert r.status_code == 404, r.text
    check(r, "GET", CERT)
    problem = r.json()
    assert problem["code"] == "NOT_FOUND"
    rows = _audit_rows(ctx, problem["request_id"])
    assert [(x.action, x.result) for x in rows] == [("CROSS_ORG_ACCESS_ATTEMPT", "DENIED")], \
        "다른 기관 삭제 증명 조회에 교차 기관 시도 감사가 없습니다"
    audit = rows[0]
    assert (audit.actor_kind, audit.actor_id) == ("USER", fid("usr", ADMIN_A))
    assert (audit.organization_id, audit.target_type, audit.target_id) == (ORG_B, "DeletionCertificate", cert)
    assert audit.details["path"] == path


def test_unknown_certificate_is_404_without_audit(ctx: Ctx) -> None:
    unknown = new_id("deletion_certificate")
    r = ctx.client.get(CERT.format(certificate_id=unknown), headers=ctx.as_user(ADMIN_A))
    assert r.status_code == 404, r.text
    check(r, "GET", CERT)
    assert _audit_rows(ctx, r.json()["request_id"]) == []  # 없는 리소스는 교차 기관 시도가 아니다
