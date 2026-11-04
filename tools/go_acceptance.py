#!/usr/bin/env python3
"""Go 에이전트 시험 결과를 수용시험 보고(17번 요구사항 1.1)에 연결한다.

Go 시험 함수 바로 위 주석에 `// covers: T02-a, T02-b`로 덮는 세부 ID를 적는다.
`go test -json` 결과를 읽어 tests/conftest.py와 같은 JSON Lines 형식으로 이어 쓴다.

사용:
    python tools/go_acceptance.py --out var/acceptance-results.jsonl              # agent에서 go test 실행
    python tools/go_acceptance.py --input go-test.json --out var/acceptance-results.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent"
COVERS = re.compile(r"^\s*//\s*covers:\s*(.+)$")
FUNC = re.compile(r"^func (Test\w+)\(")


def covers_map(agent: Path = AGENT) -> dict[tuple[str, str], list[str]]:
    """(패키지 상대 경로, 시험 함수 이름) → 세부 ID 목록."""
    out: dict[tuple[str, str], list[str]] = {}
    for f in sorted(agent.rglob("*_test.go")):
        pending: list[str] = []
        for line in f.read_text(encoding="utf-8").splitlines():
            m = COVERS.match(line)
            if m:
                pending = [x.strip() for x in m.group(1).split(",") if x.strip()]
                continue
            fm = FUNC.match(line)
            if fm and pending:
                out[(f.parent.relative_to(agent).as_posix(), fm.group(1))] = pending
            if not line.lstrip().startswith("//"):
                pending = []
    return out


def run_go(agent: Path = AGENT) -> str:
    go = shutil.which("go")
    if not go:
        sys.exit("go가 없습니다 — Go 시험 결과를 연결하지 못했습니다")
    r = subprocess.run([go, "test", "-json", "./..."], cwd=agent, capture_output=True, text=True,
                       check=False)  # nosec B603
    return r.stdout


def results(events_text: str, cmap: dict[tuple[str, str], list[str]], module: str) -> list[dict]:
    logs: dict[tuple[str, str], list[str]] = {}
    outcome: dict[tuple[str, str], str] = {}
    for raw in events_text.splitlines():
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        test, pkg = e.get("Test"), e.get("Package", "")
        if not test or "/" in test:  # 하위 시험은 상위 결과로 판정
            continue
        rel = pkg[len(module) + 1:] if pkg.startswith(module + "/") else pkg
        key = (rel, test)
        if key not in cmap:
            continue
        act = e.get("Action")
        if act == "output":
            logs.setdefault(key, []).append(e.get("Output", ""))
        elif act in ("pass", "fail", "skip"):
            outcome[key] = {"pass": "passed", "fail": "failed", "skip": "skipped"}[act]
    rows = []
    for key, ids in sorted(cmap.items()):
        rel, test = key
        res = outcome.get(key, "failed")  # 실행되지 않음(컴파일 실패 등)도 실패로 본다
        log = "".join(logs.get(key, []))[-2000:] if res == "failed" else ""
        if key not in outcome:
            log = log or "go test 결과에 이 시험이 없습니다 (컴파일 실패 또는 이름 변경)"
        rows.append({"nodeid": f"agent/{rel}::{test}", "covers": ids, "outcome": res, "log": log,
                     "reproduce": f"cd agent && go test -run '^{test}$' ./{rel}"})
    return rows


def module_name(agent: Path = AGENT) -> str:
    for line in (agent / "go.mod").read_text(encoding="utf-8").splitlines():
        if line.startswith("module "):
            return line.split()[1]
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="go test -json 출력 파일 (없으면 직접 실행)")
    ap.add_argument("--out", default=str(ROOT / "var/acceptance-results.jsonl"))
    a = ap.parse_args()
    text = Path(a.input).read_text(encoding="utf-8") if a.input else run_go()
    rows = results(text, covers_map(), module_name())
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "a", encoding="utf-8") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    bad = [r["nodeid"] for r in rows if r["outcome"] == "failed"]
    print(f"Go 수용 시험 {len(rows)}건 기록, 실패 {len(bad)}건" + (": " + ", ".join(bad) if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
