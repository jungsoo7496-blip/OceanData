#!/usr/bin/env python3
"""라이선스 허용 목록 검사 (01번 요구사항 9.3, tech.md 3장).

배포물에 들어가는 의존성만 본다:
- Python: --python으로 지정한 환경(런타임 의존성만 설치한 가상환경)의 패키지 메타데이터
- 웹: web/package-lock.json에서 dev가 아닌 패키지
허용 목록 밖이면 패키지 이름과 라이선스를 출력하고 1로 끝난다. 예외는 tools/license-exceptions.json(ADR 필수).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404 - 지정한 파이썬으로 메타데이터만 읽는다
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {"MIT", "MIT-0", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "Apache-2.0", "ISC", "MPL-2.0", "PostgreSQL",
           "Zlib", "EDL-1.0", "OFL-1.1"}
CLASSIFIER_MAP = {
    "MIT License": "MIT", "BSD License": "BSD-3-Clause", "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC", "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0", "zlib/libpng License": "Zlib",
}
TEXT_MAP = [
    (r"\bapache(\s+software)?(\s+license)?,?\s*(version\s*)?2(\.0)?\b|^apache-2\.0$|^apache 2(\.0)?$", "Apache-2.0"),
    (r"^mit( license)?$|^expat$", "MIT"),
    (r"^bsd-3-clause$|^bsd 3-clause|^new bsd|^modified bsd|^3-clause bsd|^bsd$|^bsd license$", "BSD-3-Clause"),
    (r"^bsd-2-clause$|^bsd 2-clause|^simplified bsd", "BSD-2-Clause"),
    (r"^isc( license)?$", "ISC"),
    (r"^mpl[- ]?2\.0$|mozilla public license 2\.0", "MPL-2.0"),
    (r"^psf|python software foundation", "PSF-2.0"),
]

_DUMP = r"""
import json, importlib.metadata as m
out = []
for d in m.distributions():
    md = d.metadata
    out.append({"name": md["Name"], "version": md["Version"], "expr": md.get("License-Expression"),
                "license": (md.get("License") or "")[:200],
                "classifiers": [c for c in (md.get_all("Classifier") or []) if c.startswith("License ::")]})
print(json.dumps(out))
"""


def normalize_text(text: str) -> str | None:
    t = text.strip().lower()
    for pattern, spdx in TEXT_MAP:
        if re.search(pattern, t):
            return spdx
    return None


def spdx_ids(expr: str) -> list[list[str]]:
    """'A OR (B AND C)' → [[A], [B, C]] (간단한 SPDX 식만)."""
    expr = expr.replace("(", " ").replace(")", " ")
    return [[t.strip() for t in alt.split(" AND ")] for alt in re.split(r"\s+OR\s+", expr)]


def classify_python(pkg: dict) -> tuple[bool, str]:
    if pkg.get("expr"):
        alts = spdx_ids(pkg["expr"])
        ok = any(all(x in ALLOWED for x in alt) for alt in alts)
        return ok, pkg["expr"]
    found = {CLASSIFIER_MAP.get(c.split(" :: ")[-1]) for c in pkg["classifiers"]} - {None}
    if found:
        return any(f in ALLOWED for f in found), " / ".join(sorted(x for x in found if x))
    norm = normalize_text(pkg.get("license") or "")
    if norm:
        return norm in ALLOWED, norm
    return False, (pkg.get("license") or "UNKNOWN")[:60] or "UNKNOWN"


def python_packages(python: str) -> list[dict]:
    out = subprocess.run([python, "-c", _DUMP], check=True, capture_output=True, text=True).stdout  # nosec B603
    skip = {"pip", "setuptools", "wheel", "marine-platform"}
    return [p for p in json.loads(out) if p["name"] and p["name"].lower() not in skip]


def web_packages(lock: Path) -> list[dict]:
    data = json.loads(lock.read_text(encoding="utf-8"))
    out = []
    for path, meta in data.get("packages", {}).items():
        if not path or meta.get("dev") or meta.get("devOptional"):
            continue
        out.append({"name": path.split("node_modules/")[-1], "version": meta.get("version"),
                    "expr": meta.get("license") or "UNKNOWN"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable, help="런타임 의존성만 설치한 환경의 python")
    ap.add_argument("--web-lock", default=str(ROOT / "web" / "package-lock.json"))
    args = ap.parse_args()
    exceptions = json.loads((ROOT / "tools" / "license-exceptions.json").read_text(encoding="utf-8"))
    allowed_exc = {e["package"].lower(): e for e in exceptions["exceptions"]}
    bad: list[str] = []
    checked = 0
    for pkg in python_packages(args.python):
        checked += 1
        ok, lic = classify_python(pkg)
        if not ok and pkg["name"].lower() not in allowed_exc:
            bad.append(f"python  {pkg['name']} {pkg['version']}: {lic}")
    lock = Path(args.web_lock)
    if lock.exists():
        for pkg in web_packages(lock):
            checked += 1
            ok = any(all(x in ALLOWED for x in alt) for alt in spdx_ids(pkg["expr"]))
            if not ok and pkg["name"].lower() not in allowed_exc:
                bad.append(f"npm     {pkg['name']} {pkg['version']}: {pkg['expr']}")
    pending = [e["package"] for e in exceptions["exceptions"] if not e.get("approved")]
    if pending:
        print("경고: 승인 대기 중인 라이선스 예외 — " + ", ".join(pending) + " (ADR 확인 필요)", file=sys.stderr)
    if bad:
        print("라이선스 허용 목록(tech.md 3장) 밖의 의존성:", file=sys.stderr)
        for line in bad:
            print("  " + line, file=sys.stderr)
        print("허용하려면 ADR을 쓰고 tools/license-exceptions.json에 사유와 함께 추가한다.", file=sys.stderr)
        return 1
    print(f"라이선스 검사 통과 ({checked}개 패키지, 예외 {len(allowed_exc)}건)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
