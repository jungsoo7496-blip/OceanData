"""통합 검토 VALIDATE-1·VALIDATE-2 (ADR-0086): 계약 검증의 메모리와 일이 위치(geometry) 크기에 맞게 묶인다.

- geometry는 Point·LineString·Polygon의 oneOf다. 맞는 가지가 없으면 표준 oneOf가 가지마다의 오류를 모두
  모아(context) 오류 하나에 달아 두었다. 큰 항적에서 좌표 하나만 틀려도 Polygon 가지가 좌표마다 오류 셋을 만들어
  문서 1MB에 1GB가 넘는 메모리를 썼다(요청 본문 상한 20MiB 안에서 요청 하나로 메모리 고갈)
- 이벤트 문서는 unevaluatedProperties 보고 보정(ADR-0067)과 위치 재검사가 location을 여러 번 다시 검사하고
  location 전체를 깊은 복사해, 같은 문서의 CPU가 보정 전의 두 배를 넘었다
판정과 문제 목록은 바뀌지 않는다(표준 키워드와 같은 오류를 내는지 함께 본다). 시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import json
import tracemalloc
from collections.abc import Callable
from typing import Any

import pytest
from jsonschema import Draft202012Validator, validators

from app.services import contracts
from app.services.contracts import contracts_dir, validate_document

SIZE = 5000  # 좌표 수. 수정 전에는 이 크기(문서 약 0.1MB)에서도 검증 한 번이 100MB 넘게 할당했다
SWAPPED = [33.2, 126.5]  # 위경도가 뒤바뀐 좌표 (위도 자리 126.5)


def _example(name: str) -> dict[str, Any]:
    return json.loads((contracts_dir() / "examples" / name).read_text(encoding="utf-8"))


def _track(n: int, *, bad: bool) -> dict[str, Any]:
    """n개 좌표의 항적. bad면 마지막 좌표 하나만 뒤바뀐다 (앞 가지들이 끝까지 본 뒤에 실패한다)."""
    coords: list[Any] = [[126.5 + i * 1e-6, 33.2] for i in range(n)]
    if bad:
        coords[-1] = list(SWAPPED)
    return {"type": "LineString", "coordinates": coords}


def _area(n: int, *, bad: bool) -> dict[str, Any]:
    """좌표 4개짜리 닫힌 링 n/4개로 된 감시 구역. bad면 마지막 링의 셋째 좌표 하나만 뒤바뀐다
    (LineString 가지는 링 하나하나를 좌표로 보고 실패한다)."""
    rings: list[Any] = []
    for k in range(n // 4):
        x = 126.5 + k * 1e-4
        rings.append([[x, 33.2], [x + 1e-5, 33.2], [x, 33.2 + 1e-5], [x, 33.2]])
    if bad:
        rings[-1][2] = list(SWAPPED)
    return {"type": "Polygon", "coordinates": rings}


def _in_event(location: dict[str, Any]) -> dict[str, Any]:
    return {**_example("event.manual-safety.json"), "location": location}


def _in_manifest(location: dict[str, Any]) -> dict[str, Any]:
    return {**_example("upload-manifest.vessel-video.json"), "location": location}


def _in_asset(location: dict[str, Any]) -> dict[str, Any]:
    return {**_example("asset-metadata.vessel-video-l0.json"), "location": location}  # oneOf[geometry, null]


def _in_batch_item(location: dict[str, Any]) -> dict[str, Any]:
    doc = _example("observation-batch.ocean-sensor.json")
    doc["observations"][0]["location"] = location
    return doc


def _parsed(doc: dict[str, Any]) -> dict[str, Any]:
    """요청 본문처럼 JSON을 거친 문서 (공유된 하위 객체가 없다)."""
    return json.loads(json.dumps(doc))  # type: ignore[no-any-return]


def _peak_bytes(contract: str, doc: dict[str, Any]) -> tuple[int, list[Any]]:
    tracemalloc.start()
    try:
        problems, _ = validate_document(contract, doc)
        return tracemalloc.get_traced_memory()[1], problems
    finally:
        tracemalloc.stop()


# ---------------------------------------------------------------- VALIDATE-1 메모리

MEMORY_CASES: list[tuple[str, str, Callable[..., dict[str, Any]], Callable[..., dict[str, Any]], tuple[str, str]]] = [
    ("이벤트 항적", "event", _in_event, _track, ("/location/coordinates/4999", "lonlat_swapped")),
    ("매니페스트 항적", "upload-manifest", _in_manifest, _track, ("/location/coordinates/4999", "lonlat_swapped")),
    ("이벤트 감시 구역(링 여럿)", "event", _in_event, _area, ("/location/coordinates/1249/2", "lonlat_swapped")),
    ("자산 메타데이터 항적(겹 oneOf)", "asset-metadata", _in_asset, _track,
     ("/location/coordinates/4999", "lonlat_swapped")),
    ("관측 항목 항적", "observation-batch", _in_batch_item, _track, ("/observations/0/location", "schema.oneOf")),
]


@pytest.mark.parametrize(("name", "contract", "wrap", "shape", "expected"), MEMORY_CASES, ids=[c[0] for c in MEMORY_CASES])
def test_one_bad_coordinate_in_a_large_geometry_keeps_validation_memory_bounded(
        name: str, contract: str, wrap: Callable[..., dict[str, Any]], shape: Callable[..., dict[str, Any]],
        expected: tuple[str, str]) -> None:
    """좌표 하나가 틀린 큰 geometry를 검사하는 동안의 최대 할당은 문서 JSON 크기의 스무 배 아래다.
    수정 전에는 약 천 배였고 문서 크기에 비례해 늘었다 (0.9MB 문서에 1.4GB)."""
    doc = _parsed(wrap(shape(SIZE, bad=True)))
    size = len(json.dumps(doc))
    peak, problems = _peak_bytes(contract, doc)
    assert expected in [(p.pointer, p.code) for p in problems], (name, problems)
    assert peak < 20 * size, f"{name}: 문서 {size}B 검증에 최대 {peak / 1e6:.1f}MB 할당"


def test_one_of_reports_the_standard_error_without_collecting_branch_errors() -> None:
    """맞는 가지가 없을 때의 oneOf 오류는 표준과 자리·키워드·메시지가 같다. 가지마다의 오류(context)만 모으지 않는다.
    두 가지 이상이 맞을 때와 정확히 하나가 맞을 때도 표준과 같은 판정이다."""
    standard = validators.extend(contracts._ContractValidator,
                                 validators={"oneOf": Draft202012Validator.VALIDATORS["oneOf"]})

    def pair(schema: dict[str, Any]) -> tuple[Any, Any]:
        kw = {"registry": contracts.registry(), "format_checker": contracts.CONTRACT_FORMATS}
        return contracts.contract_validator(schema), standard(contracts.strict_patterns(schema), **kw)

    def report(validator: Any, instance: Any) -> list[tuple[list[Any], str, str]]:
        return [(list(e.absolute_path), str(e.validator), e.message) for e in validator.iter_errors(instance)]

    geometry = {"$ref": contracts.BASE + "common.schema.json#/$defs/geometry"}
    instances: list[Any] = [
        {"type": "Point", "coordinates": [126.5, 33.2]}, {"type": "Point", "coordinates": SWAPPED},
        _track(5, bad=False), _track(5, bad=True), _area(6, bad=False), _area(6, bad=True),
        {"type": "Point", "coordinates": [1, 2], "bbox": [0, 0, 1, 1]}, {"type": "Foo", "coordinates": [1, 2]},
        {"type": "LineString"}, {"type": "Polygon", "coordinates": [[[1, 2], [3, 4], [1, 2]]]}, None, 5, "x", [],
    ]
    ours, std = pair(geometry)
    for instance in instances:
        assert report(ours, instance) == report(std, instance), instance
        assert all(not e.context for e in ours.iter_errors(instance)), instance
    overlapping = {"oneOf": [{"type": "number"}, {"minimum": 0}]}  # 5는 두 가지 모두에 맞는다
    ours, std = pair(overlapping)
    for instance in (5, -1, "x", None):
        assert report(ours, instance) == report(std, instance), instance
    assert [e.message for e in ours.iter_errors(5)] == [
        "5 is valid under each of {'minimum': 0}, {'type': 'number'}"]


# ---------------------------------------------------------------- VALIDATE-2 이벤트 문서의 되풀이 검사

def _position_checks(monkeypatch: pytest.MonkeyPatch, contract: str, doc: dict[str, Any]) -> int:
    """검증 한 번 동안 좌표 스키마(position의 prefixItems)를 적용한 횟수. 스키마를 몇 번 다시 훑는지의 척도다."""
    seen = [0]
    standard = contracts._ContractValidator.VALIDATORS["prefixItems"]

    def counting(validator: Any, prefix_items: Any, instance: Any, schema: Any) -> Any:
        seen[0] += 1
        return standard(validator, prefix_items, instance, schema)

    with monkeypatch.context() as patch:
        patch.setitem(contracts._ContractValidator.VALIDATORS, "prefixItems", counting)
        validate_document(contract, doc)
    return seen[0]


@pytest.mark.parametrize("unknown_field", [False, True], ids=["좌표 하나만", "계약에 없는 속성도"])
def test_event_with_one_bad_coordinate_rechecks_its_track_at_most_twice_as_often_as_a_correct_one(
        monkeypatch: pytest.MonkeyPatch, unknown_field: bool) -> None:
    """좌표 하나가 틀린 이벤트 항적을 훑는 횟수는 좌표가 모두 맞는 같은 크기 문서의 두 배 이하다.
    수정 전에는 6.5배였다(보정 전 판은 3배): unevaluatedProperties 보정이 표준 키워드를 두 번 돌리고 실패한
    allOf 항목을 다시 내려가 검사했고, 위치 재검사는 고친 사본을 이벤트 계약 전체로 다시 검사했다."""
    extra = {"zzz_unknown": 1} if unknown_field else {}
    good = _position_checks(monkeypatch, "event", _parsed({**_in_event(_track(2000, bad=False)), **extra}))
    bad = _position_checks(monkeypatch, "event", _parsed({**_in_event(_track(2000, bad=True)), **extra}))
    assert good >= 2000, good  # 세는 방법이 맞는지 (좌표마다 적어도 한 번)
    assert bad <= 2 * good, f"올바른 항적 {good}번, 좌표 하나가 틀린 항적 {bad}번"


def test_repaired_copy_replaces_only_the_flagged_positions_and_leaves_the_document_alone() -> None:
    """위치 오류가 가리키는 자리만 바꾼 사본은 그 자리까지의 배열만 새로 만든다. 나머지 좌표는 원래 객체를 함께 쓰고
    (location 전체를 깊은 복사하지 않는다) 원래 문서는 바뀌지 않는다 (같은 좌표 객체를 여러 자리가 공유해도)."""
    track = _parsed(_track(1000, bad=True))
    problems = contracts._location_problems({"location": track})
    repaired = contracts._repaired_location(track, problems)
    assert repaired is not None
    assert repaired["coordinates"][-1] == [1, 1] and track["coordinates"][-1] == SWAPPED
    assert all(a is b for a, b in zip(repaired["coordinates"][:-1], track["coordinates"][:-1], strict=True))

    shared = [10, 200]  # 같은 좌표 객체를 두 자리가 쓰는 링 (JSON을 거치지 않은 호출)
    ring = [[126, 35], shared, shared, [127, 36], [126, 35]]
    area = {"type": "Polygon", "coordinates": [ring, [[1, 2], [3, 4], [5, 6], [1, 2]]]}
    problems = contracts._location_problems({"location": area})
    assert [p.pointer for p in problems] == ["/location/coordinates/0/1", "/location/coordinates/0/2"]
    repaired = contracts._repaired_location(area, problems)
    assert repaired is not None
    assert repaired["coordinates"][0] == [[126, 35], [1, 1], [1, 1], [127, 36], [126, 35]]
    assert repaired["coordinates"][1] is area["coordinates"][1]  # 손대지 않은 링은 그대로 함께 쓴다
    assert area["coordinates"][0] is ring and ring == [[126, 35], [10, 200], [10, 200], [127, 36], [126, 35]]
    assert shared == [10, 200]


def test_location_recheck_uses_the_location_schema_of_every_contract_that_has_one() -> None:
    """고친 사본은 그 계약의 location 속성 스키마로만 다시 검사한다. 계약 전체로 검사한 /location 아래 오류와
    같은 판정인지 계약마다 여러 위치 값으로 본다. location 속성이 있는 계약은 모두 표에 있어야 한다."""
    samples: list[Any] = [_track(3, bad=False), _track(3, bad=True), {"type": "Point", "coordinates": [1, 2], "bbox": 1},
                          {"type": "Polygon", "coordinates": [[[1, 2], [3, 4], [1, 2]]]}, None, "x"]
    for contract, name in contracts.SCHEMA_FOR_CONTRACT.items():
        whole = contracts.contract_validator({"$ref": contracts.BASE + f"{name}.schema.json"})
        reaches = any(list(e.absolute_path)[:1] == ["location"] for s in samples
                      for e in whole.iter_errors({"location": s}))
        assert reaches == (contract in contracts._LOCATION_SCHEMA), contract
        if not reaches:
            continue
        own = contracts.contract_validator({"$ref": contracts.BASE + contracts._LOCATION_SCHEMA[contract]})
        for s in samples:
            under = any(list(e.absolute_path)[:1] == ["location"] for e in whole.iter_errors({"location": s}))
            assert under == (not own.is_valid(s)), (contract, s)
