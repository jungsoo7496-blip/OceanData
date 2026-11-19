"""계약 점검 S43·S44 (ADR-0081): 사전 검증(contracts.prevalidate)이 운영 경로의 문서 규칙을 더하는 방식.

DB 없이 본다. 기관 하위코드 등록 여부는 호출자가 넘기는 함수로 정한다. 시험 데이터는 합성 값이다.
운영 경로와 판정이 같은지는 tests/integration/test_contract_validator_rules.py가 본다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.services import contracts
from app.services.contracts import contracts_dir, validate_document


def _example(name: str) -> dict[str, Any]:
    return json.loads((contracts_dir() / "examples" / name).read_text(encoding="utf-8"))


def _registered(_code: str) -> bool:
    return True


def _unregistered(_code: str) -> bool:
    return False


def _event(**fields: Any) -> dict[str, Any]:
    return {**_example("event.model-detection.json"), **fields}


def _item(**fields: Any) -> dict[str, Any]:
    return {**_example("observation-batch.ocean-sensor.json")["observations"][0], **fields}


def _batch(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {**_example("observation-batch.ocean-sensor.json"), "observations": items}


def _codes(problems: list[Any]) -> list[tuple[str, str]]:
    return [(p.pointer, p.code) for p in problems]


@pytest.mark.parametrize("name", sorted(p.name for p in (contracts_dir() / "examples").glob("*.json")
                                        if p.name.split(".")[0] in ("event", "observation-batch", "upload-manifest")))
def test_published_examples_get_no_rule_problem_once_their_subcodes_are_registered(name: str) -> None:
    contract, doc = name.split(".")[0], _example(name)
    problems, _ = validate_document(contract, doc)
    assert contracts.prevalidate(contract, doc, subcode_registered=_registered) == (True, problems)


def test_subcode_is_asked_of_the_callers_registry_only_for_subcodes() -> None:
    asked: list[str] = []

    def registry(code: str) -> bool:
        asked.append(code)
        return False

    assert contracts.prevalidate("event", _event(), subcode_registered=registry) == (True, [])
    valid, problems = contracts.prevalidate("event", _event(event_code="FLOATING_OBJECT.log"), subcode_registered=registry)
    assert not valid and _codes(problems) == [("/event_code", "unregistered_event_subcode")]
    assert asked == ["FLOATING_OBJECT.log"]


def test_time_order_is_compared_only_when_both_times_are_readable() -> None:
    """읽히지 않는 시각은 스키마 오류가 말한다. 시각 순서 문제를 덧붙이지 않는다."""
    valid, problems = contracts.prevalidate("event", _event(started_at="garbage"), subcode_registered=_registered)
    assert not valid and "before_start" not in {p.code for p in problems}
    doc = {**_example("upload-manifest.vessel-video.json"), "capture_start_at": "garbage",
           "capture_end_at": "2020-01-01T00:00:00Z"}
    valid, problems = contracts.prevalidate("upload-manifest", doc, subcode_registered=_registered)
    assert not valid and "time_rule" not in {p.code for p in problems}


def test_rule_problems_alone_follow_the_list_limit() -> None:
    doc = _batch([_item(metric=f"no_such_metric_{i}") for i in range(150)])
    valid, problems = contracts.prevalidate("observation-batch", doc, subcode_registered=_registered)
    assert not valid and len(problems) == contracts.MAX_PROBLEMS + 1
    assert {p.code for p in problems[:-1]} == {"unknown_metric"} and problems[-1].code == contracts.TRUNCATED_CODE
    assert problems[0].pointer == "/observations/0/metric"


def test_rule_problems_come_after_the_problems_validate_document_reports() -> None:
    """규칙 문제는 스키마·위치·항목 문제 뒤에 붙는다. 포인터 순서로 섞지 않는다 (ADR-0081 결정 5)."""
    doc = _batch([_item(metric="no_such_metric"), _item(quality_flag="NOPE"), _item(observed_at="2027-05-10T10:00:00")])
    valid, problems = contracts.prevalidate("observation-batch", doc, subcode_registered=_registered)
    assert not valid and _codes(problems) == [("/observations/1/quality_flag", "schema.enum"),
                                              ("/observations/2/observed_at", "timezone_required"),
                                              ("/observations/0/metric", "unknown_metric")]
    doc = _event(confidence=7, event_code="NOPE_CATEGORY", ended_at="2000-01-01T00:00:00Z")
    valid, problems = contracts.prevalidate("event", doc, subcode_registered=_registered)
    assert not valid and _codes(problems) == [("/confidence", "schema.maximum"),
                                              ("/event_code", "unknown_event_category"),
                                              ("/ended_at", "before_start")]


def test_rule_problems_past_a_full_list_keep_one_notice_at_the_end() -> None:
    """validate_document 문제가 이미 100개를 넘으면 규칙 문제는 잘리고 안내는 끝에 하나만 남는다."""
    doc = _batch([_item(quality_flag="NOPE") for _ in range(150)] + [_item(metric="no_such_metric")])
    valid, problems = contracts.prevalidate("observation-batch", doc, subcode_registered=_registered)
    assert not valid and len(problems) == contracts.MAX_PROBLEMS + 1
    assert [p.code for p in problems] == ["schema.enum"] * contracts.MAX_PROBLEMS + [contracts.TRUNCATED_CODE]


def test_a_list_validate_document_cut_short_stays_cut_with_one_notice() -> None:
    """validate_document가 자른 목록은 100개보다 짧아도(같은 자리의 스키마 오류를 항목 문제가 가린 경우) 다 모으지
    못한 목록이다. 규칙 문제를 더해도 잘린 목록으로 남고, 안내는 끝으로 옮겨 하나만 둔다."""
    doc = _batch([_item(observed_at="2027-05-10T10:00:00") for _ in range(60)] + [_item(metric="no_such_metric")])
    base, _ = validate_document("observation-batch", doc)
    assert contracts.is_truncated(base) and len(base) < contracts.MAX_PROBLEMS  # 이 시험이 보는 경로의 전제
    valid, problems = contracts.prevalidate("observation-batch", doc, subcode_registered=_registered)
    assert not valid
    assert _codes(problems) == [(f"/observations/{i}/observed_at", "timezone_required") for i in range(60)] + [
        ("/observations/60/metric", "unknown_metric"), ("", contracts.TRUNCATED_CODE)]


def test_verdict_sees_rule_problems_cut_from_the_list() -> None:
    """경고만으로 목록이 찬 뒤의 규칙 문제는 목록에서 잘려도 판정을 무효로 만든다 (ADR-0081 결정 5)."""
    doc = _batch([_item(unit="furlong_per_fortnight") for _ in range(150)] + [_item(metric="no_such_metric")])
    valid, problems = contracts.prevalidate("observation-batch", doc, subcode_registered=_registered)
    assert [p.code for p in problems] == ["unknown_unit"] * contracts.MAX_PROBLEMS + [contracts.TRUNCATED_CODE]
    assert not valid


def test_verdict_sees_item_errors_validate_document_would_cut_behind_warnings() -> None:
    """경고 100개 뒤 항목의 ERROR(같은 자리의 스키마 오류를 대신하는 시간대 힌트)도 판정을 무효로 만든다.
    운영 경로(POST /v1/observations)는 그 항목을 TIMEZONE_UNKNOWN으로 거부한다. 목록은 앞의 100개와 안내 하나다
    (통합 검토 VALIDATE-4: 전에는 100개에서 자른 목록으로 판정해 valid:true였다)."""
    doc = _batch([_item(unit="furlong_per_fortnight") for _ in range(100)] + [_item(observed_at="2027-05-10T10:00:00")])
    valid, problems = contracts.prevalidate("observation-batch", doc, subcode_registered=_registered)
    assert [p.code for p in problems] == ["unknown_unit"] * contracts.MAX_PROBLEMS + [contracts.TRUNCATED_CODE]
    assert not valid


def test_batch_production_cannot_check_item_by_item_is_invalid_even_with_warnings_only(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """운영 경로는 ITEM_PROBLEM_LIMIT까지 모은 목록도 잘리면 배치 전체를 400(too_many_problems)으로 거부한다.
    경고만 넘쳐도 같으므로 사전 검증도 무효다 (상한을 150으로 낮춰 본다)."""
    monkeypatch.setattr(contracts, "ITEM_PROBLEM_LIMIT", 150)
    doc = _batch([_item(unit="furlong_per_fortnight") for _ in range(151)])
    valid, problems = contracts.prevalidate("observation-batch", doc, subcode_registered=_registered)
    assert [p.code for p in problems] == ["unknown_unit"] * contracts.MAX_PROBLEMS + [contracts.TRUNCATED_CODE]
    assert not valid
    valid, _ = contracts.prevalidate("observation-batch", _batch([_item(unit="furlong_per_fortnight")] * 150),
                                     subcode_registered=_registered)
    assert valid  # 상한 안의 경고는 판정을 바꾸지 않는다 (SUSPECT로 저장)


def test_verdict_collects_the_production_list_only_when_the_cut_list_shows_no_error(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """앞 목록에 ERROR가 있으면 판정이 이미 정해져 운영 경로의 상한까지 다시 모으지 않는다 (문제가 많은 문서의 비용은
    전과 같다). 앞 목록이 경고로만 차서 잘렸을 때만 다시 모은다."""
    limits: list[int] = []
    original = contracts.validate_document

    def recording(contract: str, doc: Any, *, limit: int = contracts.MAX_PROBLEMS) -> Any:
        limits.append(limit)
        return original(contract, doc, limit=limit)

    monkeypatch.setattr(contracts, "validate_document", recording)
    many_errors = _batch([_item(quality_flag="NOPE") for _ in range(300)])
    assert contracts.prevalidate("observation-batch", many_errors, subcode_registered=_registered)[0] is False
    assert limits == [contracts.MAX_PROBLEMS]
    limits.clear()
    warnings_first = _batch([_item(unit="furlong_per_fortnight") for _ in range(120)])
    assert contracts.prevalidate("observation-batch", warnings_first, subcode_registered=_registered)[0] is True
    assert limits == [contracts.MAX_PROBLEMS, contracts.ITEM_PROBLEM_LIMIT]


def test_rules_are_not_checked_where_validate_document_stops() -> None:
    """스키마 버전 거부·너무 깊은 문서는 운영 경로도 거기서 멈춘다. 그 문제 하나만 돌려준다."""
    bad = {"event_code": "NOPE_CATEGORY", "ended_at": "2000-01-01T00:00:00Z"}
    valid, problems = contracts.prevalidate("event", _event(schema_version="9.0.0", **bad),
                                            subcode_registered=_unregistered)
    assert not valid and _codes(problems) == [("/schema_version", "UNSUPPORTED_SCHEMA_VERSION")]
    deep: Any = "x"
    for _ in range(contracts.MAX_DEPTH + 1):
        deep = [deep]
    valid, problems = contracts.prevalidate("event", _event(note=deep, **bad), subcode_registered=_unregistered)
    assert not valid and _codes(problems) == [("", "schema.depth")]
