"""ReportRendererPort 어댑터 (10번 요구사항 4.5, ADR-0018): PDF(WeasyPrint)·XLSX(openpyxl)·HWPX(양식 XML 치환).

입력은 서비스가 만든 '렌더링 모델'(dict) 하나다. 모든 형식에 초안 표시·생성 시각·데이터 기준 시각을 넣고,
'검증 필요' 지표는 숫자 대신 '검증 필요'로 쓴다 (확정 성능값으로 내보내지 않음, 요구사항 3.4).
"""

from __future__ import annotations

import codecs
import functools
import html
import io
import logging
import lzma
import re
import threading
import unicodedata
import zipfile
import zlib
from collections.abc import Generator, Iterator
from contextlib import closing, contextmanager
from typing import Any
from xml.sax.saxutils import escape as xml_escape

log = logging.getLogger(__name__)

STATUS_LABEL = {"DRAFT": "초안", "APPROVED": "승인본", "SUBMITTED": "제출본", "SUPERSEDED": "대체됨"}
METRIC_STATUS = {"OK": "확정", "NO_DATA": "데이터 없음", "NEEDS_VERIFICATION": "검증 필요"}
MIME = {"PDF": "application/pdf", "XLSX": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "HWPX": "application/hwp+zip"}
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")
# XML 1.0이 글자로 허용하지 않는 코드 포인트(NUL·탭/줄바꿈/CR을 뺀 C0 제어 문자·U+FFFE·U+FFFF·낱개 서로게이트).
# XLSX(openpyxl)는 이런 문자가 있으면 예외를 내고, HWPX(양식 XML 치환)는 파서가 못 읽는 파일이 된다 (ADR-0056).
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_ATTR_ENTITIES = {'"': "&quot;", "'": "&apos;"}  # 양식의 속성 값 안에 값이 들어가도 속성을 끼워 넣지 못하게


def xml_safe(text: str) -> str:
    return _XML_ILLEGAL.sub("", text)


def _clean(node: Any) -> Any:
    """렌더링 모델의 모든 문자열에서 XML이 담을 수 없는 문자를 뺀다 (보이지 않는 제어 문자라 내용은 달라지지 않는다)."""
    if isinstance(node, str):
        return xml_safe(node)
    if isinstance(node, dict):
        return {k: _clean(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_clean(v) for v in node]
    return node


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


@functools.cache
def _no_fetch() -> Any:
    """어떤 주소도 가져오지 않는 WeasyPrint URL fetcher (ADR-0066).

    WeasyPrint 기본 fetcher는 이미지·스타일·글꼴 주소를 http·https·file·data 가리지 않고 가져온다. 보고서 HTML은 고정
    스타일과 이스케이프한 글뿐이라 가져올 것이 없다 — 글꼴은 이미지에 설치한 나눔 글꼴을 fontconfig로 찾는다
    (ADR-0018). 그래도 이스케이프가 뚫리는 경우를 대비해 렌더러가 네트워크·파일을 건드리지 못하게 한다.
    WeasyPrint 70은 fetcher가 URLFetcher처럼 동작하기를 기대하므로(실패 처리에서 속성을 읽는다) 상속해 fetch만
    바꾼다. 상태가 없어 렌더끼리 한 개를 같이 쓴다."""
    from weasyprint.urls import URLFetcher, URLFetchingError

    class NoFetch(URLFetcher):  # type: ignore[misc]
        def fetch(self, url: str, headers: Any = None) -> Any:
            log.warning("보고서 PDF 렌더가 외부 자원을 가져오려 해 막았습니다 (%s:)", url.split(":", 1)[0][:16])
            raise URLFetchingError("보고서 PDF는 외부 자원을 가져오지 않습니다")

    return NoFetch()


def html_to_pdf(text: str) -> bytes:
    from weasyprint import HTML

    return HTML(string=text, url_fetcher=_no_fetch()).write_pdf()  # type: ignore[no-any-return]


def render_pdf(doc: dict[str, Any]) -> bytes:
    return html_to_pdf(to_html(doc))


PDF_RETRY_AFTER_S = 5  # 차례가 없을 때 Retry-After. 보통 보고서는 1초 안팎, 합성 최악 기록은 10초대 (ADR-0066)


class RenderSlots:
    """프로세스마다 동시에 만드는 PDF 수 (ADR-0066).

    PDF 렌더는 동기 엔드포인트 안에서 CPU를 오래 쓴다(계약 한계 길이의 기록 한 건이 10초대). 그동안 요청은 스레드와
    DB 차례(ADR-0062)를 쥐고 있어, 렌더가 몰리면 다른 요청이 차례를 못 받아 503이 된다. 차례는 기다리지 않는다 —
    기다리면 기다리는 요청도 스레드와 DB 차례를 쥔다. 차례가 없으면 호출자가 429 RATE_LIMITED로 답한다."""

    def __init__(self, total: int) -> None:
        self.total = total
        self._sem = threading.BoundedSemaphore(total)

    @contextmanager
    def try_hold(self) -> Iterator[bool]:
        got = self._sem.acquire(blocking=False)
        try:
            yield got
        finally:
            if got:
                self._sem.release()


# ------------------------------------------------------------------ XLSX


def render_xlsx(doc: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    doc = _clean(doc)
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
    _neutralize_formulas(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# 수식으로 해석될 수 있는 첫 글자 (OWASP CSV/수식 주입 안내). 보고서 제목·입력 항목·회차 기록은 사람·장비가 쓴 글이라
# openpyxl이 '='로 시작하는 문자열을 수식 셀로 저장하면 스프레드시트에서 계산된다 (HYPERLINK·DDE 등).
FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _neutralize_formulas(wb: Any) -> None:
    """위험한 첫 글자로 시작하는 문자열은 값을 바꾸지 않고 문자열 셀로 고정하고 quotePrefix를 켠다
    (열어도 계산하지 않고, 셀을 고쳐 써도 수식이 되지 않는다)."""
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith(FORMULA_LEAD):
                    cell.data_type = "s"
                    cell.quotePrefix = True


# ------------------------------------------------------------------ HWPX (양식 XML 치환)


class HwpxTemplateError(Exception):
    """렌더러가 쓸 수 없는 HWPX 양식 (올릴 때 400, 검사 전에 저장된 양식이면 내보내기 409, ADR-0066)."""


HWPX_MAX_UNPACKED = 100 * 1024 * 1024
# 망가진 zip을 읽을 때 zipfile·압축 해제기가 내는 예외 (헤더·CRC·이름 불일치, 암호화, 모르는 압축 방식·zip 버전,
# 잘린 압축 자료, UTF-8 표시가 있지만 UTF-8이 아닌 이름 등). 무작위로 망가뜨린 양식으로 확인했다.
_ZIP_ERRORS = (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, ValueError, zlib.error,
               lzma.LZMAError)
# 항목을 한 번에 읽는 양. 이 이미지의 Python(3.12) zipfile은 한 번 읽을 때 푸는 양도 요청한 크기로 묶으므로(deflate·
# bzip2·lzma), 헤더에 적은 크기보다 훨씬 길게 풀리는 압축 자료(압축 폭탄)도 조각 하나 넘게 메모리에 올리지 않는다.
# 헤더에 적은 크기를 넘는 부분은 zipfile이 버린다 (ADR-0066)
_READ_CHUNK = 256 * 1024
# XML 선언은 문서 맨 앞의 짧은 줄이다. 인코딩 선언은 앞 4KiB 안에서만 찾는다 (검사가 항목 전체를 글로 들고 있지 않게)
_XML_HEAD = 4096
_XML_ENCODING = re.compile(r"""\A\ufeff?<\?xml[^>]*?\bencoding\s*=\s*["']([^"']*)["']""")
# 항목 이름에 넣을 수 없는 글자의 유니코드 범주: 제어 문자 Cc(C0·DEL·C1), 보이지 않는 서식 문자 Cf(방향 제어
# U+200E·U+200F·U+202A~U+202E·U+2066~U+2069, 폭 없는 문자 U+200B~U+200D, BOM 등), 줄·문단 구분 Zl·Zp(U+2028·U+2029).
# 계약이 다른 곳(ClipRequest.reason)에서 '제어 문자·보이지 않는 서식 문자'로 정한 글자를 모두 포함한다
_NAME_FORBIDDEN = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _bad_entry_name(name: str) -> bool:
    """zip 항목 이름이 패키지 밖을 가리키거나, OS마다 다르게 풀리거나, 보이는 것과 다른 이름이면 참.

    ZIP 형식의 구분자는 '/'뿐이다. 역슬래시는 Windows에서 구분자라 '..\\'·'C:\\'·UNC('\\\\서버')가 되고,
    ':'는 드라이브 문자('C:x')나 NTFS 스트림('a.xml:s')이 된다. 그래서 POSIX 형태('/'로 시작·'..' 단계)와 함께
    역슬래시·':'가 든 이름을 거부한다. 제어 문자와 보이지 않는 서식 문자(방향 제어로 'evil‹RLO›lmx.png'가
    'evilgnp.xml'처럼 보이는 등)도 거부한다. 일반 항목 이름('Contents/section0.xml', 'BinData/그림 1.png' 등)은
    해당이 없다."""
    return (not name or name.startswith("/") or "\\" in name or ":" in name or ".." in name.split("/")
            or any(unicodedata.category(ch) in _NAME_FORBIDDEN for ch in name))


def _is_xml(info: zipfile.ZipInfo) -> bool:
    return info.filename.lower().endswith(".xml")


def _entry_chunks(z: zipfile.ZipFile, info: zipfile.ZipInfo) -> Generator[bytes, None, None]:
    """항목 하나를 _READ_CHUNK씩 읽는다. 읽을 수 없으면(손상·암호화·지원하지 않는 압축) HwpxTemplateError."""
    try:
        with z.open(info) as fp:
            while chunk := fp.read(_READ_CHUNK):
                yield chunk
    except _ZIP_ERRORS:
        raise HwpxTemplateError(
            f"zip 항목을 읽을 수 없습니다(손상·암호화·지원하지 않는 압축): {info.filename[:120]}") from None


def _read_entry(z: zipfile.ZipFile, info: zipfile.ZipInfo, keep: bool) -> bytes | str:
    """항목 하나를 끝까지 읽어 확인한다. XML 항목은 읽으면서 조각마다 엄격한 UTF-8인지 보고(글은 만들지 않고 버린다),
    앞부분의 인코딩 선언을 본다. keep이면 내용(XML은 UTF-8로 푼 글, 나머지는 바이트)을, 아니면 빈 값을 돌려준다."""
    name = info.filename[:120]
    utf8 = codecs.getincrementaldecoder("utf-8")("strict") if _is_xml(info) else None
    head = b""
    parts: list[bytes] = []
    try:
        with closing(_entry_chunks(z, info)) as chunks:
            for chunk in chunks:
                if keep:
                    parts.append(chunk)
                if utf8 is not None:
                    utf8.decode(chunk)
                    if len(head) < _XML_HEAD:
                        head += chunk[:_XML_HEAD - len(head)]
        if utf8 is not None:
            utf8.decode(b"", final=True)  # 끝이 잘린 글자
    except UnicodeDecodeError:  # zipfile 쪽 오류는 _entry_chunks가 이미 HwpxTemplateError로 바꿨다
        raise HwpxTemplateError(f"XML 항목이 UTF-8이 아닙니다: {name}") from None
    raw = b"".join(parts)
    parts.clear()
    if utf8 is None:
        return raw
    # head는 UTF-8 글의 앞부분이라 끝에 잘린 글자 하나만 있을 수 있다
    declared = _XML_ENCODING.match(head.decode("utf-8", "ignore"))
    if declared and declared.group(1).strip().lower() not in ("utf-8", "utf8"):
        # 내보내기는 UTF-8로 쓴다. 다른 인코딩을 선언한 문서에 UTF-8 한글을 넣으면 읽는 쪽이 글자를 깨뜨린다
        raise HwpxTemplateError(f"XML 항목의 인코딩 선언이 UTF-8이 아닙니다: {name}")
    return raw.decode("utf-8")


def _template_entries(data: bytes, *, keep: bool) -> Iterator[tuple[zipfile.ZipInfo, bytes | str]]:
    """양식의 항목을 순서대로 읽는다. keep이면 XML 항목은 UTF-8로 푼 글(str), 나머지는 바이트다.

    올릴 때 검사(check_hwpx)와 내보내기(render_hwpx)가 이 함수 하나로 양식을 읽는다 — 검사를 통과한 양식은 내보낼 때도
    같은 길로 열린다. 검사는 keep=False로 읽은 조각을 바로 버려, 올리기 한 번이 양식을 푼 크기만큼 메모리를 쓰지
    않는다(조각 몇 개 크기). 쓸 수 없는 양식이면 HwpxTemplateError (내부 구조는 해석하지 않는다, ADR-0018)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except _ZIP_ERRORS:
        raise HwpxTemplateError("HWPX(zip) 파일이 아닙니다") from None
    with z:
        infos = z.infolist()
        if any(_bad_entry_name(i.filename) for i in infos):
            raise HwpxTemplateError("zip 항목 경로가 올바르지 않습니다")
        if not any(_is_xml(i) for i in infos):
            raise HwpxTemplateError("XML 항목이 없습니다")
        if sum(i.file_size for i in infos) > HWPX_MAX_UNPACKED:
            raise HwpxTemplateError("압축을 푼 크기가 너무 큽니다")
        for info in infos:
            yield info, _read_entry(z, info, keep)


def check_hwpx(data: bytes) -> list[str]:
    """올린 양식을 렌더러가 쓸 수 있는지 본다: zip 항목 이름, 모든 항목 읽기, XML 항목의 UTF-8 (ADR-0018·0066)."""
    try:
        for _ in _template_entries(data, keep=False):
            pass
    except HwpxTemplateError as exc:
        return [str(exc)]
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


class HwpxPlaceholderMissing(Exception):
    """꼭 넣어야 할 값의 자리표시자가 양식의 XML 항목에 없다 (초안의 {{report.status}}, ADR-0080)."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(", ".join(names))
        self.names = names


def render_hwpx(template: bytes, doc: dict[str, Any], *, required: tuple[str, ...] = ()) -> bytes:
    """양식의 XML 항목에서 {{변수}}를 바꾼다. 쓸 수 없는 양식이면 HwpxTemplateError (check_hwpx와 같은 기준).

    required의 변수가 바꾼 자리 중에 하나도 없으면 HwpxPlaceholderMissing — 렌더러가 실제로 바꾸는 자리(XML 항목의
    PLACEHOLDER)만 센다. 여러 텍스트 조각으로 나뉜 자리표시자는 없는 것으로 본다 (양식 구조는 해석하지 않는다)."""
    values = hwpx_values(doc)
    seen: set[str] = set()

    def sub(m: re.Match[str]) -> str:
        seen.add(m.group(1))
        return xml_escape(xml_safe(values.get(m.group(1), m.group(0))), _ATTR_ENTITIES)

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        # 순서·압축 방식을 그대로 둔다 (첫 항목이 무압축이어야 하는 패키지 형식 대비)
        for info, content in _template_entries(template, keep=True):
            data = PLACEHOLDER.sub(sub, content).encode("utf-8") if isinstance(content, str) else content
            dst.writestr(info, data, compress_type=info.compress_type)
    missing = [name for name in required if name not in seen]
    if missing:
        raise HwpxPlaceholderMissing(missing)
    return out.getvalue()
