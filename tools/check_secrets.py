#!/usr/bin/env python3
"""비밀값 패턴 검사 (01번 요구사항 9.4). git이 추적하는 파일만 본다.

CI에서는 gitleaks도 함께 돌린다. 이 스크립트는 개발자가 로컬에서 바로 돌릴 수 있는 최소 검사다.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----"),
    "aws_access_key": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "generic_secret": re.compile(
        r"(?i)\b(secret|password|passwd|api[_-]?key|token)[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+_\-]{24,})"),
}
# 개발용 자리표시자·시험값은 허용한다
ALLOW_VALUE = re.compile(r"(?i)change-me|dev-|test-|example|placeholder|x{8,}")
SKIP_FILES = {"web/package-lock.json"}


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True).stdout  # nosec
    return [f for f in out.decode().split("\0") if f and f not in SKIP_FILES]


def main() -> int:
    findings = []
    for rel in tracked_files():
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pat in PATTERNS.items():
                m = pat.search(line)
                if m and not ALLOW_VALUE.search(m.group(0)):
                    findings.append(f"{rel}:{lineno}: {name}")
    if findings:
        print("비밀값으로 보이는 문자열이 커밋되어 있습니다 (값은 출력하지 않음):", file=sys.stderr)
        for f in findings:
            print("  " + f, file=sys.stderr)
        return 1
    print("비밀값 패턴 검사 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
