#!/usr/bin/env python3
"""합성 관측값 생성 (07번 요구사항 9.1). 수온·파고 시계열에 시험 사례를 섞어 JSON 배치·CSV로 쓴다.

사례: 정상(UTC·KST), 단위 오류, 시간대 없음, KST 원천 시각(source_timestamp), 결측, 중복, 미래 시각
사용: tools/sample_observations.py --device dev_... [--hours 6] [--out var/samples/obs]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

Z = "%Y-%m-%dT%H:%M:%SZ"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True)
    ap.add_argument("--session", default=None)
    ap.add_argument("--hours", type=int, default=6)
    ap.add_argument("--step-min", type=int, default=10)
    ap.add_argument("--out", default="var/samples/obs")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rnd = random.Random(a.seed)  # noqa: S311 - 합성 시험 데이터
    start = (datetime.now(UTC) - timedelta(hours=a.hours)).replace(minute=0, second=0, microsecond=0)
    items: list[dict] = []
    rows: list[dict] = []
    cases: dict[str, str] = {}
    n = a.hours * 60 // a.step_min
    for i in range(n):
        t = start + timedelta(minutes=i * a.step_min)
        temp = round(17 + 1.5 * math.sin(i / 12) + rnd.uniform(-0.1, 0.1), 2)
        wave = round(0.8 + 0.3 * math.sin(i / 7) + rnd.uniform(-0.05, 0.05), 2)
        rows.append({"time": (t + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S"), "temp": temp, "wave": wave})
        items.append({"metric": "sea_water_temperature", "observed_at": t.strftime(Z), "value": temp,
                      "unit": "degC", "quality_flag": "GOOD", "depth_m": 1.5})
        items.append({"metric": "significant_wave_height",
                      "observed_at": (t + timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                      "value": wave, "unit": "m", "quality_flag": "GOOD"})
    t0 = start + timedelta(minutes=5)
    extra = [
        ("단위 오류(SUSPECT)", {"metric": "sea_water_temperature", "observed_at": t0.strftime(Z), "value": 290.1,
                             "unit": "kelvinish", "quality_flag": "GOOD", "depth_m": 3.0}),
        ("시간대 없음(TIMEZONE_UNKNOWN)", {"metric": "sea_water_temperature",
                                       "observed_at": t0.strftime("%Y-%m-%dT%H:%M:%S"), "value": 17.0,
                                       "unit": "degC", "quality_flag": "GOOD", "depth_m": 5.0}),
        ("KST 원천 시각(장비 시간대로 변환)", {
            "metric": "significant_wave_height",
            "source_timestamp": (t0 + timedelta(hours=9)).strftime("%Y/%m/%d %H:%M:%S"),
            "value": 0.9, "unit": "m", "quality_flag": "GOOD", "channel": "b"}),
        ("결측(MISSING)", {"metric": "sea_water_temperature", "observed_at": (t0 + timedelta(minutes=1)).strftime(Z),
                         "value": None, "quality_flag": "MISSING", "depth_m": 1.5}),
        ("중복(DUPLICATE)", dict(items[0])),
        ("미래 시각(FUTURE_TIMESTAMP)", {"metric": "sea_water_temperature",
                                     "observed_at": (datetime.now(UTC) + timedelta(hours=2)).strftime(Z),
                                     "value": 17.0, "unit": "degC", "quality_flag": "GOOD"}),
    ]
    for label, it in extra:
        cases[str(len(items))] = label
        items.append(it)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    batch: dict = {"schema_version": "1.0.0", "source_system_id": a.device, "observations": items}
    if a.session:
        batch["session_id"] = a.session
    (out / "batch.json").write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    with (out / "sensor.csv").open("w", newline="", encoding="utf-8") as f:  # 장비 현지 시각(KST) CSV
        w = csv.DictWriter(f, fieldnames=["time", "temp", "wave"])
        w.writeheader()
        w.writerows(rows)
    print(f"{out}/batch.json ({len(items)}개, 시험 사례 {len(cases)}개), {out}/sensor.csv ({len(rows)}행)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
