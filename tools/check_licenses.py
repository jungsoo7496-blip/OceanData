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
from dataclasses import dataclass
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
    (r"apache( software)?( license)?,?\s*(version )?2(\.0)?|apache-2\.0", "Apache-2.0"),
    (r"^mit( license)?$|^expat$", "MIT"),
    (r"bsd[- ]3-clause( license)?|new bsd( license)?|modified bsd( license)?|3-clause bsd( license)?", "BSD-3-Clause"),
    (r"bsd[- ]2-clause( license)?|simplified bsd( license)?", "BSD-2-Clause"),
    (r"^isc( license)?$", "ISC"),
    (r"mpl[- ]?2\.0|mozilla public license 2\.0", "MPL-2.0"),
    (r"psf[- ]2\.0|python software foundation( license)?", "PSF-2.0"),
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
    """명확한 별칭만 해석한다. 뒤에 붙은 별도 조건·변형을 버리지 않는다."""
    t = " ".join(text.strip().lower().split())
    canonical = {x.lower(): x for x in ALLOWED}
    if t in canonical:
        return canonical[t]
    for pattern, spdx in TEXT_MAP:
        if re.fullmatch(pattern, t):
            return spdx
    return None


def package_name(name: str, ecosystem: str = "pypi") -> str:
    """PyPI 이름의 -, _, .는 같은 이름이다 (PyPA 이름 정규화 규칙)."""
    return re.sub(r"[-_.]+", "-", name).lower() if ecosystem == "pypi" else name.lower()


@dataclass(frozen=True)
class LicenseNode:
    operator: str
    value: str = ""
    children: tuple[LicenseNode, ...] = ()


_TOKEN = re.compile(r"(?:DocumentRef-[A-Za-z0-9.-]+:)?LicenseRef-[A-Za-z0-9.-]+|[A-Za-z0-9][A-Za-z0-9.-]*\+?|[()]")
_OPERATORS = {"AND", "OR", "WITH"}
ALLOWED_STATUS, DENIED_STATUS, UNKNOWN_STATUS = "allowed", "denied", "unknown"


def parse_spdx(expr: str) -> LicenseNode:
    """SPDX Annex D: WITH > AND > OR, 괄호 보존. 알 수 없는 문법은 거부한다.

    설치 없이 실행하는 CI 도구이므로 외부 파서 의존성을 추가하지 않는다.
    WITH를 기본 라이선스와 분리해 버리면 예외의 추가 조건을 놓치므로 하나의 조건으로 둔다.
    """
    if not isinstance(expr, str) or not expr.strip() or len(expr) > 4096:
        raise ValueError("빈 값이거나 너무 긴 SPDX 식")
    tokens, end = [], 0
    for match in _TOKEN.finditer(expr):
        if expr[end:match.start()].strip():
            raise ValueError("지원하지 않는 SPDX 문자")
        tokens.append(match.group())
        end = match.end()
    if expr[end:].strip() or not tokens or len(tokens) > 1024:
        raise ValueError("지원하지 않는 SPDX 식")
    pos = 0

    def take(expected: str | None = None) -> str:
        nonlocal pos
        if pos == len(tokens) or (expected is not None and tokens[pos] != expected):
            raise ValueError("SPDX 피연산자 또는 괄호 누락")
        value = tokens[pos]
        pos += 1
        return value

    def primary(depth: int) -> LicenseNode:
        if depth > 32:
            raise ValueError("SPDX 괄호 중첩 한도 초과")
        value = take()
        if value == "(":
            node = disjunction(depth + 1)
            take(")")
            return node
        if value in _OPERATORS or value == ")":
            raise ValueError("SPDX 라이선스 식별자 누락")
        node = LicenseNode("ID", value)
        if pos < len(tokens) and tokens[pos] == "WITH":
            take("WITH")
            exception = take()
            if exception in _OPERATORS or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", exception):
                raise ValueError("SPDX 예외 식별자 오류")
            node = LicenseNode("WITH", children=(node, LicenseNode("ID", exception)))
        return node

    def conjunction(depth: int) -> LicenseNode:
        node = primary(depth)
        while pos < len(tokens) and tokens[pos] == "AND":
            take("AND")
            node = LicenseNode("AND", children=(node, primary(depth)))
        return node

    def disjunction(depth: int) -> LicenseNode:
        node = conjunction(depth)
        while pos < len(tokens) and tokens[pos] == "OR":
            take("OR")
            node = LicenseNode("OR", children=(node, conjunction(depth)))
        return node

    result = disjunction(0)
    if pos != len(tokens):
        raise ValueError("SPDX 식 뒤에 해석하지 못한 항목이 있음")
    return result


def node_status(node: LicenseNode) -> str:
    if node.operator == "ID":
        value = node.value.lower()
        if value in {x.lower() for x in ALLOWED}:
            return ALLOWED_STATUS
        if value in {"noassertion", "none", "unknown"} or "licenseref-" in value:
            return UNKNOWN_STATUS
        return DENIED_STATUS
    if node.operator == "WITH":
        return DENIED_STATUS  # 허용 목록에는 예외가 결합된 식이 없다. 기본 라이선스만 허용하지 않는다.
    statuses = [node_status(child) for child in node.children]
    if node.operator == "AND":
        return DENIED_STATUS if DENIED_STATUS in statuses else (
            UNKNOWN_STATUS if UNKNOWN_STATUS in statuses else ALLOWED_STATUS)
    return ALLOWED_STATUS if ALLOWED_STATUS in statuses else (
        UNKNOWN_STATUS if UNKNOWN_STATUS in statuses else DENIED_STATUS)


def license_status(expr: str) -> str:
    try:
        return node_status(parse_spdx(expr))
    except (ValueError, RecursionError):
        return UNKNOWN_STATUS


def expression_key(node: LicenseNode) -> tuple:
    """괄호·우선순위는 보존하고 같은 AND/OR의 순서만 정규화한다."""
    if node.operator == "ID":
        return ("ID", node.value.lower())
    if node.operator == "WITH":
        return ("WITH", *(expression_key(c) for c in node.children))
    children = []
    for child in node.children:
        key = expression_key(child)
        children.extend(key[1:] if key[0] == node.operator else [key])
    return (node.operator, *sorted(children))


def matching_exception(name: str, ecosystem: str, expr: str, exceptions: list[dict]) -> dict | None:
    """같은 패키지라도 실제 라이선스 식이 바뀌면 기존 예외로 통과시키지 않는다."""
    try:
        actual = expression_key(parse_spdx(expr))
        for entry in exceptions:
            kind = entry.get("ecosystem", "pypi")
            if kind != ecosystem or package_name(entry["package"], kind) != package_name(name, ecosystem):
                continue
            if expression_key(parse_spdx(entry["license"])) == actual:
                return entry
    except (ValueError, RecursionError):
        pass
    return None


def pending_unknown(name: str, ecosystem: str, expr: str, exceptions: list[dict]) -> bool:
    """개발 중인 기존 예외의 메타데이터 미상은 경고로 남긴다. 릴리스에서는 사용하지 않는다."""
    return license_status(expr) == UNKNOWN_STATUS and any(
        entry.get("approved") is not True and entry.get("ecosystem", "pypi") == ecosystem
        and package_name(entry["package"], ecosystem) == package_name(name, ecosystem)
        for entry in exceptions)


def classify_python(pkg: dict) -> tuple[bool, str]:
    if pkg.get("expr"):
        return license_status(pkg["expr"]) == ALLOWED_STATUS, pkg["expr"]
    classifiers = {c.split(" :: ")[-1] for c in pkg.get("classifiers", [])} - {"OSI Approved"}
    if classifiers - CLASSIFIER_MAP.keys():
        return False, "UNKNOWN (라이선스 분류자 확인 필요)"
    norm = normalize_text(pkg.get("license") or "")
    found = {CLASSIFIER_MAP[c] for c in classifiers}
    if norm:
        found.add(norm)
    if found:
        expr = " AND ".join(sorted(x for x in found if x))
        return license_status(expr) == ALLOWED_STATUS, expr
    return False, (pkg.get("license") or "UNKNOWN")[:60] or "UNKNOWN"


def python_packages(python: str) -> list[dict]:
    # 명시한 런타임 Python으로 고정된 메타데이터 조회 코드만 실행한다.
    out = subprocess.run(  # noqa: S603  # nosec B603
        [python, "-c", _DUMP], check=True, capture_output=True, text=True).stdout
    skip = {"pip", "setuptools", "wheel", "marine-platform"}
    return [p for p in json.loads(out) if p["name"] and package_name(p["name"]) not in skip]


def web_packages(lock: Path) -> list[dict]:
    data = json.loads(lock.read_text(encoding="utf-8"))
    out = []
    for path, meta in data.get("packages", {}).items():
        if not path or meta.get("dev") or meta.get("devOptional"):
            continue
        out.append({"name": path.split("node_modules/")[-1], "version": meta.get("version"),
                    "expr": meta.get("license") or "UNKNOWN"})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable, help="런타임 의존성만 설치한 환경의 python")
    ap.add_argument("--web-lock", default=str(ROOT / "web" / "package-lock.json"))
    ap.add_argument("--strict", action="store_true", help="릴리스용: 승인 대기 예외·미상 라이선스도 실패")
    args = ap.parse_args(argv)
    exceptions = json.loads((ROOT / "tools" / "license-exceptions.json").read_text(encoding="utf-8"))
    entries = exceptions["exceptions"]
    bad: list[str] = []
    checked = 0
    for pkg in python_packages(args.python):
        checked += 1
        ok, lic = classify_python(pkg)
        if not ok:
            entry = matching_exception(pkg["name"], "pypi", lic, entries)
            if entry and (entry.get("approved") is True or not args.strict):
                continue
            if not args.strict and pending_unknown(pkg["name"], "pypi", lic, entries):
                print(f"경고: 예외 승인 대기 패키지의 라이선스 확인 필요 - {pkg['name']}: {lic}", file=sys.stderr)
                continue
            bad.append(f"python  {pkg['name']} {pkg['version']}: {lic}" +
                       (" (예외 승인 대기)" if entry else ""))
    lock = Path(args.web_lock)
    if lock.exists():
        for pkg in web_packages(lock):
            checked += 1
            ok = license_status(pkg["expr"]) == ALLOWED_STATUS
            entry = matching_exception(pkg["name"], "npm", pkg["expr"], entries)
            if not ok and not (entry and (entry.get("approved") is True or not args.strict)):
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
    print(f"라이선스 검사 통과 ({checked}개 패키지, 예외 {len(entries)}건)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
