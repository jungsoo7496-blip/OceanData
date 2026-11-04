"""T07 수집 정상성 (docs/ACCEPTANCE.md): 에이전트 중지 → 기준 시간 안에 경보·장애 기록, 실증 예정일 무수집,
관측값 수신 지연·시계 오차, 기업 화면에 원인·조치 안내.

시간이 흐른 상황은 고정 시계(FixedClock)로 감시 평가만 앞당겨 만든다. 하트비트·관측값 저장은 실제 시각.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.adapters.system_clock import FixedClock
from app.db.models import Device
from app.seed import DEV_A_COLLECTOR, DEV_A_SENSOR, ORG_A, ORG_B, PRJ_A
from app.services import monitoring
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_agents import _device_headers, hb

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]
FREE = 50 * 1024**3  # 현장 저장 여유 기준(10GiB) 위


def run_watch(ctx, at: datetime) -> monitoring.EvalResult:
    c = dataclasses.replace(ctx.c, clock=FixedClock(at))
    with c.sessionmaker.begin() as db:
        return monitoring.evaluate(c, db)


def sent(ctx, kind: str, rule: str) -> list:
    return [n for n in ctx.c.notifier.sent if n.kind == kind and n.data.get("rule_code") == rule]


def baseline(ctx, at: datetime) -> None:
    """시드의 진행 중 회차(장비 지정, 자료 없음)가 이미 SESSION_NO_DATA를 일으키므로 먼저 한 번 평가해 둔다."""
    run_watch(ctx, at)


def assert_clean_body(n) -> None:  # 요구사항 3.4: 토큰·URL·개인정보 없음
    for bad in ("http", "@", "Bearer", "token", "X-Amz"):
        assert bad not in n.body and bad not in n.subject, bad


@pytest.mark.covers("T07-a", "T07-d")
def test_t07a_agent_stop_raises_alert_and_incident(ctx) -> None:
    dh = _device_headers(ctx)
    assert ctx.client.post("/v1/agents/heartbeat", json=hb(disk_free_bytes=FREE), headers=dh).status_code == 204
    now = datetime.now(UTC)
    baseline(ctx, now)
    assert run_watch(ctx, now + timedelta(minutes=1)).opened == []  # 살아 있음

    # 에이전트 중지: 기본 기준(하트비트 주기 60초 × 3, 최소 300초)을 넘긴 6분 뒤 평가
    res = run_watch(ctx, now + timedelta(minutes=6))
    assert len(res.opened) == 1 and res.opened_rules == ["HEARTBEAT_MISSING"]
    opened = sent(ctx, "watch.opened", "HEARTBEAT_MISSING")
    assert len(opened) == 1
    assert opened[0].recipients == ("admin-a@example.invalid", "operator2@example.invalid",
                                    "operator@example.invalid")
    assert_clean_body(opened[0])
    assert "marine-agent status" in opened[0].body  # 조치 안내

    # 같은 원인이 이어지면 재알림 간격(3600초) 전에는 다시 보내지 않는다
    assert run_watch(ctx, now + timedelta(minutes=30)).reminded == []
    assert len(sent(ctx, "watch.reminder", "HEARTBEAT_MISSING")) == 0
    run_watch(ctx, now + timedelta(minutes=67))
    assert len(sent(ctx, "watch.reminder", "HEARTBEAT_MISSING")) == 1

    # T07-d: 기업 화면(수집 상태·장애 목록)에 원인과 조치 안내가 보인다
    r = ctx.client.get("/v1/ingest-status", headers=ctx.as_user(102))
    check(r, "GET", "/v1/ingest-status")
    body = r.json()
    inc = next(i for i in body["open_incidents"] if i["rule_code"] == "HEARTBEAT_MISSING")
    assert (inc["rule_code"], inc["device_id"], inc["status"]) == ("HEARTBEAT_MISSING", DEV_A_COLLECTOR, "OPEN")
    assert "marine-agent" in inc["guidance"]
    dev = next(d for d in body["devices"] if d["device_id"] == DEV_A_COLLECTOR)
    assert dev["open_alerts"] == 1
    # 다른 기관은 볼 수 없다 (T01)
    r = ctx.client.get("/v1/ingest-status", headers=ctx.as_user(103))
    assert {i["organization_id"] for i in r.json()["open_incidents"]} <= {ORG_B}
    assert DEV_A_COLLECTOR not in [d["device_id"] for d in r.json()["devices"]]

    # 확인(ACK)하면 재알림하지 않는다
    r = ctx.client.patch(f"/v1/incidents/{inc['incident_id']}", json={"status": "ACKNOWLEDGED", "assignee": "김담당"},
                         headers=ctx.as_user(101))
    check(r, "PATCH", "/v1/incidents/{incident_id}")
    assert r.json()["status"] == "ACKNOWLEDGED"
    run_watch(ctx, now + timedelta(minutes=140))
    assert len(sent(ctx, "watch.reminder", "HEARTBEAT_MISSING")) == 1

    # 에이전트가 돌아오면 해제 알림 + 장애 자동 해결
    assert ctx.client.post("/v1/agents/heartbeat", json=hb(disk_free_bytes=FREE), headers=dh).status_code == 204
    res = run_watch(ctx, datetime.now(UTC) + timedelta(seconds=30))
    assert inc["incident_id"] in res.resolved
    resolved = sent(ctx, "watch.resolved", "HEARTBEAT_MISSING")
    assert len(resolved) == 1
    assert_clean_body(resolved[0])
    r = ctx.client.get("/v1/incidents", params={"device_id": DEV_A_COLLECTOR}, headers=ctx.as_user(101))
    check(r, "GET", "/v1/incidents")
    item = r.json()["items"][0]
    assert (item["status"], item["assignee"]) == ("RESOLVED", "김담당") and "자동 해제" in item["memo"]


@pytest.mark.covers("T07-b")
def test_t07b_scheduled_session_without_data(ctx) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    r = ctx.client.post("/v1/sessions", headers=ctx.as_user(101), json={
        "project_id": PRJ_A, "label": "A-T07B", "title": "무수집 시험",
        "starts_at": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"), "device_ids": [DEV_A_SENSOR]})
    assert r.status_code == 201, r.text
    session_id = r.json()["session_id"]
    run_watch(ctx, now)
    r = ctx.client.get("/v1/incidents", params={"status": "OPEN"}, headers=ctx.as_user(102))
    check(r, "GET", "/v1/incidents")
    inc = next(i for i in r.json()["items"] if i["session_id"] == session_id)
    assert inc["rule_code"] == "SESSION_NO_DATA"
    assert inc["session_id"] == session_id and "A-T07B" in inc["summary"]

    # 관측값이 들어오면 해제
    item = {"metric": "sea_water_temperature", "observed_at": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "value": 18.0, "unit": "degC", "quality_flag": "GOOD"}
    r = ctx.client.post("/v1/observations", headers=ctx.as_user(102), json={
        "schema_version": "1.0.0", "source_system_id": DEV_A_SENSOR, "session_id": session_id,
        "observations": [item]})
    assert r.status_code in (200, 207), r.text
    res = run_watch(ctx, now + timedelta(seconds=10))
    assert inc["incident_id"] in res.resolved


@pytest.mark.covers("T07-c")
def test_t07c_observation_lag_and_clock_offset(ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        db.execute(update(Device).where(Device.device_id == DEV_A_SENSOR).values(expected_interval_s=60))
    now = datetime.now(UTC).replace(microsecond=0)
    baseline(ctx, now)
    item = {"metric": "sea_water_temperature", "observed_at": (now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "value": 18.0, "unit": "degC", "quality_flag": "GOOD"}
    r = ctx.client.post("/v1/observations", headers=ctx.as_user(102), json={
        "schema_version": "1.0.0", "source_system_id": DEV_A_SENSOR, "clock_offset_ms": 5000, "observations": [item]})
    assert r.status_code in (200, 207), r.text
    # 수집기도 시계 오차·저장 여유 부족을 보고
    dh = _device_headers(ctx)
    assert ctx.client.post("/v1/agents/heartbeat", json=hb(clock_offset_ms=-3500, disk_free_bytes=2 * 1024**3),
                           headers=dh).status_code == 204

    res = run_watch(ctx, now + timedelta(seconds=60))  # 아직 기대 주기 3배 안
    assert sorted(res.opened_rules) == ["CLOCK_OFFSET", "CLOCK_OFFSET", "LOCAL_DISK_LOW"]
    res = run_watch(ctx, now + timedelta(minutes=4))  # 관측 3분 넘게 없음 (하트비트는 아직 기준 안)
    assert res.opened_rules == ["OBSERVATION_LAG"]
    h = ctx.client.get(f"/v1/agents/{DEV_A_COLLECTOR}/health", headers=ctx.as_user(102))
    check(h, "GET", "/v1/agents/{device_id}/health")
    assert (h.json()["clock_offset_ms"], h.json()["disk_free_bytes"]) == (-3500, 2 * 1024**3)
    r = ctx.client.get("/v1/ingest-status", headers=ctx.as_user(102))
    sensor = next(d for d in r.json()["devices"] if d["device_id"] == DEV_A_SENSOR)
    assert sensor["kind"] == "SOURCE" and sensor["clock_offset_ms"] == 5000 and sensor["last_observation_at"]
    assert sensor["organization_id"] == ORG_A
