"""03번 요구사항 13.2·4: 사전 검증 API와 운영 업로드·관측값·이벤트 API가 같은 엄격한 검증을 한다 (지시서 12번 — ADR-0056).

전에는 잘못된 값이 검증을 통과하고 뒤에서 500이 되거나(끝 개행이 붙은 체크섬, 존재하지 않는 날짜), 저장돼 보고서 생성을
깨뜨렸다(NaN). 시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.seed import DEV_A_SENSOR, SES_A
from app.services.contracts import contracts_dir
from tests.integration.test_ingest import manifest

pytestmark = pytest.mark.integration
T = (datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
JSON = {"Content-Type": "application/json"}


def _item(**kw: Any) -> dict[str, Any]:
    return {"metric": "sea_water_temperature", "observed_at": T, "value": 1.0, "unit": "degC",
            "quality_flag": "GOOD", **kw}


def _batch(*items: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": "1.0.0", "source_system_id": DEV_A_SENSOR, "session_id": SES_A,
            "observations": list(items)}


def _observations(ctx) -> int:  # type: ignore[no-untyped-def]
    with ctx.c.engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM observations")).scalar_one())


def test_validate_api_reports_malformed_documents_instead_of_500(ctx) -> None:
    bad_time = _batch(_item(observed_at="0001-01-01T00:00:00+23:59"))
    for doc in ({**_batch(_item()), "observations": 1.5}, bad_time,
                {**_batch(_item()), "location": {"type": "LineString", "coordinates": 5}}):
        r = ctx.client.post("/v1/contracts/validate", json={"contract": "observation-batch", "document": doc},
                            headers=ctx.as_user(102))
        assert r.status_code == 200, r.text
        assert r.json()["valid"] is False


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_observation_value_is_rejected_and_nothing_is_stored(ctx, bad: str) -> None:
    """항목의 값 오류는 항목별 REJECTED다(요구사항 1.3). 전에는 NaN이 숫자로 통과해 저장됐고, 그 구간을 포함한 보고서 생성이 500이었다."""
    before = _observations(ctx)
    raw = json.dumps(_batch(_item(value=0.0))).replace('"value": 0.0', f'"value": {bad}')
    assert bad in raw
    r = ctx.client.post("/v1/observations", content=raw, headers={**JSON, **ctx.as_user(102)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["accepted"], body["rejected"]) == (0, 1) and body["results"][0]["status"] == "REJECTED", body
    assert _observations(ctx) == before
    v = ctx.client.post("/v1/contracts/validate", headers={**JSON, **ctx.as_user(102)},
                        content='{"contract": "observation-batch", "document": ' + raw + "}")
    assert v.status_code == 200 and v.json()["valid"] is False
    assert any(p["pointer"] == "/observations/0/value" for p in v.json()["problems"])


def test_checksum_with_trailing_newline_is_400_not_a_database_error(ctx) -> None:
    data = b"x" * 64
    sha = hashlib.sha256(data).hexdigest()
    r = ctx.client.post("/v1/uploads", json=manifest(data, source=DEV_A_SENSOR, data_type="SENSOR", variant="ORIGINAL",
                                                      mime="text/csv", sha=sha + "\n"),
                        headers=ctx.as_user(102))
    assert r.status_code == 400, r.text
    assert any(e["pointer"] == "/file/checksum_sha256" for e in r.json()["errors"])


def test_event_with_a_date_that_does_not_exist_is_400(ctx) -> None:
    doc = json.loads((contracts_dir() / "examples" / "event.manual-safety.json").read_text(encoding="utf-8"))
    doc["started_at"] = "2026-02-30T00:00:00Z"
    r = ctx.client.post("/v1/events", json=doc, headers=ctx.as_user(102))
    assert r.status_code == 400, r.text
    assert any(e["pointer"] == "/started_at" for e in r.json()["errors"])


def test_a_batch_with_many_bad_items_rejects_each_item_not_crashes(ctx) -> None:
    """문제가 100개를 넘어도 항목별 판정은 끝까지 간다: 150개 항목이 모두 REJECTED이고 500이 아니다.
    (문제 목록을 100개에서 자르면 뒤쪽 잘못된 항목이 검사 없이 정규화로 넘어가 예외가 난다)"""
    before = _observations(ctx)
    items = [_item(value="abc") for _ in range(150)]
    r = ctx.client.post("/v1/observations", json=_batch(*items), headers=ctx.as_user(102))
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["accepted"], body["rejected"]) == (0, 150), body
    assert _observations(ctx) == before


def test_the_400_body_lists_at_most_100_problems(ctx) -> None:
    """항목마다 다른 이유로 배치 전체가 400이어도 응답은 앞의 100개와 안내 하나다 (요청보다 응답이 크지 않게)."""
    items = [_item(bogus=i) for i in range(150)]  # 계약에 없는 필드: 항목별 사유가 없어 배치 전체 400
    r = ctx.client.post("/v1/observations", json=_batch(*items), headers=ctx.as_user(102))
    assert r.status_code == 400, r.text
    errors = r.json()["errors"]
    assert len(errors) == 101 and errors[-1]["code"] == "schema.truncated"

