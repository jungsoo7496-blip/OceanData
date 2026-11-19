"""계약 점검 S43·S44: 사전 검증이 운영 경로의 코드표·시각 순서 규칙도 본다 (03번 요구사항 13.1·13.2, ADR-0081).

전에는 사전 검증이 스키마·위치·관측 항목 힌트만 봤다. 운영 경로가 그 뒤에 따로 보는 규칙(이벤트 상위분류·기관 하위코드,
관측 항목 코드, 이벤트·매니페스트의 시각 순서)은 보지 않아, 운영 경로가 거부하는 문서에 valid:true를 답했다.
각 시험은 같은 문서를 사전 검증과 운영 경로에 보내 판정이 같은지 본다. 운영 경로의 응답 본문과 검사 순서는 그대로다.
시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.db.models import OrgEventCode
from app.seed import DEV_A_SENSOR, ORG_A, SES_A, SES_B
from app.services.contracts import contracts_dir
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
V = "/v1/contracts/validate"
T = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
UNKNOWN_CATEGORY = "공통 상위분류에 없는 이벤트 코드입니다"
UNREGISTERED = "기관 하위코드가 등록되어 있지 않습니다"
CATEGORY_CONDITION = "event_code 상위분류 ∈ event-categories"
BEFORE_START = {"pointer": "/ended_at", "code": "before_start", "message": "종료가 시작보다 빠릅니다"}
TIME_RULE = {"pointer": "/capture_start_at", "code": "time_rule", "message": "capture_end_at이 capture_start_at보다 빠릅니다"}


def z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _event(**kw: Any) -> dict[str, Any]:
    return {"schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL", "started_at": z(T),
            "timestamp_quality": {"source": "DEVICE", "verified": False}, "source": "MANUAL", **kw}


def _item(**kw: Any) -> dict[str, Any]:
    return {"metric": "sea_water_temperature", "observed_at": z(T), "value": 1.0, "unit": "degC",
            "quality_flag": "GOOD", **kw}


def _batch(*items: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": "1.0.0", "source_system_id": DEV_A_SENSOR, "session_id": SES_A,
            "observations": list(items)}


def _manifest(**kw: Any) -> dict[str, Any]:
    doc = json.loads((contracts_dir() / "examples" / "upload-manifest.vessel-video.json").read_text(encoding="utf-8"))
    return {**doc, **kw}


def _validate(ctx: Any, contract: str, doc: dict[str, Any], user: int = 102) -> dict[str, Any]:
    r = ctx.client.post(V, headers=ctx.as_user(user), json={"contract": contract, "document": doc})
    assert r.status_code == 200, r.text
    check(r, "POST", V)
    return r.json()  # type: ignore[no-any-return]


def _error(problem: dict[str, str]) -> dict[str, str]:
    return {**problem, "severity": "ERROR"}


def _register(ctx: Any, organization_id: str, code: str) -> None:
    with ctx.c.sessionmaker.begin() as db:
        db.add(OrgEventCode(organization_id=organization_id, code=code, label="시험 하위코드", created_by="t",
                            created_at=datetime.now(UTC)))


# ---------------------------------------------------------------- S43 코드표

@pytest.mark.covers("T05-f")
@pytest.mark.parametrize(("code", "problem_code", "message", "condition", "actual"), [
    ("NOPE_CATEGORY", "unknown_event_category", UNKNOWN_CATEGORY, CATEGORY_CONDITION, "NOPE_CATEGORY"),
    ("NOPE_CATEGORY.small_boat", "unknown_event_category", UNKNOWN_CATEGORY, CATEGORY_CONDITION, "NOPE_CATEGORY"),
    ("SAFETY_INCIDENT.near_miss", "unregistered_event_subcode", UNREGISTERED, "기관 하위코드 등록",
     "SAFETY_INCIDENT.near_miss"),
], ids=["unknown-category", "unknown-category-with-subcode", "unregistered-subcode"])
def test_event_code_outside_the_code_tables_is_invalid_as_production_rejects_it(
        ctx: Any, code: str, problem_code: str, message: str, condition: str, actual: str) -> None:
    doc = _event(event_code=code)
    assert _validate(ctx, "event", doc) == {
        "valid": False, "problems": [_error({"pointer": "/event_code", "code": problem_code, "message": message})]}
    r = ctx.client.post("/v1/events", headers=ctx.as_user(102), json=doc)
    assert r.status_code == 422, r.text
    body = r.json()
    assert (body["code"], body["detail"], body["unmet_conditions"]) == (
        "RULE_VIOLATION", message, [{"condition": condition, "actual": actual}])


def test_published_event_example_needs_its_subcode_registered_by_the_caller(ctx: Any) -> None:
    """공개 예시(SAFETY_INCIDENT.near_miss)는 그 하위코드를 등록한 기관에서만 유효하다. 하위코드는 호출자 기관 기준이다."""
    doc = json.loads((contracts_dir() / "examples" / "event.manual-safety.json").read_text(encoding="utf-8"))
    del doc["linked_asset_ids"]  # 예시의 자산은 시험 DB에 없다
    doc.update(session_id=SES_A, started_at=z(T), ended_at=z(T + timedelta(minutes=5)))
    unregistered = _error({"pointer": "/event_code", "code": "unregistered_event_subcode", "message": UNREGISTERED})
    assert _validate(ctx, "event", doc) == {"valid": False, "problems": [unregistered]}
    _register(ctx, ORG_A, doc["event_code"])
    assert _validate(ctx, "event", doc) == {"valid": True, "problems": []}
    assert ctx.client.post("/v1/events", headers=ctx.as_user(102), json=doc).status_code == 201
    # 다른 기관(B)이 등록한 하위코드가 아니다: B 사용자에게는 여전히 무효이고 운영 경로도 422
    other = {**doc, "session_id": SES_B}
    assert _validate(ctx, "event", other, user=104) == {"valid": False, "problems": [unregistered]}
    assert ctx.client.post("/v1/events", headers=ctx.as_user(104), json=other).status_code == 422


def test_unknown_metric_is_invalid_as_production_rejects_the_item(ctx: Any) -> None:
    doc = _batch(_item(), _item(metric="no_such_metric"))
    assert _validate(ctx, "observation-batch", doc) == {"valid": False, "problems": [_error({
        "pointer": "/observations/1/metric", "code": "unknown_metric",
        "message": "관측 항목 코드표(metrics)에 없는 항목입니다 (항목을 UNKNOWN_METRIC으로 거부)"})]}
    r = ctx.client.post("/v1/observations", headers=ctx.as_user(102), json=doc)
    assert r.status_code == 200, r.text
    assert r.json()["results"] == [{"index": 0, "status": "ACCEPTED"},
                                   {"index": 1, "status": "REJECTED", "reason": "UNKNOWN_METRIC"}]


def test_values_the_schema_already_rejects_get_no_second_problem(ctx: Any) -> None:
    """모양이 계약에 맞지 않는 코드는 스키마 오류 하나로만 나온다 (코드표 문제를 겹쳐 붙이지 않는다)."""
    v = _validate(ctx, "event", _event(event_code="vessel"))
    assert [(p["pointer"], p["code"]) for p in v["problems"]] == [("/event_code", "schema.pattern")]
    v = _validate(ctx, "observation-batch", _batch(_item(metric="No-Metric")))
    assert [(p["pointer"], p["code"]) for p in v["problems"]] == [("/observations/0/metric", "schema.pattern")]


def test_known_codes_stay_valid(ctx: Any) -> None:
    assert _validate(ctx, "event", _event()) == {"valid": True, "problems": []}
    assert _validate(ctx, "event", _event(event_code="SAFETY_INCIDENT")) == {"valid": True, "problems": []}
    assert _validate(ctx, "observation-batch", _batch(_item(), _item(metric="wind_speed", unit="m/s"))) == {
        "valid": True, "problems": []}


# ---------------------------------------------------------------- S44 시각 순서

@pytest.mark.covers("T05-f")
@pytest.mark.parametrize(("started", "ended"), [
    (z(T), z(T - timedelta(hours=1))),
    ("2026-11-10T10:00:00.5Z", "2026-11-10T10:00:00Z"),  # 문자열로는 앞서지만 시각으로는 0.5초 뒤 — UTC 값으로 비교한다
], ids=["hour-earlier", "half-second-earlier"])
def test_event_ending_before_it_starts_is_invalid_as_production_rejects_it(ctx: Any, started: str, ended: str) -> None:
    doc = _event(started_at=started, ended_at=ended)
    assert _validate(ctx, "event", doc) == {"valid": False, "problems": [_error(BEFORE_START)]}
    r = ctx.client.post("/v1/events", headers=ctx.as_user(102), json=doc)
    assert r.status_code == 400, r.text
    assert r.json()["errors"] == [BEFORE_START]


def test_event_ending_when_it_starts_stays_valid(ctx: Any) -> None:
    doc = _event(ended_at=z(T))
    assert _validate(ctx, "event", doc) == {"valid": True, "problems": []}
    assert ctx.client.post("/v1/events", headers=ctx.as_user(102), json=doc).status_code == 201


def test_validator_lists_every_rule_and_production_keeps_its_order(ctx: Any) -> None:
    """사전 검증은 규칙 위반을 모두 말한다. 운영 경로의 검사 순서(회차 범위 404 → 코드 422 → 시각 순서 400)는 그대로다."""
    doc = _event(event_code="NOPE_CATEGORY", ended_at=z(T - timedelta(hours=1)))
    assert _validate(ctx, "event", doc) == {"valid": False, "problems": [
        _error({"pointer": "/event_code", "code": "unknown_event_category", "message": UNKNOWN_CATEGORY}),
        _error(BEFORE_START)]}
    h = ctx.as_user(102)
    assert ctx.client.post("/v1/events", headers=h, json=doc).status_code == 422
    assert ctx.client.post("/v1/events", headers=h, json={**doc, "session_id": SES_B}).status_code == 404


@pytest.mark.covers("T05-f")
@pytest.mark.parametrize(("start", "end"), [
    ("2027-05-10T01:30:00Z", "2020-01-01T00:00:00Z"),
    ("2027-05-10T01:30:00.5Z", "2027-05-10T01:30:00Z"),
], ids=["years-earlier", "half-second-earlier"])
def test_manifest_capture_ending_before_it_starts_is_invalid_as_production_rejects_it(
        ctx: Any, start: str, end: str) -> None:
    doc = _manifest(capture_start_at=start, capture_end_at=end)  # 근거 출처 GPS
    assert _validate(ctx, "upload-manifest", doc) == {"valid": False, "problems": [_error(TIME_RULE)]}
    r = ctx.client.post("/v1/uploads", headers=ctx.as_user(102), json=doc)
    assert r.status_code == 400, r.text
    assert r.json()["errors"] == [TIME_RULE]


def test_manifest_time_rule_only_for_evidence_sources(ctx: Any) -> None:
    """근거 출처가 UNKNOWN이면 운영 경로는 시각을 버린다(시각 순서 오류가 아니다). 사전 검증도 time_rule을 붙이지 않고
    스키마 오류(UNKNOWN이면 시각은 null)만 운영 경로와 같게 말한다."""
    doc = _manifest(capture_end_at="2020-01-01T00:00:00Z", timestamp_quality={"source": "UNKNOWN", "verified": False})
    v = _validate(ctx, "upload-manifest", doc)
    r = ctx.client.post("/v1/uploads", headers=ctx.as_user(102), json=doc)
    assert r.status_code == 400, r.text
    assert v["valid"] is False
    assert [(p["pointer"], p["code"]) for p in v["problems"] if p["severity"] == "ERROR"] == [
        (e["pointer"], e["code"]) for e in r.json()["errors"]]
    assert "time_rule" not in {p["code"] for p in v["problems"]}
    assert _validate(ctx, "upload-manifest", _manifest(capture_end_at=None)) == {"valid": True, "problems": []}
