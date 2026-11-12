"""기간 보고서 만들기의 사업 범위·기간 검사 (계약 점검 S30·S31, 10번 요구사항 3.2·3.3·4.1).

- S30: 기간 보고서의 관측값 지표(OBSERVATION_STAT)가 같은 기관의 다른 사업 장비 관측값까지 세고, 제출 뒤 관리기관
  열람자에게 그 장비 ID가 보였다. 이제 그 사업의 회차로 보낸 관측값, 그 사업에 배정된 장비의 관측값, 그 사업 회차의
  장비가 회차 시간 안에 보낸 관측값만 센다 (ADR-0079). '데이터 없음' 판단(has_any_data)도 같은 범위다.
- S31: 한국 시각으로 바꾸면 9999-12-31을 넘는 from·to(9999-12-31T15:00:00Z 이후)가 500이었다. 400 VALIDATION_ERROR다.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from app.db.models import Device, Project, Report
from app.seed import DEV_A_SENSOR, ORG_A, PRJ_A, SEED_AT, SES_A, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
ADMIN, MEMBER, VIEWER, OP = 101, 102, 105, 106
PRJ_X = fid("prj", 288)  # 같은 기관(ORG_A)의 다른 사업
DEV_X = fid("dev", 388)  # PRJ_X 장비
DEV_FREE = fid("dev", 389)  # 사업이 정해지지 않은 장비
REPORTS = "/v1/reports"


def _other_project(ctx: Ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        db.add(Project(project_id=PRJ_X, organization_id=ORG_A, name="A 다른 사업", status="ACTIVE",
                       created_at=SEED_AT))
        db.flush()
        for device_id, project_id, name in ((DEV_X, PRJ_X, "다른 사업 센서"), (DEV_FREE, None, "사업 없는 센서")):
            db.add(Device(device_id=device_id, organization_id=ORG_A, project_id=project_id, name=name,
                          kind="SOURCE", device_type="SENSOR", collector_device_id=None, status="ACTIVE",
                          collection_mode="C3_APPROVED_FULL", local_copy_policy="RETAINED",
                          local_retention_days=None, expected_interval_s=None, source_timezone=None,
                          last_heartbeat_at=None, created_at=SEED_AT))


def _observe(ctx: Ctx, device_id: str, value: float, at: str, session_id: str | None = None) -> None:
    body: dict[str, Any] = {"schema_version": "1.0.0", "source_system_id": device_id, "observations": [
        {"metric": "sea_water_temperature", "observed_at": at, "value": value, "unit": "degC",
         "quality_flag": "GOOD"}]}
    if session_id:
        body["session_id"] = session_id
    r = ctx.client.post("/v1/observations", headers=ctx.as_user(MEMBER), json=body)
    assert r.status_code == 200 and r.json()["accepted"] == 1, r.text


def _period_template(ctx: Ctx, code: str, sections: list[dict[str, Any]]) -> None:
    r = ctx.client.post("/v1/report-templates", headers=ctx.as_user(OP),
                        json={"code": code, "name": "기간 보고서", "scope": "PERIOD", "sections": sections})
    assert r.status_code == 201, r.text


def _period_report(ctx: Ctx, code: str, project_id: str, start: str, end: str) -> Any:
    return ctx.client.post(REPORTS, headers=ctx.as_user(MEMBER),
                           json={"template_code": code, "project_id": project_id, "from": start, "to": end})


def _metric(report: dict[str, Any], code: str) -> dict[str, Any]:
    return next(m for m in report["metrics"] if m["metric_code"] == code)


def _ranges(metric: dict[str, Any]) -> list[tuple[str, int]]:
    return sorted((g["source_system_id"], g["count"]) for g in metric["sources"]["observation_ranges"])


METRICS = [{"kind": "METRICS", "title": "지표", "metrics": ["EXAMPLE_MEAN_WATER_TEMP", "EXAMPLE_VESSEL_EVENTS"]}]
MARCH = ("2025-03-01T00:00:00Z", "2025-04-01T00:00:00Z")  # 시드 자료와 겹치지 않는 달


def test_period_report_counts_only_the_projects_observations(ictx: Ctx) -> None:
    ctx = ictx
    _other_project(ctx)
    _observe(ctx, DEV_A_SENSOR, 10.0, "2025-03-10T00:00:00Z")  # 이 사업 장비
    _observe(ctx, DEV_X, 30.0, "2025-03-11T00:00:00Z")  # 다른 사업 장비 — 세면 안 된다
    _observe(ctx, DEV_FREE, 50.0, "2025-03-12T00:00:00Z")  # 사업 없는 장비, 회차 표시 없음 — 세면 안 된다
    _observe(ctx, DEV_FREE, 20.0, "2025-03-13T00:00:00Z", session_id=SES_A)  # 이 사업 회차로 보냄 — 센다
    _period_template(ctx, "SCOPE_PERIOD", METRICS)

    r = _period_report(ctx, "SCOPE_PERIOD", PRJ_A, *MARCH)
    assert r.status_code == 201, r.text
    check(r, "POST", REPORTS)
    temp = _metric(r.json(), "EXAMPLE_MEAN_WATER_TEMP")
    # 수정 전: 기관 전체 네 건의 평균 27.5, 원천에 다른 사업 장비(DEV_X)·사업 없는 장비까지
    assert (temp["status"], temp["value"], temp["input_count"]) == ("OK", 15.0, 2), temp
    assert _ranges(temp) == sorted([(DEV_A_SENSOR, 1), (DEV_FREE, 1)])

    other = _period_report(ctx, "SCOPE_PERIOD", PRJ_X, *MARCH)
    assert other.status_code == 201, other.text
    temp_x = _metric(other.json(), "EXAMPLE_MEAN_WATER_TEMP")
    assert (temp_x["value"], temp_x["input_count"], _ranges(temp_x)) == (30.0, 1, [(DEV_X, 1)])

    # 제출 뒤 관리기관 열람자(PRJ_A만 배정)가 보는 지표에도 다른 사업 장비가 없다
    rid = r.json()["report_id"]
    assert ctx.client.post(f"{REPORTS}/{rid}/approve", headers=ctx.as_user(ADMIN)).status_code == 200
    assert ctx.client.post(f"{REPORTS}/{rid}/submit", headers=ctx.as_user(ADMIN)).status_code == 200
    seen = ctx.client.get(f"{REPORTS}/{rid}", headers=ctx.as_user(VIEWER))
    assert seen.status_code == 200, seen.text
    check(seen, "GET", "/v1/reports/{report_id}")
    seen_temp = _metric(seen.json(), "EXAMPLE_MEAN_WATER_TEMP")
    assert seen_temp["value"] == 15.0
    assert DEV_X not in {source for source, _ in _ranges(seen_temp)}


def _session(ctx: Ctx, label: str, starts_at: str, ends_at: str | None, device_ids: list[str]) -> str:
    r = ctx.client.post("/v1/sessions", headers=ctx.as_user(MEMBER), json={
        "project_id": PRJ_A, "label": label, "title": f"회차 {label}", "starts_at": starts_at, "ends_at": ends_at,
        "device_ids": device_ids})
    assert r.status_code == 201, r.text
    return str(r.json()["session_id"])


def test_period_report_counts_session_device_observations_inside_the_session_window(ctx: Ctx) -> None:
    """회차 장비(사업 없는 장비·다른 사업 장비)가 회차 표시 없이 회차 시간 안에 보낸 값은 회차 보고서에 들어가므로
    같은 기간의 사업 보고서에도 들어간다. 회차 시간 밖의 값은 들어가지 않는다."""
    _other_project(ctx)
    closed = _session(ctx, "SCOPE-CLOSED", "2025-03-05T00:00:00Z", "2025-03-10T00:00:00Z", [DEV_FREE, DEV_X])
    _session(ctx, "SCOPE-OPEN", "2025-03-20T00:00:00Z", None, [DEV_FREE])  # 끝이 없는 회차
    _observe(ctx, DEV_FREE, 12.0, "2025-03-06T00:00:00Z")  # 닫힌 회차 안 — 센다
    _observe(ctx, DEV_X, 18.0, "2025-03-07T00:00:00Z")  # 다른 사업 장비, 닫힌 회차 안 — 센다
    _observe(ctx, DEV_FREE, 77.0, "2025-03-10T00:00:00Z")  # 닫힌 회차의 끝 시각(끝은 포함하지 않음) — 세지 않는다
    _observe(ctx, DEV_FREE, 99.0, "2025-03-15T00:00:00Z")  # 어느 회차 시간도 아님 — 세지 않는다
    _observe(ctx, DEV_FREE, 30.0, "2025-03-25T00:00:00Z")  # 끝이 없는 회차 안 — 센다
    _period_template(ctx, "SCOPE_SESSDEV", METRICS)

    r = _period_report(ctx, "SCOPE_SESSDEV", PRJ_A, *MARCH)
    assert r.status_code == 201, r.text
    check(r, "POST", REPORTS)
    temp = _metric(r.json(), "EXAMPLE_MEAN_WATER_TEMP")
    # 고치기 전(기관 전체): 다섯 건 평균 47.2. 장비·회차 조건만: 이 사업 자료 없음(NO_DATA)
    assert (temp["status"], temp["value"], temp["input_count"]) == ("OK", 20.0, 3), temp
    assert _ranges(temp) == sorted([(DEV_FREE, 2), (DEV_X, 1)])
    # 이 사업 관측값이 있으니 이벤트 수는 '데이터 없음'이 아니라 0건이다 (요구사항 3.3)
    vessels = _metric(r.json(), "EXAMPLE_VESSEL_EVENTS")
    assert (vessels["status"], vessels["value"]) == ("OK", 0)

    # 같은 값이 닫힌 회차의 회차 보고서에도 들어간다 — 회차 보고서의 원천은 기간 보고서 원천 안에 있다
    rs = ctx.client.post(REPORTS, headers=ctx.as_user(MEMBER),
                         json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": closed})
    assert rs.status_code == 201, rs.text
    temp_s = _metric(rs.json(), "EXAMPLE_MEAN_WATER_TEMP")
    assert (temp_s["value"], temp_s["input_count"], _ranges(temp_s)) == (15.0, 2, sorted([(DEV_FREE, 1), (DEV_X, 1)]))

    # 다른 사업(PRJ_X) 보고서는 자기 장비 값만 — PRJ_A 회차에 붙은 사업 없는 장비 값은 들어가지 않는다
    other = _period_report(ctx, "SCOPE_SESSDEV", PRJ_X, *MARCH)
    assert other.status_code == 201, other.text
    temp_x = _metric(other.json(), "EXAMPLE_MEAN_WATER_TEMP")
    assert (temp_x["value"], temp_x["input_count"], _ranges(temp_x)) == (18.0, 1, [(DEV_X, 1)])


def test_period_report_with_only_other_project_observations_is_no_data(ctx: Ctx) -> None:
    _other_project(ctx)
    _observe(ctx, DEV_X, 30.0, "2025-04-10T00:00:00Z")
    _period_template(ctx, "SCOPE_EMPTY", METRICS)

    r = _period_report(ctx, "SCOPE_EMPTY", PRJ_A, "2025-04-01T00:00:00Z", "2025-05-01T00:00:00Z")
    assert r.status_code == 201, r.text
    check(r, "POST", REPORTS)
    temp = _metric(r.json(), "EXAMPLE_MEAN_WATER_TEMP")
    # 수정 전: 다른 사업 장비 값 30.0이 이 사업의 평균 수온이 됐다
    assert (temp["status"], temp["value"], temp["input_count"], _ranges(temp)) == ("NO_DATA", None, 0, [])
    # 0건과 '기록 없음'의 구분(요구사항 3.3)도 이 사업 자료로만 한다. 수정 전에는 다른 사업 관측값 때문에 0건(OK)
    vessels = _metric(r.json(), "EXAMPLE_VESSEL_EVENTS")
    assert (vessels["status"], vessels["value"]) == ("NO_DATA", None)


# ------------------------------------------------------------------ S31: 한국 시각으로 쓸 수 없는 기간


TEXT = [{"kind": "TEXT", "title": "기간", "text": "{{period.from}} ~ {{period.to}}"}]


@pytest.mark.parametrize(("start", "end", "pointers"), [
    ("9999-12-31T10:00:00Z", "9999-12-31T15:00:00Z", ["/to"]),
    ("9999-12-31T14:00:00Z", "9999-12-31T23:59:59Z", ["/to"]),
    ("9999-12-31T15:00:00Z", "9999-12-31T16:00:00Z", ["/from", "/to"]),
])
def test_period_report_rejects_bounds_past_the_last_kst_date(ctx: Ctx, start: str, end: str,
                                                             pointers: list[str]) -> None:
    _period_template(ctx, "KST_EDGE", TEXT)
    r = _period_report(ctx, "KST_EDGE", PRJ_A, start, end)
    assert r.status_code == 400, r.text  # 수정 전: 한국 시각 변환의 OverflowError로 500
    check(r, "POST", REPORTS)
    assert r.json()["code"] == "VALIDATION_ERROR"
    assert [e["pointer"] for e in r.json()["errors"]] == pointers
    assert "REPORT_CREATED" not in ctx.audit_actions()
    with ctx.c.sessionmaker() as db:
        assert db.scalar(select(func.count()).select_from(Report).where(Report.template_code == "KST_EDGE")) == 0


def test_period_report_accepts_the_last_kst_representable_instant(ctx: Ctx) -> None:
    _period_template(ctx, "KST_EDGE", TEXT)
    r = _period_report(ctx, "KST_EDGE", PRJ_A, "9999-12-31T10:00:00Z", "9999-12-31T14:59:59Z")
    assert r.status_code == 201, r.text
    check(r, "POST", REPORTS)
    assert r.json()["period_to"] == "9999-12-31T14:59:59Z"
    assert ctx.audit_actions().count("REPORT_CREATED") == 1
