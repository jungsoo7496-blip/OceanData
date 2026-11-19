"""10번 요구사항 4.5: 보고서 내보내기 API 보강 (검토 2026-11-12 V7·V8, ADR-0066).

- 렌더러가 쓸 수 없는 HWPX 양식(UTF-8 아님·항목 경로가 패키지 밖)은 PUT에서 400이다. 전에는 200으로 저장되고
  내보내기가 500이었다. 이 검사 전에 저장된 양식으로 내보내면 409 CONFLICT(양식을 다시 올리라는 안내)다.
- PDF 렌더는 프로세스마다 동시에 REPORT_PDF_MAX_CONCURRENT건까지다. 넘으면 기다리지 않고 429 RATE_LIMITED와
  Retry-After — 느린 렌더가 요청 스레드와 DB 차례(ADR-0062)를 한없이 쥐지 않게 한다. XLSX·HWPX는 상관없다.
"""

from __future__ import annotations

import io
import threading
import zipfile
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from app.adapters import report_renderers as rr
from app.db.models import ReportTemplate
from app.seed import SES_A
from tests import conftest
from tests.conftest import Ctx, StaticIdentity
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
MEMBER, OP = 102, 106
PUT = "/v1/report-templates/{template_code}/versions/{version}/hwpx"
EXPORT = "/v1/reports/{report_id}/export"
TPL = "/v1/report-templates/EXAMPLE_SESSION_LOG/versions/1/hwpx"


@pytest.fixture()
def app_ctx(ictx: Ctx, db_url: str, engine: Any) -> Iterator[Ctx]:
    """세 형식을 켜고 PDF 동시 렌더를 1건으로 줄인 앱. ictx가 시드한 DB와 S3 저장소(버킷)를 같이 쓴다."""
    from fastapi.testclient import TestClient

    from app.adapters.notifier import LogNotifier
    from app.container import build_container
    from app.main import create_app

    buckets = {f"bucket_{z}": getattr(ictx.c.settings, f"bucket_{z}")
               for z in ("raw", "proc", "evidence", "datasets", "quarantine")}
    settings = conftest.test_settings(db_url, report_formats=["XLSX", "HWPX", "PDF"], report_pdf_max_concurrent=1,
                                      **buckets)
    container = build_container(settings, engine=engine, storage=ictx.c.storage, identity=StaticIdentity(),
                                notifier=LogNotifier())
    with TestClient(create_app(container), raise_server_exceptions=False) as client:
        yield Ctx(client, container)


def _zip(entries: list[tuple[str, bytes | str]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            z.writestr(zipfile.ZipInfo(name), data, compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def _report(c: Ctx) -> str:
    r = c.client.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A},
                      headers=c.as_user(MEMBER))
    assert r.status_code == 201, r.text
    return str(r.json()["report_id"])


def _put(c: Ctx, data: bytes) -> Any:
    return c.client.put(TPL, content=data, headers={**c.as_user(OP), "Content-Type": "application/octet-stream"})


BAD_TEMPLATES = {
    "utf-16": [("Contents/section0.xml", '<?xml version="1.0" encoding="UTF-16"?><s>{{report.title}}</s>'
                .encode("utf-16"))],
    "euc-kr": [("Contents/section0.xml", "<s>한글 {{report.title}}</s>".encode("euc-kr"))],
    "backslash-traversal": [("Contents/section0.xml", "<s/>"), ("..\\..\\evil.xml", "<a/>")],
    "drive-letter": [("Contents/section0.xml", "<s/>"), ("C:/Windows/evil.xml", "<a/>")],
    # 방향 제어 문자(U+202E)로 'evilgnp.xml'처럼 보이는 이름. 소스에 보이지 않는 글자를 넣지 않으려고 chr로 만든다
    "bidi-name": [("Contents/section0.xml", "<s/>"), ("BinData/evil" + chr(0x202E) + "lmx.png", b"\x89PNG")],
}


@pytest.mark.parametrize("case", list(BAD_TEMPLATES))
def test_put_rejects_templates_the_renderer_cannot_use(app_ctx: Ctx, case: str) -> None:
    r = _put(app_ctx, _zip(BAD_TEMPLATES[case]))
    check(r, "PUT", PUT)
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION_ERROR", r.text  # 전에는 200으로 저장됐다
    listed = app_ctx.client.get("/v1/report-templates", headers=app_ctx.as_user(OP)).json()["items"]
    assert next(t for t in listed if t["code"] == "EXAMPLE_SESSION_LOG")["has_hwpx"] is False


def test_export_with_a_stored_unusable_template_is_a_conflict_not_500(app_ctx: Ctx) -> None:
    """검사가 생기기 전에 저장된 양식: 내보내기는 500이 아니라 409이고, 다시 올리면 된다."""
    c = app_ctx
    rid = _report(c)
    key = "ops/report-templates/EXAMPLE_SESSION_LOG/v1.hwpx"
    c.c.storage.put_object(c.c.settings.bucket_evidence, key, _zip(BAD_TEMPLATES["euc-kr"]), rr.MIME["HWPX"])
    with c.c.sessionmaker.begin() as db:
        tpl = db.execute(select(ReportTemplate).where(ReportTemplate.code == "EXAMPLE_SESSION_LOG")).scalar_one()
        tpl.hwpx_key, tpl.hwpx_sha256 = key, "0" * 64
    r = c.client.get(EXPORT.format(report_id=rid), params={"format": "HWPX"}, headers=c.as_user(MEMBER))
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT", r.text  # 전에는 UnicodeDecodeError → 500
    check(r, "GET", EXPORT)
    assert "다시 올리" in r.json()["detail"]
    assert "REPORT_EXPORTED" not in c.audit_actions()

    good = _zip([("mimetype", "application/hwp+zip"), ("Contents/section0.xml", "<s>{{report.status}}</s>")])
    assert _put(c, good).status_code == 200
    r = c.client.get(EXPORT.format(report_id=rid), params={"format": "HWPX"}, headers=c.as_user(MEMBER))
    assert r.status_code == 200, r.text
    assert zipfile.ZipFile(io.BytesIO(r.content)).read("Contents/section0.xml").decode() == "<s>초안</s>"


def test_pdf_renders_beyond_the_process_cap_are_rate_limited(app_ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    c = app_ctx
    path = EXPORT.format(report_id=_report(c))
    started, release = threading.Event(), threading.Event()

    def slow_render(doc: dict[str, Any]) -> bytes:
        started.set()
        release.wait(10)  # 수정 전에는 두 번째 요청도 여기서 기다렸다가 200이 된다
        return b"%PDF-1.7 synthetic"

    monkeypatch.setattr(rr, "render_pdf", slow_render)
    first: dict[str, Any] = {}

    def export_first() -> None:
        first["r"] = c.client.get(path, params={"format": "PDF"}, headers=c.as_user(MEMBER))

    t = threading.Thread(target=export_first)
    t.start()
    try:
        assert started.wait(10), "첫 PDF 렌더가 시작되지 않았다"
        r = c.client.get(path, params={"format": "PDF"}, headers=c.as_user(MEMBER))
        assert r.status_code == 429, (r.status_code, r.headers.get("content-type"))
        check(r, "GET", EXPORT)
        assert r.json()["code"] == "RATE_LIMITED", r.text
        assert int(r.headers["retry-after"]) >= 1
        xlsx = c.client.get(path, params={"format": "XLSX"}, headers=c.as_user(MEMBER))
        assert xlsx.status_code == 200, xlsx.text  # 차례는 PDF에만 있다
    finally:
        release.set()
        t.join(20)
    assert first["r"].status_code == 200 and first["r"].content.startswith(b"%PDF")
    again = c.client.get(path, params={"format": "PDF"}, headers=c.as_user(MEMBER))
    assert again.status_code == 200, again.text  # 렌더가 끝나면 차례를 돌려준다
    assert c.audit_actions().count("REPORT_EXPORTED") == 3  # 429는 내보내기 감사를 남기지 않는다
