"""이용 신청 기간의 날짜 — 달력에 없는 날짜와 기간 끝을 표현할 수 없는 끝 날짜는 400 (계약 점검 S07)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.db.models import AccessRequest
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract
from tests.integration.test_grant_access_request_tenant_audit import CONSUMER, published_version

pytestmark = pytest.mark.integration
PATH = "/v1/access-requests"
BAD = {  # (시작, 끝) → 오류 위치
    ("2026-02-30", "2026-12-31"): "/period/start_date",
    ("2026-13-01", "2026-12-31"): "/period/start_date",
    ("2026-12-01", "2027-02-30"): "/period/end_date",
    ("2026-12-01", "2027-01-00"): "/period/end_date",
    ("2026-12-01", "9999-12-31"): "/period/end_date",  # 기간 끝(다음 날 0시)을 표현할 수 없다
}


def _body(vid: str, start: str, end: str) -> dict:
    return {"dataset_version_id": vid, "purpose": "AI_TRAINING", "purpose_detail": "연안 객체 탐지 모델 학습용",
            "period": {"start_date": start, "end_date": end},
            "security_measures": "사내 격리 서버 보관, 접근 2인 제한, 종료 후 파기", "agreement_accepted": True}


def test_access_request_rejects_impossible_or_unrepresentable_dates(ictx: Ctx) -> None:
    ctx = ictx
    _, vid = published_version(ctx)
    got = {}
    for (start, end), pointer in BAD.items():
        r = ctx.client.post(PATH, headers=ctx.as_user(CONSUMER), json=_body(vid, start, end))
        problem = r.json()
        got[(start, end)] = (r.status_code, problem.get("code"), [e["pointer"] for e in problem.get("errors", [])])
        if r.status_code == 400:
            assert_matches_contract(r, "POST", PATH)
    assert got == {k: (400, "VALIDATION_ERROR", [pointer]) for k, pointer in BAD.items()}
    with ctx.c.sessionmaker() as db:
        assert db.scalar(select(func.count()).select_from(AccessRequest)) == 0
    assert "ACCESS_REQUESTED" not in ctx.audit_actions()


def test_access_request_accepts_last_representable_end_date(ictx: Ctx) -> None:
    ctx = ictx
    _, vid = published_version(ctx)
    r = ctx.client.post(PATH, headers=ctx.as_user(CONSUMER), json=_body(vid, "2026-12-01", "9999-12-30"))
    assert r.status_code == 201, r.text
    assert_matches_contract(r, "POST", PATH)
    with ctx.c.sessionmaker() as db:
        req = db.get(AccessRequest, r.json()["access_request_id"])
        assert req.period_end == datetime(9999, 12, 31, tzinfo=UTC)  # 끝 날짜 다음 날 0시 (UTC)
