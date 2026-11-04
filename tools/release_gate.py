#!/usr/bin/env python3
"""Release_Gate (17번 요구사항 6): 병합·릴리스 전 필수 검사 묶음.

    make release-gate MILESTONE=M1        # 검사 실행 + var/release-gate.json
    python tools/release_gate.py --notes  # 릴리스 노트 초안 (var/RELEASE_NOTES.md)

검사: 계약, 추적성, 비밀값, 라이선스(런타임 + SBOM이 있으면 SBOM), 마이그레이션 up/down/up(시험 DB가 있으면),
단위·통합·계약 시험, 수용시험(마일스톤 세부 케이스는 PASS여야 함), 계약 주버전 변경 시 하위호환 기간·지원 종료일.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404 - 저장소 안의 고정된 명령만 실행
import sys
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


def run(name: str, args: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None) -> dict:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env={**os.environ, **(env or {})},
                       check=False)  # nosec B603
    return {"check": name, "ok": r.returncode == 0, "command": " ".join(args), "tail": (r.stdout + r.stderr)[-1500:]}


def _major(v: str | None) -> str:
    m = re.match(r"(\d+)", v or "")
    return m.group(1) if m else "0"


def contract_major_ok(base_ref: str | None) -> dict:
    """주버전이 바뀌면 info.x-compatibility의 하위호환 기간·이전 판 지원 종료일이 있어야 한다 (요구사항 6.3)."""
    cur = yaml.safe_load((ROOT / "contracts/openapi.yaml").read_text(encoding="utf-8"))
    ver = str(cur.get("info", {}).get("version", "0"))
    prev = None
    if base_ref:
        r = subprocess.run(["git", "show", f"{base_ref}:contracts/openapi.yaml"],
                           cwd=ROOT, capture_output=True, text=True, check=False)  # nosec B603 B607
        if r.returncode == 0:
            prev = str(yaml.safe_load(r.stdout).get("info", {}).get("version", "0"))
    changed = prev is not None and _major(prev) != _major(ver)
    compat = cur.get("info", {}).get("x-compatibility", {})
    ok = not changed or bool(compat.get("overlap_until") and compat.get("previous_end_of_support"))
    return {"check": "contract_major_version", "ok": ok, "command": f"info.version {prev} → {ver}",
            "tail": "" if ok else
            "주버전이 바뀌었는데 info.x-compatibility.overlap_until·previous_end_of_support가 없습니다"}


def gate(milestone: str, base_ref: str | None, with_tests: bool) -> dict:
    checks = [
        run("contracts", [PY, "tools/validate_contracts.py"]),
        run("traceability", [PY, "tools/check_traceability.py"]),
        run("secrets", [PY, "tools/check_secrets.py"]),
        contract_major_ok(base_ref),
    ]
    runtime_py = os.environ.get("RUNTIME_PYTHON")
    checks.append(run("licenses", [PY, "tools/check_licenses.py", *(["--python", runtime_py] if runtime_py else [])]))
    sbom = ROOT / "var/sbom.spdx.json"
    if sbom.exists():
        checks.append(run("sbom_licenses", [PY, "tools/check_sbom_licenses.py", str(sbom)]))
    if with_tests and os.environ.get("TEST_DATABASE_URL"):
        checks.append(run("tests_and_acceptance", ["make", "test-acceptance"]))
    report = json.loads((ROOT / "var/acceptance.json").read_text(encoding="utf-8")) \
        if (ROOT / "var/acceptance.json").exists() else {"items": []}
    status = {r["id"]: r["status"] for r in report["items"]}
    need = MILESTONE_CASES.get(milestone, [])
    missing = [i for i in need if status.get(i) != "PASS"]
    detail = ", ".join(f"{i}({status.get(i, '보고 없음')})" for i in missing)
    checks.append({"check": f"acceptance_{milestone}", "ok": not missing, "command": "var/acceptance.json",
                   "tail": f"PASS가 아닌 세부 케이스: {detail}" if missing else ""})
    return {"milestone": milestone, "ok": all(c["ok"] for c in checks), "checks": checks}


def release_notes(base_ref: str | None) -> str:
    """포함 스펙·바뀐 계약·통과 수용시험·알려진 제한 (요구사항 6.2)."""
    lines = ["# 릴리스 노트 (초안)", ""]
    specs = sorted(p.parent.name for p in (ROOT / ".kiro/specs").glob("*/tasks.md"))
    lines += ["## 포함 스펙", *[f"- {s}" for s in specs], ""]
    if base_ref:
        diff = subprocess.run(["git", "diff", "--stat", base_ref, "--", "contracts/"], cwd=ROOT,
                              capture_output=True, text=True, check=False).stdout  # nosec B603 B607
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
    ap.add_argument("--milestone", default=os.environ.get("MILESTONE", "M1"))
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
