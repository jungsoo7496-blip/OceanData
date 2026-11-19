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

