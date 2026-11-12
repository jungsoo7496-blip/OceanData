"""겹치는 자산 목록의 페이지 경계와 범위를 검증한다 (계약 점검 12번)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.api.pagination import encode_cursor
from app.seed import DEV_A_CAMERA, DEV_A_SENSOR, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, PRJ_B, SES_A, SES_B, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract
from tests.integration.test_event_overlap import T, _put, z

pytestmark = pytest.mark.integration
PATH = "/v1/events/{event_id}/overlapping-assets"


def test_overlapping_assets_page_uses_full_capture_timestamp_and_asset_id(ctx: Ctx) -> None:
    # 496 ordered starts and nine more within the same second. The default 500
    # cuts through four equal timestamps; the last asset has a smaller ID.
    with ctx.c.engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO assets (asset_id, schema_version, organization_id, project_id, session_id,
                                source_system_id, data_type, variant, data_role, capture_start_at, capture_end_at,
                                ingested_at, timestamp_quality, mime_type, byte_size, checksum_sha256, has_binary,
                                collection_mode, privacy_status, sensitivity_class, sharing_scope,
                                retention_policy_id, retention_until, quality_status, ingest_status, share_status,
                                backup_class, provenance, storage_zone, storage_key, created_at, updated_at)
            SELECT 'ast_' || lpad(n::text, 26, '0'), '1.0.0', :org, :project, :session, :device,
                   'VIDEO', 'ORIGINAL', 'CORE', capture, capture + interval '1 second', capture,
                   '{"source": "DEVICE", "verified": false}', 'video/mp4', 1000,
                   md5(n::text) || md5((n + 1)::text), true, 'C3_APPROVED_FULL', 'REVIEW_REQUIRED',
                   'RESTRICTED', 'PRIVATE', 'raw_default', capture + interval '3650 days', 'PASSED',
                   'READY_PRIVATE', 'NONE', 'NONE', '{"ingest_channel": "AGENT"}', 'RAW', 'synthetic/' || n,
                   capture, capture
            FROM generate_series(1, 505) AS series(n)
            CROSS JOIN LATERAL (
                SELECT CAST(:started AS timestamptz) + CASE WHEN n < 497 THEN n * interval '1 second'
                    ELSE interval '497 seconds' +
                         (ARRAY[900, 500, 500, 100, 500, 500, 600, 300, 750])[n - 496] * interval '1 millisecond'
                    END AS capture
            ) times
        """), {"started": T, "org": ORG_A, "project": PRJ_A, "session": SES_A, "device": DEV_A_CAMERA})
    _put(ctx, None, None)
    _put(ctx, None, None, dev=DEV_A_SENSOR)
    _put(ctx, None, None, dev=DEV_B_CAMERA, org=ORG_B, prj=PRJ_B, session=SES_B)

    member = ctx.as_user(102)
    linked = fid("ast", 501)
    event = ctx.client.post("/v1/events", headers=member, json={
        "schema_version": "1.0.0", "session_id": SES_A, "event_code": "SAFETY_INCIDENT", "source": "MANUAL",
        "started_at": z(T), "ended_at": z(T + timedelta(minutes=10)), "linked_asset_ids": [linked],
        "timestamp_quality": {"source": "DEVICE", "verified": False},
    })
    assert event.status_code == 201, event.text
    event_id = event.json()["event_id"]
    url = PATH.format(event_id=event_id)
    expected_ids = [fid("ast", n) for n in (*range(1, 497), 500, 504, 498, 499, 501, 502, 503, 505, 497)]

    first = ctx.client.get(url, headers=member)
    assert first.status_code == 200, first.text
    assert_matches_contract(first, "GET", PATH)
    page = first.json()
    assert [item["asset"]["asset_id"] for item in page["items"]] == expected_ids[:500]
    assert page["unknown_capture_count"] == 2
    cursor = page.get("next_cursor")
    assert isinstance(cursor, str) and cursor, "500건 뒤에 이어지는 페이지의 커서가 필요합니다"

    second = ctx.client.get(url, headers=member, params={"cursor": cursor})
    assert second.status_code == 200, second.text
    assert_matches_contract(second, "GET", PATH)
    rest = second.json()
    assert [item["asset"]["asset_id"] for item in rest["items"]] == expected_ids[500:]
    assert [item["asset"]["asset_id"] for item in rest["items"] if item["linked"]] == [linked]
    assert (rest["event_id"], rest["session_id"], rest["window"], rest["unknown_capture_count"]) == (
        event_id, SES_A, page["window"], 2,
    )
    assert rest["next_cursor"] is None

    filtered = ctx.client.get(url, headers=member, params={"source_system_id": DEV_A_SENSOR})
    assert filtered.status_code == 200, filtered.text
    assert (filtered.json()["items"], filtered.json()["unknown_capture_count"]) == ([], 1)
    foreign = ctx.client.get(url, headers=ctx.as_user(103), params={"cursor": cursor})
    assert foreign.status_code == 404, foreign.text
    bad_cursors = ("not-a-cursor!", encode_cursor("[]"), encode_cursor(f'["0001-01-01T00:00:00+09:00","{linked}"]'))
    for bad_cursor in bad_cursors:
        malformed = ctx.client.get(url, headers=member, params={"cursor": bad_cursor})
        assert malformed.status_code == 400, malformed.text
        assert_matches_contract(malformed, "GET", PATH)
        assert malformed.json()["code"] == "VALIDATION_ERROR"
    single = ctx.client.get(url, headers=member, params={"limit": 1})
    assert single.status_code == 200, single.text
    assert [item["asset"]["asset_id"] for item in single.json()["items"]] == expected_ids[:1]
    assert single.json()["next_cursor"]
