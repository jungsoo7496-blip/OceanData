"""03번 요구사항 4·13: 계약 검증기가 도메인 문제로 스키마 오류를 가릴 때의 범위.

- 경고(WARNING, 예: 단위 사전에 없는 단위)는 아무 스키마 오류도 가리지 않는다
- 포인터는 조각 단위로 비교한다 (/observations/10/... 이 /observations/1 의 오류를 가리지 않는다)
- 관측 항목의 도메인 오류는 같은 포인터의 스키마 오류만 가리고, 문서 루트(필수 필드 누락)는 가리지 않는다
- 위치 좌표 오류는 /location 아래 스키마 오류(geometry oneOf)를 계속 가린다
시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.contracts import contracts_dir, validate_document

DEVICE = "dev_00000000000000000000000301"


def _item(**kw: Any) -> dict[str, Any]:
    return {"metric": "sea_water_temperature", "observed_at": "2026-11-02T00:00:00Z", "value": 18.2, "unit": "degC",
            "quality_flag": "GOOD", **kw}


def _codes(problems: list[Any]) -> list[tuple[str, str, str]]:
    return [(p.pointer, p.code, p.severity) for p in problems]


def test_unknown_unit_warning_does_not_hide_missing_root_field() -> None:
    doc = {"schema_version": "1.0.0", "observations": [_item(unit="zzz")]}
    problems, rejected = validate_document("observation-batch", doc)
    assert not rejected
    codes = _codes(problems)
    assert ("", "schema.required", "ERROR") in codes, codes  # source_system_id 누락은 남는다
    assert ("/observations/0/unit", "unknown_unit", "WARNING") in codes


def test_item_error_does_not_hide_missing_root_field() -> None:
    doc = {"schema_version": "1.0.0", "observations": [_item(observed_at="2026-11-02T00:00:00")]}
    codes = _codes(validate_document("observation-batch", doc)[0])
    assert ("", "schema.required", "ERROR") in codes, codes
    assert ("/observations/0/observed_at", "timezone_required", "ERROR") in codes
    # 같은 포인터의 형식 오류(pattern·format)는 도메인 사유로 바뀐다
    assert not [c for c in codes if c[0] == "/observations/0/observed_at" and c[1].startswith("schema.")]


def test_item_ten_does_not_hide_errors_of_item_one() -> None:
    for item10 in (_item(unit="zzz"), _item(observed_at="2026-11-02T00:00:00")):
        observations = [_item() for _ in range(11)]
        observations[1] = _item(bogus=1)  # 계약에 없는 필드 → /observations/1 additionalProperties
        observations[10] = item10
        doc = {"schema_version": "1.0.0", "source_system_id": DEVICE, "observations": observations}
        codes = _codes(validate_document("observation-batch", doc)[0])
        assert ("/observations/1", "schema.additionalProperties", "ERROR") in codes, (item10, codes)


def test_valid_batch_with_unknown_unit_only_warns() -> None:
    doc = {"schema_version": "1.0.0", "source_system_id": DEVICE, "observations": [_item(unit="zzz")]}
    assert _codes(validate_document("observation-batch", doc)[0]) == [
        ("/observations/0/unit", "unknown_unit", "WARNING")]


def test_location_error_still_replaces_geometry_schema_error() -> None:
    bad = json.loads((contracts_dir() / "examples" / "invalid" / "upload-manifest.swapped-lonlat.json")
                     .read_text(encoding="utf-8"))
    problems, _ = validate_document("upload-manifest", bad)
    assert [p.code for p in problems] == ["lonlat_swapped"]
    # 위치 밖의 스키마 오류는 위치 오류가 있어도 남는다
    problems, _ = validate_document("upload-manifest", {**bad, "unexpected_field": 1})
    assert sorted(p.code for p in problems) == ["lonlat_swapped", "schema.additionalProperties"]
