"""시각 입력 오류가 두 쓰기 API의 400 응답 계약을 따른다."""

import pytest

from app.seed import DEV_A_SENSOR, SES_A
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
INVALID_TIMES = ["2026-11-01T10:00:00", "invalid-time", "2026-02-30T10:00:00Z",
                 "2026-11-01T19:00:00+09:00", None]


@pytest.mark.parametrize("ended_at", INVALID_TIMES, ids=["naive", "text", "date", "offset", "null"])
def test_event_patch_rejects_invalid_end_time(ctx: Ctx, ended_at: str | None) -> None:
    headers = ctx.as_user(102)
    created = ctx.client.post("/v1/events", headers=headers, json={
        "schema_version": "1.0.0", "session_id": SES_A, "started_at": "2026-11-01T09:00:00Z",
        "timestamp_quality": {"source": "DEVICE", "verified": False},
        "event_code": "SAFETY_INCIDENT", "source": "MANUAL", "severity": "WARNING", "note": "합성 원래 내용",
    })
    assert created.status_code == 201, created.text
    path = f"/v1/events/{created.json()['event_id']}"
    response = ctx.client.patch(path, headers=headers, json={"ended_at": ended_at, "note": "합성 변경"})
    assert response.status_code == 400, response.text
    check(response, "PATCH", "/v1/events/{event_id}")
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.json()["errors"][0]["pointer"] == "/ended_at"
    assert ctx.client.get(path, headers=headers).json()["note"] == "합성 원래 내용"


@pytest.mark.parametrize("observed_at", INVALID_TIMES[:-1], ids=["naive", "text", "date", "offset"])
def test_observation_correction_rejects_invalid_time(ctx: Ctx, observed_at: str) -> None:
    path = "/v1/observations/corrections"
    response = ctx.client.post(path, headers=ctx.as_user(101), json={
        "source_system_id": DEV_A_SENSOR, "metric": "sea_water_temperature",
        "observed_at": observed_at, "value": 11, "reason": "합성 관측값 정정",
    })
    assert response.status_code == 400, response.text
    check(response, "POST", path)
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert response.json()["errors"][0]["pointer"] == "/observed_at"
