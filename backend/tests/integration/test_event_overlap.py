"""07번 요구사항 7.6: 이벤트 구간과 촬영 구간이 겹치는 같은 회차 자산 조회."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from app.seed import DEV_A_CAMERA, DEV_A_SENSOR, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, PRJ_B, SES_A
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
T = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
MEMBER_A, ADMIN_B, OP = 102, 103, 106


def z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _put(ctx, start, end, *, dev=DEV_A_CAMERA, session=SES_A, org=ORG_A, prj=PRJ_A) -> str:  # type: ignore[no-untyped-def]
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(org, prj, dev, session=session)
        a.capture_start_at, a.capture_end_at = start, end
        a.checksum_sha256 = hashlib.sha256(a.asset_id.encode()).hexdigest()  # 계약 형식 (합성)
        a.location_precision = "EXACT_RESTRICTED"
        if start is not None:
            a.timestamp_quality = {"source": "DEVICE", "verified": True, "est_offset_ms": 20}
        db.add(a)
        return a.asset_id


def test_event_overlapping_assets(ctx) -> None:
    h = ctx.as_user(MEMBER_A)
    # 이벤트 03:10~03:20
    r = ctx.client.post("/v1/events", headers=h, json={
        "schema_version": "1.0.0", "session_id": SES_A, "event_code": "SAFETY_INCIDENT", "source": "MANUAL",
        "severity": "WARNING", "started_at": z(T + timedelta(minutes=10)), "ended_at": z(T + timedelta(minutes=20)),
        "timestamp_quality": {"source": "DEVICE", "verified": False}})
    assert r.status_code == 201, r.text
    eid = r.json()["event_id"]
    inside = _put(ctx, T + timedelta(minutes=12), T + timedelta(minutes=14))          # 안
    straddle = _put(ctx, T, T + timedelta(minutes=11))                                 # 앞쪽 1분 겹침
    covers = _put(ctx, T, T + timedelta(hours=1))                                      # 전체 포함
    instant = _put(ctx, T + timedelta(minutes=15), None)                               # 순간(사진 등)
    before = _put(ctx, T, T + timedelta(minutes=9, seconds=30))                        # 30초 전에 끝남
    after = _put(ctx, T + timedelta(minutes=21), T + timedelta(minutes=30))            # 1분 뒤 시작
    sensor = _put(ctx, T + timedelta(minutes=12), T + timedelta(minutes=13), dev=DEV_A_SENSOR)
    _put(ctx, None, None)                                                              # 촬영시각 미상
    _put(ctx, T + timedelta(minutes=12), T + timedelta(minutes=13), session=None)      # 다른(미상) 회차
    other_org = _put(ctx, T + timedelta(minutes=12), T + timedelta(minutes=13), dev=DEV_B_CAMERA, org=ORG_B,
                     prj=PRJ_B, session=None)
    ctx.client.patch(f"/v1/events/{eid}", json={"add_linked_asset_ids": [inside]}, headers=h)

    path = f"/v1/events/{eid}/overlapping-assets"
    r = ctx.client.get(path, headers=h)
    check(r, "GET", "/v1/events/{event_id}/overlapping-assets")
    body = r.json()
    got = {x["asset"]["asset_id"]: x for x in body["items"]}
    assert set(got) == {inside, straddle, covers, instant, sensor}
    assert got[inside]["linked"] is True and got[covers]["linked"] is False
    assert got[straddle]["overlap_s"] == 60 and got[covers]["overlap_s"] == 600 and got[instant]["overlap_s"] == 0
    assert body["unknown_capture_count"] == 1 and other_org not in got
    assert [x["asset"]["asset_id"] for x in body["items"]][0] in (straddle, covers)  # 촬영 시작 순

    # 여유(margin_s)로 넓히면 앞뒤 자산이 들어온다, 장비로 좁히기
    wide = {x["asset"]["asset_id"] for x in ctx.client.get(path, params={"margin_s": 60}, headers=h).json()["items"]}
    assert {before, after} <= wide
    r = ctx.client.get(path, params={"source_system_id": DEV_A_SENSOR}, headers=h)
    assert [x["asset"]["asset_id"] for x in r.json()["items"]] == [sensor]
    assert ctx.client.get(path, params={"margin_s": 99999}, headers=h).status_code == 400
    assert ctx.client.get(path, params={"source_system_id": "nope"}, headers=h).status_code == 400

    # 기관 격리: 다른 기관 이벤트는 없는 것으로, 운영자는 메타데이터로 본다
    assert ctx.client.get(path, headers=ctx.as_user(ADMIN_B)).status_code == 404
    assert ctx.client.get(path, headers=ctx.as_user(OP)).status_code == 200
