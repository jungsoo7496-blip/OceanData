"""07번 통합 시험: 관측값 배치(항목별 결과)·조회·집계·정정, 매핑 프로필, CSV 파싱 경로, 실증 이벤트."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, text

from app.db.models import OrgEventCode, SensorParseRun
from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_A_SENSOR, DEV_B_CAMERA, ORG_A, SES_A, SES_B
from app.workers import main as worker
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
T = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)


def z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def kst(dt: datetime) -> str:
    return (dt + timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def batch(items: list[dict], **kw) -> dict:
    return {"schema_version": "1.0.0", "source_system_id": DEV_A_SENSOR, "session_id": SES_A,
            "observations": items, **kw}


def test_batch_item_results_and_rules(ctx) -> None:
    h = ctx.as_user(102)
    items = [
        {"metric": "sea_water_temperature", "observed_at": kst(T), "value": 18.4, "unit": "°C", "quality_flag": "GOOD",
         "depth_m": 1.5},                                                                                    # 0 ACCEPTED
        {"metric": "sea_water_temperature", "observed_at": z(T + timedelta(minutes=10)), "value": None,
         "quality_flag": "MISSING", "depth_m": 1.5},                                                         # 1 결측
        {"metric": "wind_speed", "source_timestamp": (T + timedelta(hours=9)).strftime("%Y/%m/%d %H:%M:%S"),
         "value": 12.5, "unit": "kn", "quality_flag": "GOOD"},                                               # 2 장비 시간대
        {"metric": "significant_wave_height", "observed_at": z(T), "value": 0.8, "unit": "furlong",
         "quality_flag": "GOOD"},                                                                            # 3 SUSPECT
        {"metric": "sea_water_temperature", "observed_at": "2026-11-01T10:00:00", "value": 1, "unit": "degC",
         "quality_flag": "GOOD"},                                                                            # 4 시간대 없음
        {"metric": "sea_water_temperature", "observed_at": z(datetime.now(UTC) + timedelta(hours=1)), "value": 1,
         "unit": "degC", "quality_flag": "GOOD"},                                                            # 5 미래
        {"metric": "sea_water_temperature", "observed_at": kst(T), "value": 18.4, "unit": "degC", "quality_flag": "GOOD",
         "depth_m": 1.5},                                                                                    # 6 배치 내 중복
        {"metric": "no_such_metric", "observed_at": z(T), "value": 1, "unit": "m", "quality_flag": "GOOD"},  # 7
        {"metric": "sea_water_temperature", "observed_at": z(T), "value": 0, "quality_flag": "MISSING"},    # 8 결측 표기 위반
    ]
    r = ctx.client.post("/v1/observations", json=batch(items, clock_offset_ms=120), headers=h)
    check(r, "POST", "/v1/observations")
    res = {x["index"]: (x["status"], x.get("reason")) for x in r.json()["results"]}
    assert res == {0: ("ACCEPTED", None), 1: ("ACCEPTED", None), 2: ("ACCEPTED", None), 3: ("ACCEPTED", None),
                   4: ("REJECTED", "TIMEZONE_UNKNOWN"), 5: ("REJECTED", "FUTURE_TIMESTAMP"), 6: ("DUPLICATE", None),
                   7: ("REJECTED", "UNKNOWN_METRIC"), 8: ("REJECTED", "MISSING_FLAG_MISMATCH")}
    assert (r.json()["accepted"], r.json()["duplicates"], r.json()["rejected"]) == (4, 1, 4)
    # 같은 키·다른 값은 덮어쓰지 않는다 (요구사항 3.1)
    again = [{**items[0]}, {**items[0], "value": 19.9}]
    r2 = ctx.client.post("/v1/observations", json=batch([again[1]]), headers=h).json()
    assert r2["results"] == [{"index": 0, "status": "REJECTED", "reason": "VALUE_CONFLICT"}]
    assert ctx.client.post("/v1/observations", json=batch([again[0]]), headers=h).json()["duplicates"] == 1
    q = {"from": z(T - timedelta(hours=1)), "to": z(T + timedelta(hours=2))}
    lst = ctx.client.get("/v1/observations", params={**q, "metric": "sea_water_temperature"}, headers=h)
    check(lst, "GET", "/v1/observations")
    rows = lst.json()["items"]
    assert [x["value"] for x in rows] == [18.4, None]
    assert rows[0]["clock_offset_ms"] == 120 and rows[0]["original_unit"] == "°C" and rows[0]["unit"] == "degC"
    wind = ctx.client.get("/v1/observations", params={**q, "metric": "wind_speed"}, headers=h).json()["items"][0]
    assert wind["observed_at"] == z(T) and wind["value"] == pytest.approx(6.4304, rel=1e-3)
    assert wind["source_timestamp"].startswith((T + timedelta(hours=9)).strftime("%Y/%m/%d"))


def test_batch_errors_limit_scope_and_idempotency(ctx) -> None:
    h = ctx.as_user(102)
    assert ctx.client.post("/v1/observations", json={"observations": "x"}, headers=h).status_code == 400
    ctx.c.settings.observation_batch_max = 2
    try:
        big = batch([{"metric": "sea_water_temperature", "observed_at": z(T), "value": 1, "unit": "degC",
                      "quality_flag": "GOOD"}] * 3)
        big["observations"][0] = "not even an object"  # 한도 검사가 스키마보다 먼저
        assert ctx.client.post("/v1/observations", json=big, headers=h).json()["code"] == "PAYLOAD_TOO_LARGE"
    finally:
        ctx.c.settings.observation_batch_max = 5000
    other = {**batch([]), "source_system_id": DEV_B_CAMERA,
             "observations": [{"metric": "sea_water_temperature", "observed_at": z(T), "value": 1, "unit": "degC",
                               "quality_flag": "GOOD"}]}
    assert ctx.client.post("/v1/observations", json=other, headers=h).status_code == 404  # T01
    assert ctx.client.post("/v1/observations", json={**batch(other["observations"]), "session_id": SES_B},
                           headers=h).status_code == 404
    k = {**h, "Idempotency-Key": "obs-1"}
    b = batch([{"metric": "air_pressure", "observed_at": z(T), "value": 1012.3, "unit": "hPa", "quality_flag": "GOOD"}])
    first = ctx.client.post("/v1/observations", json=b, headers=k).json()
    again = ctx.client.post("/v1/observations", json=b, headers=k)
    assert again.headers.get("idempotent-replayed") == "true" and again.json() == first


def test_aggregation_and_correction(ctx) -> None:
    h = ctx.as_user(102)
    items = [{"metric": "sea_water_temperature", "observed_at": z(T + timedelta(minutes=10 * i)), "value": v,
              "unit": "degC", "quality_flag": "MISSING" if v is None else "GOOD"}
             for i, v in enumerate([10.0, 20.0, None, 30.0, 40.0, 50.0])]
    ctx.client.post("/v1/observations", json=batch(items), headers=h)
    agg = ctx.client.get("/v1/observations", headers=h, params={
        "metric": "sea_water_temperature", "from": z(T), "to": z(T + timedelta(hours=1)), "interval": "PT30M"})
    check(agg, "GET", "/v1/observations")
    b = agg.json()["items"]
    assert [(x["count"], x["missing_count"], x["avg"]) for x in b] == [(3, 1, 15.0), (3, 0, 40.0)]  # 결측은 평균 밖
    assert ctx.client.get("/v1/observations", headers=h, params={"from": z(T), "to": z(T + timedelta(hours=1)),
                                                                 "interval": "PT1H"}).status_code == 400
    page1 = ctx.client.get("/v1/observations", headers=h, params={"from": z(T), "to": z(T + timedelta(hours=1)),
                                                                   "limit": 4}).json()
    page2 = ctx.client.get("/v1/observations", headers=h, params={"from": z(T), "to": z(T + timedelta(hours=1)),
                                                                   "limit": 4, "cursor": page1["next_cursor"]}).json()
    assert len(page1["items"]) == 4 and len(page2["items"]) == 2 and page2["next_cursor"] is None
    corr = {"source_system_id": DEV_A_SENSOR, "metric": "sea_water_temperature", "observed_at": z(T + timedelta(minutes=10)),
            "value": 21.5, "reason": "센서 교정값 반영"}
    assert ctx.client.post("/v1/observations/corrections", json=corr, headers=h).status_code == 403
    r = ctx.client.post("/v1/observations/corrections", json=corr, headers=ctx.as_user(101))
    check(r, "POST", "/v1/observations/corrections")
    assert r.json()["value"] == 21.5
    with ctx.c.engine.connect() as conn:
        old = conn.execute(text("SELECT old_value, new_value, reason FROM observation_corrections")).one()
    assert tuple(old) == (20.0, 21.5, "센서 교정값 반영")
    assert "OBSERVATION_CORRECTED" in ctx.audit_actions()
    assert ctx.client.get("/v1/observations", headers=ctx.as_user(104),
                          params={"from": z(T), "to": z(T + timedelta(hours=1))}).json()["items"] == []  # T01


MAPPING = {"format": "csv", "delimiter": ",", "header_row": 1,
           "timestamp": {"field": "time", "format": "%Y-%m-%d %H:%M:%S"}, "missing_values": ["", "-999"],
           "columns": [{"field": "temp", "metric": "sea_water_temperature", "unit": "degC", "depth_m": 1.5},
                       {"field": "sal", "metric": "sea_water_salinity", "unit": "PSU"}]}


def _upload_csv(ictx, data: bytes) -> str:
    h = ictx.as_user(102)
    m = {"schema_version": "1.0.0", "source_system_id": DEV_A_SENSOR, "data_type": "SENSOR", "variant": "ORIGINAL",
         "session_id": SES_A, "timestamp_quality": {"source": "UNKNOWN", "verified": False},
         "file": {"original_filename": "ctd.csv", "byte_size": len(data), "mime_type": "text/csv",
                  "checksum_sha256": hashlib.sha256(data).hexdigest()}, "client": {"kind": "API", "version": "t"}}
    created = ictx.client.post("/v1/uploads", json=m, headers=h).json()
    r = httpx.put(created["parts"][0]["url"], content=data)
    ictx.client.post(f"/v1/uploads/{created['upload_id']}/complete",
                     json={"parts": [{"part_number": 1, "etag": r.headers["etag"]}]}, headers=h)
    while worker.run_once(ictx.c, "t"):
        pass
    return created["asset_id"]


def test_csv_parsing_path_matches_rest_and_reparse(ictx) -> None:
    h, admin = ictx.as_user(102), ictx.as_user(101)
    local = lambda m: (T + timedelta(hours=9, minutes=m)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731 - 장비 현지 시각
    csv_bytes = (f"time,temp,sal\n{local(0)},18.4,33.1\n{local(10)},-999,33.2\n{local(20)},abc,33.0\n").encode()
    # 매핑 없이 올리면 파싱 실패 기록 + 통지, 원본 자산은 그대로 (요구사항 5.3)
    asset_id = _upload_csv(ictx, csv_bytes)
    with ictx.c.sessionmaker() as db:
        run = db.execute(select(SensorParseRun)).scalar_one()
    assert run.status == "FAILED" and run.row_errors == [{"row": 0, "reason": "MAPPING_NOT_FOUND"}]
    assert any(n.kind == "sensor_parse.failed" for n in ictx.c.notifier.sent)
    assert ictx.client.get(f"/v1/assets/{asset_id}", headers=h).json()["ingest_status"] == "READY_PRIVATE"
    # 매핑 등록 (감사 전후) → 정책에 매핑 버전
    r = ictx.client.put(f"/v1/devices/{DEV_A_SENSOR}/observation-mapping", json=MAPPING, headers=admin)
    check(r, "PUT", "/v1/devices/{device_id}/observation-mapping")
    assert r.json()["version"] == 1
    assert ictx.client.put(f"/v1/devices/{DEV_A_SENSOR}/observation-mapping", json=MAPPING, headers=h).status_code == 403
    check(ictx.client.get(f"/v1/devices/{DEV_A_SENSOR}/observation-mapping", headers=h), "GET",
          "/v1/devices/{device_id}/observation-mapping")
    pol = ictx.client.get(f"/v1/devices/{DEV_A_COLLECTOR}/policy", headers=h).json()
    assert {s["device_id"]: s["observation_mapping_version"] for s in pol["sources"]}[DEV_A_SENSOR] == "m1"
    assert "OBSERVATION_MAPPING_CHANGED" in ictx.audit_actions()
    # 운영자 재파싱 → 이전 결과와 차이 기록 (요구사항 5.4)
    rp = ictx.client.post(f"/v1/assets/{asset_id}/reprocess", json={"steps": ["SENSOR_PARSE"], "reason": "매핑 등록"},
                          headers=ictx.as_user(106, mfa=True))
    assert rp.status_code == 202
    while worker.run_once(ictx.c, "t"):
        pass
    with ictx.c.sessionmaker() as db:
        last = db.execute(select(SensorParseRun).order_by(SensorParseRun.run_id.desc())).scalars().first()
    assert last.status == "PARTIAL" and last.mapping_version == 1
    assert (last.accepted, last.rejected) == (5, 0)
    assert last.row_errors == [{"row": 4, "reason": "NOT_A_NUMBER", "field": "temp"}]
    assert last.diff_from_previous["accepted"] == 5
    q = {"from": z(T - timedelta(hours=1)), "to": z(T + timedelta(hours=1)), "metric": "sea_water_temperature"}
    rows = ictx.client.get("/v1/observations", params=q, headers=h).json()["items"]
    assert [(x["observed_at"], x["value"], x["quality_flag"], x["source_asset_id"]) for x in rows] == [
        (z(T), 18.4, "GOOD", asset_id), (z(T + timedelta(minutes=10)), None, "MISSING", asset_id)]
    # 같은 관측값을 REST로 보내면 같은 저장 결과(중복) (요구사항 9.2)
    rest = batch([{"metric": "sea_water_temperature", "observed_at": z(T), "value": 18.4, "unit": "degC",
                   "quality_flag": "GOOD", "depth_m": 1.5},
                  {"metric": "sea_water_salinity", "observed_at": kst(T + timedelta(minutes=20)), "value": 33.0,
                   "unit": "PSU", "quality_flag": "GOOD"}])
    out = ictx.client.post("/v1/observations", json=rest, headers=h).json()
    assert out["duplicates"] == 2


def test_events(ctx) -> None:
    h = ctx.as_user(102)
    base = {"schema_version": "1.0.0", "session_id": SES_A, "started_at": z(T),
            "timestamp_quality": {"source": "DEVICE", "verified": False}}
    manual = {**base, "event_code": "SAFETY_INCIDENT", "source": "MANUAL", "severity": "WARNING", "note": "근접 항해"}
    r = ctx.client.post("/v1/events", json=manual, headers=h)
    check(r, "POST", "/v1/events")
    assert r.status_code == 201 and r.json()["human_verified"] is False
    eid = r.json()["event_id"]
    bad = {**base, "event_code": "VESSEL", "source": "MODEL", "source_system_id": DEV_A_CAMERA}
    assert ctx.client.post("/v1/events", json=bad, headers=h).status_code == 400  # model_ref·confidence 필수
    model = {**bad, "model_ref": "yolo-test@1", "confidence": 0.82, "source_event_id": "cam-ev-1"}
    m = ctx.client.post("/v1/events", json=model, headers=h)
    assert m.status_code == 201 and m.json()["human_verified"] is False
    dup = ctx.client.post("/v1/events", json=model, headers=h)
    assert dup.status_code == 200 and dup.json()["event_id"] == m.json()["event_id"]  # 원천 이벤트 ID 중복
    sub = {**manual, "event_code": "VESSEL.small_boat"}
    assert ctx.client.post("/v1/events", json=sub, headers=h).json()["code"] == "RULE_VIOLATION"
    with ctx.c.sessionmaker.begin() as db:
        db.add(OrgEventCode(organization_id=ORG_A, code="VESSEL.small_boat", label="소형선", created_by="t",
                            created_at=datetime.now(UTC)))
    assert ctx.client.post("/v1/events", json=sub, headers=h).status_code == 201
    assert ctx.client.post("/v1/events", json={**manual, "event_code": "NOPE"}, headers=h).status_code == 422
    v = ctx.client.patch(f"/v1/events/{m.json()['event_id']}", json={"human_verified": True,
                                                                       "ended_at": z(T + timedelta(minutes=1))},
                         headers=h)
    check(v, "PATCH", "/v1/events/{event_id}")
    assert v.json()["human_verified"] is True and v.json()["verified_by"] == "usr_" + "0" * 23 + "102"
    lst = ctx.client.get("/v1/events", params={"event_code": "VESSEL", "human_verified": "true"}, headers=h)
    check(lst, "GET", "/v1/events")
    assert [e["event_id"] for e in lst.json()["items"]] == [m.json()["event_id"]]
    check(ctx.client.get(f"/v1/events/{eid}", headers=h), "GET", "/v1/events/{event_id}")
    # 다른 기관 (T01)
    hb = ctx.as_user(104)
    assert ctx.client.get(f"/v1/events/{eid}", headers=hb).status_code == 404
    assert ctx.client.patch(f"/v1/events/{eid}", json={"note": "x"}, headers=hb).status_code == 404
    assert ctx.client.post("/v1/events", json={**manual, "session_id": SES_B}, headers=h).status_code == 404
    assert ctx.client.get("/v1/events", headers=hb).json()["items"] == []
