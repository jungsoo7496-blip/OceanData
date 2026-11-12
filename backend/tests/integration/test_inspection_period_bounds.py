"""정기 점검 기한의 날짜 범위를 검사한다 (계약 점검 13번)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db.models import Inspection
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract

pytestmark = pytest.mark.integration
INSPECTIONS_PATH = "/v1/inspections"


@pytest.mark.parametrize(("cycle", "period_key"), [
    ("DAILY", "9999-12-31"),
    ("WEEKLY", "9999-W52"),
    ("MONTHLY", "9999-12"),
    ("QUARTERLY", "9999-Q4"),
    ("YEARLY", "9999"),
])
def test_inspection_rejects_unrepresentable_due_at(ctx: Ctx, cycle: str, period_key: str) -> None:
    response = ctx.client.post(INSPECTIONS_PATH, headers=ctx.as_user(106), json={
        "cycle": cycle, "period_key": period_key,
    })
    assert response.status_code == 400, response.text
    assert_matches_contract(response, "POST", INSPECTIONS_PATH)
    problem = response.json()
    assert problem["code"] == "VALIDATION_ERROR"
    assert [error["pointer"] for error in problem["errors"]] == ["/period_key"]
    with ctx.c.sessionmaker() as db:
        assert db.scalar(select(Inspection.inspection_id)) is None
    assert "INSPECTION_CREATED" not in ctx.audit_actions()


def test_inspection_accepts_adjacent_representable_due_at(ctx: Ctx) -> None:
    operator = ctx.as_user(106)
    body = {"cycle": "DAILY", "period_key": "9999-12-30"}
    response = ctx.client.post(INSPECTIONS_PATH, headers=operator, json=body)
    assert response.status_code == 201, response.text
    assert_matches_contract(response, "POST", INSPECTIONS_PATH)
    midnight = datetime(9999, 12, 31, tzinfo=ZoneInfo(ctx.c.settings.ops_timezone))
    assert datetime.fromisoformat(response.json()["due_at"]) == midnight.astimezone(UTC)
    duplicate = ctx.client.post(INSPECTIONS_PATH, headers=operator, json=body)
    assert duplicate.status_code == 409, duplicate.text
    assert_matches_contract(duplicate, "POST", INSPECTIONS_PATH)
    assert ctx.audit_actions().count("INSPECTION_CREATED") == 1
