"""03번 단위 시험: 기본값, variant 규칙, 스키마 버전, 촬영시각·위치·단위 정규화, 품질 평가."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import metadata, normalize, quality
from app.services.contracts import contracts_dir


def test_defaults_are_safe_and_do_not_override() -> None:
    out = metadata.apply_defaults({}, device_collection_mode="C2_EVENT_CLIPS")
    assert out == {"collection_mode": "C2_EVENT_CLIPS", "data_role": "CORE", "privacy_status": "REVIEW_REQUIRED",
                   "sensitivity_class": "RESTRICTED", "sharing_scope": "PRIVATE", "share_status": "NONE",
                   "backup_class": "NONE", "quality_status": "PENDING"}
    assert metadata.apply_defaults({"data_role": "BYPRODUCT"}, device_collection_mode="C1_SAMPLE_PREVIEW")[
        "data_role"] == "BYPRODUCT"
    assert metadata.apply_defaults({}, device_collection_mode="C1_SAMPLE_PREVIEW",
                                   rule_data_role="BYPRODUCT")["data_role"] == "BYPRODUCT"


@pytest.mark.parametrize(("mode", "dtype", "variant", "ok"), [
    ("C0_METADATA_ONLY", "VIDEO", "PROXY", False),
    ("C0_METADATA_ONLY", "SENSOR", "ORIGINAL", False),
    ("C1_SAMPLE_PREVIEW", "VIDEO", "ORIGINAL", False),
    ("C1_SAMPLE_PREVIEW", "VIDEO", "PROXY", True),
    ("C2_EVENT_CLIPS", "VIDEO", "CLIP", True),
    ("C2_EVENT_CLIPS", "IMAGE", "ORIGINAL", False),
    ("C3_APPROVED_FULL", "VIDEO", "ORIGINAL", True),
    ("C1_SAMPLE_PREVIEW", "SENSOR", "ORIGINAL", True),
])
def test_variant_allowed_by_collection_mode(mode, dtype, variant, ok) -> None:
    assert metadata.variant_allowed(mode, dtype, variant) is ok


def test_inherit_strictest() -> None:
    parents = [{"data_role": "BYPRODUCT", "sensitivity_class": "INTERNAL"},
               {"data_role": "CORE", "sensitivity_class": "SECURITY"}]
    assert metadata.inherit_strictest(parents) == {"data_role": "CORE", "sensitivity_class": "SECURITY"}


@pytest.mark.parametrize(("declared", "ok"), [("1.0.0", True), ("1.0.7", True), ("1.1.0", False),
                                              ("2.0.0", False), ("0.9.0", False), ("1.0", False), (None, False)])
def test_schema_version_rule(declared, ok) -> None:
    assert metadata.check_schema_version(declared, ["1.0.0"]).ok is ok
    assert metadata.check_schema_version("1.1.0", ["1.0.0", "1.2.0"]).ok


@pytest.mark.covers("T05-b")
def test_capture_time_unknown_without_evidence() -> None:
    r = normalize.resolve_capture_time("2026-11-01T10:00:00+09:00", None, {"source": "UNKNOWN", "verified": False},
                                       {"mtime": "2026-11-01T10:00:00+09:00"})
    assert r.capture_start_at is None and r.timestamp_quality["source"] == "UNKNOWN"
    assert r.warnings == ["capture_time_unknown"]
    assert r.original_timestamps == {"mtime": "2026-11-01T10:00:00+09:00"}  # 보존만 한다


def test_capture_time_offset_converted_and_original_kept() -> None:
    r = normalize.resolve_capture_time("2026-11-01T10:00:00+09:00", "2026-11-01T10:05:00+09:00",
                                       {"source": "DEVICE", "verified": True, "est_offset_ms": 40})
    assert r.capture_start_at == datetime(2026, 11, 1, 1, 0, tzinfo=UTC)
    assert r.original_timestamps["capture_start_at"] == "2026-11-01T10:00:00+09:00"
    assert r.timestamp_quality == {"source": "DEVICE", "verified": True, "est_offset_ms": 40}
    manual = normalize.resolve_capture_time("2026-11-01T01:00:00Z", None, {"source": "MANUAL", "verified": True})
    assert manual.timestamp_quality["verified"] is False


@pytest.mark.parametrize("bad", ["2026-11-01T10:00:00", "2026/11/01 10:00", "not-a-time"])
def test_capture_time_without_offset_rejected(bad) -> None:
    with pytest.raises(normalize.TimeRuleError):
        normalize.resolve_capture_time(bad, None, {"source": "DEVICE", "verified": False})


@given(st.floats(-180, 180), st.floats(-90, 90))
def test_valid_positions_pass(lon, lat) -> None:
    probs = normalize.check_location({"type": "Point", "coordinates": [lon, lat]})
    assert all(p.code == "null_island" for p in probs)


@given(st.floats(-90, 90), st.floats(90.0001, 180))
@pytest.mark.covers("T05-c")
def test_swapped_lonlat_detected(lat_like, lon_like) -> None:
    probs = normalize.check_location({"type": "Point", "coordinates": [lat_like, lon_like]})
    assert [p.code for p in probs] == ["lonlat_swapped"]


def test_generalize_location() -> None:
    g = {"type": "Point", "coordinates": [126.123456, 34.987654]}
    assert normalize.generalize_location(g, "GENERALIZED") == {"type": "Point", "coordinates": [126.12, 34.99]}
    assert normalize.generalize_location(g, "NONE") is None
    assert normalize.generalize_location(g, "EXACT_RESTRICTED") == g


def _units() -> normalize.UnitDictionary:
    data = json.loads((contracts_dir() / "codes" / "units.json").read_text(encoding="utf-8"))
    return normalize.UnitDictionary(data["units"])


def test_unit_normalization_keeps_original() -> None:
    d = _units()
    r = d.normalize(212.0, "degF")
    assert r.unit == "degC" and r.value == pytest.approx(100.0, abs=1e-6)
    assert (r.original_value, r.original_unit, r.known) == (212.0, "degF", True)
    assert d.normalize(20.0, "℃").unit == "degC"
    unknown = d.normalize(5.0, "furlong/fortnight")
    assert unknown.known is False and unknown.value == 5.0 and unknown.unit == "furlong/fortnight"


def test_quality_evaluator_rules() -> None:
    a = {"data_type": "VIDEO", "mime_type": "video/mp4", "byte_size": 1, "checksum_sha256": "a" * 64,
         "timestamp_quality": {"source": "DEVICE"}}
    assert quality.evaluate(a) == ("PASSED", [])
    assert quality.evaluate({**a, "timestamp_quality": {"source": "UNKNOWN"}}) == ("WARNING", ["capture_time_unknown"])
    assert quality.evaluate(a, facts={"playable": False}) == ("FAILED", ["not_playable"])
    assert quality.evaluate(a, rules={"capture_time_unknown": "FAILED"},
                            facts={})[0] == "PASSED"
    assert quality.evaluate({**a, "byte_size": None})[1] == ["missing:byte_size"]
