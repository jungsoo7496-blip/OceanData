"""03번 요구사항 4·6·13, 07번 요구사항 2.1: 계약 검증기의 문제 목록이 실제 결함을 빠짐없이, 엉뚱한 것 없이 말한다
(검토 2026-11-12 V9 — ADR-0067).

- 위치 오류(위경도 뒤바뀜·범위·(0, 0)·잘못된 좌표)는 그 좌표 때문에 생긴 geometry 스키마 오류만 대신한다.
  /location 아래의 다른 결함(계약에 없는 속성, 모자란 좌표 수, 셋째 값)은 함께 보고한다
- 이벤트 문서의 한 필드가 틀려도 루트에 '알려진 속성이 모두 평가되지 않음'(unevaluatedProperties)이 붙지 않는다.
  계약에 없는 속성은 다른 오류가 있어도 그 이름으로 보고한다
- observed_at의 시간대 힌트(timezone_required)·UTC 범위 힌트(invalid_time)는 그것만 틀렸을 때만 형식 오류를 대신한다
- 위 경로에서 검토가 확인한 불변식(무효 → 유효로 뒤집히지 않음, 스키마 오류마다 같은 자리나 그 아래에 ERROR,
  경고는 가리지 않음, 해시 시드와 무관, 문제 수 상한)을 속성 시험으로 확인한다
시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, validators

from app.services import contracts
from app.services.contracts import contracts_dir, validate_document

BACKEND = Path(__file__).resolve().parents[2]


def _example(name: str) -> dict[str, Any]:
    return json.loads((contracts_dir() / "examples" / name).read_text(encoding="utf-8"))


def _manifest(location: Any) -> dict[str, Any]:
    return {**_example("upload-manifest.vessel-video.json"), "location": location}


def _event(**fields: Any) -> dict[str, Any]:
    return {**_example("event.manual-safety.json"), **fields}


def _batch_with(observed_at: str) -> dict[str, Any]:
    doc = _example("observation-batch.ocean-sensor.json")
    doc["observations"][0]["observed_at"] = observed_at
    return doc


def _errors(problems: list[Any]) -> list[tuple[str, str]]:
    return [(p.pointer, p.code) for p in problems if p.severity == "ERROR"]


def _under_location(problems: list[Any]) -> list[tuple[str, str]]:
    return [(p, c) for p, c in _errors(problems) if p == "/location" or p.startswith("/location/")]


LOCATION_DOMAIN_CODES = {"lonlat_swapped", "out_of_range", "null_island", "invalid_position", "unsupported_geometry"}


# ---------------------------------------------------------------- 1. 위치 오류가 다른 결함을 가리지 않는다

SECOND_DEFECT: list[tuple[str, dict[str, Any], str]] = [
    ("(0, 0) + 계약에 없는 bbox", {"type": "Point", "coordinates": [0, 0], "bbox": [0, 0, 1, 1]}, "null_island"),
    ("위도 100 + 링 좌표 3개(4개 이상)", {"type": "Polygon", "coordinates": [[[126, 35], [127, 100], [128, 36]]]},
     "out_of_range"),
    ("위경도 뒤바뀜 + 셋째 값이 문자", {"type": "Point", "coordinates": [10, 200, "abc"]}, "lonlat_swapped"),
    ("좌표가 숫자 + 계약에 없는 bbox", {"type": "LineString", "coordinates": 5, "bbox": 1}, "invalid_position"),
    ("뒤바뀐 좌표 하나뿐인 항적(2개 이상)", {"type": "LineString", "coordinates": [[10, 200]]}, "lonlat_swapped"),
    ("(0, 0) + 좌표 값 4개(3개까지)", {"type": "Point", "coordinates": [0, 0, 1, 2]}, "null_island"),
]


@pytest.mark.parametrize(("name", "location", "domain_code"), SECOND_DEFECT, ids=[c[0] for c in SECOND_DEFECT])
def test_location_error_does_not_hide_an_independent_geometry_defect(name: str, location: dict[str, Any],
                                                                     domain_code: str) -> None:
    codes = _under_location(validate_document("upload-manifest", _manifest(location))[0])
    assert domain_code in {c for _, c in codes}, (name, codes)
    assert any(c.startswith("schema.") for _, c in codes), f"{name}: 두 번째 결함이 가려졌다 {codes}"


ONLY_DOMAIN: list[tuple[str, dict[str, Any], set[str]]] = [
    ("위경도 뒤바뀜", {"type": "Point", "coordinates": [10, 200]}, {"lonlat_swapped"}),
    ("뒤바뀜 + 숫자 높이", {"type": "Point", "coordinates": [10, 200, 5]}, {"lonlat_swapped"}),
    ("항적 좌표가 숫자", {"type": "LineString", "coordinates": 5}, {"invalid_position"}),
    ("링 하나가 null", {"type": "Polygon", "coordinates": [None]}, {"invalid_position"}),
    ("링 안의 좌표 하나가 범위 밖", {"type": "Polygon", "coordinates": [[[126, 35], [200, 10], [127, 36], [126, 35]]]},
     {"out_of_range"}),
    ("좌표가 불리언", {"type": "Point", "coordinates": [True, False]}, {"invalid_position"}),
    ("좌표가 400자리 정수", {"type": "Point", "coordinates": [10**400, 1]}, {"invalid_position"}),
    ("좌표 없음", {"type": "Point"}, {"invalid_position"}),
    # 형상 종류가 틀리면 나머지를 무엇에 비춰 볼지 알 수 없어 종류 오류 하나로 말한다 (전과 같다)
    ("모르는 형상 + bbox", {"type": "Foo", "coordinates": [1, 2], "bbox": 1}, {"unsupported_geometry"}),
]


@pytest.mark.parametrize(("name", "location", "expected"), ONLY_DOMAIN, ids=[c[0] for c in ONLY_DOMAIN])
def test_location_error_alone_still_replaces_the_geometry_schema_error(name: str, location: dict[str, Any],
                                                                       expected: set[str]) -> None:
    codes = _under_location(validate_document("upload-manifest", _manifest(location))[0])
    assert {c for _, c in codes} == expected, (name, codes)


def test_many_coordinate_errors_keep_the_geometry_error_when_not_all_were_collected() -> None:
    """좌표 오류는 200개까지만 모은다. 그보다 많으면 나머지 자리를 고칠 수 없으니 스키마 오류도 남긴다(가리지 않는 쪽)."""
    def codes(n: int) -> set[str]:
        location = {"type": "LineString", "coordinates": [[10, 200]] * n}
        return {c for _, c in _under_location(validate_document("upload-manifest", _manifest(location))[0])}

    assert codes(150) == {"lonlat_swapped"}
    assert codes(250) == {"lonlat_swapped", "schema.oneOf"}


def test_asset_metadata_location_keeps_its_second_defect_too() -> None:
    """자산 메타데이터의 location은 oneOf[geometry, null]이라 스키마 오류가 한 단계 더 감싸여 있다."""
    doc = _example("asset-metadata.vessel-video-l0.json")
    doc["location"] = {"type": "Point", "coordinates": [0, 0], "bbox": 1}
    codes = _under_location(validate_document("asset-metadata", doc)[0])
    assert "null_island" in {c for _, c in codes} and any(c.startswith("schema.") for _, c in codes), codes
    doc["location"] = {"type": "Point", "coordinates": [10, 200]}
    assert {c for _, c in _under_location(validate_document("asset-metadata", doc)[0])} == {"lonlat_swapped"}


# ---------------------------------------------------------------- 2. 이벤트 문서의 불필요한 루트 오류

ROOT_UNEVALUATED = ("", "schema.unevaluatedProperties")

EVENT_DEFECTS: list[tuple[str, dict[str, Any]]] = [
    ("없는 날짜", {"started_at": "2026-02-30T00:00:00Z"}),
    ("필수 필드 누락", {"session_id": None}),
    ("시각 품질 값 형식", {"timestamp_quality": {"source": "MANUAL", "verified": "yes"}}),
    ("심각도 코드", {"severity": "LOUD"}),
    ("위경도 뒤바뀜", {"location": {"type": "Point", "coordinates": [10, 200]}}),
]


@pytest.mark.parametrize(("name", "change"), EVENT_DEFECTS, ids=[c[0] for c in EVENT_DEFECTS])
def test_event_defect_elsewhere_gives_no_root_unevaluated_properties_error(name: str, change: dict[str, Any]) -> None:
    doc = _event(**change)
    for key in [k for k, v in change.items() if v is None]:
        del doc[key]
    errors = _errors(validate_document("event", doc)[0])
    assert errors, name
    assert ROOT_UNEVALUATED not in errors, f"{name}: 알려진 속성이 모두 '평가되지 않음'으로 나왔다 {errors}"


def test_event_unknown_field_is_still_reported_by_name_next_to_another_defect() -> None:
    problems, _ = validate_document("event", _event(started_at="2026-02-30T00:00:00Z", zzz_unknown=1))
    assert ("/started_at", "schema.format") in _errors(problems)
    root = [p for p in problems if (p.pointer, p.code) == ROOT_UNEVALUATED]
    assert len(root) == 1 and "zzz_unknown" in root[0].message, problems
    assert "session_id" not in root[0].message and "event_code" not in root[0].message, root[0].message


def test_event_unknown_field_alone_is_still_reported() -> None:
    problems, _ = validate_document("event", _example("invalid/event.unknown-field.json"))
    root = [p for p in problems if (p.pointer, p.code) == ROOT_UNEVALUATED]
    assert len(root) == 1 and "organization_id" in root[0].message, problems


def test_the_jsonschema_helper_the_report_relies_on_is_present() -> None:
    """보고 보정은 jsonschema 내부 함수(표준 unevaluatedProperties가 쓰는 것)에 기댄다. 버전을 올려 없어지면
    표준 보고로 돌아가므로(유효성은 같다) 여기서 먼저 알린다."""
    assert contracts._evaluated_keys is not None


def test_event_examples_keep_their_result() -> None:
    for name in ("event.manual-safety.json", "event.model-detection.json"):
        assert _errors(validate_document("event", _example(name))[0]) == [], name
    for name in ("event.model-verified-at-creation.json", "event.model-without-confidence.json", "event.unknown-field.json"):
        assert _errors(validate_document("event", _example("invalid/" + name))[0]), name


# ---------------------------------------------------------------- 3. 시각 힌트는 그것만 틀렸을 때만

NOT_ONLY_OFFSET = ["2026-11-02", "2026-11-02T00:00", "2026-11-02 00:00:00", "20261102T000000", "2026-11-02T00",
                   "2026-11-02t00:00:00", "2026-11-02T00:00:00.1234567890", "2026-W45-1T00:00:00"]


@pytest.mark.parametrize("value", NOT_ONLY_OFFSET)
def test_timezone_hint_does_not_replace_a_format_error(value: str) -> None:
    codes = {c for p, c in _errors(validate_document("observation-batch", _batch_with(value))[0])
             if p == "/observations/0/observed_at"}
    assert "timezone_required" not in codes and any(c.startswith("schema.") for c in codes), (value, codes)


@pytest.mark.parametrize("value", ["2026-11-02T00:00:00", "2026-11-02T09:30:15.5", "2024-02-29T23:59:59.123456789"])
def test_timezone_hint_stays_when_only_the_offset_is_missing(value: str) -> None:
    codes = {c for p, c in _errors(validate_document("observation-batch", _batch_with(value))[0])
             if p == "/observations/0/observed_at"}
    assert codes == {"timezone_required"}, (value, codes)


def test_utc_range_hint_only_for_a_well_formed_value() -> None:
    def codes(value: str) -> set[str]:
        return {c for p, c in _errors(validate_document("observation-batch", _batch_with(value))[0])
                if p == "/observations/0/observed_at"}

    assert codes("0001-01-01T00:00:00+23:59") == {"invalid_time"}
    bad_shape = codes("0001-01-01 00:00:00+23:59")
    assert "invalid_time" not in bad_shape and any(c.startswith("schema.") for c in bad_shape), bad_shape


# ---------------------------------------------------------------- 4. 불변식 (속성 시험)

_REFERENCE = validators.extend(contracts._ContractValidator, validators={
    "unevaluatedProperties": Draft202012Validator.VALIDATORS["unevaluatedProperties"]})


def _raw_pointers(contract: str, doc: Any) -> list[str]:
    """표준 jsonschema 동작(우리 값 검사 포함)의 스키마 오류 자리. 검토가 비교한 기준이다."""
    ref = contracts.BASE + f"{contracts.SCHEMA_FOR_CONTRACT[contract]}.schema.json"
    validator = _REFERENCE(contracts.strict_patterns({"$ref": ref}), registry=contracts.registry(),
                           format_checker=contracts.CONTRACT_FORMATS)
    return [contracts._ptr(e.absolute_path) for e in validator.iter_errors(doc)]


def _check_invariants(contract: str, doc: Any) -> list[Any]:
    problems, version_rejected = validate_document(contract, doc)
    if version_rejected:  # 스키마 버전을 먼저 본다 (요구사항 4.3): 그 문제 하나로 끝난다
        assert [p.code for p in problems] == ["UNSUPPORTED_SCHEMA_VERSION"]
        return problems
    assert len(problems) <= contracts.MAX_PROBLEMS + 1
    errors = [p.pointer for p in problems if p.severity == "ERROR"]
    raw = _raw_pointers(contract, doc)
    if raw:  # 무효 문서는 무효로 남는다
        assert errors, (contract, doc)
    if len(problems) <= contracts.MAX_PROBLEMS:  # 잘리지 않았으면 스키마 오류마다 그 자리나 그 아래에 ERROR가 있다
        for q in raw:
            assert any(contracts._within(p, q) for p in errors), (q, problems)
    return problems


GOOD_RANGE = {"lon": (-179.0, 179.0), "lat": (1.0, 89.0)}  # 범위 안이고 (0, 0)이 아닌 좌표
BAD_POSITIONS: list[Any] = [[10, 200], [200, 10], [-181, 0], [0, 0], "x", [1], [True, 1], None, [float("nan"), 1]]


def _domain_bad(position: Any) -> bool:
    """check_location이 이 좌표에 오류를 내는가 (합성 좌표는 범위 안의 좋은 값이거나 BAD_POSITIONS 중 하나다)."""
    return not (isinstance(position, list) and len(position) >= 2
                and all(type(x) is float for x in position[:2]) and position[1] >= 1.0)


def test_property_location_second_defects_are_reported_and_lone_domain_errors_still_replace() -> None:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    good = st.tuples(st.floats(*GOOD_RANGE["lon"]), st.floats(*GOOD_RANGE["lat"])).map(list)
    position = st.one_of(good, good, st.sampled_from(BAD_POSITIONS))

    @st.composite
    def geometry(draw: Any) -> tuple[dict[str, Any], bool, bool]:
        kind = draw(st.sampled_from(["Point", "LineString", "Polygon"]))
        size = {"Point": (1, 1), "LineString": (2, 5), "Polygon": (4, 6)}[kind]
        positions = copy.deepcopy(draw(st.lists(position, min_size=size[0], max_size=size[1])))
        extra = draw(st.sampled_from(["none", "none", "bbox", "third", "short"]))
        if extra == "third" and isinstance(positions[0], list) and len(positions[0]) >= 2:
            positions[0] = [*positions[0][:2], "abc"]  # 셋째 값(높이)이 문자
        elif extra == "short" and kind != "Point":
            positions = positions[:1] if kind == "LineString" else positions[:3]  # 좌표 수 모자람
        elif extra != "bbox":
            extra = "none"
        coords: Any = positions[0] if kind == "Point" else positions if kind == "LineString" else [positions]
        geo: dict[str, Any] = {"type": kind, "coordinates": coords}
        if extra == "bbox":
            geo["bbox"] = [0, 0, 1, 1]  # 계약에 없는 속성
        return geo, any(_domain_bad(p) for p in positions), extra != "none"

    @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(case=geometry())
    def check(case: tuple[dict[str, Any], bool, bool]) -> None:
        geo, domain_bad, independent = case
        doc = _manifest(geo)
        problems = _check_invariants("upload-manifest", doc)
        codes = _under_location(problems)
        domain = {c for _, c in codes if c in LOCATION_DOMAIN_CODES}
        schema = [c for _, c in codes if c.startswith("schema.")]
        assert bool(domain) == domain_bad, (geo, codes)
        if independent:
            assert schema, f"두 번째 결함이 가려졌다: {geo} {codes}"
        elif domain_bad:
            assert not schema, f"좌표 오류가 설명하는 스키마 오류가 남았다: {geo} {codes}"

    check()


def test_property_event_root_error_appears_only_for_an_unknown_field() -> None:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    junk = st.one_of(st.none(), st.booleans(), st.integers(-3, 3), st.text(max_size=8),
                     st.sampled_from(["2026-02-30T00:00:00Z", "2026-11-02T00:00:00", "LOUD", "MODEL", [], {}]))
    known = sorted((_event().keys() | {"ended_at", "location", "source_system_id", "model_ref", "confidence",
                                       "human_verified", "source_event_id"}) - {"schema_version"})

    @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(changes=st.dictionaries(st.sampled_from(known), junk, max_size=3),
           drop=st.lists(st.sampled_from(known), max_size=2), unknown=st.booleans())
    def check(changes: dict[str, Any], drop: list[str], unknown: bool) -> None:
        doc = _event(**changes)
        for key in drop:
            doc.pop(key, None)
        if unknown:
            doc["zzz_unknown"] = 1
        problems = _check_invariants("event", doc)
        root = [p for p in problems if (p.pointer, p.code) == ROOT_UNEVALUATED]
        if unknown:
            assert len(root) == 1 and "zzz_unknown" in root[0].message, problems
            assert not any(f"'{k}'" in root[0].message for k in [*known, "schema_version"]), root[0].message
        else:
            assert not root, (doc, problems)

    check()


def test_property_timezone_hint_means_only_the_offset_is_missing() -> None:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    base = st.sampled_from(["2026-11-02T00:00:00", "2026-11-02T23:59:59.123", "2026-11-02T00:00:00Z",
                            "2026-11-02T00:00:00+09:00", "0001-01-01T00:00:00+23:59", "2024-02-29T12:00:00"])

    @st.composite
    def value(draw: Any) -> str:
        text = draw(base)
        for _ in range(draw(st.integers(0, 2))):
            i = draw(st.integers(0, len(text)))
            op = draw(st.sampled_from(["del", "ins", "rep"]))
            ch = draw(st.sampled_from(list("0123456789-:T tZz+.W")))
            text = text[:i] + (ch if op != "del" else "") + text[i + (op != "ins"):]
        return text

    @settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(ts=value())
    def check(ts: str) -> None:
        problems = _check_invariants("observation-batch", _batch_with(ts))
        codes = {c for p, c in _errors(problems) if p == "/observations/0/observed_at"}
        only_offset_missing = not contracts.schema_problems(
            "observation-batch", ts + "Z", fragment="#/$defs/observation/properties/observed_at")
        # 오프셋만 빠졌으면 힌트 하나, 아니면 힌트 없이 스키마 오류
        assert ("timezone_required" in codes) == only_offset_missing, (ts, codes)
        if only_offset_missing:
            assert codes == {"timezone_required"}, (ts, codes)
        if "invalid_time" in codes:
            assert codes == {"invalid_time"}, (ts, codes)

    check()


def test_problem_lists_do_not_depend_on_the_hash_seed() -> None:
    """출력은 PYTHONHASHSEED와 무관하다 (세트를 거치는 새 경로 포함)."""
    script = (
        "import json\n"
        "from app.services.contracts import validate_document, contracts_dir\n"
        "ex = contracts_dir() / 'examples'\n"
        "m = json.loads((ex / 'upload-manifest.vessel-video.json').read_text(encoding='utf-8'))\n"
        "e = json.loads((ex / 'event.manual-safety.json').read_text(encoding='utf-8'))\n"
        "docs = [('upload-manifest', {**m, 'location': {'type': 'Polygon', 'coordinates': [[[1, 2], [10, 200], [0, 0]]],"
        " 'bbox': 1, 'zz': 2}}),\n"
        "        ('event', {**e, 'started_at': 'x', 'b_unknown': 1, 'a_unknown': 2, 'z_unknown': 3, 'm_unknown': 4}),\n"
        "        ('event', {**e, 'severity': 'LOUD', 'timestamp_quality': 1})]\n"
        "print(json.dumps([[p.as_dict() for p in validate_document(c, d)[0]] for c, d in docs], ensure_ascii=False))\n"
    )
    outs = set()
    for seed in ("0", "1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        run = subprocess.run([sys.executable, "-c", script], cwd=BACKEND, env=env, capture_output=True, text=True,
                             timeout=120, check=True)
        outs.add(run.stdout)
    assert len(outs) == 1, outs
