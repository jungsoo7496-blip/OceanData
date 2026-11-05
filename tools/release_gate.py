#!/usr/bin/env python3
"""Release_Gate (17번 요구사항 6): 병합·릴리스 전 필수 검사 묶음.

    make release-gate MILESTONE=M1        # 검사 실행 + var/release-gate.json
    python tools/release_gate.py --notes  # 릴리스 노트 초안 (var/RELEASE_NOTES.md)

필수 검사·도구·입력이 없으면 실패한다. --no-tests는 미리보기이며 릴리스 허용 결과를 만들지 않는다.
DB 마이그레이션은 임시 DB에서만 실행한다. 보안 결과·시험 로그는 var/release-checks/에 남긴다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404 - 저장소 안의 고정된 명령만 실행
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / "backend/.venv/bin/python") if (ROOT / "backend/.venv/bin/python").exists() else sys.executable
# docs/ROADMAP.md 2장 M2 완료 기준 세부 케이스 (M1은 기능 범위, 실측은 M2)
MILESTONE_CASES = {
    "M1": ["T01-a", "T01-b", "T01-c", "T04-a", "T04-c", "T04-d", "T05-a", "T05-b", "T06-a", "T07-a", "T11-a"],
    "M2": ["T01-a", "T01-b", "T01-c", "T01-d", "T01-e", "T01-f", "T02-a", "T02-b", "T02-c", "T02-d", "T02-g",
           "T02-h", "T03-a", "T03-b", "T03-c", "T04-a", "T04-b", "T04-c", "T04-d", "T04-e", "T04-f", "T04-g",
           "T05-a", "T05-b", "T05-c", "T05-d", "T05-e", "T05-f", "T06-a", "T06-e", "T07-a", "T07-b", "T07-c",
           "T07-d", "T10-a", "T10-b", "T12-a", "T12-b", "T12-c"],
    "M3": ["T08-a", "T08-b", "T08-c", "T08-d", "T08-e", "T08-f", "T08-g", "T08-h", "T08-i", "T08-j", "T09-a",
           "T09-b", "T09-c", "T15-d", "T15-e", "T15-g"],
}


def blocked(name: str, reason: str) -> dict:
    return {"check": name, "ok": False, "command": "", "tail": reason}


def run(name: str, args: list[str], cwd: Path | None = None, env: dict[str, str] | None = None,
        output: Path | None = None) -> dict:
    """도구 부재·비정상 종료·시간 초과도 결과에 남기고 다른 필수 검사들을 계속한다."""
    log_path = ROOT / "var/release-checks" / f"{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(  # noqa: S603  # nosec B603
            args, cwd=cwd or ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})}, check=False, timeout=7200)
        ok, stdout, stderr = r.returncode == 0, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok, stdout, stderr = False, "", f"{type(exc).__name__}: 검사 도구 실행에 실패했습니다"
    content = stdout + stderr
    # 하위 도구가 연결 문자열을 출력해도 보고서에는 자격증명을 남기지 않는다.
    content = re.sub(r"(postgresql(?:\+\w+)?://)[^@\s]+@", r"\1[REDACTED]@", content)
    log_path.write_text(content, encoding="utf-8")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        if ok:
            output.write_text(stdout, encoding="utf-8")
        else:
            output.unlink(missing_ok=True)
    return {"check": name, "ok": ok, "command": " ".join(args), "log": str(log_path.relative_to(ROOT)),
            "tail": content[-1500:]}


def _major(v: str | None) -> str:
    m = re.match(r"(\d+)", v or "")
    return m.group(1) if m else "0"


def contract_major_ok(base_ref: str | None) -> dict:
    """주버전이 바뀌면 info.x-compatibility의 하위호환 기간·이전 판 지원 종료일이 있어야 한다 (요구사항 6.3)."""
    cur = yaml.safe_load((ROOT / "contracts/openapi.yaml").read_text(encoding="utf-8"))
    ver = str(cur.get("info", {}).get("version", "0"))
    prev = None
    base_ref = base_ref or "origin/main"
    try:
        r = subprocess.run(  # noqa: S603, S607  # nosec B603 B607
            ["git", "show", f"{base_ref}:contracts/openapi.yaml"],  # noqa: S607
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return blocked("contract_major_version", "기준 계약을 읽지 못했습니다. BASE_REF와 git 실행 환경을 확인하세요")
    if r.returncode != 0:
        return blocked("contract_major_version", "기준 계약을 읽지 못했습니다. 유효한 BASE_REF가 필요합니다")
    prev = str(yaml.safe_load(r.stdout).get("info", {}).get("version", "0"))
    changed = prev is not None and _major(prev) != _major(ver)
    compat = cur.get("info", {}).get("x-compatibility", {})
    ok = not changed or bool(compat.get("overlap_until") and compat.get("previous_end_of_support"))
    return {"check": "contract_major_version", "ok": ok, "command": f"info.version {prev} → {ver}",
            "tail": "" if ok else
            "주버전이 바뀌었는데 info.x-compatibility.overlap_until·previous_end_of_support가 없습니다"}


def gate(milestone: str, base_ref: str | None, with_tests: bool) -> dict:
    if milestone not in MILESTONE_CASES:
        return {"milestone": milestone, "ok": False,
                "checks": [blocked("milestone", "지원하지 않는 마일스톤입니다 (M1·M2·M3)")]}
    checks = [
        run("contracts", [PY, "tools/validate_contracts.py"]),
        run("traceability", [PY, "tools/check_traceability.py"]),
        run("secrets", [PY, "tools/check_secrets.py"]),
        contract_major_ok(base_ref),
    ]
    runtime_py = os.environ.get("RUNTIME_PYTHON")
    checks.append(run("licenses", [PY, "tools/check_licenses.py", "--strict",
                                  *(["--python", runtime_py] if runtime_py else [])]))
    checks.append(run("tool_tests", [PY, "-m", "unittest", "discover", "-s", "tools/tests", "-q"]))
    checks.append(run("python_static_security", [PY, "-m", "bandit", "-q", "-ll", "-r", "app",
                                                 "-x", "app/db/migrations"], ROOT / "backend"))
    checks.append(run("python_vulnerabilities", [PY, "-m", "pip_audit", "--skip-editable"]))
    npm = "npm.cmd" if os.name == "nt" else "npm"
    web_install = run("web_install", [npm, "ci", "--no-audit", "--no-fund"], ROOT / "web")
    checks.append(web_install)
    if web_install["ok"]:
        web_build = run("web_build", [npm, "run", "build"], ROOT / "web")
        checks.append(web_build)
        # 09번 요구사항 11.2·ADR-0039: 빌드한 웹을 합성 응답으로 띄워 axe serious·critical 0·휴대폰 폭·초점 표시
        checks.append(run("web_a11y", [npm, "run", "test:a11y"], ROOT / "web") if web_build["ok"]
                      else blocked("web_a11y", "웹 빌드 실패로 접근성 검사를 하지 못했습니다"))
        checks.append(run("web_vulnerabilities", [npm, "audit", "--omit=dev", "--audit-level=high"], ROOT / "web"))
    else:
        checks.extend(blocked(n, "현재 잠금 파일대로 웹 의존성을 설치하지 못했습니다")
                      for n in ("web_build", "web_a11y", "web_vulnerabilities"))
    image = "marine-api:release-check"
    build = run("runtime_image", ["docker", "build", "-f", "backend/Dockerfile", "-t", image, "."])
    checks.append(build)
    sbom = ROOT / "var/sbom.spdx.json"
    if build["ok"]:
        bom = run("sbom", ["docker", "run", "--rm", "-v", "/var/run/docker.sock:/var/run/docker.sock",
                           "anchore/syft:v1.18.1", image, "-o", "spdx-json"], output=sbom)
        checks.append(bom)
        checks.append(run("sbom_licenses", [PY, "tools/check_sbom_licenses.py", str(sbom), "--strict"])
                      if bom["ok"] else blocked("sbom_licenses", "이번 실행의 SBOM을 생성하지 못했습니다"))
        checks.append(run("image_vulnerabilities", ["docker", "run", "--rm", "-v",
                          "/var/run/docker.sock:/var/run/docker.sock", "aquasec/trivy:0.58.1", "image",
                          "--severity", "HIGH,CRITICAL", "--ignore-unfixed", "--exit-code", "1",
                          "--format", "json", image], output=ROOT / "var/release-checks/trivy-image.json"))
    else:
        checks.extend(blocked(n, "이번 실행의 런타임 이미지를 빌드하지 못했습니다")
                      for n in ("sbom", "sbom_licenses", "image_vulnerabilities"))
    checks.append(run("source_vulnerabilities", ["docker", "run", "--rm", "-v", f"{ROOT}:/src:ro",
                      "aquasec/trivy:0.58.1", "fs", "--scanners", "vuln", "--severity", "HIGH,CRITICAL",
                      "--ignore-unfixed", "--exit-code", "1", "--format", "json", "/src"],
                      output=ROOT / "var/release-checks/trivy-fs.json"))
    for name, variable, filename in (("dast", "RELEASE_DAST_URL", "zap-report.json"),
                                     ("web_dast", "RELEASE_DAST_WEB_URL", "zap-web-report.json")):
        target = os.environ.get(variable)
        if target:
            zap_dir = ROOT / "var/release-checks"
            zap_dir.mkdir(parents=True, exist_ok=True)
            (zap_dir / filename).unlink(missing_ok=True)
            # 기본 WARN을 무시한 실행 코드만으로 판정하지 않고 실제 위험 등급을 별도로 확인한다.
            checks.append(run(name, ["docker", "run", "--rm", "--network", "host", "-u", "0",
                                     "-v", f"{zap_dir}:/zap/wrk", "ghcr.io/zaproxy/zaproxy:2.15.0",
                                     "zap-baseline.py", "-t", target, "-I", "-J", filename]))
            checks.append(run(name + "_risk", [PY, "tools/zap_check.py", str(zap_dir / filename)]))
        else:
            checks.extend(blocked(n, f"{variable}에 격리 시험 대상 주소를 지정해야 합니다")
                          for n in (name, name + "_risk"))
    results = ROOT / "var/acceptance-results.jsonl"
    report: dict = {"items": []}
    if with_tests:
        results.parent.mkdir(parents=True, exist_ok=True)
        results.write_text("", encoding="utf-8")
        if os.environ.get("TEST_DATABASE_URL"):
            checks.append(run("migrations", [PY, "tools/migration_check.py"]))
            checks.append(run("backend_tests", [PY, "-m", "pytest", "-q"], ROOT / "backend",
                              {"ACCEPTANCE_RESULTS": str(results)}))
        else:
            checks.extend(blocked(n, "TEST_DATABASE_URL이 없어 필수 시험을 실행하지 못했습니다")
                          for n in ("migrations", "backend_tests"))
        checks.append(run("go_vet", ["go", "vet", "./..."], ROOT / "agent"))
        checks.append(run("go_tests", [PY, "tools/go_acceptance.py", "--out", str(results)]))
        checks.append(run("web_e2e", [PY, "tools/web_acceptance.py", "--out", str(results)])
                      if web_install["ok"] else blocked("web_e2e", "현재 잠금 파일의 웹 의존성 설치 실패"))
        security_names = {"image_vulnerabilities", "source_vulnerabilities", "dast", "dast_risk",
                          "web_dast", "web_dast_risk"}
        if all(c["ok"] for c in checks if c["check"] in security_names):
            directory = ROOT / "var/release-checks"
            checks.append(run("security_acceptance", [PY, "tools/security_acceptance.py", "--trivy-image",
                              str(directory / "trivy-image.json"), "--trivy-source", str(directory / "trivy-fs.json"),
                              "--zap-api", str(directory / "zap-report.json"), "--zap-web",
                              str(directory / "zap-web-report.json"), "--out", str(results)]))
        else:
            checks.append(blocked("security_acceptance", "이번 실행의 필수 보안 보고서가 없거나 실패했습니다"))
        aggregate = run("acceptance_report", [PY, "tools/acceptance_report.py", "--results", str(results)])
        checks.append(aggregate)
        path = ROOT / "var/acceptance.json"
        if aggregate["ok"] and path.exists():
            report = json.loads(path.read_text(encoding="utf-8"))
    else:
        checks.append(blocked("tests", "--no-tests는 미리보기입니다. 시험 생략으로 릴리스를 허용할 수 없습니다"))
    status = {r["id"]: r["status"] for r in report["items"]}
    need = MILESTONE_CASES[milestone]
    missing = [i for i in need if status.get(i) != "PASS"]
    detail = ", ".join(f"{i}({status.get(i, '보고 없음')})" for i in missing)
    checks.append({"check": f"acceptance_{milestone}", "ok": not missing, "command": "var/acceptance.json",
                   "tail": f"PASS가 아닌 세부 케이스: {detail}" if missing else ""})
    return {"milestone": milestone, "tested_at": datetime.now(UTC).isoformat(),
            "ok": all(c["ok"] for c in checks), "checks": checks}


def release_notes(base_ref: str | None) -> str:
    """포함 스펙·바뀐 계약·통과 수용시험·알려진 제한 (요구사항 6.2)."""
    lines = ["# 릴리스 노트 (초안)", ""]
    specs = sorted(p.parent.name for p in (ROOT / ".kiro/specs").glob("*/tasks.md"))
    lines += ["## 포함 스펙", *[f"- {s}" for s in specs], ""]
    if base_ref:
        diff = subprocess.run(  # noqa: S603, S607  # nosec B603 B607
            ["git", "diff", "--stat", base_ref, "--", "contracts/"], cwd=ROOT,  # noqa: S607
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False).stdout
        lines += ["## 바뀐 계약", "```", diff.strip() or "(변경 없음)", "```", ""]
    rep = ROOT / "var/acceptance.json"
    if rep.exists():
        items = json.loads(rep.read_text(encoding="utf-8"))["items"]
        lines += ["## 통과한 수용시험", ", ".join(i["id"] for i in items if i["status"] == "PASS") or "-", ""]
    lines += ["## 알려진 제한 (tasks.md 미완료 항목)"]
    for t in sorted((ROOT / ".kiro/specs").glob("*/tasks.md")):
        for line in t.read_text(encoding="utf-8").splitlines():
            if line.startswith("- [ ]"):
                lines.append(f"- {t.parent.name}: {line[6:]}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--milestone", choices=sorted(MILESTONE_CASES), default=os.environ.get("MILESTONE") or "M1")
    ap.add_argument("--base-ref", default=os.environ.get("BASE_REF"))
    ap.add_argument("--no-tests", action="store_true")
    ap.add_argument("--notes", action="store_true")
    a = ap.parse_args()
    (ROOT / "var").mkdir(exist_ok=True)
    if a.notes:
        (ROOT / "var/RELEASE_NOTES.md").write_text(release_notes(a.base_ref), encoding="utf-8")
        print("var/RELEASE_NOTES.md")
        return 0
    rep = gate(a.milestone, a.base_ref, not a.no_tests)
    (ROOT / "var/release-gate.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    for c in rep["checks"]:
        print(f"{'통과' if c['ok'] else '실패'}  {c['check']}" + ("" if c["ok"] else f"\n    {c['tail'][-400:]}"))
    print("릴리스 게이트 " + ("통과" if rep["ok"] else "실패"))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
