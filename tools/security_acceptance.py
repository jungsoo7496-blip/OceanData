#!/usr/bin/env python3
"""로컬 Trivy·ZAP 보고서 4개를 T01-h 수용시험 결과에 연결한다 (17번 요구사항 1·4).

보고서의 내용만 검증하며 GitHub Actions 성공을 뜻하지 않는다. 실행 환경·스캔 대상은 원본 보고서를 함께 확인한다.
Trivy는 SchemaVersion=2, 비어 있지 않은 Results, HIGH·CRITICAL 0건이어야 한다.
ZAP Traditional JSON은 zap_check의 높은 위험 판정을 그대로 사용한다. 누락·손상·미상 위험은 실패다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess  # nosec B404 - 재현 명령의 Windows 인용에만 사용한다
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zap_check import assess as assess_zap

NODE_ID = "tools/security_acceptance.py::T01_h_local_scan_reports"
SEVERITIES = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})


def assess_trivy(doc: dict, artifact_type: str) -> tuple[bool, str]:
    """Trivy v2 취약점 보고서를 검사한다. 생략된 Vulnerabilities는 발견 0건이다."""
    if not isinstance(doc.get("SchemaVersion"), int) or doc["SchemaVersion"] != 2:
        return False, "Trivy SchemaVersion=2 보고서가 필요합니다"
    if doc.get("ArtifactType") != artifact_type:
        return False, "Trivy 스캔 대상 유형이 요청한 보고서와 다릅니다"
    if not isinstance(doc.get("ArtifactName"), str) or not doc["ArtifactName"].strip():
        return False, "Trivy 스캔 대상 이름이 없습니다"
    results = doc.get("Results")
    if not isinstance(results, list) or not results:
        return False, "Trivy 검사 대상 결과가 없습니다"
    high, count = 0, 0
    for result in results:
        if (not isinstance(result, dict) or not isinstance(result.get("Target"), str)
                or not result["Target"].strip()):
            return False, "Trivy 결과 형식이 올바르지 않습니다"
        if (not isinstance(result.get("Class"), str) or result["Class"] not in {"os-pkgs", "lang-pkgs"}
                or not isinstance(result.get("Type"), str) or not result["Type"].strip()):
            return False, "Trivy 취약점 검사 대상 유형을 확인하지 못했습니다"
        vulnerabilities = result.get("Vulnerabilities", [])
        if not isinstance(vulnerabilities, list):
            return False, "Trivy 취약점 목록 형식이 올바르지 않습니다"
        for vulnerability in vulnerabilities:
            if (not isinstance(vulnerability, dict) or not isinstance(vulnerability.get("Severity"), str)
                    or vulnerability["Severity"] not in SEVERITIES):
                return False, "Trivy 위험 등급을 확인하지 못했습니다"
            count += 1
            high += vulnerability["Severity"] in {"HIGH", "CRITICAL"}
    return high == 0, f"Trivy 검사 대상 {len(results)}개, 취약점 {count}건, HIGH·CRITICAL {high}건"


def inspect_report(path: Path, kind: str) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "ok": False}
    try:
        raw = path.read_bytes()
        result["sha256"] = hashlib.sha256(raw).hexdigest()
        doc = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(doc, dict):
            result["message"] = "보고서가 JSON 객체가 아닙니다"
            return result
        ok, message = (assess_zap(doc) if kind.startswith("zap_") else assess_trivy(
            doc, "container_image" if kind == "trivy_image" else "filesystem"))
        result.update(ok=ok, message=message)
    except (OSError, UnicodeError, ValueError):
        result["message"] = "보고서를 읽지 못했거나 JSON 형식이 올바르지 않습니다"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trivy-image", required=True, type=Path)
    parser.add_argument("--trivy-source", required=True, type=Path)
    parser.add_argument("--zap-api", required=True, type=Path)
    parser.add_argument("--zap-web", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    reports = {kind: inspect_report(getattr(args, kind), kind)
               for kind in ("trivy_image", "trivy_source", "zap_api", "zap_web")}
    ok = all(report["ok"] for report in reports.values())
    log = "로컬 보안 스캔 보고서 검증 (GitHub Actions 실행 결과와 별개)\n" + "\n".join(
        f"{kind}: {report['message']}" for kind, report in reports.items())
    command = ["python", "tools/security_acceptance.py"]
    for kind in reports:
        command.extend(["--" + kind.replace("_", "-"), str(getattr(args, kind))])
    command.extend(["--out", str(args.out)])
    row = {"nodeid": NODE_ID, "covers": ["T01-h"], "outcome": "passed" if ok else "failed", "log": log,
           "reproduce": subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command),
           "tested_at": datetime.now(UTC).isoformat(), "evidence_source": "LOCAL_SECURITY_SCAN_REPORTS",
           "reports": reports}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(log)
    print("T01-h 로컬 보안 보고서 검증 " + ("통과" if ok else "실패"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
