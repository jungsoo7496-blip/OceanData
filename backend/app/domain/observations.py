"""관측값 규칙 (domain.md 6·7장, 07번 요구사항 2·3). 순수 함수만."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.normalize import TimeRuleError, UnitDictionary, parse_rfc3339

NAIVE_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
                 "%Y/%m/%d %H:%M", "%Y%m%d%H%M%S")


@dataclass(frozen=True)
class Normalized:
    metric: str
    observed_at: datetime
    value: float | None
    unit: str | None
    original_value: float | None
    original_unit: str | None
    quality_flag: str
    source_timestamp: str | None
    depth_m: float | None
    channel: str | None

    def key(self) -> tuple[Any, ...]:
        """중복 판단 키 (기관·장비 제외 부분): metric, observed_at, depth_m, channel."""
        return (self.metric, self.observed_at, self.depth_m, self.channel)


class Rejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_local(text: str, tz: str, fmt: str | None = None) -> datetime:
    """오프셋 없는 원천 시각을 장비 정책의 시간대로 해석해 UTC로 (요구사항 2.1)."""
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise Rejected("TIMEZONE_UNKNOWN") from exc
    for f in ((fmt,) if fmt else NAIVE_FORMATS):
        try:
            dt = datetime.strptime(text.strip(), f)  # noqa: DTZ007 - 아래에서 장비 시간대를 붙인다
        except ValueError:
            continue
        if dt.tzinfo is not None:
            return dt.astimezone(UTC)
        return dt.replace(tzinfo=zone).astimezone(UTC)
    raise Rejected("TIMEZONE_UNKNOWN")


def resolve_time(item: dict[str, Any], source_timezone: str | None, now: datetime, future_tolerance: timedelta,
                 fmt: str | None = None) -> tuple[datetime, str | None]:
    if item.get("observed_at"):
        try:
            when = parse_rfc3339(str(item["observed_at"]))
        except TimeRuleError:
            raise Rejected("TIMEZONE_UNKNOWN") from None
        source_ts = item.get("source_timestamp")
    elif item.get("source_timestamp"):
        if not source_timezone:  # 오프셋도 source_timezone도 없으면 거부 (요구사항 2.2)
            raise Rejected("TIMEZONE_UNKNOWN")
        source_ts = str(item["source_timestamp"])
        when = parse_local(source_ts, source_timezone, fmt)
    else:
        raise Rejected("TIMEZONE_UNKNOWN")
    if when - now > future_tolerance:
        raise Rejected("FUTURE_TIMESTAMP")
    return when, source_ts


def normalize(item: dict[str, Any], *, metrics: dict[str, str], units: UnitDictionary, source_timezone: str | None,
              now: datetime, future_tolerance: timedelta, clock_offset_ms: int | None = None,
              clock_offset_warn_ms: int = 2000, time_format: str | None = None) -> Normalized:
    """항목 하나를 저장 형태로 정규화한다. 거부 사유가 있으면 Rejected."""
    metric = item.get("metric")
    if metric not in metrics:
        raise Rejected("UNKNOWN_METRIC")
    when, source_ts = resolve_time(item, source_timezone, now, future_tolerance, time_format)
    flag = item.get("quality_flag", "GOOD")
    raw = item.get("value")
    # 결측 표기: value=null ⇔ MISSING. 0이나 직전 값으로 채우지 않는다 (요구사항 2.4)
    if (raw is None) != (flag == "MISSING"):
        raise Rejected("MISSING_FLAG_MISMATCH")
    unit_in = item.get("unit")
    value, unit, orig_value, orig_unit = raw, unit_in, raw, unit_in
    if raw is not None and unit_in:
        res = units.normalize(float(raw), str(unit_in))
        value, unit = res.value, res.unit
        if not res.known or res.unit != metrics[metric]:
            flag = "SUSPECT" if flag == "GOOD" else flag  # 사전에 없거나 항목 단위와 다른 단위 (요구사항 2.3)
    elif raw is None:
        unit = unit_in or metrics[metric]
    if clock_offset_ms is not None and abs(clock_offset_ms) > clock_offset_warn_ms and flag == "GOOD":
        flag = "SUSPECT"  # 시계 오차가 임계값을 넘으면 품질 경고 (observed_at은 바꾸지 않는다, 요구사항 2.6)
    depth = item.get("depth_m")
    return Normalized(metric=str(metric), observed_at=when, value=value, unit=unit, original_value=orig_value,
                      original_unit=orig_unit, quality_flag=flag, source_timestamp=source_ts,
                      depth_m=float(depth) if depth is not None else None, channel=item.get("channel"))


def same_value(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))
