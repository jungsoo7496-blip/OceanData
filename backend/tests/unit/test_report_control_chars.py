"""10번 요구사항 4.5: 보고서 내보내기는 사람이 쓴 글의 제어 문자·따옴표 때문에 깨지거나 문서 구조를 바꾸지 않는다
(지시서 12번 독립 검토 — ADR-0056).

XML 1.0이 허용하지 않는 문자(NUL·U+0001~0008·0B·0C·0E~1F·U+FFFE·U+FFFF)는 XLSX 쓰기에서 예외(→ 500)가 났고,
HWPX에는 그대로 들어가 한글 프로그램·XML 파서가 읽지 못하는 파일이 200으로 나갔다. 따옴표는 양식의 속성 값 안에서
속성을 끼워 넣을 수 있었다. 시험 데이터는 합성 값이다.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

import pytest
from openpyxl import load_workbook

from app.adapters import report_renderers as rr

ILLEGAL = ["\x00", "\x01", "\x08", "\x0b", "\x0c", "\x0e", "\x1b", "\x1f", "￾", "￿"]
LEGAL_WHITESPACE = "\t\n\r"


def _doc(title: str, value: str, record_text: str) -> dict[str, Any]:
    return {
        "title": title, "status_label": "초안", "version": 1, "generated_at": "2026-11-02 09:00 KST",
        "data_as_of": "2026-11-02 09:00 KST", "damage_note": None, "variables": {},
        "sections": [
            {"kind": "TEXT", "title": "개요", "text": value},
            {"kind": "FIELDS", "title": "입력", "fields": [
                {"key": "summary", "label": "요약", "value": value, "auto_value": value, "edited": True}]},
            {"kind": "METRICS", "title": "지표", "metrics": []},
            {"kind": "RECORDS", "title": "기록", "records": [
                {"record_id": "rec_00000000000000000000000001", "version": 1, "title": record_text,
                 "test_item": record_text, "conditions": record_text, "result": record_text, "evidence_count": 0}]},
        ],
    }


def _texts(data: bytes) -> list[str]:
    wb = load_workbook(io.BytesIO(data))
    return [c.value for ws in wb.worksheets for row in ws.iter_rows() for c in row if isinstance(c.value, str)]


@pytest.mark.parametrize("bad", ILLEGAL, ids=[f"U+{ord(c):04X}" for c in ILLEGAL])
def test_xlsx_export_drops_characters_xml_cannot_hold(bad: str) -> None:
    text = f"앞{bad}뒤"
    texts = _texts(rr.render_xlsx(_doc(text, text, text)))  # 전에는 IllegalCharacterError → 500
    assert "앞뒤" in texts
    assert not any(bad in t for t in texts)


def test_xlsx_export_keeps_tab_newline_and_carriage_return() -> None:
    text = f"가{LEGAL_WHITESPACE}나"
    texts = _texts(rr.render_xlsx(_doc("제목", text, "기록")))
    assert any(t.startswith("가") and t.endswith("나") and "\t" in t for t in texts)


def _template(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("Contents/section0.xml", xml)
    return buf.getvalue()


def _section(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read("Contents/section0.xml").decode("utf-8")


@pytest.mark.parametrize("bad", ILLEGAL, ids=[f"U+{ord(c):04X}" for c in ILLEGAL])
def test_hwpx_export_is_well_formed_xml_even_with_control_characters(bad: str) -> None:
    template = _template('<?xml version="1.0" encoding="UTF-8"?><sec t="{{report.title}}">{{field.summary}}</sec>')
    out = _section(rr.render_hwpx(template, _doc(f"앞{bad}뒤", f"값{bad}끝", "기록")))
    root = ET.fromstring(out)  # 전에는 ExpatError — 한글 프로그램도 열지 못한다
    assert root.attrib["t"] == "앞뒤" and root.text == "값끝"


@pytest.mark.parametrize("quote", ['"', "'"])
def test_hwpx_value_cannot_add_attributes_to_the_template(quote: str) -> None:
    attack = f"x{quote} onmouseover={quote}1"
    template = _template(f'<?xml version="1.0" encoding="UTF-8"?><sec a={quote}{{{{report.title}}}}{quote}/>')
    out = _section(rr.render_hwpx(template, _doc(attack, "값", "기록")))
    root = ET.fromstring(out)
    assert list(root.attrib) == ["a"] and root.attrib["a"] == attack
