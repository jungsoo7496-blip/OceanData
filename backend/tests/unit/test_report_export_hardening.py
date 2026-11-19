"""10번 요구사항 4.5: 보고서 내보내기 보강 (검토 2026-11-12 V7·V8, ADR-0066).

- HWPX 양식: UTF-8이 아니거나 읽을 수 없는 양식(손상·암호화·지원하지 않는 압축)이 올릴 때 검사를 통과하고 내보낼 때
  예외(→ 500)가 났다. zip 항목 이름 검사는 POSIX 형태('/'로 시작·'..')만 막아 역슬래시·드라이브 문자·UNC 이름이 통과했다.
  제어 문자(C0·C1)와 보이지 않는 서식 문자(방향 제어 등)가 든 이름도 거부한다.
- 양식 읽기 메모리: 검사와 내보내기는 항목을 조각으로 읽는다. 검사는 XML 항목을 글로 풀지 않고, 헤더에 적힌 크기보다
  길게 풀리는 압축 자료(압축 폭탄)도 적힌 크기까지만 읽는다.
- 항목 종류(통합 검토 REPORTS-1): 심볼릭 링크 등 일반 파일·폴더가 아닌 항목은 거부한다. 내보낸 파일에는 양식 항목의
  이름·순서·시각·압축 방식만 옮기고 외부 속성(유닉스 파일 종류·권한)과 추가 필드는 옮기지 않는다.
- PDF: WeasyPrint 기본 URL fetcher는 http·file 주소를 가져온다. 지금은 HTML 이스케이프가 사용자 글을 막지만, 그것이
  뚫려도 렌더러는 어떤 주소도 가져오지 않아야 한다.

시험 데이터는 합성 값이고 네트워크는 127.0.0.1만 쓴다.
"""

from __future__ import annotations

import http.server
import io
import random
import stat
import struct
import sys
import threading
import tracemalloc
import zipfile
import zlib
from collections.abc import Callable, Iterator
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
    "Contents/sec\ttion.xml",  # 제어 문자 C0
    # C1 제어 문자(NEL·CSI)와 보이지 않는 서식 문자. 소스에 보이지 않는 글자를 넣지 않으려고 chr로 만든다
    "Contents/a" + chr(0x85) + "b.xml", "Contents/a" + chr(0x9B) + "31mb.xml",
    "BinData/evil" + chr(0x202E) + "lmx.png",  # RLO: 'evilgnp.xml'처럼 보인다
    "Contents/a" + chr(0x200B) + "b.xml", "Contents/a" + chr(0x2028) + "b.xml", "Contents/a" + chr(0xFEFF) + "b.xml",
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
             "BinData/image1.png", "BinData/그림 1.png", "Preview/PrvText.txt", "Preview/a..b.png", "./x.xml",
             "BinData/" + chr(0x3000) + "전각 공백·가운뎃점.png"]
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


# ------------------------------------------------------------------ HWPX: 항목 종류와 내보낸 항목 (통합 검토 REPORTS-1)

FILE, DIR = stat.S_IFREG | 0o644, stat.S_IFDIR | 0o755
# 일반 파일·폴더가 아닌 유닉스 파일 종류 (zip 외부 속성의 위 16비트에 적힌다)
NOT_FILE_KINDS = {"symlink": stat.S_IFLNK, "char-device": stat.S_IFCHR, "block-device": stat.S_IFBLK,
                  "fifo": stat.S_IFIFO, "socket": stat.S_IFSOCK}


def _info(name: str, mode: int, *, system: int = 3, method: int = zipfile.ZIP_DEFLATED,
          extra: bytes = b"") -> zipfile.ZipInfo:
    """만든 OS 표시(create_system: 3 유닉스, 0 MS-DOS)와 유닉스 모드(외부 속성의 위 16비트)를 적은 항목."""
    info = zipfile.ZipInfo(name, (2026, 11, 20, 9, 0, 0))
    info.create_system, info.compress_type, info.extra = system, method, extra
    info.external_attr = mode << 16 | (0x10 if name.endswith("/") else 0)  # 0x10: MS-DOS 폴더 표시
    return info


def _zip_infos(entries: list[tuple[zipfile.ZipInfo, bytes | str]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for info, data in entries:
            z.writestr(info, data)
    return buf.getvalue()


@pytest.mark.parametrize("system", [3, 0], ids=["unix", "msdos"])
@pytest.mark.parametrize("kind", list(NOT_FILE_KINDS))
def test_entries_that_are_not_regular_files_or_folders_are_rejected(kind: str, system: int) -> None:
    """링크 항목(대상은 패키지 밖)과 그 링크를 지나는 파일 항목: 이름은 패키지 안이지만 링크를 따르는 도구로 풀면 패키지
    밖을 가리킨다. 전에는 검사가 이름만 봐 통과했고(PUT 200), 내보낸 파일이 링크 항목을 그대로 옮겼다."""
    data = _zip_infos([(_info("Contents/section0.xml", FILE), SECTION),
                       (_info("BinData/up", NOT_FILE_KINDS[kind] | 0o777, system=system), "../../../../tmp"),
                       (_info("BinData/up/synthetic.txt", FILE), "합성")])
    assert rr.check_hwpx(data) == ["zip 항목 종류가 일반 파일·폴더가 아닙니다(심볼릭 링크 등): BinData/up"]
    with pytest.raises(rr.HwpxTemplateError):
        rr.render_hwpx(data, _doc())


def test_regular_files_and_folders_keep_working_whatever_mode_they_record() -> None:
    """종류를 적지 않은 항목(위 16비트가 0 — Windows 도구가 만든 zip)과 일반 파일·폴더는 권한·만든 OS 표시와 상관없이 받는다."""
    entries = [(_info("mimetype", 0, system=0, method=zipfile.ZIP_STORED), "application/hwp+zip"),
               (_info("Contents/", DIR), b""),
               (_info("Contents/section0.xml", stat.S_IFREG | 0o600), SECTION),
               (_info("BinData/image1.png", stat.S_IFREG | 0o755, system=0), b"PNG"),
               (_info("Preview/", 0, system=0), b"")]
    data = _zip_infos(entries)
    assert rr.check_hwpx(data) == []
    out = zipfile.ZipFile(io.BytesIO(rr.render_hwpx(data, _doc(title="제목"))))
    assert out.namelist() == [info.filename for info, _ in entries]


def test_export_copies_only_name_time_and_compression_of_template_entries() -> None:
    """내보낸 파일의 항목에는 양식 항목의 이름·순서·시각·압축 방식만 옮기고, 외부 속성(유닉스 파일 종류·권한)과 추가
    필드는 새로 쓴다. 전에는 양식의 ZipInfo를 그대로 써서 둘 다 옮겼다.

    - 권한: setuid·모두 쓰기(0o4777)가 내보낸 파일에 그대로 실렸다.
    - 유니코드 경로 추가 필드(0x7075): 머리글 이름이 CP437 바이트 'Contents/(0x80).xml'이면 검사는 그 이름을 'Contents/Ç.xml'로
      본다(추가 필드의 CRC가 머리글과 맞지 않아 무시된다). 내보낼 때 zipfile이 이름을 UTF-8로 다시 쓰면 같이 옮긴 추가 필드의
      CRC가 새 머리글과 맞아, 내보낸 파일의 그 항목 이름이 '../../evil.xml'로 읽혔다."""
    shown = "Contents/" + chr(0xC7) + ".xml"  # CP437 0x80을 푼 이름
    up = struct.pack("<BL", 1, zlib.crc32(shown.encode())) + b"../../evil.xml"
    unicode_path = struct.pack("<HH", 0x7075, len(up)) + up
    entries = [(_info("mimetype", 0, system=0, method=zipfile.ZIP_STORED), "application/hwp+zip"),
               (_info("Contents/", DIR), b""),
               (_info("Contents/section0.xml", FILE), SECTION),
               (_info("Contents/X.xml", stat.S_IFREG | 0o4777, extra=unicode_path), "<a/>")]
    data = _zip_infos(entries).replace(b"Contents/X.xml", b"Contents/" + bytes([0x80]) + b".xml")
    assert zipfile.ZipFile(io.BytesIO(data)).namelist()[-1] == shown
    assert rr.check_hwpx(data) == []
    out = zipfile.ZipFile(io.BytesIO(rr.render_hwpx(data, _doc())))
    assert out.namelist() == ["mimetype", "Contents/", "Contents/section0.xml", shown]  # 전에는 끝이 '../../evil.xml'
    for got, (want, _) in zip(out.infolist(), entries, strict=True):
        assert (got.date_time, got.compress_type, got.extra) == (want.date_time, want.compress_type, b""), got
        assert got.external_attr >> 16 == (DIR if got.is_dir() else FILE), (got.filename, oct(got.external_attr))


# ------------------------------------------------------------------ HWPX: 읽을 때 메모리 (검토 지적, ADR-0066)

MiB = 1 << 20
BOMB_METHODS = {"deflate": zipfile.ZIP_DEFLATED, "bzip2": zipfile.ZIP_BZIP2, "lzma": zipfile.ZIP_LZMA}


def _a_entry(method: int, size: int, *, tail: bytes = b"", declared: int | None = None) -> bytes:
    """'a' size바이트(+ tail)인 XML 항목 하나짜리 양식. 내용을 조각으로 써서 시험이 그만큼 메모리를 쓰지 않는다.

    declared를 주면 헤더(로컬·중앙 디렉터리)의 크기와 CRC를 앞 declared바이트의 것으로 고친다 — 압축 자료는 size
    바이트로 풀리는데 헤더는 declared바이트라고 적은 양식(압축 폭탄)이다."""
    buf = io.BytesIO()
    info = zipfile.ZipInfo("Contents/section0.xml")
    info.compress_type = method
    with zipfile.ZipFile(buf, "w") as z, z.open(info, "w") as w:
        for _ in range(size // MiB):
            w.write(b"a" * MiB)
        w.write(tail)
    if declared is None:
        return buf.getvalue()
    b = bytearray(buf.getvalue())
    crc, cd = zlib.crc32(b"a" * declared), b.find(b"PK\x01\x02")
    for crc_at, size_at in ((14, 22), (cd + 16, cd + 24)):
        struct.pack_into("<I", b, crc_at, crc)
        struct.pack_into("<I", b, size_at, declared)
    return bytes(b)


def _peak(fn: Callable[[], object]) -> int:
    """fn이 도는 동안 Python이 잡은 메모리의 최댓값 (바이트)."""
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_check_does_not_hold_a_large_xml_part_in_memory() -> None:
    """올릴 때 검사는 XML 항목을 조각으로 읽어 UTF-8만 보고 버린다. 'a' 32MiB 끝에 4바이트 글자 하나를 붙인 항목
    (올리는 크기 32KiB 남짓)을 글로 통째로 풀면 글자마다 4바이트라 160MiB가 넘는다 (검토: 98KB 올리기에 600MB)."""
    data = _a_entry(zipfile.ZIP_DEFLATED, 32 * MiB, tail=chr(0x1F600).encode())
    assert len(data) < 64 * 1024
    problems: list[str] = []
    peak = _peak(lambda: problems.extend(rr.check_hwpx(data)))
    assert problems == []
    assert peak < 8 * MiB, f"{peak / MiB:.1f}MiB"  # 수정 전 160MiB 남짓


@pytest.mark.parametrize("method", list(BOMB_METHODS))
def test_compression_bomb_entry_is_read_only_up_to_its_declared_size(method: str) -> None:
    """헤더에는 1000바이트, 실제로는 32MiB로 풀리는 항목: 읽기는 적힌 크기에서 멈추고 한 번에 푸는 양도 조각 크기로
    묶인다. 전에는 압축 해제 한 번에 끝까지 풀어(그 뒤 잘라 냄) 올리기 1MB 남짓이면 메모리 2GB를 썼다."""
    data = _a_entry(BOMB_METHODS[method], 32 * MiB, declared=1000)
    assert len(data) < 64 * 1024
    problems: list[str] = []
    peak = _peak(lambda: problems.extend(rr.check_hwpx(data)))
    assert problems == []  # 적힌 크기만큼 읽은 내용과 CRC가 맞는다 — 쓸 수 있는 양식이다
    assert peak < 16 * MiB, f"{peak / MiB:.1f}MiB"  # 수정 전 77~85MiB (lzma는 압축 해제 사전 8MiB를 쓴다)
    out = zipfile.ZipFile(io.BytesIO(rr.render_hwpx(data, _doc())))
    assert out.read("Contents/section0.xml") == b"a" * 1000
    if method != "lzma":  # lzma는 내보낼 때 다시 묶는 압축기가 그 자체로 90MiB 남짓을 쓴다 (읽기와 무관)
        peak = _peak(lambda: rr.render_hwpx(data, _doc()))
        assert peak < 16 * MiB, f"{peak / MiB:.1f}MiB"  # 수정 전 77MiB 남짓


@pytest.mark.parametrize(("chunks", "delta"), [(1, -1), (1, -2), (3, 5)], ids=["chunk-1", "chunk-2", "inside"])
@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED], ids=["stored", "deflate"])
def test_utf8_is_checked_across_read_chunks(method: int, chunks: int, delta: int) -> None:
    """조각으로 읽어도 판정은 항목 전체를 한 번에 푼 것과 같다: 조각 경계에 걸친 글자는 받고, 경계 뒤의 잘못된
    바이트와 끝이 잘린 글자는 거부한다."""
    at = chunks * rr._READ_CHUNK + delta  # 이 위치에서 글자가 시작한다
    han = "한".encode()

    def tpl(body: bytes) -> bytes:
        return _zip([("Contents/section0.xml", b"<s>" + b"a" * (at - 3) + body)], method=method)

    ok = tpl(han + b"{{report.title}}</s>")  # '한'(3바이트)이 조각 경계에 걸친다
    assert rr.check_hwpx(ok) == []
    out = zipfile.ZipFile(io.BytesIO(rr.render_hwpx(ok, _doc(title="합성"))))
    assert out.read("Contents/section0.xml").endswith(han + "합성</s>".encode())
    for bad in (b"\xed\xa0\x80</s>", b"\xff</s>", han[:2]):  # 서로게이트·UTF-8에 없는 바이트·끝이 잘린 글자
        assert rr.check_hwpx(tpl(bad)) == ["XML 항목이 UTF-8이 아닙니다: Contents/section0.xml"], bad
        with pytest.raises(rr.HwpxTemplateError):
            rr.render_hwpx(tpl(bad), _doc())


def test_encoding_declaration_is_found_in_a_part_longer_than_a_chunk() -> None:
    body = b'<?xml version="1.0" encoding="EUC-KR"?><s>' + b"a" * (3 * rr._READ_CHUNK) + b"</s>"
    assert rr.check_hwpx(_zip([("Contents/section0.xml", body)])) == [
        "XML 항목의 인코딩 선언이 UTF-8이 아닙니다: Contents/section0.xml"]


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
