#!/usr/bin/env python3
"""SBOM(SPDX JSON, Syft 생성) 라이선스를 허용 목록(tech.md 3장)으로 검사 (17번 요구사항 4.3).

    syft dir:. -o spdx-json > var/sbom.spdx.json && python tools/check_sbom_licenses.py var/sbom.spdx.json

허용 목록·예외는 tools/check_licenses.py와 같다. 라이선스 정보가 없는 패키지는 경고로 따로 보여 준다.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_licenses import ALLOWED

ROOT = Path(__file__).resolve().parents[1]


def load_exceptions() -> list[dict]:
    return json.loads((ROOT / "tools/license-exceptions.json").read_text(encoding="utf-8"))["exceptions"]


TOKEN = re.compile(r"[A-Za-z0-9.+-]+")


def allowed(expr: str) -> bool:
    """SPDX 식: OR는 하나라도 허용이면, AND는 모두 허용이어야."""
    if not expr or expr in ("NOASSERTION", "NONE"):
        return False
    for alt in re.split(r"\s+OR\s+", expr.replace("(", " ").replace(")", " ")):
        parts = [p for p in re.split(r"\s+AND\s+", alt.strip()) if p]
        if parts and all(p.strip() in ALLOWED for p in parts):
            return True
    return False


APP_TYPES = ("pkg:pypi/", "pkg:npm/", "pkg:golang/")  # 운영체제 패키지(deb 등)는 배포판 라이선스 정책으로 따로 본다


def purl(p: dict) -> str:
    return next((r.get("referenceLocator", "") for r in p.get("externalRefs", [])
                 if r.get("referenceType") == "purl"), "")


def main(path: str) -> int:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    exceptions = {e["package"].lower() for e in load_exceptions()}
    bad, unknown = [], []
    for p in doc.get("packages", []):
        if not purl(p).startswith(APP_TYPES):
            continue
        name = (p.get("name") or "").lower()
        lic = p.get("licenseConcluded") if p.get("licenseConcluded") not in (None, "NOASSERTION") \
            else p.get("licenseDeclared")
        if name in exceptions:
            continue
        if not lic or lic in ("NOASSERTION", "NONE"):
            unknown.append(name)
        elif not allowed(lic):
            bad.append(f"{name} {p.get('versionInfo', '')}: {lic}")
    for u in sorted(set(unknown))[:50]:
        print(f"경고: 라이선스 정보 없음 — {u}")
    for b in sorted(bad):
        print(f"허용 목록 밖: {b}")
    print(f"SBOM 패키지 {len(doc.get('packages', []))}개, 목록 밖 {len(bad)}개, 정보 없음 {len(set(unknown))}개")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "var/sbom.spdx.json"))
