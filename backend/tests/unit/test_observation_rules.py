"""07번 단위 시험: 관측값 시간·단위·결측·미래 시각·시계 오차 규칙."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.observations import Rejected, normalize, same_value
from app.services.contracts import code_table, units

NOW = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
METRICS = {m["code"]: m["unit"] for m in code_table("metrics")["items"]}


def norm(item, tz=None, **kw):
    return normalize(item, metrics=METRICS, units=units(), source_timezone=tz, now=NOW,
                     future_tolerance=timedelta(minutes=10), **kw)


def test_kst_offset_and_unit() -> None:
    n = norm({"metric": "sea_water_temperature", "observed_at": "2026-11-03T10:00:00+09:00", "value": 64.4,
              "unit": "degF", "quality_flag": "GOOD", "depth_m": 1.5})
    assert n.observed_at == datetime(2026, 11, 3, 1, 0, tzinfo=UTC)
    assert n.unit == "degC" and n.value == pytest.approx(18.0, abs=1e-6)
    assert (n.original_value, n.original_unit, n.quality_flag) == (64.4, "degF", "GOOD")


def test_source_timestamp_needs_timezone() -> None:
    item = {"metric": "wind_speed", "source_timestamp": "2026/11/03 10:20:00", "value": 12.5, "unit": "kn",
            "quality_flag": "GOOD"}
    with pytest.raises(Rejected, match="TIMEZONE_UNKNOWN"):
        norm(item)
    n = norm(item, tz="Asia/Seoul")
    assert n.observed_at == datetime(2026, 11, 3, 1, 20, tzinfo=UTC) and n.source_timestamp == "2026/11/03 10:20:00"
    with pytest.raises(Rejected, match="TIMEZONE_UNKNOWN"):
        norm({**item, "observed_at": "2026-11-03T10:00:00"})  # 오프셋 없는 observed_at


@pytest.mark.parametrize(("item", "reason"), [
    ({"metric": "sea_water_temperature", "observed_at": "2026-11-03T00:00:00Z", "value": 0, "quality_flag": "MISSING"},
     "MISSING_FLAG_MISMATCH"),
    ({"metric": "sea_water_temperature", "observed_at": "2026-11-03T00:00:00Z", "value": None, "quality_flag": "GOOD"},
     "MISSING_FLAG_MISMATCH"),
    ({"metric": "sea_water_temperature", "observed_at": "2026-11-04T00:11:00Z", "value": 1, "unit": "degC",
      "quality_flag": "GOOD"}, "FUTURE_TIMESTAMP"),
    ({"metric": "no_such_metric", "observed_at": "2026-11-03T00:00:00Z", "value": 1, "quality_flag": "GOOD"},
     "UNKNOWN_METRIC"),
])
def test_rejections(item, reason) -> None:
    with pytest.raises(Rejected, match=reason):
        norm(item)


def test_missing_is_kept_as_null_and_unknown_unit_is_suspect() -> None:
    n = norm({"metric": "sea_water_temperature", "observed_at": "2026-11-03T00:00:00Z", "value": None,
              "quality_flag": "MISSING"})
    assert n.value is None and n.quality_flag == "MISSING"
    s = norm({"metric": "sea_water_temperature", "observed_at": "2026-11-03T00:00:00Z", "value": 3,
              "unit": "furlong", "quality_flag": "GOOD"})
    assert (s.value, s.unit, s.quality_flag) == (3, "furlong", "SUSPECT")
    wrong = norm({"metric": "sea_water_temperature", "observed_at": "2026-11-03T00:00:00Z", "value": 3, "unit": "m",
                  "quality_flag": "GOOD"})
    assert wrong.quality_flag == "SUSPECT"  # 항목과 맞지 않는 단위


def test_clock_offset_warning_keeps_time() -> None:
    item = {"metric": "sea_water_temperature", "observed_at": "2026-11-03T00:00:00Z", "value": 3, "unit": "degC",
            "quality_flag": "GOOD"}
    n = norm(item, clock_offset_ms=5000)
    assert n.quality_flag == "SUSPECT" and n.observed_at == datetime(2026, 11, 3, tzinfo=UTC)
    assert norm(item, clock_offset_ms=100).quality_flag == "GOOD"


@given(st.floats(-1e6, 1e6, allow_nan=False))
def test_same_value(x) -> None:
    assert same_value(x, x) and not same_value(x, None) and same_value(None, None)
    assert not same_value(x, x + 1)
