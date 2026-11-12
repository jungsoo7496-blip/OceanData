"""Scratch verification for the status/events/observations contract sweep (read-only review; not part of repo)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.db.models import Device, EvidenceCitation, SessionRecord, SessionRecordVersion
from app.domain.ids import new_id
from app.seed import (DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_A_SENSOR, DEV_B_CAMERA, ORG_A, PRJ_A, SES_A)
from tests.contract.openapi_contract import assert_matches_contract
from tests.integration.test_catalog import _asset

OUT: list[str] = []


def log(label: str, value: object) -> None:
    line = f"[{label}] {value}"
    OUT.append(line)
    print(line)


def contract_ok(r, method: str, path: str) -> str:
    try:
        assert_matches_contract(r, method, path)
        return "OK"
    except AssertionError as exc:
        return "VIOLATION: " + str(exc)[:600]


def z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def device_headers(ctx) -> dict[str, str]:
    code = ctx.client.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes",
                           headers=ctx.as_user(101, mfa=True)).json()["code"]
    cred = ctx.client.post("/v1/agents/enroll", json={"enrollment_code": code, "agent_version": "0.1.0",
                                                      "os": "windows"}).json()
    tok = ctx.client.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": cred["client_id"],
                                                  "client_secret": cred["client_secret"]}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def audit_rows(ctx, action: str) -> list[tuple]:
    with ctx.c.engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT action, actor_kind, organization_id, target_type, target_id FROM audit_log WHERE action=:a "
            "ORDER BY seq"), {"a": action})]


def test_events_observations(ctx) -> None:
    member, admin = ctx.as_user(102), ctx.as_user(101)
    T = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
    base = {"schema_version": "1.0.0", "session_id": SES_A, "event_code": "SAFETY_INCIDENT", "source": "MANUAL",
            "started_at": z(T), "timestamp_quality": {"source": "DEVICE", "verified": False},
            "source_event_id": "src-evt-1"}
    k = {**member, "Idempotency-Key": "evt-key-0001"}
    r1 = ctx.client.post("/v1/events", json=base, headers=k)
    log("events.idem.first", (r1.status_code, r1.headers.get("idempotent-replayed")))
    r2 = ctx.client.post("/v1/events", json=base, headers=k)
    log("events.idem.same_key_same_body", (r2.status_code, r2.headers.get("idempotent-replayed"),
                                           r2.json().get("event_id") == r1.json().get("event_id")))
    r3 = ctx.client.post("/v1/events", json={**base, "note": "different body"}, headers=k)
    log("events.idem.same_key_diff_body", (r3.status_code, r3.json().get("code")))
    eid = r1.json()["event_id"]

    # PATCH ended_at without Z / garbage
    for v in ("2026-11-01T10:00:00", "garbage"):
        r = ctx.client.patch(f"/v1/events/{eid}", json={"ended_at": v}, headers=member)
        log(f"events.patch.ended_at={v}", (r.status_code, r.json().get("code")))

    # observation correction with observed_at w/o Z
    ctx.client.post("/v1/observations", headers=member, json={
        "schema_version": "1.0.0", "source_system_id": DEV_A_SENSOR, "observations": [
            {"metric": "sea_water_temperature", "observed_at": z(T), "value": 10.0, "unit": "degC",
             "quality_flag": "GOOD"}]})
    for v in ("2026-11-01T03:00:00", "not-a-time"):
        r = ctx.client.post("/v1/observations/corrections", headers=admin, json={
            "source_system_id": DEV_A_SENSOR, "metric": "sea_water_temperature", "observed_at": v, "value": 1.0,
            "reason": "test reason"})
        log(f"obs.correction.observed_at={v}", (r.status_code, r.json().get("code")))

    # POST /v1/observations with other org's source (contract documents no 404)
    r = ctx.client.post("/v1/observations", headers=member, json={
        "schema_version": "1.0.0", "source_system_id": DEV_B_CAMERA, "observations": [
            {"metric": "sea_water_temperature", "observed_at": z(T), "value": 10.0, "unit": "degC",
             "quality_flag": "GOOD"}]})
    log("obs.post.other_org_device", (r.status_code, r.json().get("code")))
    try:
        assert_matches_contract(r, "POST", "/v1/observations")
        log("obs.post.other_org_device.contract", "OK")
    except AssertionError as exc:
        log("obs.post.other_org_device.contract", "VIOLATION " + str(exc)[:200])

    # device token: extra unmapped source device in org A + asset from it
    unmapped = "dev_" + "0" * 23 + "399"
    with ctx.c.sessionmaker.begin() as db:
        db.add(Device(device_id=unmapped, organization_id=ORG_A, project_id=PRJ_A, name="A 다른 카메라",
                      kind="SOURCE", device_type="CAMERA", collector_device_id=None, status="ACTIVE",
                      collection_mode="C2_EVENT_CLIPS", local_copy_policy="RETAINED", local_retention_days=None,
                      expected_interval_s=None, source_timezone=None, last_heartbeat_at=None, created_at=T))
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, unmapped, session=SES_A)
        a.capture_start_at, a.capture_end_at = T + timedelta(minutes=1), T + timedelta(minutes=2)
        a.checksum_sha256 = hashlib.sha256(a.asset_id.encode()).hexdigest()
        a.timestamp_quality = {"source": "DEVICE", "verified": True, "est_offset_ms": 20}
        a.source_location = {"type": "Point", "coordinates": [126.51, 34.71]}
        db.add(a)
        unmapped_asset = a.asset_id
    dh = device_headers(ctx)
    log("device.get_assets", ctx.client.get("/v1/assets", headers=dh).status_code)
    log("device.get_asset_detail", ctx.client.get(f"/v1/assets/{unmapped_asset}", headers=dh).status_code)
    ev = ctx.client.post("/v1/events", headers=dh, json={
        "schema_version": "1.0.0", "session_id": SES_A, "event_code": "SAFETY_INCIDENT", "source": "DEVICE",
        "source_system_id": DEV_A_CAMERA, "started_at": z(T - timedelta(days=30)), "ended_at": z(T + timedelta(days=30)),
        "timestamp_quality": {"source": "DEVICE", "verified": False}})
    log("device.create_event", ev.status_code)
    ov = ctx.client.get(f"/v1/events/{ev.json()['event_id']}/overlapping-assets", headers=dh)
    items = ov.json().get("items", [])
    log("device.overlapping_assets", (ov.status_code, [(x["asset"]["asset_id"] == unmapped_asset,
                                                         x["asset"].get("source_system_id"),
                                                         sorted(x["asset"].keys())[:6]) for x in items]))
    log("device.ingest_status_devices",
        [d["device_id"][-3:] for d in ctx.client.get("/v1/ingest-status", headers=dh).json()["devices"]])
    log("device.events_list", len(ctx.client.get("/v1/events", headers=dh).json()["items"]))
    # device POST observations for unmapped source in own org
    r = ctx.client.post("/v1/observations", headers=dh, json={
        "schema_version": "1.0.0", "source_system_id": unmapped, "observations": [
            {"metric": "sea_water_temperature", "observed_at": z(T), "value": 10.0, "unit": "degC",
             "quality_flag": "GOOD"}]})
    log("device.post_obs_unmapped", (r.status_code, r.json().get("code")))

    # device GET mapping of org B device: audit?
    before = len(audit_rows(ctx, "CROSS_ORG_ACCESS_ATTEMPT"))
    r = ctx.client.get(f"/v1/devices/{DEV_B_CAMERA}/observation-mapping", headers=dh)
    after = len(audit_rows(ctx, "CROSS_ORG_ACCESS_ATTEMPT"))
    log("device.mapping_other_org", (r.status_code, "cross_org_audits_added", after - before,
                                     "scope_violation_audits", len(audit_rows(ctx, "DEVICE_SCOPE_VIOLATION"))))
    before = after
    r = ctx.client.get(f"/v1/devices/{DEV_B_CAMERA}/observation-mapping", headers=admin)
    after = len(audit_rows(ctx, "CROSS_ORG_ACCESS_ATTEMPT"))
    log("user.mapping_other_org", (r.status_code, "cross_org_audits_added", after - before))


def test_status_ops(ctx) -> None:
    op, op_mfa = ctx.as_user(106), ctx.as_user(106, mfa=True)
    # backup-runs dedupe
    r1 = ctx.client.post("/v1/backup-runs", headers=op)
    j1 = r1.json().get("job_id")
    with ctx.c.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status='DONE' WHERE job_id=:j"), {"j": j1})
    r2 = ctx.client.post("/v1/backup-runs", headers=op)
    with ctx.c.engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM jobs WHERE job_type='backup'")).scalar()
        queued = conn.execute(text("SELECT count(*) FROM jobs WHERE job_type='backup' AND status='QUEUED'")).scalar()
    log("backup.second_post", (r1.status_code, r2.status_code, "same_job_id", r2.json().get("job_id") == j1,
                               "backup_jobs_total", n, "queued", queued))

    # inspections far-future period
    for cyc, key in (("DAILY", "9999-12-31"), ("YEARLY", "9999"), ("DAILY", "2026-02-30")):
        r = ctx.client.post("/v1/inspections", headers=op, json={"cycle": cyc, "period_key": key})
        log(f"inspection.create.{cyc}.{key}", (r.status_code, r.json().get("code")))

    # external source PATCH with nulls
    src = {"source_id": "khoa-test", "name": "합성 소스", "adapter_kind": "MOCK", "interval_s": 600,
           "stations": [{"code": "S1", "name": "합성", "lat": 34.7, "lon": 126.5}],
           "field_mapping": {"station_path": "$.s", "time_path": "$.t",
                             "metrics": {"sea_water_temperature": {"path": "$.v", "unit": "degC"}}},
           "attribution": "합성 출처", "redistribution_allowed": False}
    log("external.create", ctx.client.post("/v1/external-sources", json=src, headers=op).status_code)
    for body in ({"enabled": None}, {"interval_s": None}):
        r = ctx.client.patch("/v1/external-sources/khoa-test", json=body, headers=op)
        cur = [s for s in ctx.client.get("/v1/external-sources", headers=op).json()["items"]
               if s["source_id"] == "khoa-test"][0]
        log(f"external.patch.{list(body)[0]}=null", (r.status_code, r.json().get("code"),
                                                      "after", cur.get("enabled"), cur.get("interval_s")))

    # organization policy null quota
    r = ctx.client.patch(f"/v1/organization-policies/{ORG_A}", json={"quota_bytes": None, "reason": "null 시험"},
                         headers=op_mfa)
    log("orgpolicy.patch.quota_null", (r.status_code, r.json().get("code")))

    # usage as operator with no OrgQuota rows
    r = ctx.client.get("/v1/usage", headers=op)
    log("usage.operator.items", [x["organization_id"][-3:] for x in r.json()["items"]])
    log("orgpolicies.operator.items",
        [x["organization_id"][-3:] for x in ctx.client.get("/v1/organization-policies", headers=op).json()["items"]])

    # retention dry-run warning shape
    now = ctx.c.clock.now()
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, session=SES_A)
        a.checksum_sha256 = hashlib.sha256(a.asset_id.encode()).hexdigest()
        a.retention_policy_id, a.retention_until = "raw_default", now - timedelta(days=1)
        db.add(a)
        rid = new_id("record") if True else None
        db.add(SessionRecord(record_id=rid, organization_id=ORG_A, project_id=PRJ_A, session_id=SES_A,
                             latest_version=1, created_by="t", created_at=now, updated_at=now))
        db.flush()
        db.add(SessionRecordVersion(record_id=rid, version=1, kind="TEST", title="합성", evidence_refs=[],
                                    status="APPROVED", approved_by="t", approved_at=now, created_by="t",
                                    created_at=now))
        db.flush()
        db.add(EvidenceCitation(organization_id=ORG_A, citing_type="RECORD", citing_id=rid, citing_version=1,
                                asset_id=a.asset_id, cited_at=now))
    r = ctx.client.post("/v1/retention/dry-run", headers=op)
    log("retention.dry_run.warnings", (r.status_code, r.json().get("warnings")))
    log("retention.dry_run.contract", contract_ok(r, "POST", "/v1/retention/dry-run"))


def test_overlap_truncation(ctx) -> None:
    member = ctx.as_user(102)
    T = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
    r = ctx.client.post("/v1/events", headers=member, json={
        "schema_version": "1.0.0", "session_id": SES_A, "event_code": "SAFETY_INCIDENT", "source": "MANUAL",
        "started_at": z(T), "ended_at": z(T + timedelta(hours=1)),
        "timestamp_quality": {"source": "DEVICE", "verified": False}})
    eid = r.json()["event_id"]
    with ctx.c.sessionmaker.begin() as db:
        for i in range(505):
            a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, session=SES_A)
            a.capture_start_at = T + timedelta(seconds=5 * i)
            a.capture_end_at = a.capture_start_at + timedelta(seconds=4)
            a.checksum_sha256 = hashlib.sha256(a.asset_id.encode()).hexdigest()
            a.timestamp_quality = {"source": "DEVICE", "verified": True, "est_offset_ms": 20}
            db.add(a)
    ov = ctx.client.get(f"/v1/events/{eid}/overlapping-assets", headers=member)
    body = ov.json()
    log("overlap.truncation", (ov.status_code, "items", len(body["items"]), "keys", sorted(body.keys())))
    with open("/tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/sweep/"
              "status-events-observations/out.txt", "a", encoding="utf-8") as fh:
        fh.write("\n".join(OUT) + "\n")


def teardown_module(module) -> None:  # noqa: ANN001
    with open("/tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/sweep/"
              "status-events-observations/out.txt", "a", encoding="utf-8") as fh:
        fh.write("\n".join(OUT) + "\n")
    json.dumps(OUT)
