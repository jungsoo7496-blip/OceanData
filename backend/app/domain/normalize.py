"""Normalizer — 촬영시각·위치·단위 정규화 (domain.md 6장, 03번 요구사항 5·6·7). 순수 함수만."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# 촬영시각 근거로 인정하는 출처. 파일 생성·수정 시각, 업로드 시각은 근거가 아니다 (요구사항 5.1)
EVIDENCE_SOURCES = ("GPS", "NTP", "DEVICE", "MANIFEST", "MANUAL")
NON_EVIDENCE_KEYS = frozenset({"mtime", "ctime", "atime", "birthtime", "file_mtime", "file_ctime",
                               "uploaded_at", "upload_time", "received_at"})


@dataclass
class CaptureTime:
    capture_start_at: datetime | None
    capture_end_at: datetime | None
    timestamp_quality: dict[str, Any]
    original_timestamps: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class TimeRuleError(ValueError):
    """시간대 없는 시각 등 거부해야 하는 입력."""


class NoOffsetError(TimeRuleError):
    """시각은 읽히지만 시간대 오프셋이 없다 — 원천 시각이면 source_timestamp로 보낸다."""


class TimeRangeError(TimeRuleError):
    """읽히지만 UTC로 바꾸면 표현 범위를 넘는다 (예: 0001-01-01T00:00:00+23:59)."""


def parse_rfc3339(value: str) -> datetime:
    """오프셋이 있는 RFC 3339만 받는다. 'Z' 허용."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise TimeRuleError(f"시각 형식이 올바르지 않습니다: {value!r}") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise NoOffsetError(f"시간대 오프셋이 없는 시각입니다: {value!r}")
    try:
        return dt.astimezone(UTC)
    except OverflowError as exc:
        raise TimeRangeError(f"UTC로 바꿀 수 없는 시각입니다: {value!r}") from exc


def resolve_capture_time(start: str | None, end: str | None, quality: dict[str, Any] | None,
                         original_timestamps: dict[str, str] | None = None) -> CaptureTime:
    """제출된 촬영시각과 근거로 저장값을 정한다.

    - 근거 출처가 UNKNOWN이거나 없으면 시각을 비우고 WARNING (요구사항 5.2)
    - 오프셋이 있으면 UTC로 바꾸고 원래 문자열을 original_timestamps에 보존 (요구사항 5.3)
    - mtime·업로드 시각 같은 키는 보존만 하고 근거로 쓰지 않는다 (요구사항 5.1)
    """
    q = dict(quality or {})
    source = q.get("source") or "UNKNOWN"
    originals = {k: v for k, v in (original_timestamps or {}).items()}
    if source not in EVIDENCE_SOURCES or start is None:
        return CaptureTime(None, None, {"source": "UNKNOWN", "verified": False, "est_offset_ms": None},
                           originals, ["capture_time_unknown"])
    start_utc = parse_rfc3339(start)
    end_utc = parse_rfc3339(end) if end else None
    if end_utc is not None and end_utc < start_utc:
        raise TimeRuleError("capture_end_at이 capture_start_at보다 빠릅니다")
    originals.setdefault("capture_start_at", start)
    if end:
        originals.setdefault("capture_end_at", end)
    verified = bool(q.get("verified", False)) and source != "MANUAL"  # 사람이 입력한 시각은 기본 미검증
    return CaptureTime(start_utc, end_utc, {"source": source, "verified": verified,
                                            "est_offset_ms": q.get("est_offset_ms")}, originals, [])


# ---- 위치
@dataclass(frozen=True)
class LocationProblem:
    pointer: str
    code: str
    message: str


def _positions(geometry: dict[str, Any]) -> list[tuple[str, Any]]:
    """좌표 자리와 그 값. 배열이 아닌 곳은 그 자리 하나만 돌려줘 문자·키마다 문제를 만들지 않는다."""
    t, c = geometry.get("type"), geometry.get("coordinates")
    if t == "Point":
        return [("/coordinates", c)]
    if t not in ("LineString", "Polygon") or c is None:
        return []
    if not isinstance(c, list):
        return [("/coordinates", c)]
    if t == "LineString":
        return [(f"/coordinates/{i}", p) for i, p in enumerate(c)]
    out: list[tuple[str, Any]] = []
    for r, ring in enumerate(c):
        if isinstance(ring, list):
            out += [(f"/coordinates/{r}/{i}", p) for i, p in enumerate(ring)]
        else:
            out.append((f"/coordinates/{r}", ring))
    return out


def _number(x: Any) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool)


def check_location(geometry: dict[str, Any] | None, base_pointer: str = "/location") -> list[LocationProblem]:
    """GeoJSON 좌표 범위 검사. 위도 자리 값이 ±90을 넘고 경도 자리 값이 ±90 안이면 순서 뒤바뀜으로 본다.

    어떤 JSON이 와도 예외를 내지 않고 문제 목록을 돌려준다 (사전 검증 API가 500을 내지 않게)."""
    if geometry is None:
        return []
    if geometry.get("type") not in ("Point", "LineString", "Polygon"):
        return [LocationProblem(base_pointer + "/type", "unsupported_geometry", "Point·LineString·Polygon만 받습니다")]
    problems: list[LocationProblem] = []
    for ptr, pos in _positions(geometry):
        if len(problems) >= 200:  # 응답 크기 상한(검증기가 앞 100개만 보여준다)보다 넉넉하면 충분하다
            break
        if not isinstance(pos, list) or len(pos) < 2 or not all(_number(x) for x in pos[:2]):
            problems.append(LocationProblem(base_pointer + ptr, "invalid_position",
                                            "좌표는 [경도, 위도] 숫자 배열입니다"))
            continue
        try:
            lon, lat = float(pos[0]), float(pos[1])
        except OverflowError:  # 아주 큰 정수
            lon = lat = math.inf
        if not (math.isfinite(lon) and math.isfinite(lat)):
            problems.append(LocationProblem(base_pointer + ptr, "invalid_position", "좌표가 유한한 수가 아닙니다"))
        elif abs(lat) > 90 and abs(lon) <= 90:
            problems.append(LocationProblem(base_pointer + ptr, "lonlat_swapped",
                                            "위도 자리 값이 ±90을 벗어납니다. 좌표 순서는 [경도, 위도]입니다"))
        elif abs(lon) > 180 or abs(lat) > 90:
            problems.append(LocationProblem(base_pointer + ptr, "out_of_range", "좌표가 범위를 벗어납니다"))
        elif lon == 0 and lat == 0:
            problems.append(LocationProblem(base_pointer + ptr, "null_island",
                                            "(0, 0)은 위치 미상 표기로 쓰지 않습니다. 모르면 location을 비웁니다"))
    return problems


def generalize_location(geometry: dict[str, Any] | None, precision: str | None,
                        decimals: int = 2) -> dict[str, Any] | None:
    """응답용 위치 정밀도 낮추기 (요구사항 6.4). NONE이면 숨기고 GENERALIZED면 소수 둘째 자리(약 1km)."""
    if geometry is None or precision == "NONE":
        return None
    if precision != "GENERALIZED":
        return geometry

    def rnd(p: list[float]) -> list[float]:
        return [round(float(x), decimals) for x in p[:2]]

    t, c = geometry["type"], geometry["coordinates"]
    if t == "Point":
        return {"type": t, "coordinates": rnd(c)}
    if t == "LineString":
        return {"type": t, "coordinates": [rnd(p) for p in c]}
    return {"type": t, "coordinates": [[rnd(p) for p in ring] for ring in c]}


# ---- 단위
@dataclass(frozen=True)
class UnitResult:
    value: float | None
    unit: str
    original_value: float | None
    original_unit: str
    known: bool


class UnitDictionary:
    """contracts/codes/units.json 기반. 정규화 값 = 원래 값 × scale + offset."""

    def __init__(self, units: list[dict[str, Any]]) -> None:
        self._by_alias: dict[str, dict[str, Any]] = {}
        for u in units:
            for name in [u["code"], *u.get("aliases", [])]:
                self._by_alias[name.strip().lower()] = u

    def lookup(self, unit: str) -> dict[str, Any] | None:
        return self._by_alias.get(unit.strip().lower())

    def normalize(self, value: float | None, unit: str) -> UnitResult:
        u = self.lookup(unit)
        if u is None:
            return UnitResult(value, unit, value, unit, known=False)
        conv = u.get("to_canonical")
        if conv is None:
            return UnitResult(value, u["code"], value, unit, known=True)
        new = None if value is None else value * float(conv.get("scale", 1)) + float(conv.get("offset", 0))
        return UnitResult(new, conv["unit"], value, unit, known=True)
