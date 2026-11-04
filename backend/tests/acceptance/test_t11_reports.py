"""T11 보고서·T06-a 계보 (docs/ACCEPTANCE.md): 합성 회차(선박 영상·모델/수동 이벤트·관측값 없음)로
회차 기록 → 증빙 승인 → 보고서 초안(계산식·원천·데이터 없음·검증 필요) → 수정 이력 → 내보내기 → 승인·제출,
인용 자산 격리 시 근거 손상 표시·알림·제출 차단, 지표 → 기록 → 이벤트 → 자산 → 원본 역추적.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from openpyxl import load_workbook
from sqlalchemy import select, update

from app.adapters.system_clock import FixedClock
from app.db.models import Asset, AssetLineage, ReportVersion
from app.seed import DEV_A_CAMERA, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, PRJ_B, SES_A
from app.services import evidence
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]
MEMBER, ADMIN, VIEWER, OP = 102, 101, 105, 106
T = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)


def z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def synthetic_session(ctx) -> dict[str, str]:
    """선박 원본 영상(30분) + 프록시, 다른 기관 자산, 모델 이벤트(선박, 미확인)·수동 이벤트(안전사고)."""
    with ctx.c.sessionmaker.begin() as db:
        orig = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        orig.ext = {"video_ext": {"duration_s": 1800, "fps": 25, "resolution": "1920x1080"}}
        orig.capture_start_at = T
        proxy = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="PROXY")
        other = _asset(ORG_B, PRJ_B, DEV_B_CAMERA, session=None)
        db.add_all([orig, proxy, other])
        db.flush()
        db.add(AssetLineage(child_asset_id=proxy.asset_id, parent_asset_id=orig.asset_id, relation="PROXY",
                            created_at=T))
        ids = {"orig": orig.asset_id, "proxy": proxy.asset_id, "other": other.asset_id}
    h = ctx.as_user(MEMBER)
    base = {"schema_version": "1.0.0", "session_id": SES_A, "started_at": z(T + timedelta(minutes=5)),
            "timestamp_quality": {"source": "DEVICE", "verified": True}}
    r = ctx.client.post("/v1/events", headers=h, json={
        **base, "event_code": "VESSEL", "source": "MODEL", "source_system_id": DEV_A_CAMERA,
        "model_ref": "synthetic-detector@1", "confidence": 0.91, "linked_asset_ids": [ids["proxy"]]})
    assert r.status_code == 201, r.text
    ids["model_event"] = r.json()["event_id"]
    r = ctx.client.post("/v1/events", headers=h, json={**base, "event_code": "SAFETY_INCIDENT", "source": "MANUAL",
                                                       "note": "합성 근접 항해"})
    ids["manual_event"] = r.json()["event_id"]
    return ids


@pytest.mark.covers("T11-a", "T11-b", "T11-c", "T11-d", "T11-e", "T06-a")
def test_t11_report_flow_and_t06a_trace(ictx) -> None:
    ctx = ictx
    ids = synthetic_session(ctx)
    hm, ha = ctx.as_user(MEMBER), ctx.as_user(ADMIN)

    # 회차 기록 + 근거 연결. 다른 기관 자산은 '없음'으로 거부 (요구사항 1.2)
    rec_body = {"kind": "TEST", "title": "1차 접근 시험", "test_item": "선박 탐지", "conditions": "주간·파고 0.5m",
                "result": "탐지 1건", "evidence_refs": [
                    {"kind": "ASSET", "asset_id": ids["orig"], "from_offset_s": 300, "to_offset_s": 360},
                    {"kind": "EVENT", "event_id": ids["model_event"]}]}
    bad = {**rec_body, "evidence_refs": [{"kind": "ASSET", "asset_id": ids["other"]}]}
    r = ctx.client.post(f"/v1/sessions/{SES_A}/records", json=bad, headers=hm)
    assert r.status_code == 400 and r.json()["errors"][0]["code"] == "not_found"
    r = ctx.client.post(f"/v1/sessions/{SES_A}/records", json=rec_body, headers=hm)
    check(r, "POST", "/v1/sessions/{session_id}/records")
    rec = r.json()
    path = f"/v1/records/{rec['record_id']}"
    r = ctx.client.patch(path, json={"result": "탐지 1건 (모델)"}, headers=hm)
    check(r, "PATCH", "/v1/records/{record_id}")
    assert r.json()["version"] == 2
    # 증빙 승인: ORG_ADMIN만, 인용 자산(원본 + 이벤트에 연결된 프록시) PROTECTED (요구사항 2)
    assert ctx.client.post(path + "/approve", headers=hm).status_code == 403
    r = ctx.client.post(path + "/approve", headers=ha)
    check(r, "POST", "/v1/records/{record_id}/approve")
    assert (r.json()["status"], r.json()["version"]) == ("APPROVED", 2)
    with ctx.c.sessionmaker() as db:
        assert {a.backup_class for a in db.execute(select(Asset).where(
            Asset.asset_id.in_([ids["orig"], ids["proxy"]]))).scalars()} == {"PROTECTED"}
    # 승인 후 수정은 새 버전, 승인본은 그대로 (요구사항 1.3, 2.1)
    assert ctx.client.patch(path, json={"note": "추가 메모"}, headers=hm).json()["version"] == 3
    r = ctx.client.get(path + "/versions", headers=hm)
    check(r, "GET", "/v1/records/{record_id}/versions")
    assert [(v["version"], v["status"]) for v in r.json()["items"]] == [(1, "DRAFT"), (2, "APPROVED"), (3, "DRAFT")]
    assert r.json()["items"][1]["result"] == "탐지 1건 (모델)"

    # T11-a·b·c: 양식으로 초안 — 계산식·원천, 데이터 없음, 검증 필요
    r = ctx.client.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A},
                        headers=hm)
    check(r, "POST", "/v1/reports")
    rep = r.json()
    m = {x["metric_code"]: x for x in rep["metrics"]}
    vessel = m["EXAMPLE_VESSEL_EVENTS"]
    assert (vessel["status"], vessel["value"], vessel["sources"]["event_ids"]) == \
        ("NEEDS_VERIFICATION", 1, [ids["model_event"]])
    assert "VESSEL" in vessel["formula"] and vessel["sources"]["asset_ids"] == [ids["proxy"]]
    assert (m["EXAMPLE_FAULT_EVENTS"]["status"], m["EXAMPLE_FAULT_EVENTS"]["value"]) == ("OK", 0)
    assert (m["EXAMPLE_MEAN_WATER_TEMP"]["status"], m["EXAMPLE_MEAN_WATER_TEMP"]["value"]) == ("NO_DATA", None)
    assert (m["EXAMPLE_VIDEO_HOURS"]["value"], m["EXAMPLE_VIDEO_HOURS"]["sources"]["asset_ids"]) == \
        (0.5, [ids["orig"]])
    assert rep["status"] == "DRAFT" and rep["record_ids"] == [rec["record_id"]]
    summary = next(f for f in rep["fields"] if f["key"] == "summary")
    assert summary["auto_value"] == "회차 A-2026-11-01 실증 결과 요약" and summary["edited"] is False

    # T11-d: 자동값을 고치면 구분·이력
    rpath = f"/v1/reports/{rep['report_id']}"
    r = ctx.client.patch(rpath + "/fields", json={"fields": [{"key": "summary", "value": "선박 1척 탐지(검증 필요)"}]},
                         headers=hm)
    check(r, "PATCH", "/v1/reports/{report_id}/fields")
    f = next(x for x in r.json()["fields"] if x["key"] == "summary")
    assert (f["auto_value"], f["value"], f["edited"]) == ("회차 A-2026-11-01 실증 결과 요약", "선박 1척 탐지(검증 필요)", True)
    assert f["history"][0]["previous"] == "회차 A-2026-11-01 실증 결과 요약" and f["history"][0]["edited_by"]
    assert ctx.client.patch(rpath + "/fields", json={"fields": [{"key": "nope", "value": "x"}]},
                            headers=hm).status_code == 400

    # 내보내기: 초안 표시, '검증 필요'·'데이터 없음'은 숫자 대신 글자 (요구사항 3.4, 4.4)
    r = ctx.client.get(rpath + "/export", params={"format": "XLSX"}, headers=hm)
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    assert wb["보고서"]["A2"].value == "초안"
    rows = {row[0]: row for row in wb["지표"].iter_rows(min_row=2, values_only=True)}
    assert rows["EXAMPLE_VESSEL_EVENTS"][2] == "검증 필요" and rows["EXAMPLE_MEAN_WATER_TEMP"][2] == "데이터 없음"
    assert rows["EXAMPLE_VIDEO_HOURS"][2] == 0.5
    assert ctx.client.get(rpath + "/export", params={"format": "HWPX"}, headers=hm).status_code == 409  # 원본 없음
    pdf = ctx.client.get(rpath + "/export", params={"format": "PDF"}, headers=hm)
    from app.adapters.report_renderers import pdf_available

    assert pdf.status_code == (200 if pdf_available() else 409)
    if pdf.status_code == 200:
        assert pdf.content.startswith(b"%PDF")
    # HWPX: 운영자가 올린 양식의 {{변수}} 치환 (합성 양식 — 실제 관리기관 양식은 T11-f, 확보 후)
    tpl = io.BytesIO()
    with zipfile.ZipFile(tpl, "w") as zf:
        zf.writestr("Contents/section0.xml", "<sec><p>{{session.label}} / {{field.summary}} / "
                                             "{{metric.EXAMPLE_VESSEL_EVENTS}} / {{report.status}} &amp;</p></sec>")
    r = ctx.client.put("/v1/report-templates/EXAMPLE_SESSION_LOG/versions/1/hwpx", content=tpl.getvalue(),
                       headers={**ctx.as_user(OP), "Content-Type": "application/octet-stream"})
    check(r, "PUT", "/v1/report-templates/{template_code}/versions/{version}/hwpx")
    assert ctx.client.put("/v1/report-templates/EXAMPLE_SESSION_LOG/versions/1/hwpx", content=b"not zip",
                          headers={**ctx.as_user(OP), "Content-Type": "application/octet-stream"}).status_code == 400
    r = ctx.client.get(rpath + "/export", params={"format": "HWPX"}, headers=hm)
    xml = zipfile.ZipFile(io.BytesIO(r.content)).read("Contents/section0.xml").decode()
    assert xml == "<sec><p>A-2026-11-01 / 선박 1척 탐지(검증 필요) / 검증 필요 / 초안 &amp;</p></sec>"

    # 승인: 버전 고정·EVIDENCE 존 스냅숏·sha256 (요구사항 5.2). 관리기관 열람자는 제출 전엔 볼 수 없다
    assert ctx.client.get(rpath, headers=ctx.as_user(VIEWER)).status_code == 404
    assert ctx.client.post(rpath + "/submit", headers=ha).status_code == 409  # 승인 전 제출 (요구사항 5.4)
    r = ctx.client.post(rpath + "/approve", headers=ha)
    check(r, "POST", "/v1/reports/{report_id}/approve")
    approved = r.json()
    snap = ctx.c.storage.get_object(ctx.c.settings.bucket_evidence, f"{ORG_A}/reports/{rep['report_id']}/v1.json")
    assert hashlib.sha256(snap).hexdigest() == approved["snapshot_sha256"]
    assert ctx.client.patch(rpath + "/fields", json={"fields": [{"key": "issues", "value": "x"}]},
                            headers=hm).status_code == 409  # 승인본은 못 고친다

    # T11-e: 인용 자산이 격리되면 근거 손상 표시·작성자와 ORG_ADMIN 알림·제출 차단 (요구사항 6)
    with ctx.c.sessionmaker.begin() as db:
        db.execute(update(Asset).where(Asset.asset_id == ids["proxy"])
                   .values(ingest_status="QUARANTINED", quarantine_reason="CORRUPT_FILE"))
    with ctx.c.sessionmaker.begin() as db:
        flagged = evidence.check_damage(ctx.c, db)
    assert {(x["kind"], x["id"]) for x in flagged} >= {("REPORT", rep["report_id"]), ("RECORD", rec["record_id"])}
    note = next(n for n in ctx.c.notifier.sent if n.kind == "evidence.damaged" and n.data["id"] == rep["report_id"])
    assert note.recipients == ("admin-a@example.invalid", "member-a@example.invalid")
    with ctx.c.sessionmaker.begin() as db:
        assert evidence.check_damage(ctx.c, db) == []  # 한 번만 표시
    r = ctx.client.post(rpath + "/submit", headers=ha)
    assert r.status_code == 422 and ids["proxy"] in r.json()["unmet_conditions"][0]["actual"]
    d = ctx.client.get(rpath, headers=hm).json()
    assert d["evidence_damaged"] and (d["damage"][0]["asset_id"], d["damage"][0]["reason"]) == (ids["proxy"], "QUARANTINED")

    # 근거를 바꾼 새 버전: 기록에서 모델 이벤트를 빼고 수동 이벤트로 → 승인 → 보고서 새 버전 → 승인·제출
    ctx.client.patch(path, json={"evidence_refs": [{"kind": "ASSET", "asset_id": ids["orig"]},
                                                   {"kind": "EVENT", "event_id": ids["manual_event"]}]}, headers=hm)
    assert ctx.client.post(path + "/approve", headers=ha).json()["version"] == 4
    with ctx.c.sessionmaker.begin() as db:  # 모델 이벤트의 프록시 연결도 지표 원천에서 빠지게 정상화
        db.execute(update(Asset).where(Asset.asset_id == ids["proxy"]).values(ingest_status="READY_PRIVATE"))
    r = ctx.client.post(rpath + "/versions", headers=hm)
    check(r, "POST", "/v1/reports/{report_id}/versions")
    assert r.json()["version"] == 2 and r.json()["evidence_damaged"] is False
    ctx.client.post(rpath + "/approve", headers=ha)
    r = ctx.client.post(rpath + "/submit", headers=ha)
    check(r, "POST", "/v1/reports/{report_id}/submit")
    assert r.json()["status"] == "SUBMITTED"
    with ctx.c.sessionmaker() as db:
        assert db.get(ReportVersion, (rep["report_id"], 1)).status == "SUPERSEDED"
        assert db.get(Asset, ids["orig"]).sharing_scope == "PRIVATE"  # 제출은 공유 등급을 바꾸지 않는다

    # 관리기관 열람자: 배정 사업의 제출본만 (요구사항 5.3)
    r = ctx.client.get(rpath, headers=ctx.as_user(VIEWER))
    check(r, "GET", "/v1/reports/{report_id}")
    assert (r.json()["version"], r.json()["status"]) == (2, "SUBMITTED")
    assert ctx.client.get(rpath, params={"version": 1}, headers=ctx.as_user(VIEWER)).status_code == 404
    assert [x["report_id"] for x in ctx.client.get("/v1/reports", headers=ctx.as_user(VIEWER)).json()["items"]] == \
        [rep["report_id"]]
    assert ctx.client.get(rpath, headers=ctx.as_user(103)).status_code == 404  # 다른 기관 (T01)

    # T06-a: 지표 → 회차 기록 → 이벤트 → 자산 → 원본
    r = ctx.client.get(rpath + "/metrics/EXAMPLE_VESSEL_EVENTS/trace", params={"version": 1}, headers=hm)
    check(r, "GET", "/v1/reports/{report_id}/metrics/{metric_code}/trace")
    t = r.json()
    assert [e["event_id"] for e in t["events"]] == [ids["model_event"]]
    assert t["events"][0]["asset_ids"] == [ids["proxy"]]
    assert t["assets"] == [{"asset_id": ids["proxy"], "variant": "PROXY", "ingest_status": "READY_PRIVATE",
                            "originals": [ids["orig"]]}]
    assert [x["record_id"] for x in t["records"]] == [rec["record_id"]]
    # 관리기관 열람자는 L0(비공개) 자산을 볼 수 없다: 숨긴 수만
    t = ctx.client.get(rpath + "/metrics/EXAMPLE_VIDEO_HOURS/trace", headers=ctx.as_user(VIEWER)).json()
    assert t["assets"] == [] and t["hidden_count"] == 1

    # 08번 연결: 승인본이 인용한 자산은 삭제 요청 시 영향 목록과 함께 거부
    r = ctx.client.post(f"/v1/assets/{ids['orig']}/deletion", json={"reason": "시험"},
                        headers=ctx.as_user(ADMIN, mfa=True))
    assert r.status_code == 409
    assert {(x["cited_by"], x["id"]) for x in r.json()["affected"]} >= {("RECORD", rec["record_id"]),
                                                                         ("REPORT", rep["report_id"])}


def test_t11_reprocess_marks_damage(ictx) -> None:
    ctx = ictx
    ids = synthetic_session(ctx)
    r = ctx.client.post(f"/v1/sessions/{SES_A}/records", headers=ctx.as_user(MEMBER), json={
        "kind": "LOG", "title": "영상 기록", "evidence_refs": [{"kind": "ASSET", "asset_id": ids["proxy"]}]})
    rec = r.json()
    later = dataclasses.replace(ctx.c, clock=FixedClock(datetime.now(UTC) + timedelta(minutes=1)))
    with later.sessionmaker.begin() as db:  # 원본 재처리 → 원본·파생본 모두 재처리 표시
        for a in db.execute(select(Asset).where(Asset.asset_id.in_([ids["orig"], ids["proxy"]]))).scalars():
            a.last_reprocessed_at = later.clock.now()
    with later.sessionmaker.begin() as db:
        flagged = evidence.check_damage(later, db)
    assert flagged[0]["damage"][0]["reason"] == "REPROCESSED"
    r = ctx.client.get(f"/v1/records/{rec['record_id']}", headers=ctx.as_user(MEMBER))
    check(r, "GET", "/v1/records/{record_id}")
    assert r.json()["evidence_damaged"] is True
