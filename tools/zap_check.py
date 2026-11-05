#!/usr/bin/env python3
"""T01-h: ZAP Traditional JSON의 높은 위험을 검사한다. 누락·손상 보고는 실패다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def assess(doc: dict) -> tuple[bool, str]:
    sites = doc.get("site")
    if not isinstance(sites, list) or not sites:
        return False, "ZAP 검사 대상 결과가 없습니다"
    high, count = [], 0
    for site in sites:
        if (not isinstance(site, dict) or not isinstance(site.get("@name"), str)
                or not site["@name"].strip() or not isinstance(site.get("alerts"), list)):
            return False, "ZAP 결과 형식이 올바르지 않습니다"
        for alert in site["alerts"]:
            if not isinstance(alert, dict) or str(alert.get("riskcode")) not in {"0", "1", "2", "3"}:
                return False, "ZAP 위험 등급을 확인하지 못했습니다"
            count += 1
            if str(alert["riskcode"]) == "3":
                high.append(str(alert.get("pluginid", "식별자 미상")))
    return not high, f"ZAP 경보 {count}종, 높은 위험 {len(high)}종" + (
        " (규칙 " + ", ".join(sorted(set(high))) + ")" if high else "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        doc = json.loads(args.report.read_text(encoding="utf-8-sig"))
        ok, message = assess(doc) if isinstance(doc, dict) else (False, "ZAP 결과가 JSON 객체가 아닙니다")
    except (OSError, ValueError):
        ok, message = False, "ZAP 보고서를 읽지 못했습니다"
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
