"""10번 요구사항 4.5: 보고서 내보내기 보강 (검토 2026-11-12 V7·V8, ADR-0066).

- HWPX 양식: UTF-8이 아니거나 읽을 수 없는 양식(손상·암호화·지원하지 않는 압축)이 올릴 때 검사를 통과하고 내보낼 때
  예외(→ 500)가 났다. zip 항목 이름 검사는 POSIX 형태('/'로 시작·'..')만 막아 역슬래시·드라이브 문자·UNC 이름이 통과했다.
- PDF: WeasyPrint 기본 URL fetcher는 http·file 주소를 가져온다. 지금은 HTML 이스케이프가 사용자 글을 막지만, 그것이
  뚫려도 렌더러는 어떤 주소도 가져오지 않아야 한다.

시험 데이터는 합성 값이고 네트워크는 127.0.0.1만 쓴다.
"""

from __future__ import annotations

import http.server
import io
import random
import struct
import sys
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.adapters import report_renderers as rr

SECTION = '<?xml version="1.0" encoding="UTF-8"?><sec t="{{report.title}}">{{field.summary}}</sec>'


def _doc(title: str = "합성 보고서", value: str = "요약 값") -> dict[str, Any]:
    return {
        "title": title, "status_label": "초안", "version": 1, "generated_at": "2026-11-02 09:00 KST",
        "data_as_of": "2026-11-02 09:00 KST", "damage_note": None, "variables": {},
        "sections": [
            {"kind": "TEXT", "title": "개요", "text": value},
            {"kind": "FIELDS", "title": "입력", "fields": [
                {"key": "summary", "label": "요약", "value": value, "auto_value": value, "edited": False}]},
            {"kind": "METRICS", "title": "지표", "metrics": []},
            {"kind": "RECORDS", "title": "기록", "records": [
                {"record_id": "rec_00000000000000000000000001", "version": 1, "title": "합성 기록",
                 "test_item": "선박 탐지", "conditions": "주간", "result": "탐지 1건", "evidence_count": 0}]},
        ],
    }


def _zip(entries: list[tuple[str, bytes | str]], method: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            z.writestr(zipfile.ZipInfo(name), data, compress_type=method)
    return buf.getvalue()


def _patch_headers(data: bytes, *, flags: int = 0, method: int | None = None, version: int | None = None) -> bytes:
    """모든 로컬·중앙 디렉터리 헤더의 일반 플래그·압축 방식·필요 버전을 바꾼다 (암호화·새 압축 방식 흉내)."""
    b = bytearray(data)
    # (서명, 필요 버전 위치, 플래그 위치, 압축 방식 위치)
    for sig, ver_at, flag_at, method_at in ((b"PK\x03\x04", 4, 6, 8), (b"PK\x01\x02", 6, 8, 10)):
        i = b.find(sig)
        while i >= 0:
            struct.pack_into("<H", b, i + flag_at, struct.unpack_from("<H", b, i + flag_at)[0] | flags)
            if method is not None:
                struct.pack_into("<H", b, i + method_at, method)
            if version is not None:
                struct.pack_into("<H", b, i + ver_at, version)
            i = b.find(sig, i + 4)
    return bytes(b)


def _bad_crc() -> bytes:
    data = bytearray(_zip([("Contents/section0.xml", SECTION)], method=zipfile.ZIP_STORED))
    at = data.find(b"<sec")
    data[at + 1] ^= 0x01  # 내용 한 바이트만 바꾼다 (CRC 불일치)
    return bytes(data)


def _undecodable_name() -> bytes:
    data = _zip([("Contents/section0.xml", SECTION), ("BinData/가.png", b"\x89PNG")])
    return data.replace("가".encode(), b"\xff\xfe\x80")  # UTF-8 이름 플래그는 그대로, 이름은 UTF-8이 아니다


UNUSABLE = {
    "utf-16": lambda: _zip([("Contents/section0.xml",
                             '<?xml version="1.0" encoding="UTF-16"?><s>{{report.title}}</s>'.encode("utf-16"))]),
    "euc-kr-bytes": lambda: _zip([("Contents/section0.xml", "<s>한글 {{report.title}}</s>".encode("euc-kr"))]),
    "declares-euc-kr": lambda: _zip([("Contents/section0.xml",
                                      b'<?xml version="1.0" encoding="EUC-KR"?><s>{{report.title}}</s>')]),
    "bad-crc": _bad_crc,
    "encrypted": lambda: _patch_headers(_zip([("Contents/section0.xml", SECTION)]), flags=0x1),
    "unsupported-method": lambda: _patch_headers(_zip([("Contents/section0.xml", SECTION)]), method=99),
    "newer-zip-version": lambda: _patch_headers(_zip([("Contents/section0.xml", SECTION)]), version=0xFF),
    "undecodable-name": _undecodable_name,
}


@pytest.mark.parametrize("case", list(UNUSABLE))
def test_template_the_renderer_cannot_use_is_rejected_by_the_check(case: str) -> None:
    data = UNUSABLE[case]()
    problems = rr.check_hwpx(data)  # 전에는 [](통과)였거나 예외(→ PUT 500)였다
    assert len(problems) == 1 and problems[0], problems
    with pytest.raises(rr.HwpxTemplateError):  # 전에는 UnicodeDecodeError·BadZipFile 등 (→ 내보내기 500)
        rr.render_hwpx(data, _doc())


def test_any_template_that_passes_the_check_renders() -> None:
    """검사와 렌더가 같은 길로 양식을 읽는다: 무작위로 망가뜨린 양식도 검사는 예외 없이 답하고, 통과하면 렌더된다."""
    base = _zip([("mimetype", "application/hwp+zip"), ("Contents/section0.xml", SECTION * 3),
                 ("BinData/image1.bin", bytes(range(256)) * 2)])
    stored = _zip([("mimetype", "application/hwp+zip"), ("Contents/section0.xml", SECTION)],
                  method=zipfile.ZIP_STORED)
    rng = random.Random(20261112)
    passed = rejected = 0
    for i in range(400):
        b = bytearray(base if i % 2 else stored)
        for _ in range(rng.randint(1, 3)):
            b[rng.randrange(len(b))] = rng.randrange(256)
        data = bytes(b)
        problems = rr.check_hwpx(data)
        if problems:
            rejected += 1
            with pytest.raises(rr.HwpxTemplateError):
                rr.render_hwpx(data, _doc())
        else:
            passed += 1
            assert zipfile.ZipFile(io.BytesIO(rr.render_hwpx(data, _doc()))).testzip() is None
    assert passed and rejected  # 두 갈래를 모두 지났다


BAD_NAMES = [
    "..\\..\\evil.xml", "Contents\\..\\..\\evil.xml", "Contents/..\\..\\evil.xml",  # 역슬래시·섞인 구분자
    "C:/Windows/evil.xml", "C:evil.xml", "c:\\evil.xml", "Contents/section0.xml:stream",  # 드라이브·스트림
    "\\\\server\\share\\evil.xml", "//server/share/evil.xml",  # UNC
    "/etc/evil.xml", "a/../../evil.xml", "../evil.xml",  # POSIX (전에도 막았다)
    "Contents/sec\ttion.xml",  # 제어 문자
]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_entry_names_that_leave_the_package_are_rejected(name: str) -> None:
    data = _zip([("Contents/section0.xml", SECTION), (name, "<a/>")])
    assert rr.check_hwpx(data) == ["zip 항목 경로가 올바르지 않습니다"]
    with pytest.raises(rr.HwpxTemplateError):
        rr.render_hwpx(data, _doc())


def test_ordinary_package_entry_names_keep_working() -> None:
    names = ["mimetype", "version.xml", "Contents/", "Contents/header.xml", "Contents/section0.xml",
             "Contents/content.hpf", "META-INF/container.xml", "META-INF/manifest.xml", "settings.xml",
             "BinData/image1.png", "BinData/그림 1.png", "Preview/PrvText.txt", "Preview/a..b.png", "./x.xml"]
    data = _zip([(n, b"" if n.endswith("/") else (SECTION if n.endswith(".xml") else b"\x00\x01")) for n in names])
    assert rr.check_hwpx(data) == []
    out = zipfile.ZipFile(io.BytesIO(rr.render_hwpx(data, _doc(title="제목"))))
    assert out.namelist() == names
    assert out.read("Contents/section0.xml").decode() == SECTION.replace("{{report.title}}", "제목").replace(
        "{{field.summary}}", "요약 값")


@pytest.mark.parametrize("head", ["", '<?xml version="1.0" encoding="UTF-8"?>', "<?xml version='1.0' encoding='utf-8'?>",
                                  '<?xml version="1.0" encoding="utf8" standalone="yes"?>',
                                  '<?xml version="1.0"?>'],
                         ids=["none", "UTF-8", "utf-8-single-quotes", "utf8-standalone", "no-encoding"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"], ids=["no-bom", "bom"])
def test_utf8_parts_with_or_without_declaration_are_accepted(head: str, bom: bytes) -> None:
    data = _zip([("Contents/section0.xml", bom + (head + "<s>{{report.title}} 한글</s>").encode())])
    assert rr.check_hwpx(data) == []
    out = zipfile.ZipFile(io.BytesIO(rr.render_hwpx(data, _doc(title="합성"))))
    assert out.read("Contents/section0.xml") == bom + (head + "<s>합성 한글</s>").encode()


# ------------------------------------------------------------------ PDF: 어떤 주소도 가져오지 않는다

needs_pdf = pytest.mark.skipif(not rr.pdf_available(), reason="WeasyPrint(pango)가 없다")
_WATCH: dict[str, Any] = {"paths": set(), "events": None}


def _audit(event: str, args: tuple[Any, ...]) -> None:
    events = _WATCH["events"]
    if events is None:
        return
    if event == "urllib.Request":
        events.append(("urllib", str(args[0])))
    elif event == "open" and isinstance(args[0], str) and args[0] in _WATCH["paths"]:
        events.append(("open", args[0]))
    elif event == "socket.connect":
        events.append(("connect", str(args[1])))


@pytest.fixture(scope="module")
def _audit_hook() -> None:
    sys.addaudithook(_audit)  # 뗄 수 없으므로 모듈에서 한 번만 걸고, 기록은 _watch() 안에서만 한다


class _Watch:
    def __init__(self, paths: set[str]) -> None:
        self.events: list[tuple[str, str]] = []
        self.paths = paths

    def __enter__(self) -> list[tuple[str, str]]:
        _WATCH["paths"], _WATCH["events"] = self.paths, self.events
        return self.events

    def __exit__(self, *_exc: object) -> None:
        _WATCH["events"] = None


@pytest.fixture()
def canary() -> Iterator[tuple[str, list[str]]]:
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_a: Any) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", hits
    finally:
        srv.shutdown()
        srv.server_close()


def _hostile_html(base: str, tmp: Path) -> str:
    """이스케이프가 뚫렸다고 가정한 HTML: 가져오기를 일으키는 자리를 모두 넣는다."""
    css, svg = tmp / "canary.css", tmp / "canary.svg"
    css.write_text("body{color:red}", encoding="utf-8")
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>', encoding="utf-8")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<link rel='stylesheet' href='{base}/link.css'><link rel='stylesheet' href='{css.as_uri()}'>"
        f"<style>@import url({base}/import.css); @font-face{{font-family:x;src:url({base}/font.woff)}}"
        f"body{{background:url({base}/background.png)}} h1{{font-family:x}}</style></head><body><h1>x</h1>"
        f"<img src='{base}/img.png'><img src='{svg.as_uri()}'>"
        f"<div style='background-image:url({base}/style-attr.png)'>x</div>"
        f"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'><image href='{base}/svg-image.png' "
        "width='10' height='10'/></svg>"
        f"<object data='{base}/object.svg'></object></body></html>"
    )


@needs_pdf
def test_pdf_renderer_fetches_nothing_even_if_escaping_were_bypassed(
    _audit_hook: None, canary: tuple[str, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from weasyprint import HTML

    base, hits = canary
    hostile = _hostile_html(base, tmp_path)
    paths = {str(tmp_path / "canary.css"), str(tmp_path / "canary.svg")}
    # 대조: WeasyPrint 기본값은 이 HTML로 http·file 주소를 가져온다 (감시가 가져오기를 알아챈다는 증거)
    with _Watch(paths) as events:
        HTML(string=hostile).write_pdf()
    assert len(hits) >= 5 and {kind for kind, _ in events} >= {"urllib", "open", "connect"}, (hits, events)

    hits.clear()
    monkeypatch.setattr(rr, "to_html", lambda _doc: hostile)
    with _Watch(paths) as events:
        pdf = rr.render_pdf(_doc())
    assert pdf.startswith(b"%PDF")
    assert hits == [] and events == []  # 전에는 기본 fetcher가 위 주소를 모두 가져왔다


@needs_pdf
def test_ordinary_report_pdf_needs_no_fetch(caplog: pytest.LogCaptureFixture) -> None:
    """글꼴은 이미지에 설치된 나눔 글꼴(fontconfig)에서 오고 HTML은 외부 자원을 쓰지 않는다 — 막는 fetcher가 불릴 일이 없다."""
    with caplog.at_level("WARNING"):
        pdf = rr.render_pdf(_doc(title="제목 <img src='http://127.0.0.1:9/x'>", value="값 & 'quote' \"dq\""))
    assert pdf.startswith(b"%PDF")
    assert not [r for r in caplog.records if r.name.startswith(("weasyprint", rr.__name__))], caplog.text


# ------------------------------------------------------------------ PDF: 동시 렌더 차례


def test_render_slots_do_not_wait_and_are_released() -> None:
    slots = rr.RenderSlots(1)
    with slots.try_hold() as first:
        with slots.try_hold() as second:
            assert (first, second) == (True, False)  # 기다리지 않고 바로 '없음'
    with pytest.raises(RuntimeError), slots.try_hold() as got:
        assert got
        raise RuntimeError("렌더 실패")
    with slots.try_hold() as again:  # 예외가 나도 차례는 돌려준다
        assert again
