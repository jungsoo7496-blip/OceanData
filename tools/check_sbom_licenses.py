#!/usr/bin/env python3
"""SBOM(SPDX JSON, Syft 생성) 라이선스를 허용 목록(tech.md 3장)으로 검사 (17번 요구사항 4.3).

    syft dir:. -o spdx-json > var/sbom.spdx.json && python tools/check_sbom_licenses.py var/sbom.spdx.json

허용 목록·예외는 tools/check_licenses.py와 같다. 개발 검사는 미상·승인 대기 항목을 경고하고,
--strict 릴리스 검사는 미상·미해석 LicenseRef·승인 대기 예외가 있으면 실패한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_licenses import (  # noqa: E402 - 독립 CLI 실행과 테스트 import를 함께 지원
    ALLOWED_STATUS,
    DENIED_STATUS,
    UNKNOWN_STATUS,
    LicenseNode,
    license_status,
    matching_exception,
    node_status,
    normalize_text,
    parse_spdx,
)

ROOT = Path(__file__).resolve().parents[1]


def load_exceptions() -> list[dict]:
    return json.loads((ROOT / "tools/license-exceptions.json").read_text(encoding="utf-8"))["exceptions"]


def allowed(expr: str) -> bool:
    return license_status(expr) == ALLOWED_STATUS


def render(node: LicenseNode) -> str:
    if node.operator == "ID":
        return node.value
    return "(" + f" {node.operator} ".join(render(c) for c in node.children) + ")"


def extracted_licenses(doc: dict) -> dict[str, dict]:
    """같은 LicenseRef에 서로 다른 근거가 있으면 해석하지 않는다."""
    found: dict[str, dict] = {}
    conflicting: set[str] = set()
    for entry in doc.get("hasExtractedLicensingInfos", []):
        ref = entry.get("licenseId")
        if not isinstance(ref, str):
            continue
        if ref in found and found[ref] != entry:
            conflicting.add(ref)
        else:
            found[ref] = entry
    return {ref: entry for ref, entry in found.items() if ref not in conflicting}


def resolve_refs(node: LicenseNode, evidence: dict[str, dict], seen: frozenset[str] = frozenset()) -> LicenseNode:
    """LicenseRef 이름을 잘라 SPDX로 가정하지 않고 추출 원문을 해석한다.

    Syft 1.18.1 실측: pg8000은 'BSD 3-Clause License', s3transfer는 'Apache License 2.0',
    python-dateutil은 'Dual License'를 extractedText로 기록한다. 마지막 것은 선택 근거가 없으므로
    미해석으로 남긴다. name이나 seeAlsos 링크만으로 추가 조건이 붙은 원문을 덮어쓰지 않는다.
    """
    if node.operator != "ID":
        return LicenseNode(node.operator, children=tuple(resolve_refs(c, evidence, seen) for c in node.children))
    ref = node.value
    if "LicenseRef-" not in ref or ref in seen or len(seen) >= 32:
        return node
    text = evidence.get(ref, {}).get("extractedText")
    if not isinstance(text, str) or not text.strip():
        return node
    try:
        resolved = parse_spdx(normalize_text(text) or text)
        return resolve_refs(resolved, evidence, seen | {ref})
    except (ValueError, RecursionError):
        return node


def resolved_expression(expr: str, evidence: dict[str, dict]) -> tuple[str, str]:
    try:
        node = resolve_refs(parse_spdx(expr), evidence)
        return render(node), node_status(node)
    except (ValueError, RecursionError):
        return expr or "UNKNOWN", UNKNOWN_STATUS


APP_TYPES = ("pkg:pypi/", "pkg:npm/", "pkg:golang/")  # 운영체제 패키지(deb 등)는 배포판 라이선스 정책으로 따로 본다


def purl(p: dict) -> str:
    return next((r.get("referenceLocator", "") for r in p.get("externalRefs", [])
                 if r.get("referenceType") == "purl"), "")


def main(path: str, *, strict: bool = False) -> int:
    doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    exceptions = load_exceptions()
    evidence = extracted_licenses(doc)
    bad, unknown, pending = [], [], []
    checked = 0
    for p in doc.get("packages", []):
        ref = purl(p)
        if not ref.startswith(APP_TYPES):
            continue
        checked += 1
        kind, locator = ref[4:].split("/", 1)
        # 예외를 표시 이름으로 우회하지 않도록 PURL의 실제 패키지 이름을 쓴다.
        name = unquote(locator.split("?", 1)[0].split("#", 1)[0].rsplit("@", 1)[0])
        lic = p.get("licenseConcluded") if p.get("licenseConcluded") not in (None, "", "NOASSERTION") \
            else p.get("licenseDeclared")
        expr, status = resolved_expression(lic if isinstance(lic, str) else "UNKNOWN", evidence)
        if status == ALLOWED_STATUS:
            continue
        entry = matching_exception(name, kind, expr, exceptions)
        detail = f"{name} {p.get('versionInfo', '')}: {expr}"
        if entry:
            if entry.get("approved") is not True:
                pending.append(detail)
        elif status == DENIED_STATUS:
            bad.append(detail)
        else:
            unknown.append(detail)
    for u in sorted(set(unknown))[:50]:
        print(f"경고: 라이선스 정보 없음·식 해석 불가 — {u}")
    for item in sorted(set(pending)):
        print(f"경고: 라이선스 예외 승인 대기 — {item} (ADR 확인 필요)")
    for b in sorted(bad):
        print(f"허용 목록 밖: {b}")
    if not checked:
        print("경고: 검사할 pypi·npm·golang 패키지가 없습니다")
    print(f"SBOM 패키지 {len(doc.get('packages', []))}개, 검사 {checked}개, 목록 밖 {len(bad)}개, "
          f"정보 없음 {len(set(unknown))}개, 승인 대기 {len(set(pending))}개")
    if strict and (unknown or pending or not checked):
        print("릴리스 차단: 미상·미해석 라이선스 또는 미승인 예외가 있습니다")
    return 1 if bad or (strict and (unknown or pending or not checked)) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="var/sbom.spdx.json")
    parser.add_argument("--strict", action="store_true", help="릴리스용: 승인 대기 예외·미상 라이선스도 실패")
    args = parser.parse_args()
    raise SystemExit(main(args.path, strict=args.strict))
