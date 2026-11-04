"""T17 환경 정보 결합 (docs/ACCEPTANCE.md): 모의 Open API 소스 → 주기 수집·보충 → 관측소 장비·관측값 →
자산 env_context(거리·시각 차이) → 형식 변경 격리·경보 → 재배포 제한 → 환경 조건 검색·보고서 변수.
실제 기관 API 명세는 확인 후 어댑터로 붙인다 (지금은 모의 어댑터만).
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from app.adapters.system_clock import FixedClock
from app.db.models import Asset, Device, ExternalSource, Incident
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A, SES_A
from app.services import external, monitoring
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]
OP = 106
NOW = datetime.now(UTC).replace(second=0, microsecond=0)
SOURCE = {
    "source_id": "mock-ocean", "name": "모의 해양 관측", "adapter_kind": "MOCK", "interval_s": 600,
    "stations": [{"code": "S1", "name": "시험 부이 1", "lat": 34.70, "lon": 126.50},
                 {"code": "S2", "name": "시험 부이 2", "lat": 35.50, "lon": 129.40}],
    "field_mapping": {"station_path": "stn", "time_path": "tm", "metrics": {
        "significant_wave_height": {"path": "wh", "unit": "m"}, "wind_speed": {"path": "ws_kn", "unit": "kn"},
        "sea_water_temperature": {"path": "wt", "unit": "degC"}}},
    "attribution": "모의 해양 관측(시험용)", "redistribution_allowed": False,
}


def run_jobs(ctx) -> list[dict]:
    out = []
    while (job := ctx.c.queue.claim(["collect_source"], "t", ("CPU",))) is not None:
        out.append(external.collect(ctx.c, job.payload["source_id"], datetime.fromisoformat(job.payload["since"]),
                                    datetime.fromisoformat(job.payload["until"])))
        ctx.c.queue.complete(job.job_id)
    return out


def asset_at(ctx, captured: datetime | None, lon: float = 126.52, lat: float = 34.71) -> str:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, session=SES_A)
        a.capture_start_at = captured
        a.location = {"type": "Point", "coordinates": [lon, lat]}
        a.location_precision = "EXACT_RESTRICTED"
        a.checksum_sha256 = hashlib.sha256(a.asset_id.encode()).hexdigest()
        if captured is not None:  # 촬영시각이 있으면 근거도 (asset-metadata 불변식)
            a.timestamp_quality = {"source": "DEVICE", "verified": True, "est_offset_ms": 20}
        db.add(a)
        return a.asset_id


@pytest.mark.covers("T17-a", "T17-b", "T17-c")
def test_t17_collect_join_backfill_and_alert(ictx) -> None:
    ctx = ictx
    assert ctx.client.post("/v1/external-sources", json=SOURCE, headers=ctx.as_user(101)).status_code == 403
    assert ctx.client.post("/v1/external-sources", json={**SOURCE, "adapter_kind": "KMA_REST"},
                           headers=ctx.as_user(OP)).status_code == 400  # 명세 미확인 어댑터 없음
    bad = {**SOURCE, "field_mapping": {**SOURCE["field_mapping"], "metrics": {"wave_power": {"path": "x", "unit": "m"}}}}
    assert ctx.client.post("/v1/external-sources", json=bad, headers=ctx.as_user(OP)).status_code == 400
    r = ctx.client.post("/v1/external-sources", json=SOURCE, headers=ctx.as_user(OP))
    check(r, "POST", "/v1/external-sources")
    assert r.json()["spec_status"] == "NEEDS_CONFIRMATION"

    # 주기 수집 (요구사항 2.1, 4): 관측소마다 EXTERNAL_STATION 장비, 원 응답 참조, 단위 정규화
    c = dataclasses.replace(ctx.c, clock=FixedClock(NOW))
    with c.sessionmaker.begin() as db:
        db.execute(update(ExternalSource).values(collected_until=NOW - timedelta(hours=1)))
        assert external.schedule(c, db) == ["mock-ocean"]
    out = run_jobs(ctx)
    assert out[0]["status"] == "OK" and out[0]["stored"] == 6 * 2 * 3  # 1시간·10분 간격·2개소·3항목
    with ctx.c.engine.connect() as conn:
        row = conn.execute(text("SELECT value, unit, original_value, original_unit, source_ref FROM observations "
                                "WHERE metric='wind_speed' LIMIT 1")).one()
    assert row.unit == "m/s" and row.original_unit == "kn" and abs(row.value - row.original_value * 0.514444) < 0.01
    assert row.source_ref.startswith("external/mock-ocean/")
    with ctx.c.sessionmaker() as db:
        devs = list(db.execute(select(Device).where(Device.device_type == "EXTERNAL_STATION")).scalars())
        assert len(devs) == 2

    # 결합 (요구사항 5.1·5.2): 반경 안 가장 가까운 관측소, 거리·시각 차이. 촬영시각 미상은 결합 안 함
    near = asset_at(ctx, NOW - timedelta(minutes=33))
    unknown = asset_at(ctx, None)
    with ctx.c.sessionmaker.begin() as db:
        env = external.join_env(ctx.c, db, db.get(Asset, near))
        assert external.join_env(ctx.c, db, db.get(Asset, unknown)) is None
        db.get(Asset, near).env_context = env
    wave = next(o for o in env["observations"] if o["metric"] == "significant_wave_height")
    assert wave["station_name"] == "시험 부이 1" and 1.0 < wave["distance_km"] < 3.0 and wave["time_diff_s"] <= 300
    assert wave["attribution"] == "모의 해양 관측(시험용)"

    # T17-b: 시간·해역이 맞지 않으면 결합하지 않고 경고 (이유·가장 가까운 관측소/관측)
    far = asset_at(ctx, NOW - timedelta(minutes=33), lon=124.5, lat=33.5)  # 반경 밖 (모든 관측소에서 200km 넘게)
    old = asset_at(ctx, NOW - timedelta(hours=3))  # 시간 창 밖 (아직 수집 전 구간)
    with ctx.c.sessionmaker.begin() as db:
        for aid, code in ((far, "NO_STATION_IN_RADIUS"), (old, "NO_OBSERVATION_IN_WINDOW")):
            env = external.join_env(ctx.c, db, db.get(Asset, aid))
            assert env["observations"] == [] and [w["code"] for w in env["warnings"]] == [code], env
            assert env["warnings"][0]["nearest"]["station_name"].startswith("시험 부이")
            db.get(Asset, aid).env_context = env
        assert env["warnings"][0]["nearest"]["time_diff_s"] > 2 * 3600 - 600
    r = ctx.client.get(f"/v1/assets/{old}", headers=ctx.as_user(102))
    check(r, "GET", "/v1/assets/{asset_id}")
    assert r.json()["env_context"]["warnings"][0]["code"] == "NO_OBSERVATION_IN_WINDOW"
    r = ctx.client.get("/v1/assets", params={"env_metric": "significant_wave_height", "env_min": 0},
                       headers=ctx.as_user(102))
    assert old not in r.text and far not in r.text  # 경고만 있는 자산은 환경 조건 검색에 걸리지 않는다

    # 보충 수집 (요구사항 2.2, 5.3): 수집 전 구간 자산은 처음엔 결합 없음(경고) → 보충 후 갱신
    r = ctx.client.post("/v1/external-sources/mock-ocean/backfill", headers=ctx.as_user(OP),
                        json={"from": (NOW - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "to": (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    check(r, "POST", "/v1/external-sources/{source_id}/backfill")
    assert r.json()["jobs"] == 1
    run_jobs(ctx)
    with ctx.c.sessionmaker() as db:
        assert db.get(Asset, old).env_context["observations"][0]["station_name"] == "시험 부이 1"
        assert "warnings" not in db.get(Asset, old).env_context  # 보충 뒤 결합되면 경고는 사라진다

    # 재배포 제한 (요구사항 6.1): 소유 기관은 보지만 다른 기관 주체에게는 빠진다
    own = ctx.client.get(f"/v1/assets/{near}", headers=ctx.as_user(102)).json()
    assert own["env_context"]["observations"]
    other = ctx.client.get(f"/v1/assets/{near}", headers=ctx.as_user(OP))
    assert other.status_code == 200 and other.json().get("env_context") is None
    # 환경 조건 검색 (요구사항 7.2)
    r = ctx.client.get("/v1/assets", params={"env_metric": "significant_wave_height", "env_min": 0},
                       headers=ctx.as_user(102))
    check(r, "GET", "/v1/assets")
    assert {near, old} <= {a["asset_id"] for a in r.json()["items"]}
    assert ctx.client.get("/v1/assets", params={"env_metric": "significant_wave_height", "env_min": 99},
                          headers=ctx.as_user(102)).json()["items"] == []
    # 보고서 변수 (요구사항 7.1, 6.2 출처 표기)
    tpl = {"code": "ENV_EXAMPLE", "name": "환경 (예시)", "sections": [{"kind": "FIELDS", "title": "환경", "fields": [
        {"key": "env", "label": "최대 파고", "auto": "{{env.significant_wave_height.max}} m · 출처 {{env.attribution}}"}]}]}
    assert ctx.client.post("/v1/report-templates", json=tpl, headers=ctx.as_user(OP)).status_code == 201
    rep = ctx.client.post("/v1/reports", json={"template_code": "ENV_EXAMPLE", "session_id": SES_A},
                          headers=ctx.as_user(102)).json()
    assert rep["fields"][0]["auto_value"].endswith("m · 출처 모의 해양 관측(시험용)")
    assert "(값 없음)" not in rep["fields"][0]["auto_value"]

    # 응답 형식 변경 (요구사항 3.1): 원 응답 격리 + 11번 경보, 고쳐지면 해제
    with ctx.c.sessionmaker.begin() as db:
        db.execute(update(ExternalSource).values(options={"simulate_format_change": True}))
    res = external.collect(ctx.c, "mock-ocean", NOW - timedelta(minutes=10), NOW)
    assert res["status"] == "FORMAT_ERROR"
    assert ctx.c.storage.get_object(ctx.c.settings.bucket_quarantine, res["quarantined"]).startswith(b'{"items"')
    with ctx.c.sessionmaker.begin() as db:
        opened = monitoring.evaluate(ctx.c, db)
    assert "OPENAPI_FAILURE" in opened.opened_rules
    with ctx.c.sessionmaker.begin() as db:
        db.execute(update(ExternalSource).values(options={}))
    assert external.collect(ctx.c, "mock-ocean", NOW - timedelta(minutes=10), NOW)["status"] == "OK"
    with ctx.c.sessionmaker.begin() as db:
        monitoring.evaluate(ctx.c, db)
        inc = db.execute(select(Incident).where(Incident.rule_code == "OPENAPI_FAILURE")).scalar_one()
        assert inc.status == "RESOLVED"
    r = ctx.client.get("/v1/external-sources", headers=ctx.as_user(OP))
    check(r, "GET", "/v1/external-sources")
    assert r.json()["items"][0]["consecutive_failures"] == 0
    r = ctx.client.patch("/v1/external-sources/mock-ocean", json={"enabled": False}, headers=ctx.as_user(OP))
    check(r, "PATCH", "/v1/external-sources/{source_id}")
