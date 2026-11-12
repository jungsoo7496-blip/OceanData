"""10번 보고서 계약 점검 S34~S38 (ADR-0080).

- S36 내보내기는 보고서·버전 범위를 먼저 확인한다. 형식이 꺼져 있어도 다른 기관·없는 보고서는 404(+감사)다.
"""

from __future__ import annotations

import pytest

from app.seed import SES_A
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
ADMIN, MEMBER, OTHER_ORG, VIEWER, OP = 101, 102, 103, 105, 106
GET = "/v1/reports/{report_id}"
EXPORT = "/v1/reports/{report_id}/export"
TPL = "/v1/report-templates/EXAMPLE_SESSION_LOG/versions/1/hwpx"


def _new_report(c: Ctx) -> str:
    r = c.client.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A},
                      headers=c.as_user(MEMBER))
    assert r.status_code == 201, r.text
    return str(r.json()["report_id"])


def _approve_and_submit(c: Ctx, rid: str) -> None:
    for step in ("approve", "submit"):
        r = c.client.post(f"/v1/reports/{rid}/{step}", headers=c.as_user(ADMIN))
        assert r.status_code == 200, r.text


# ------------------------------------------------------------------ S36 내보내기 확인 순서


def test_export_checks_report_scope_before_the_format_gate(ctx: Ctx) -> None:
    """ctx는 REPORT_FORMATS가 비어 있다. 그래도 다른 기관 보고서는 404 + CROSS_ORG_ACCESS_ATTEMPT, 없는 것도 404."""
    c, hm = ctx.client, ctx.as_user(MEMBER)
    rid = _new_report(ctx)
    path = EXPORT.format(report_id=rid)
    before = ctx.audit_actions().count("CROSS_ORG_ACCESS_ATTEMPT")
    r = c.get(path, params={"format": "XLSX"}, headers=ctx.as_user(OTHER_ORG))
    check(r, "GET", EXPORT)
    assert (r.status_code, r.json()["code"]) == (404, "NOT_FOUND"), "형식 확인이 범위 확인보다 먼저다"
    assert ctx.audit_actions().count("CROSS_ORG_ACCESS_ATTEMPT") == before + 1
    missing = c.get(EXPORT.format(report_id="rpt_" + "0" * 26), params={"format": "XLSX"}, headers=hm)
    assert missing.status_code == 404, missing.text
    assert c.get(path, params={"format": "XLSX", "version": 9}, headers=hm).status_code == 404
    own = c.get(path, params={"format": "XLSX"}, headers=hm)  # 볼 수 있는 보고서에만 형식 409
    check(own, "GET", EXPORT)
    assert own.status_code == 409 and "REPORT_FORMATS" in own.json()["detail"], own.text
