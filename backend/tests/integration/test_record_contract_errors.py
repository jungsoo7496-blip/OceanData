"""회차 기록 API 오류 응답과 근거 교차 기관 감사 (계약 점검 S39~S42)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from app.db.models import Asset
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, DEV_A_SENSOR, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, PRJ_B, SEED_AT, SES_A, SES_B, fid
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration

MEMBER_A, MEMBER_B = 102, 104
CREATE = "/v1/sessions/{session_id}/records"
UPDATE = "/v1/records/{record_id}"


def _obs(dev: str, frm: str = "2026-10-01T00:00:00Z", to: str = "2026-10-02T00:00:00Z") -> dict[str, Any]:
    return {"kind": "OBSERVATION_RANGE", "source_system_id": dev, "metric": "sea_water_temperature",
            "from": frm, "to": to}


def _new_record(ctx: Ctx) -> str:
    r = ctx.client.post(f"/v1/sessions/{SES_A}/records", json={"kind": "LOG", "title": "합성 일지"},
                        headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 201, r.text
    return str(r.json()["record_id"])


def _latest_version(ctx: Ctx, record_id: str) -> int:
    r = ctx.client.get(f"/v1/records/{record_id}", headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 200, r.text
    return int(r.json()["latest_version"])


def _asset(org: str, prj: str, dev: str, session: str) -> Asset:
    return Asset(asset_id=new_id("asset"), schema_version="1.0.0", organization_id=org, project_id=prj,
                 session_id=session, source_system_id=dev, data_type="VIDEO", variant="ORIGINAL", data_role="CORE",
                 ingested_at=SEED_AT, timestamp_quality={"source": "UNKNOWN", "verified": False},
                 mime_type="video/mp4", byte_size=10, checksum_sha256=new_id("asset")[4:].lower().ljust(64, "0"),
                 has_binary=True, collection_mode="C3_APPROVED_FULL", privacy_status="REVIEW_REQUIRED",
                 sensitivity_class="RESTRICTED", sharing_scope="PRIVATE", retention_policy_id="default",
                 retention_until=SEED_AT, quality_status="PENDING", ingest_status="READY_PRIVATE", share_status="NONE",
                 backup_class="NONE", provenance={"ingest_channel": "AGENT"},
                 location={"type": "Point", "coordinates": [126.5, 34.7]}, created_at=SEED_AT, updated_at=SEED_AT)


def _cross_org_rows(ctx: Ctx) -> list[dict[str, Any]]:
    with ctx.c.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(
            "SELECT organization_id, actor_id, actor_organization_id, target_type, target_id, result, details "
            "FROM audit_log WHERE action = 'CROSS_ORG_ACCESS_ATTEMPT' ORDER BY seq"))]


# ------------------------------------------------------------------ S39·S42: null을 받을 수 없는 항목


@pytest.mark.parametrize("field", ["title", "kind", "evidence_refs"])
def test_patch_null_on_non_nullable_field_is_400_at_that_field(ctx: Ctx, field: str) -> None:
    record_id = _new_record(ctx)
    r = ctx.client.patch(f"/v1/records/{record_id}", json={field: None}, headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 400, r.text
    check(r, "PATCH", UPDATE)
    assert r.json()["code"] == "VALIDATION_ERROR"
    assert [e["pointer"] for e in r.json()["errors"]] == [f"/{field}"]
    assert _latest_version(ctx, record_id) == 1  # 새 버전을 만들지 않는다


def test_patch_null_on_nullable_text_field_still_makes_new_version(ctx: Ctx) -> None:
    record_id = _new_record(ctx)
    r = ctx.client.patch(f"/v1/records/{record_id}", json={"note": None, "result": None},
                         headers=ctx.as_user(MEMBER_A))
    assert r.status_code == 200, r.text
    check(r, "PATCH", UPDATE)
    assert (r.json()["version"], r.json()["note"], r.json()["evidence_refs"]) == (2, None, [])


# ------------------------------------------------------------------ S40: 형식은 맞지만 없는 시각


@pytest.mark.parametrize(("route", "device", "field", "value"), [
    pytest.param("POST", DEV_A_SENSOR, "from", "2026-02-30T00:00:00Z", id="post-feb30-from"),
    pytest.param("POST", DEV_A_SENSOR, "to", "2026-10-01T25:00:00Z", id="post-hour25-to"),
    pytest.param("POST", DEV_A_SENSOR, "from", "2026-06-30T23:59:60Z", id="post-leap-second"),
    pytest.param("POST", fid("dev", 999), "from", "2026-13-01T00:00:00Z", id="post-unknown-device-month13"),
    pytest.param("PATCH", DEV_A_SENSOR, "to", "2026-10-01T25:00:00Z", id="patch-hour25-to"),
])
def test_observation_range_impossible_timestamp_is_400(ctx: Ctx, route: str, device: str, field: str,
                                                       value: str) -> None:
    hm = ctx.as_user(MEMBER_A)
    refs = [_obs(device, **({"frm": value} if field == "from" else {"to": value}))]
    if route == "POST":
        r = ctx.client.post(f"/v1/sessions/{SES_A}/records", json={"kind": "LOG", "title": "x", "evidence_refs": refs},
                            headers=hm)
        path = CREATE
    else:
        record_id = _new_record(ctx)
        r = ctx.client.patch(f"/v1/records/{record_id}", json={"evidence_refs": refs}, headers=hm)
        path = UPDATE
    assert r.status_code == 400, r.text
    check(r, route, path)
    assert r.json()["code"] == "VALIDATION_ERROR"
    pointers = [e["pointer"] for e in r.json()["errors"]]
    assert len(pointers) == 1 and pointers[0].startswith("/evidence_refs/0/") and pointers[0].endswith(f"/{field}")
    if route == "PATCH":
        assert _latest_version(ctx, record_id) == 1


def test_observation_range_valid_timestamp_still_accepted(ctx: Ctx) -> None:
    r = ctx.client.post(f"/v1/sessions/{SES_A}/records", headers=ctx.as_user(MEMBER_A), json={
        "kind": "LOG", "title": "x", "evidence_refs": [_obs(DEV_A_SENSOR, to="2026-10-01T23:59:59.5Z")]})
    assert r.status_code == 201, r.text
    check(r, "POST", CREATE)


# ------------------------------------------------------------------ S41: 다른 기관 근거는 '없음'으로 답하고 감사


def _foreign_refs(ctx: Ctx) -> dict[str, tuple[dict[str, Any], str, str]]:
    """{종류: (근거, 감사 대상 종류, 대상 ID)} — 모두 B 기관 소속."""
    ev = ctx.client.post("/v1/events", headers=ctx.as_user(MEMBER_B), json={
        "schema_version": "1.0.0", "session_id": SES_B, "event_code": "VESSEL", "started_at": "2026-11-01T02:00:00Z",
        "timestamp_quality": {"source": "MANUAL", "verified": False, "est_offset_ms": None}, "source": "MANUAL"})
    assert ev.status_code in (200, 201), ev.text
    event_id = ev.json()["event_id"]
    with ctx.c.sessionmaker.begin() as db:
        asset = _asset(ORG_B, PRJ_B, DEV_B_CAMERA, SES_B)
        db.add(asset)
        asset_id = asset.asset_id
    return {"ASSET": ({"kind": "ASSET", "asset_id": asset_id}, "Asset", asset_id),
            "EVENT": ({"kind": "EVENT", "event_id": event_id}, "Event", event_id),
            "OBSERVATION_RANGE": (_obs(DEV_B_CAMERA), "Device", DEV_B_CAMERA)}


MISSING = {"ASSET": {"kind": "ASSET", "asset_id": fid("ast", 999)},
           "EVENT": {"kind": "EVENT", "event_id": fid("evt", 999)},
           "OBSERVATION_RANGE": _obs(fid("dev", 999))}


@pytest.mark.parametrize("route", ["POST", "PATCH"])
@pytest.mark.parametrize("kind", ["ASSET", "EVENT", "OBSERVATION_RANGE"])
def test_cross_org_evidence_ref_is_hidden_and_audited(ctx: Ctx, kind: str, route: str) -> None:
    ref, target_type, target_id = _foreign_refs(ctx)[kind]
    hm = ctx.as_user(MEMBER_A)
    record_id = _new_record(ctx) if route == "PATCH" else ""

    def send(refs: list[dict[str, Any]]) -> Any:
        if route == "POST":
            return ctx.client.post(f"/v1/sessions/{SES_A}/records", headers=hm,
                                   json={"kind": "LOG", "title": "x", "evidence_refs": refs})
        return ctx.client.patch(f"/v1/records/{record_id}", headers=hm, json={"evidence_refs": refs})

    before = len(_cross_org_rows(ctx))
    missing = send([MISSING[kind]])
    assert missing.status_code == 400, missing.text
    assert len(_cross_org_rows(ctx)) == before  # 없는 ID는 감사하지 않는다

    # 같은 근거를 두 번 넣어도 감사는 대상마다 한 행
    foreign = send([ref, ref])
    assert foreign.status_code == 400, foreign.text
    check(foreign, route, CREATE if route == "POST" else UPDATE)
    # 응답은 없는 ID와 같다 — 존재를 드러내지 않는다 (10번 설계)
    assert foreign.json()["errors"] == [{**e, "pointer": p} for p in ("/evidence_refs/0", "/evidence_refs/1")
                                        for e in missing.json()["errors"]]
    rows = _cross_org_rows(ctx)[before:]
    assert len(rows) == 1, rows
    row = rows[0]
    assert (row["organization_id"], row["actor_organization_id"], row["target_type"], row["target_id"],
            row["result"]) == (ORG_B, ORG_A, target_type, target_id, "DENIED")
    assert row["actor_id"] == fid("usr", MEMBER_A)
    assert row["details"]["path"] == (f"/v1/sessions/{SES_A}/records" if route == "POST" else f"/v1/records/{record_id}")
    if route == "PATCH":
        assert _latest_version(ctx, record_id) == 1


def test_own_org_deleted_asset_ref_is_not_audited(ctx: Ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        asset = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, SES_A)
        asset.deleted_at = SEED_AT
        db.add(asset)
        asset_id = asset.asset_id
    before = len(_cross_org_rows(ctx))
    r = ctx.client.post(f"/v1/sessions/{SES_A}/records", headers=ctx.as_user(MEMBER_A),
                        json={"kind": "LOG", "title": "x", "evidence_refs": [{"kind": "ASSET", "asset_id": asset_id}]})
    assert r.status_code == 400 and r.json()["errors"][0]["code"] == "not_found", r.text
    assert len(_cross_org_rows(ctx)) == before
