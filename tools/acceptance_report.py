#!/usr/bin/env python3
"""Acceptance_Runner 보고 (17번 요구사항 1).

docs/ACCEPTANCE.md 세부 ID별 PASS·FAIL·NOT_IMPLEMENTED·NEEDS_HARDWARE·NEEDS_INPUT.

사용:
    make test-acceptance            # 시험 실행 + 보고 (var/acceptance.json, var/acceptance.md)
    python tools/acceptance_report.py --results var/acceptance-results.jsonl

시험 함수는 `@pytest.mark.covers("T07-a", ...)`로 덮는 세부 ID를 밝힌다. 시험이 없는 ID는
tests/acceptance/status.json의 상태(NEEDS_HARDWARE·NEEDS_INPUT·NOT_IMPLEMENTED와 사유)를 쓰고,
거기도 없으면 NOT_IMPLEMENTED.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROW = re.compile(r"^\|\s*(T\d{2}-[a-z])\s*\|\s*([^|]*)\|\s*([^|]*)\|\s*([^|]*)\|")


def catalog() -> list[dict[str, str]]:
    out = []
    for line in (ROOT / "docs/ACCEPTANCE.md").read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if m:
            out.append({"id": m.group(1), "case": m.group(2).strip(), "expected": m.group(3).strip(),
                        "specs": m.group(4).strip()})
    return out


def load_results(path: Path) -> dict[str, list[dict[str, str]]]:
    by_id: dict[str, list[dict[str, str]]] = {}
    if not path.exists():
        return by_id
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        for i in r["covers"]:
            by_id.setdefault(i, []).append(r)
    return by_id


def build(results: Path, status_file: Path) -> dict:
    declared = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
    by_id = load_results(results)
    rows = []
    for c in catalog():
        tests = by_id.get(c["id"], [])
        if tests:
            failed = [t for t in tests if t["outcome"] == "failed"]
            skipped = [t for t in tests if t["outcome"] == "skipped"]
            status = "FAIL" if failed else ("NOT_RUN" if len(skipped) == len(tests) else "PASS")
        else:
            d = declared.get(c["id"], {})
            status, failed, skipped = d.get("status", "NOT_IMPLEMENTED"), [], []
        rows.append({**c, "status": status, "reason": declared.get(c["id"], {}).get("reason", ""),
                     "tests": [t["nodeid"] for t in tests],
                     "reproduce": [t.get("reproduce") or f"cd backend && pytest -q '{t['nodeid']}'" for t in failed] or None,
                     "log": [t.get("log", "") for t in failed] or None})
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {"summary": summary, "items": rows}


def markdown(rep: dict) -> str:
    lines = ["# 수용시험 결과", "", "| 상태 | 건수 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(rep["summary"].items())]
    lines += ["", "| ID | 케이스 | 상태 | 시험 / 사유 |", "|---|---|---|---|"]
    for r in rep["items"]:
        detail = "<br>".join(r["tests"]) if r["tests"] else r["reason"]
        if r["reproduce"]:
            detail += "<br>재현: " + "<br>".join(f"`{x}`" for x in r["reproduce"])
        lines.append(f"| {r['id']} | {r['case']} | {r['status']} | {detail} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(ROOT / "var/acceptance-results.jsonl"))
    ap.add_argument("--status", default=str(ROOT / "backend/tests/acceptance/status.json"))
    ap.add_argument("--out", default=str(ROOT / "var/acceptance"))
    a = ap.parse_args()
    rep = build(Path(a.results), Path(a.status))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(markdown(rep), encoding="utf-8")
    print(" · ".join(f"{k} {v}" for k, v in sorted(rep["summary"].items())))
    return 1 if rep["summary"].get("FAIL") else 0


if __name__ == "__main__":
    raise SystemExit(main())
