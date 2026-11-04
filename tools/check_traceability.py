#!/usr/bin/env python3
"""요구사항 추적 검사.

- 기준 문서(docs/baseline-v0.04.md) 10장의 기능 ID가 모두 어느 스펙의 '추적' 절에 연결되어 있는지
- domain.md 10장의 수용시험 ID(T01~T20)가 모두 어느 스펙에 연결되어 있는지
확인하고, --write를 주면 docs/TRACEABILITY.md(요구사항 추적표)를 다시 만든다.

사용: python tools/check_traceability.py [--write]
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = ROOT / "docs" / "baseline-v0.04.md"
DOMAIN = ROOT / ".kiro" / "steering" / "domain.md"
SPECS = ROOT / ".kiro" / "specs"
OUT = ROOT / "docs" / "TRACEABILITY.md"

# 연결하지 않아도 되는 항목과 이유
EXEMPT = {
    "EXT-01": "P2 — 수요·비용 검증 후 별도 스펙",
    "EXT-02": "보류",
    "EXT-03": "제외",
    "EXT-04": "제외 (크롤러 대신 12번 Open API 수집)",
    "SEC-02": "망 구간 분리 — 운영 배포 스펙(하드웨어 후)에서",
    "PRC-09": "라벨 초안 (P1~P2) — 라벨링 도구 연계 후 별도 스펙",
    "T20": "사업 문서 시험 (SW 범위 밖)",
}

baseline = BASELINE.read_text(encoding="utf-8")
sec10 = baseline.split("## 10. 기능 목록")[1].split("\n## ")[0]
functions = re.findall(r"^\| ([A-Z]{2,3}-\d{2}) \| ([^|]+?) \| ([^|]+?) \|", sec10, re.M)

domain = DOMAIN.read_text(encoding="utf-8")
sec_t = domain.split("## 10.")[1]
tests = dict(re.findall(r"(T\d{2}) \| ([^|]+?) \|", sec_t))

func_map: dict[str, list[str]] = {fid: [] for fid, _, _ in functions}
test_map: dict[str, list[str]] = {tid: [] for tid in tests}
spec_rows = []
for spec in sorted(p for p in SPECS.iterdir() if p.is_dir()):
    text = (spec / "requirements.md").read_text(encoding="utf-8")
    trace = text.split("## 추적")[-1] if "## 추적" in text else ""
    fids: list[str] = []
    fline = next((ln for ln in trace.splitlines() if ln.startswith("- 기능 ID:")), "")
    tline = next((ln for ln in trace.splitlines() if ln.startswith("- 수용시험:")), "")
    for prefix, start, end in re.findall(r"\b([A-Z]{2,3})-(\d{2})(?:~(\d{2}))?", fline):
        last = int(end) if end else int(start)
        for n in range(int(start), last + 1):
            fids.append(f"{prefix}-{n:02d}")
    tids = sorted(set(re.findall(r"(?<![A-Za-z0-9])(T\d{2})(?![0-9])", tline)))
    for fid in fids:
        func_map.setdefault(fid, []).append(spec.name)
    for tid in tids:
        test_map.setdefault(tid, []).append(spec.name)
    spec_rows.append((spec.name, sorted(set(fids)), tids))

problems = []
# docs/ACCEPTANCE.md 담당 스펙과 각 스펙 '추적'의 수용시험이 같은지
acc = (ROOT / "docs" / "ACCEPTANCE.md").read_text(encoding="utf-8")
acc_owner: dict[str, set[str]] = {}
for line in acc.splitlines():
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) < 4:
        continue
    mt = re.match(r"\**(T\d{2})", cells[0])
    if not mt:
        continue
    for o in re.findall(r"\b(\d{2})\b", cells[3]):
        acc_owner.setdefault(o, set()).add(mt.group(1))
for name, _fids, tids in spec_rows:
    want = acc_owner.get(name[:2], set())
    if set(tids) != want:
        problems.append(f"{name}: 추적의 수용시험 {sorted(set(tids))} ≠ ACCEPTANCE.md 담당 {sorted(want)}")
unknown = [f for f in func_map if f not in {fid for fid, _, _ in functions}]
for f in unknown:
    problems.append(f"기준 문서에 없는 기능 ID가 스펙에 있음: {f} ({', '.join(func_map[f])})")
for fid, name, prio in functions:
    if not func_map.get(fid) and fid not in EXEMPT:
        problems.append(f"기능 {fid}({name.strip()}, {prio.strip()})를 다루는 스펙이 없음")
for tid in tests:
    if not test_map.get(tid) and tid not in EXEMPT:
        problems.append(f"수용시험 {tid}({tests[tid].strip()})를 다루는 스펙이 없음")

if "--write" in sys.argv:
    lines = [
        "# 요구사항 추적표",
        "",
        "`tools/check_traceability.py --write`가 만든 파일이다. 직접 고치지 말고 스펙의 '추적' 절을 고친 뒤 다시 만든다.",
        "",
        "## 1. 기준 문서 기능 ID → 스펙",
        "| 기능 ID | 기능 | 우선 | 스펙 |",
        "|---|---|---|---|",
    ]
    for fid, name, prio in functions:
        where = ", ".join(sorted(set(func_map.get(fid, [])))) or f"— ({EXEMPT.get(fid, '미연결')})"
        lines.append(f"| {fid} | {name.strip()} | {prio.strip()} | {where} |")
    lines += ["", "## 2. 수용시험 → 스펙", "| ID | 주제 | 스펙 |", "|---|---|---|"]
    for tid, topic in tests.items():
        where = ", ".join(sorted(set(test_map.get(tid, [])))) or f"— ({EXEMPT.get(tid, '미연결')})"
        lines.append(f"| {tid} | {topic.strip()} | {where} |")
    lines += ["", "## 3. 스펙별 연결", "| 스펙 | 기능 ID | 수용시험 |", "|---|---|---|"]
    for name, fids, tids in spec_rows:
        lines.append(f"| {name} | {', '.join(fids) or '—'} | {', '.join(tids) or '—'} |")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)} 작성")

mapped_f = sum(1 for fid, _, _ in functions if func_map.get(fid))
mapped_t = sum(1 for t in tests if test_map.get(t))
print(f"기능 ID {mapped_f}/{len(functions)} 연결, 수용시험 {mapped_t}/{len(tests)} 연결 (예외 {len(EXEMPT)}건)")
if problems:
    print(f"\n문제 {len(problems)}건:")
    for p in problems:
        print("  ✗", p)
    sys.exit(1)
print("추적 검사 통과")
