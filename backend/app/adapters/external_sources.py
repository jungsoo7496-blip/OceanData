"""ExternalSourcePort 어댑터 (12번 요구사항 1~3).

실제 공공 기상·해양 Open API의 명세·이용조건은 확인 후 어댑터로 붙인다 (확인 필요, 지어내지 않는다).
지금은 모의 어댑터 하나다. 모의 응답 형식은 이 플랫폼이 시험용으로 정한 것이며 어떤 실제 기관 API와도 같지 않다:
    {"items": [{"stn": "<관측소 코드>", "tm": "<RFC 3339>", "<필드>": <값>, ...}, ...]}
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class FetchResult:
    raw: dict[str, Any]  # 원 응답 (그대로 보관)
    calls: int  # 소비한 호출 수 (호출 한도 계산용)


class SourceFormatError(Exception):
    """응답 구조가 필드 매핑과 맞지 않는다 (요구사항 3.1)."""


class MockOceanSource:
    """10분 간격 합성 관측값. 같은 입력이면 같은 값 (시험 재현). 과거 조회 지원."""

    kind = "MOCK"
    supports_backfill = True

    def __init__(self, broken: bool = False) -> None:
        self.broken = broken  # 응답 형식 변경 시험용

    def fetch(self, stations: list[str], since: datetime, until: datetime) -> FetchResult:
        items = []
        t = since.replace(second=0, microsecond=0)
        t -= timedelta(minutes=t.minute % 10)
        if t < since:
            t += timedelta(minutes=10)
        while t < until:
            for i, stn in enumerate(stations):
                phase = (t.timestamp() / 3600.0) + i
                rec: dict[str, Any] = {"stn": stn, "tm": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                       "wh": round(1.2 + 0.9 * math.sin(phase / 3), 2),
                                       "ws_kn": round(12 + 6 * math.sin(phase / 5), 1),
                                       "wt": round(17 + 2 * math.sin(phase / 12), 2)}
                if self.broken:
                    rec = {"station": stn, "time": rec["tm"], "waveHeight": rec["wh"]}
                items.append(rec)
            t += timedelta(minutes=10)
        return FetchResult(raw={"items": items}, calls=max(1, len(stations)))


def get_path(rec: dict[str, Any], path: str) -> Any:
    cur: Any = rec
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise SourceFormatError(f"필드 없음: {path}")
        cur = cur[part]
    return cur


def map_items(raw: dict[str, Any], mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """필드 매핑으로 관측값 목록을 만든다. mapping = {station_path, time_path, metrics: {metric: {path, unit}}}."""
    items = raw.get("items")
    if not isinstance(items, list):
        raise SourceFormatError("items 배열이 없습니다")
    out = []
    for rec in items:
        stn, tm = str(get_path(rec, mapping["station_path"])), str(get_path(rec, mapping["time_path"]))
        for metric, m in mapping["metrics"].items():
            out.append({"station": stn, "observed_at": tm, "metric": metric, "value": get_path(rec, m["path"]),
                        "unit": m["unit"]})
    return out


def build_source(kind: str, broken: bool = False) -> MockOceanSource:
    if kind == "MOCK":
        return MockOceanSource(broken)
    raise ValueError(f"구현된 어댑터가 없습니다: {kind} (명세 확인 후 추가)")
