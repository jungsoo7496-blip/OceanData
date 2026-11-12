"""10번 보고서 계약 점검 S34~S38 (ADR-0080).

- S35 초안 HWPX는 양식에 {{report.status}} 자리가 없으면 '초안' 표시 없이 내보내지 않고 409다.
- S36 내보내기는 보고서·버전 범위를 먼저 확인한다. 형식이 꺼져 있어도 다른 기관·없는 보고서는 404(+감사)다.
- S37 /v1/system/capabilities의 report_formats는 내보내기가 받는 형식과 같다 (PDF 렌더러가 없으면 PDF 없음).
- S38 관리기관 열람자의 latest_version은 제출된 버전 중 최신이다 (제출되지 않은 초안의 수를 드러내지 않는다).
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.adapters import report_renderers as rr
from app.seed import SES_A
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
ADMIN, MEMBER, OTHER_ORG, VIEWER, OP = 101, 102, 103, 105, 106
GET = "/v1/reports/{report_id}"
EXPORT = "/v1/reports/{report_id}/export"
TPL = "/v1/report-templates/EXAMPLE_SESSION_LOG/versions/1/hwpx"


def _new_report(c: Ctx) -> str:
    r = c.client.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A},
                      headers=c.as_user(MEMBER))
    assert r.status_code == 201, r.text
    return str(r.json()["report_id"])


def _approve_and_submit(c: Ctx, rid: str) -> None:
    for step in ("approve", "submit"):
        r = c.client.post(f"/v1/reports/{rid}/{step}", headers=c.as_user(ADMIN))
        assert r.status_code == 200, r.text


# ------------------------------------------------------------------ S35 초안 HWPX의 '초안' 표시


def _zip(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def _put_template(c: Ctx, entries: dict[str, str]) -> None:
    r = c.client.put(TPL, content=_zip(entries), headers={**c.as_user(OP), "Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text


def test_draft_hwpx_needs_the_status_placeholder(ictx: Ctx) -> None:
    """양식에 {{report.status}}가 없으면 초안 HWPX는 409(감사 없음). 자리가 있으면 '초안'이 들어가고, 승인본은 상관없다."""
    c, hm = ictx.client, ictx.as_user(MEMBER)
    _put_template(ictx, {"Contents/section0.xml": "<sec><p>{{report.title}} {{report.generated_at}} "
                                                   "{{report.data_as_of}}</p></sec>"})
    rid = _new_report(ictx)
    path = EXPORT.format(report_id=rid)
    r = c.get(path, params={"format": "HWPX"}, headers=hm)
    assert r.status_code == 409, "초안 표시 없는 초안 HWPX를 내보냈다"
    check(r, "GET", EXPORT)
    assert r.json()["code"] == "CONFLICT" and "{{report.status}}" in r.json()["detail"], r.text
    assert "REPORT_EXPORTED" not in ictx.audit_actions()
    assert c.get(path, params={"format": "XLSX"}, headers=hm).status_code == 200  # 다른 형식은 그대로

    # 렌더러가 바꾸는 자리만 센다: XML이 아닌 항목에만 있으면 없는 것과 같다
    _put_template(ictx, {"Contents/section0.xml": "<sec><p>{{report.title}}</p></sec>",
                         "Preview/PrvText.txt": "{{report.status}}"})
    assert c.get(path, params={"format": "HWPX"}, headers=hm).status_code == 409

    # 앞뒤 공백이 있는 자리도 렌더러와 같이 알아본다
    _put_template(ictx, {"Contents/section0.xml": "<sec><p>{{ report.status }} {{report.title}}</p></sec>"})
    r = c.get(path, params={"format": "HWPX"}, headers=hm)
    assert r.status_code == 200, r.text
    assert zipfile.ZipFile(io.BytesIO(r.content)).read("Contents/section0.xml").decode().startswith("<sec><p>초안 ")

    # 승인본은 초안 표시가 필요 없다
    _put_template(ictx, {"Contents/section0.xml": "<sec><p>{{report.title}}</p></sec>"})
    assert c.post(f"/v1/reports/{rid}/approve", headers=ictx.as_user(ADMIN)).status_code == 200
    r = c.get(path, params={"format": "HWPX"}, headers=hm)
    assert r.status_code == 200, r.text
    assert ictx.audit_actions().count("REPORT_EXPORTED") == 3  # XLSX 1건, HWPX 2건


# ------------------------------------------------------------------ S36 내보내기 확인 순서


def test_export_checks_report_scope_before_the_format_gate(ctx: Ctx) -> None:
    """ctx는 REPORT_FORMATS가 비어 있다. 그래도 다른 기관 보고서는 404 + CROSS_ORG_ACCESS_ATTEMPT, 없는 것도 404."""
    c, hm = ctx.client, ctx.as_user(MEMBER)
    rid = _new_report(ctx)
    path = EXPORT.format(report_id=rid)
    before = ctx.audit_actions().count("CROSS_ORG_ACCESS_ATTEMPT")
    r = c.get(path, params={"format": "XLSX"}, headers=ctx.as_user(OTHER_ORG))
    check(r, "GET", EXPORT)
    assert (r.status_code, r.json()["code"]) == (404, "NOT_FOUND"), "형식 확인이 범위 확인보다 먼저다"
    assert ctx.audit_actions().count("CROSS_ORG_ACCESS_ATTEMPT") == before + 1
    missing = c.get(EXPORT.format(report_id="rpt_" + "0" * 26), params={"format": "XLSX"}, headers=hm)
    assert missing.status_code == 404, missing.text
    assert c.get(path, params={"format": "XLSX", "version": 9}, headers=hm).status_code == 404
    own = c.get(path, params={"format": "XLSX"}, headers=hm)  # 볼 수 있는 보고서에만 형식 409
    check(own, "GET", EXPORT)
    assert own.status_code == 409 and "REPORT_FORMATS" in own.json()["detail"], own.text


# ------------------------------------------------------------------ S37 켜진 형식


@pytest.mark.parametrize("renderer", [True, False], ids=["pdf-renderer", "no-pdf-renderer"])
def test_capabilities_list_only_formats_the_export_accepts(ctx: Ctx, monkeypatch: pytest.MonkeyPatch,
                                                            renderer: bool) -> None:
    monkeypatch.setattr(ctx.c.settings, "report_formats", ["XLSX", "PDF"])
    monkeypatch.setattr(rr, "pdf_available", lambda: renderer)
    monkeypatch.setattr(rr, "render_pdf", lambda doc: b"%PDF-1.7 synthetic")  # 렌더러가 있는지만 본다
    hm = ctx.as_user(MEMBER)
    rid = _new_report(ctx)
    caps = ctx.client.get("/v1/system/capabilities", headers=hm)
    check(caps, "GET", "/v1/system/capabilities")
    formats = caps.json()["report_formats"]
    assert formats == (["XLSX", "PDF"] if renderer else ["XLSX"]), "내보내기가 받지 않는 형식을 켜졌다고 알렸다"
    for fmt in ("XLSX", "PDF"):
        r = ctx.client.get(EXPORT.format(report_id=rid), params={"format": fmt}, headers=hm)
        assert (r.status_code == 200) is (fmt in formats), (fmt, r.status_code)
        if r.status_code == 409:
            detail = r.json()["detail"]  # REPORT_FORMATS에는 PDF가 있다: 이유는 렌더러다
            assert "렌더러" in detail and "(REPORT_FORMATS)" not in detail, detail


# ------------------------------------------------------------------ S38 열람자의 최신 버전


def test_viewer_latest_version_counts_only_submitted_versions(ictx: Ctx) -> None:
    c, hm, viewer = ictx.client, ictx.as_user(MEMBER), ictx.as_user(VIEWER)
    rid = _new_report(ictx)
    path = GET.format(report_id=rid)
    _approve_and_submit(ictx, rid)
    for _ in range(2):  # 제출하지 않은 초안 v2·v3
        assert c.post(f"{path}/versions", headers=hm).status_code == 201

    def seen(headers: dict[str, str], **params: int) -> list[tuple[int, int]]:
        one = c.get(path, params=params, headers=headers)
        check(one, "GET", GET)
        listed = c.get("/v1/reports", headers=headers)
        check(listed, "GET", "/v1/reports")
        items = [i for i in listed.json()["items"] if i["report_id"] == rid]
        return [(one.json()["version"], one.json()["latest_version"])] + [
            (i["version"], i["latest_version"]) for i in items]

    assert seen(viewer) == [(1, 1), (1, 1)], "열람자가 제출되지 않은 버전의 수를 알게 됐다"
    assert seen(hm) == [(3, 3), (3, 3)]  # 소유 기관은 그대로
    assert c.get(path, params={"version": 3}, headers=viewer).status_code == 404
    _approve_and_submit(ictx, rid)  # v3 승인·제출 → v1은 SUPERSEDED지만 제출된 적이 있어 열람자가 본다
    assert seen(viewer) == [(3, 3), (3, 3)]
    assert seen(viewer, version=1)[0] == (1, 3)
