"""07번 요구사항 1.2·1.3: 관측값 배치 검증에서 항목의 경고·오류가 문서 루트·다른 항목의 형식 오류를 가리지 않는다.

source_system_id가 없는 배치는 항목에 단위 경고(unknown_unit)나 시각 오류가 함께 있어도 500이 아니라 400이고,
아무것도 저장하지 않는다. 시험 데이터는 합성 값이다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.seed import DEV_A_SENSOR, SES_A
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
T = (datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(**kw: object) -> dict:
    return {"metric": "sea_water_temperature", "observed_at": T, "value": 1.0, "unit": "degC",
            "quality_flag": "GOOD", **kw}


def _count(ctx) -> int:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM observations")).scalar_one())


@pytest.mark.parametrize("item", [_item(unit="zzz"), _item(observed_at=T.removesuffix("Z"))],
                         ids=["unknown-unit-warning", "timezone-error"])
def test_missing_source_system_id_is_400_not_500(ctx, item: dict) -> None:
    before = _count(ctx)
    body = {"schema_version": "1.0.0", "observations": [item]}
    r = ctx.client.post("/v1/observations", json=body, headers=ctx.as_user(102))
    assert r.status_code == 400, r.text
    check(r, "POST", "/v1/observations")
    assert r.json()["code"] == "VALIDATION_ERROR"
    assert {"pointer": "", "code": "schema.required"}.items() <= r.json()["errors"][0].items()
    assert _count(ctx) == before


def test_warning_on_item_ten_keeps_format_error_on_item_one(ctx) -> None:
    items = [_item(observed_at=T) for _ in range(11)]
    items[1] = _item(bogus=1)  # 계약에 없는 필드는 배치 전체 400 (요구사항 1.2)
    items[10] = _item(unit="zzz")
    body = {"schema_version": "1.0.0", "source_system_id": DEV_A_SENSOR, "session_id": SES_A, "observations": items}
    before = _count(ctx)
    r = ctx.client.post("/v1/observations", json=body, headers=ctx.as_user(102))
    assert r.status_code == 400, r.text
    assert [e["pointer"] for e in r.json()["errors"]] == ["/observations/1"]
    assert _count(ctx) == before
