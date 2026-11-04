"""10번 통합 시험: 지표 정의·보고서 양식 등록(운영자·버전), 회차 기록 목록, 기간 보고서, 교차 기관 차단."""

from __future__ import annotations

import pytest

from app.seed import ORG_B, PRJ_A, SES_A, SES_B
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
OP = 106


def test_metric_definitions_and_templates(ctx) -> None:
    h = ctx.as_user(OP)
    body = {"code": "EXAMPLE_VESSEL_EVENTS", "name": "선박 이벤트 수 v2", "unit": "건", "kind": "EVENT_COUNT",
            "params": {"event_codes": ["VESSEL", "PERSON"]}}
    assert ctx.client.post("/v1/metric-definitions", json=body, headers=ctx.as_user(101)).status_code == 403
    r = ctx.client.post("/v1/metric-definitions", json=body, headers=h)
    check(r, "POST", "/v1/metric-definitions")
    assert r.json()["version"] == 2 and "PERSON" in r.json()["formula"]
    for bad in ({**body, "params": {}}, {**body, "kind": "MAGIC"}, {**body, "code": "lower"},
                {**body, "kind": "OBSERVATION_STAT", "params": {"metric": "x", "stat": "median"}}):
        assert ctx.client.post("/v1/metric-definitions", json=bad, headers=h).status_code == 400, bad
    r = ctx.client.get("/v1/metric-definitions", headers=ctx.as_user(105))
    check(r, "GET", "/v1/metric-definitions")
    assert [d["version"] for d in r.json()["items"] if d["code"] == "EXAMPLE_VESSEL_EVENTS"] == [1, 2]

    tpl = {"code": "EXAMPLE_PERIOD", "name": "월간 (예시)", "scope": "PERIOD",
           "sections": [{"kind": "TEXT", "title": "개요", "text": "{{project.name}} {{period.from}}~{{period.to}}"},
                        {"kind": "METRICS", "title": "지표", "metrics": ["EXAMPLE_VIDEO_HOURS"]}]}
    bad = {**tpl, "sections": [{"kind": "METRICS", "title": "x", "metrics": ["NOT_REGISTERED"]}]}
    assert ctx.client.post("/v1/report-templates", json=bad, headers=h).status_code == 400
    r = ctx.client.post("/v1/report-templates", json=tpl, headers=h)
    check(r, "POST", "/v1/report-templates")
    assert (r.json()["version"], r.json()["has_hwpx"]) == (1, False)
    r = ctx.client.get("/v1/report-templates", headers=ctx.as_user(102))
    check(r, "GET", "/v1/report-templates")

    # 기간 보고서: project·from·to 필요, 자료 없음이면 '데이터 없음'
    hm = ctx.as_user(102)
    assert ctx.client.post("/v1/reports", json={"template_code": "EXAMPLE_PERIOD", "project_id": PRJ_A},
                           headers=hm).status_code == 400
    r = ctx.client.post("/v1/reports", headers=hm, json={"template_code": "EXAMPLE_PERIOD", "project_id": PRJ_A,
                                                         "from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"})
    check(r, "POST", "/v1/reports")
    assert r.json()["session_id"] is None and r.json()["metrics"][0]["status"] == "NO_DATA"
    r = ctx.client.get("/v1/reports", headers=hm)
    check(r, "GET", "/v1/reports")
    assert len(r.json()["items"]) == 1
    # 운영자는 읽기만 (보고서 변경은 제공기관)
    rid = r.json()["items"][0]["report_id"]
    assert ctx.client.get(f"/v1/reports/{rid}", headers=h).status_code == 200
    assert ctx.client.post(f"/v1/reports/{rid}/versions", headers=h).status_code == 403
    # 내보내기 형식이 꺼져 있으면 409 (REPORT_FORMATS 기본 비어 있음)
    assert ctx.client.get(f"/v1/reports/{rid}/export", params={"format": "XLSX"}, headers=hm).status_code == 409


def test_records_list_and_cross_org(ctx) -> None:
    rb = ctx.client.post(f"/v1/sessions/{SES_B}/records", headers=ctx.as_user(104),
                         json={"kind": "NOTE", "title": "B 기록"})
    assert rb.status_code == 201, rb.text
    ra = ctx.client.post(f"/v1/sessions/{SES_A}/records", headers=ctx.as_user(102),
                         json={"kind": "LOG", "title": "A 일지"}, )
    r = ctx.client.get(f"/v1/sessions/{SES_A}/records", headers=ctx.as_user(102))
    check(r, "GET", "/v1/sessions/{session_id}/records")
    assert [x["record_id"] for x in r.json()["items"]] == [ra.json()["record_id"]]
    # 다른 기관 기록·회차는 404 + 감사, 다른 기관 회차에 기록 작성 불가
    b_id = rb.json()["record_id"]
    assert ctx.client.get(f"/v1/records/{b_id}", headers=ctx.as_user(102)).status_code == 404
    assert ctx.client.patch(f"/v1/records/{b_id}", json={"note": "x"}, headers=ctx.as_user(102)).status_code == 404
    assert ctx.client.post(f"/v1/records/{b_id}/approve", headers=ctx.as_user(101)).status_code == 404
    assert ctx.client.post(f"/v1/sessions/{SES_B}/records", json={"kind": "LOG", "title": "x"},
                           headers=ctx.as_user(102)).status_code == 404
    assert "CROSS_ORG_ACCESS_ATTEMPT" in ctx.audit_actions()
    # 관리기관 열람자는 회차 기록을 직접 읽지 않는다 (제출된 보고서로만)
    assert ctx.client.get(f"/v1/records/{ra.json()['record_id']}", headers=ctx.as_user(105)).status_code == 403
    assert ORG_B
