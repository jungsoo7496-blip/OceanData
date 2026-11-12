#!/usr/bin/env python3
"""17번: 모든 수용 시험 결과를 남기고 실행기 실패도 종료 코드에 반영한다."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    out = ROOT / "var/acceptance-results.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("", encoding="utf-8")
    env = {**os.environ, "ACCEPTANCE_RESULTS": str(out)}
    commands = [
        ([sys.executable, "-m", "pytest", "-q", "-m", "acceptance or covers"], ROOT / "backend"),
        ([sys.executable, "tools/go_acceptance.py", "--out", str(out)], ROOT),
        ([sys.executable, "tools/web_acceptance.py", "--out", str(out)], ROOT),
    ]
    failed = not bool(os.environ.get("TEST_DATABASE_URL"))
    if failed:
        print("TEST_DATABASE_URL이 없어 통합 수용시험을 검증할 수 없습니다", file=sys.stderr)
    for args, cwd in commands:
        try:
            r = subprocess.run(args, cwd=cwd, env=env, check=False)  # noqa: S603  # nosec B603
            failed |= r.returncode != 0
        except OSError:
            failed = True
            print("수용시험 실행 도구가 없습니다: " + args[0], file=sys.stderr)
    # 앞 시험이 실패해도 세부 ID별 결과는 저장한다.
    report = subprocess.run([sys.executable, "tools/acceptance_report.py"], cwd=ROOT,
                            check=False)  # nosec B603
    return 1 if failed or report.returncode else 0


if __name__ == "__main__":
    raise SystemExit(main())
