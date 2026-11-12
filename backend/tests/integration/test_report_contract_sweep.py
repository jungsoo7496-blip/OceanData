"""10번 보고서 계약 점검 S34~S38 (ADR-0080).

- S34 새 버전이 앞 버전(승인·제출본)의 기간을 바꾸지 않는다. 기간은 버전마다 저장한다 (마이그레이션 0025).
- S35 초안 HWPX는 양식에 {{report.status}} 자리가 없으면 '초안' 표시 없이 내보내지 않고 409다.
- S36 내보내기는 보고서·버전 범위를 먼저 확인한다. 형식이 꺼져 있어도 다른 기관·없는 보고서는 404(+감사)다.
- S37 /v1/system/capabilities의 report_formats는 내보내기가 받는 형식과 같다 (PDF 렌더러가 없으면 PDF 없음).
- S38 관리기관 열람자의 latest_version은 제출된 버전 중 최신이다 (제출되지 않은 초안의 수를 드러내지 않는다).
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.adapters import report_renderers as rr
from app.db.models import Report, ReportVersion
from app.seed import ORG_A, PRJ_A, SES_A
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


# ------------------------------------------------------------------ S34 버전별 기간


def test_new_version_keeps_the_period_of_earlier_versions(ictx: Ctx) -> None:
    """회차 끝 시각을 정하고 새 버전을 만들어도 제출된 v1의 기간은 승인 스냅숏과 같다. 새 기간은 v2에만."""
    c, hm, viewer = ictx.client, ictx.as_user(MEMBER), ictx.as_user(VIEWER)
    rid = _new_report(ictx)
    path = GET.format(report_id=rid)
    _approve_and_submit(ictx, rid)
    with ictx.c.sessionmaker() as db:
        key = db.get(ReportVersion, (rid, 1)).snapshot_key
    snap = json.loads(ictx.c.storage.get_object(ictx.c.settings.bucket_evidence, key))
    before = c.get(path, params={"version": 1}, headers=hm).json()
    assert (before["period_from"], before["period_to"]) == (snap["period_from"], snap["period_to"])

    ends = "2026-11-03T00:00:00Z"
    r = c.patch(f"/v1/sessions/{SES_A}", json={"ends_at": ends}, headers=hm)
    assert r.status_code == 200, r.text
    nv = c.post(f"{path}/versions", headers=hm)
    check(nv, "POST", "/v1/reports/{report_id}/versions")
    assert (nv.status_code, nv.json()["version"], nv.json()["period_to"]) == (201, 2, ends)

    for headers in (hm, viewer):  # 소유 기관과 관리기관 열람자 모두 v1의 원래 기간을 본다
        r = c.get(path, params={"version": 1}, headers=headers)
        check(r, "GET", GET)
        v1 = r.json()
        assert (v1["status"], v1["period_from"], v1["period_to"]) == (
            "SUBMITTED", snap["period_from"], snap["period_to"]), "제출된 v1의 기간이 새 버전 때문에 바뀌었다"
    latest = c.get(path, headers=hm).json()
    assert (latest["version"], latest["period_to"]) == (2, ends)
    listed = [i for i in c.get("/v1/reports", headers=hm).json()["items"] if i["report_id"] == rid]
    assert [(i["version"], i["period_to"]) for i in listed] == [(2, ends)]


def test_migration_0025_version_period_up_and_down(db_url: str) -> None:
    """0025 up: report_versions에 기간 칸을 더하고 보고서 행의 기간으로 채운다. down: 칸을 지운다."""
    from alembic import command
    from alembic.config import Config

    from app.seed import seed

    start, end = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 2, 12, tzinfo=UTC)
    eng = create_engine(db_url)
    with sessionmaker(eng).begin() as db:
        seed(db)
        db.add(Report(report_id="rpt_" + "0" * 25 + "1", organization_id=ORG_A, project_id=PRJ_A, session_id=SES_A,
                      period_from=start, period_to=end, title="합성 보고서", template_code="EXAMPLE_SESSION_LOG",
                      template_version=1, latest_version=2, created_by="seed", created_at=start))
        db.flush()
        for n in (1, 2):
            db.add(ReportVersion(report_id="rpt_" + "0" * 25 + "1", version=n, status="DRAFT", generated_at=end,
                                 data_as_of=end, variables={}, fields=[], metrics=[], record_refs=[],
                                 created_by="seed", created_at=end))
    base = os.path.join(os.path.dirname(__file__), "..", "..")
    cfg = Config(os.path.join(base, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(base, "app/db/migrations"))

    def columns() -> set[str]:
        with eng.connect() as conn:
            return {r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'report_versions'"))}

    command.downgrade(cfg, "0024")
    assert not {"period_from", "period_to"} & columns()
    command.upgrade(cfg, "head")
    assert {"period_from", "period_to"} <= columns()
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT version, period_from, period_to FROM report_versions ORDER BY version")).all()
    assert [(v, f, t) for v, f, t in rows] == [(1, start, end), (2, start, end)]
    eng.dispose()


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
