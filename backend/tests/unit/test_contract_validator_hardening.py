"""03번 요구사항 13.2·4: 계약 검증기 보강 (지시서 12번 독립 검토 — ADR-0056).

사전 검증 API와 운영 업로드가 같은 함수를 쓰므로, 이 함수는 (1) 어떤 JSON이 와도 예외 없이 문제 목록을 돌려주고
(2) 계약이 정한 값만 통과시키며(유한한 수, 끝 개행 없는 패턴, 실제 달력의 날짜-시각) (3) 응답 크기를 제한해야 한다.
시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.services import contracts
from app.services.contracts import contracts_dir, validate_document


def _example(name: str) -> dict[str, Any]:
    return json.loads((contracts_dir() / "examples" / name).read_text(encoding="utf-8"))


def _batch() -> dict[str, Any]:
    return _example("observation-batch.ocean-sensor.json")


def _manifest() -> dict[str, Any]:
    return _example("upload-manifest.vessel-video.json")


def _errors(problems: list[Any]) -> list[tuple[str, str]]:
    return [(p.pointer, p.code) for p in problems if p.severity == "ERROR"]


def test_valid_examples_stay_valid() -> None:
    for contract, doc in (("observation-batch", _batch()), ("upload-manifest", _manifest())):
        problems, rejected = validate_document(contract, doc)
        assert not rejected and _errors(problems) == [], (contract, problems)


# ---------------------------------------------------------------- 1. 예외 없이 문제로 돌려준다 (전에는 500)

def _with_obs(**fields: Any) -> dict[str, Any]:
    doc = _batch()
    doc["observations"][0].update(fields)
    return doc


def _with_location(geometry: Any) -> dict[str, Any]:
    doc = _manifest()
    doc["location"] = geometry
    return doc


CRASH_INPUTS: list[tuple[str, str, dict[str, Any]]] = [
    ("observation-batch", "observations가 숫자", {**_batch(), "observations": 1.5}),
    ("observation-batch", "observations가 참인 문자열", {**_batch(), "observations": "abc"}),
    ("observation-batch", "시각 UTC 변환 넘침(+23:59)", _with_obs(observed_at="0001-01-01T00:00:00+23:59")),
    ("observation-batch", "시각 UTC 변환 넘침(-23:59)", _with_obs(observed_at="9999-12-31T23:59:59-23:59")),
    ("upload-manifest", "LineString 좌표가 숫자", _with_location({"type": "LineString", "coordinates": 5})),
    ("upload-manifest", "Polygon 좌표가 [숫자]", _with_location({"type": "Polygon", "coordinates": [1]})),
    ("upload-manifest", "Polygon 좌표가 [null]", _with_location({"type": "Polygon", "coordinates": [None]})),
    ("upload-manifest", "Polygon 좌표가 숫자", _with_location({"type": "Polygon", "coordinates": 7})),
    ("upload-manifest", "Point 좌표가 400자리 정수", _with_location({"type": "Point", "coordinates": [10**400, 1]})),
    ("upload-manifest", "Point 좌표가 불리언", _with_location({"type": "Point", "coordinates": [True, False]})),
]


@pytest.mark.parametrize(("contract", "name", "doc"), CRASH_INPUTS, ids=[c[1] for c in CRASH_INPUTS])
def test_malformed_input_is_reported_not_raised(contract: str, name: str, doc: dict[str, Any]) -> None:
    problems, rejected = validate_document(contract, doc)
    assert not rejected
    assert _errors(problems), f"{name}: 잘못된 입력인데 오류 문제가 없다"


def test_very_deep_nesting_is_reported_not_raised() -> None:
    deep: Any = []
    for _ in range(9000):
        deep = [deep]
    doc = _batch()
    doc["observations"][0]["value"] = deep
    problems, _ = validate_document("observation-batch", doc)
    assert ("", "schema.depth") in _errors(problems)


def test_overflow_time_is_one_clear_problem() -> None:
    problems, _ = validate_document("observation-batch", _with_obs(observed_at="9999-12-31T23:59:59-23:59"))
    assert ("/observations/0/observed_at", "invalid_time") in _errors(problems)


def test_garbage_time_keeps_the_schema_error_not_a_misleading_timezone_hint() -> None:
    problems, _ = validate_document("observation-batch", _with_obs(observed_at="garbage"))
    codes = {c for p, c in _errors(problems) if p == "/observations/0/observed_at"}
    assert codes and "timezone_required" not in codes, codes


def test_missing_offset_still_gets_the_timezone_hint() -> None:
    problems, _ = validate_document("observation-batch", _with_obs(observed_at="2026-11-02T00:00:00"))
    assert ("/observations/0/observed_at", "timezone_required") in _errors(problems)


# ---------------------------------------------------------------- 2. 계약이 정한 값만 통과한다

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_not_numbers(bad: float) -> None:
    problems, _ = validate_document("observation-batch", _with_obs(value=bad))
    assert ("/observations/0/value", "schema.type") in _errors(problems)


def test_non_finite_number_in_an_unrelated_field_is_rejected_too() -> None:
    doc = _manifest()
    doc["file"]["byte_size"] = float("nan")
    assert _errors(validate_document("upload-manifest", doc)[0])


@pytest.mark.parametrize(("path", "value"), [
    (("file", "checksum_sha256"), "b648d973167961361eac888761515e508ce51a7e3001d1a821e7569289a084b4\n"),
    (("file", "mime_type"), "video/mp4\n"),
    (("source_system_id",), "dev_01JD0000000000000000001001\n"),
    (("project_id",), "prj_01JD0000000000000000000011\n"),
])
def test_trailing_newline_does_not_satisfy_an_anchored_pattern(path: tuple[str, ...], value: str) -> None:
    doc = _manifest()
    node = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    problems, _ = validate_document("upload-manifest", doc)
    assert any(p.startswith("/" + "/".join(path)) for p, _ in _errors(problems)), problems


def test_published_schemas_keep_ecma_262_patterns() -> None:
    """검증용 사본만 끝 `$`를 `\\Z`로 바꾼다. 클라이언트에 내려주는 스키마는 ECMA-262 문법 그대로다."""
    text = json.dumps(contracts.bundle("upload-manifest"))
    assert "\\\\Z" not in text and '$"' in text


@pytest.mark.parametrize("value", [
    "2026-11-02T00:00:00Z", "2026-11-02T09:00:00+09:00", "2026-11-02T00:00:00.123Z", "2024-02-29T12:00:00Z",
    "2026-11-02T23:59:59-23:59",
])
def test_valid_date_times_pass(value: str) -> None:
    assert contracts.CONTRACT_FORMATS.conforms(value, "date-time")


@pytest.mark.parametrize("value", [
    "2026-13-02T00:00:00Z", "2026-02-30T00:00:00Z", "2026-11-31T25:00:00Z", "2026-11-02T24:00:00Z",
    "2026-11-02T00:60:00Z", "2026-11-02T00:00:00+24:00", "2026-11-02T00:00:00", "2026-11-02", "garbage",
    "2026-11-02T00:00:00Z\n", "2025-02-29T00:00:00Z",
    "2026-12-31T23:59:60Z",  # 윤초: 서버가 UTC 시각으로 바꿀 수 없는 값은 받지 않는다 (parse_rfc3339와 같은 기준)
    "0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59",  # UTC로 바꾸면 범위를 넘는다
])
def test_impossible_or_partial_date_times_fail(value: str) -> None:
    assert not contracts.CONTRACT_FORMATS.conforms(value, "date-time")


def test_date_time_checker_agrees_with_the_server_time_parser() -> None:
    """검사기를 통과한 값은 parse_rfc3339가 항상 받는다 — 스키마를 통과한 시각이 뒤에서 500을 만들지 않는다."""
    from app.domain.normalize import parse_rfc3339

    for value in ("2026-11-02T00:00:00Z", "2026-11-02T09:00:00+09:00", "2026-11-02T00:00:00.1234567Z"):
        assert contracts.CONTRACT_FORMATS.conforms(value, "date-time")
        parse_rfc3339(value)


def test_impossible_date_in_a_document_is_a_schema_error() -> None:
    doc = _example("event.manual-safety.json")
    doc["started_at"] = "2026-02-30T00:00:00Z"
    problems, _ = validate_document("event", doc)
    assert ("/started_at", "schema.format") in _errors(problems), problems


# ---------------------------------------------------------------- 3. 응답 크기를 제한한다

def test_string_coordinates_give_one_problem_not_one_per_character() -> None:
    problems, _ = validate_document("upload-manifest",
                                    _with_location({"type": "LineString", "coordinates": "x" * 20000}))
    assert len(problems) <= contracts.MAX_PROBLEMS + 1


def test_problem_list_is_capped_with_a_truncation_notice() -> None:
    doc = copy.deepcopy(_batch())
    doc["observations"] = [{"metric": 1, "value": "x"} for _ in range(500)]
    problems, _ = validate_document("observation-batch", doc)
    assert len(problems) == contracts.MAX_PROBLEMS + 1
    assert problems[-1].code == "schema.truncated" and problems[-1].severity == "WARNING"
    assert _errors(problems)


def test_a_larger_limit_keeps_every_item_problem_for_per_item_decisions() -> None:
    """관측값 배치는 항목마다 거부 사유를 정하므로 문제 목록을 자르지 않고 받을 수 있다 (잘리면 뒤쪽 잘못된 항목이 통과한다)."""
    doc = copy.deepcopy(_batch())
    doc["observations"] = [{"metric": 1, "value": "x"} for _ in range(500)]
    problems, _ = validate_document("observation-batch", doc, limit=contracts.ITEM_PROBLEM_LIMIT)
    assert not contracts.is_truncated(problems)
    assert {p.pointer.split("/")[2] for p in problems if p.pointer.startswith("/observations/")} == {
        str(i) for i in range(500)}


# ---------------------------------------------------------------- 4. 어떤 JSON이 와도 예외가 없다 (속성 시험)

def test_arbitrary_json_never_raises_the_validator() -> None:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    scalars = (st.none() | st.booleans() | st.integers(min_value=-(10**400), max_value=10**400)
               | st.floats(allow_nan=True, allow_infinity=True) | st.text(max_size=20))
    values = st.recursive(scalars, lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(max_size=6), c, max_size=4),
                          max_leaves=15)
    batch_slots = ["observations", "observed_at", "value", "unit", "quality_flag", "location", "depth_m", "root"]
    manifest_slots = ["location", "coordinates", "file", "capture_start_at", "timestamp_quality", "root"]

    @settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(value=values, slot=st.sampled_from(batch_slots + manifest_slots))
    def check(value: Any, slot: str) -> None:
        batch, manifest = _batch(), _manifest()
        item = batch["observations"][0]
        if slot in ("observed_at", "value", "unit", "quality_flag", "location", "depth_m"):
            item[slot] = value
        elif slot == "observations":
            batch["observations"] = value
        elif slot == "coordinates":
            manifest["location"] = {"type": "Polygon", "coordinates": value}
        elif slot in ("location", "file", "capture_start_at", "timestamp_quality"):
            manifest[slot] = value
        elif slot == "root" and isinstance(value, dict):
            batch, manifest = {**batch, **value}, {**manifest, **value}
        for contract, doc in (("observation-batch", batch), ("upload-manifest", manifest)):
            problems, _ = validate_document(contract, doc)
            assert len(problems) <= contracts.MAX_PROBLEMS + 1

    check()
