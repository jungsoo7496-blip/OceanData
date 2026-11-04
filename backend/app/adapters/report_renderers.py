"""ReportRendererPort 어댑터 (10번 요구사항 4.5, ADR-0018): PDF(WeasyPrint)·XLSX(openpyxl)·HWPX(양식 XML 치환).

입력은 서비스가 만든 '렌더링 모델'(dict) 하나다. 모든 형식에 초안 표시·생성 시각·데이터 기준 시각을 넣고,
'검증 필요' 지표는 숫자 대신 '검증 필요'로 쓴다 (확정 성능값으로 내보내지 않음, 요구사항 3.4).
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from typing import Any
from xml.sax.saxutils import escape as xml_escape

STATUS_LABEL = {"DRAFT": "초안", "APPROVED": "승인본", "SUBMITTED": "제출본", "SUPERSEDED": "대체됨"}
METRIC_STATUS = {"OK": "확정", "NO_DATA": "데이터 없음", "NEEDS_VERIFICATION": "검증 필요"}
MIME = {"PDF": "application/pdf", "XLSX": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "HWPX": "application/hwp+zip"}
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")


def metric_text(m: dict[str, Any]) -> str:
    if m["status"] != "OK" or m.get("value") is None:
        return METRIC_STATUS[m["status"]]
    unit = f" {m['unit']}" if m.get("unit") else ""
    return f"{m['value']:g}{unit}"


def pdf_available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except Exception:  # noqa: BLE001 - 시스템 라이브러리(pango) 없음 등
        return False
    return True


def enabled_formats(configured: list[str]) -> list[str]:
    return [f for f in configured if f in ("XLSX", "HWPX") or (f == "PDF" and pdf_available())]


# ------------------------------------------------------------------ HTML → PDF


def to_html(doc: dict[str, Any]) -> str:
    e = html.escape
    parts = [
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'><style>",
        "@page{size:A4;margin:18mm;@top-right{content:'" + e(doc["status_label"]) + "';color:#a00}}",
        "body{font-family:'NanumGothic','Noto Sans CJK KR',sans-serif;font-size:10pt;line-height:1.5}",
        "h1{font-size:16pt} h2{font-size:12pt;border-bottom:1px solid #999;margin-top:16pt}",
        "table{border-collapse:collapse;width:100%} td,th{border:1px solid #999;padding:3pt;vertical-align:top}",
        ".draft{color:#a00;font-weight:bold} .meta{color:#444;font-size:9pt} .edited{color:#05a}",
        "</style></head><body>",
        f"<h1>{e(doc['title'])}</h1>",
        f"<p class='meta'><span class='draft'>{e(doc['status_label'])}</span> · 버전 {doc['version']} · "
        f"생성 {e(doc['generated_at'])} · 데이터 기준 {e(doc['data_as_of'])}</p>",
    ]
    if doc.get("damage_note"):
        parts.append(f"<p class='draft'>{e(doc['damage_note'])}</p>")
    for s in doc["sections"]:
        parts.append(f"<h2>{e(s['title'])}</h2>")
        if s["kind"] == "TEXT":
            parts.append(f"<p>{e(s['text']).replace(chr(10), '<br>')}</p>")
        elif s["kind"] == "FIELDS":
            parts.append("<table>")
            for f in s["fields"]:
                mark = " <span class='edited'>(수정됨)</span>" if f["edited"] else ""
                parts.append(f"<tr><th>{e(f['label'])}</th><td>{e(f['value'] or '')}{mark}</td></tr>")
            parts.append("</table>")
        elif s["kind"] == "METRICS":
            parts.append("<table><tr><th>지표</th><th>값</th><th>상태</th><th>계산식</th><th>입력·누락</th></tr>")
            for m in s["metrics"]:
                parts.append(f"<tr><td>{e(m['name'])} ({e(m['metric_code'])} v{m['metric_version']})</td>"
                             f"<td>{e(metric_text(m))}</td><td>{e(METRIC_STATUS[m['status']])}</td>"
                             f"<td>{e(m['formula'])}</td><td>{m['input_count']} / {m['missing_count']}</td></tr>")
            parts.append("</table><p class='meta'>원천 목록은 플랫폼의 지표 근거 조회에서 확인합니다.</p>")
        else:  # RECORDS
            parts.append("<table><tr><th>기록</th><th>시험 항목</th><th>결과</th><th>근거 수</th></tr>")
            for r in s["records"]:
                parts.append(f"<tr><td>{e(r['title'])} (v{r['version']})</td><td>{e(r['test_item'] or '')}</td>"
                             f"<td>{e(r['result'] or '')}</td><td>{r['evidence_count']}</td></tr>")
            parts.append("</table>")
    parts.append("</body></html>")
    return "".join(parts)


def render_pdf(doc: dict[str, Any]) -> bytes:
    from weasyprint import HTML

    return HTML(string=to_html(doc)).write_pdf()  # type: ignore[no-any-return]


# ------------------------------------------------------------------ XLSX


def render_xlsx(doc: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "보고서"
    ws.append([doc["title"]])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([doc["status_label"], f"버전 {doc['version']}", f"생성 {doc['generated_at']}",
               f"데이터 기준 {doc['data_as_of']}"])
    ws["A2"].font = Font(bold=True, color="AA0000")
    if doc.get("damage_note"):
        ws.append([doc["damage_note"]])
    ws.append([])
    ws.append(["항목", "값", "자동값", "수정 여부"])
    for s in doc["sections"]:
        if s["kind"] == "TEXT":
            ws.append([s["title"], s["text"]])
        for f in s.get("fields", []):
            ws.append([f["label"], f["value"], f["auto_value"], "수정됨" if f["edited"] else "자동"])
    mws = wb.create_sheet("지표")
    mws.append(["코드", "이름", "값", "단위", "상태", "계산식", "정의 버전", "입력 건수", "누락 건수", "원천 이벤트",
                "원천 자산", "관측 구간"])
    for s in doc["sections"]:
        for m in s.get("metrics", []):
            ok = m["status"] == "OK"
            src = m["sources"]
            mws.append([m["metric_code"], m["name"], m["value"] if ok else METRIC_STATUS[m["status"]],
                        m.get("unit") or "", METRIC_STATUS[m["status"]], m["formula"], m["metric_version"],
                        m["input_count"], m["missing_count"], len(src.get("event_ids", [])),
                        len(src.get("asset_ids", [])), len(src.get("observation_ranges", []))])
    rws = wb.create_sheet("회차 기록")
    rws.append(["기록 ID", "버전", "제목", "시험 항목", "조건", "결과", "근거 수"])
    for s in doc["sections"]:
        for r in s.get("records", []):
            rws.append([r["record_id"], r["version"], r["title"], r["test_item"], r["conditions"], r["result"],
                        r["evidence_count"]])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ HWPX (양식 XML 치환)


def check_hwpx(data: bytes) -> list[str]:
    """올린 양식이 zip이고 XML 항목이 있는지만 본다 (내부 구조는 해석하지 않는다, ADR-0018)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            if any(n.startswith("/") or ".." in n.split("/") for n in names):
                return ["zip 항목 경로가 올바르지 않습니다"]
            if not any(n.lower().endswith(".xml") for n in names):
                return ["XML 항목이 없습니다"]
            if sum(i.file_size for i in z.infolist()) > 100 * 1024 * 1024:
                return ["압축을 푼 크기가 너무 큽니다"]
    except zipfile.BadZipFile:
        return ["HWPX(zip) 파일이 아닙니다"]
    return []


def hwpx_values(doc: dict[str, Any]) -> dict[str, str]:
    values = {f"{k}": str(v) for k, v in doc["variables"].items()}
    values.update({"report.title": doc["title"], "report.status": doc["status_label"],
                   "report.version": str(doc["version"]), "report.generated_at": doc["generated_at"],
                   "report.data_as_of": doc["data_as_of"]})
    for s in doc["sections"]:
        for f in s.get("fields", []):
            values[f"field.{f['key']}"] = f["value"] or ""
        for m in s.get("metrics", []):
            values[f"metric.{m['metric_code']}"] = metric_text(m)
            values[f"metric.{m['metric_code']}.formula"] = m["formula"]
    return values


def render_hwpx(template: bytes, doc: dict[str, Any]) -> bytes:
    values = hwpx_values(doc)

    def sub(m: re.Match[str]) -> str:
        return xml_escape(values.get(m.group(1), m.group(0)))

    src = zipfile.ZipFile(io.BytesIO(template))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():  # 순서·압축 방식을 그대로 둔다 (첫 항목이 무압축이어야 하는 패키지 형식 대비)
            data = src.read(info)
            if info.filename.lower().endswith(".xml"):
                data = PLACEHOLDER.sub(sub, data.decode("utf-8")).encode("utf-8")
            dst.writestr(info, data, compress_type=info.compress_type)
    return out.getvalue()
