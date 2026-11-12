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


def test_rule_problems_follow_the_list_limit_and_the_verdict_sees_them_all() -> None:
    doc = _example("observation-batch.ocean-sensor.json")
    item = doc["observations"][0]
    doc["observations"] = [{**item, "metric": f"no_such_metric_{i}"} for i in range(150)]
    valid, problems = contracts.prevalidate("observation-batch", doc, subcode_registered=_registered)
    assert not valid and len(problems) == contracts.MAX_PROBLEMS + 1
    assert {p.code for p in problems[:-1]} == {"unknown_metric"} and problems[-1].code == contracts.TRUNCATED_CODE
    assert problems[0].pointer == "/observations/0/metric"


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
