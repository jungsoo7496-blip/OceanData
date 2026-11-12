"""Scratch probes for the reports/records/contracts/uploads contract sweep (read-only review, not committed)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.db.models import Asset, Device, Project, Upload
from app.seed import DEV_A_CAMERA, DEV_A_SENSOR, ORG_A, PRJ_A, SES_A, SEED_AT, fid

OUT: dict = {}


def _zip(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("Contents/section0.xml", xml)
    return buf.getvalue()


def _manifest(data: bytes) -> dict:
    return {
        "schema_version": "1.0.0", "source_system_id": DEV_A_CAMERA, "data_type": "VIDEO", "variant": "CLIP",
        "file": {"original_filename": "cam01_0001.mp4", "byte_size": len(data), "mime_type": "video/mp4",
                 "checksum_sha256": hashlib.sha256(data).hexdigest()},
        "timestamp_quality": {"source": "DEVICE", "verified": True, "est_offset_ms": 20},
        "capture_start_at": "2026-11-01T01:00:00Z", "session_id": SES_A,
        "client": {"kind": "AGENT", "version": "0.1.0"},
    }


def test_probe_all(ictx, monkeypatch) -> None:
    ctx = ictx
    c = ctx.client
    hm, ha, op = ctx.as_user(102), ctx.as_user(101), ctx.as_user(106)

    # ---- A. PATCH /v1/records/{id} with evidence_refs: null
    rec = c.post(f"/v1/sessions/{SES_A}/records", json={"kind": "LOG", "title": "t"}, headers=hm).json()
    r = c.patch(f"/v1/records/{rec['record_id']}", json={"evidence_refs": None}, headers=hm)
    OUT["A_patch_null_evidence_refs"] = (r.status_code, r.json().get("code"))

    # ---- B. PUT .../versions/0/hwpx attaches to the latest version
    tpl = {"code": "SWEEP_TPL", "name": "sweep", "scope": "SESSION",
           "sections": [{"kind": "TEXT", "title": "x", "text": "{{report.title}}"}]}
    v1 = c.post("/v1/report-templates", json=tpl, headers=op).json()
    v2 = c.post("/v1/report-templates", json=tpl, headers=op).json()
    r = c.put("/v1/report-templates/SWEEP_TPL/versions/0/hwpx", content=_zip("<sec>{{report.title}}</sec>"),
              headers={**op, "Content-Type": "application/octet-stream"})
    body = r.json()
    lst = {t["version"]: t["has_hwpx"] for t in c.get("/v1/report-templates", headers=op).json()["items"]
           if t["code"] == "SWEEP_TPL"}
    OUT["B_put_hwpx_version0"] = (r.status_code, body.get("version"), lst, v1.get("version"), v2.get("version"))

    # ---- C. HWPX export of a DRAFT without {{report.status}} placeholder
    r = c.put("/v1/report-templates/EXAMPLE_SESSION_LOG/versions/1/hwpx",
              content=_zip("<sec><p>{{report.title}} / {{report.version}}</p></sec>"),
              headers={**op, "Content-Type": "application/octet-stream"})
    rep = c.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A}, headers=hm).json()
    r = c.get(f"/v1/reports/{rep['report_id']}/export", params={"format": "HWPX"}, headers=hm)
    texts = []
    if r.status_code == 200:
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            texts = [z.read(n).decode() for n in z.namelist() if n.endswith(".xml")]
    OUT["C_hwpx_draft"] = (rep.get("status"), r.status_code, texts, any("초안" in t for t in texts))

    # ---- D. capabilities vs export when PDF renderer is unavailable
    import app.adapters.report_renderers as rr

    monkeypatch.setattr(rr, "pdf_available", lambda: False)
    caps = c.get("/v1/system/capabilities", headers=hm).json()
    r = c.get(f"/v1/reports/{rep['report_id']}/export", params={"format": "PDF"}, headers=hm)
    OUT["D_caps_vs_export"] = (caps.get("report_formats"), r.status_code, r.json().get("code"))
    monkeypatch.undo()

    # ---- E. expired-but-not-swept upload, same manifest again
    data = b"\x00\x00\x00\x18ftypmp42" + b"x" * 200
    m = _manifest(data)
    r1 = c.post("/v1/uploads", json=m, headers=hm)
    created = r1.json()
    with ctx.c.sessionmaker.begin() as db:
        db.execute(update(Upload).where(Upload.upload_id == created["upload_id"])
                   .values(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    r2 = c.post("/v1/uploads", json=m, headers=hm)
    with ctx.c.sessionmaker() as db:
        a = db.get(Asset, created["asset_id"])
        state = (a.ingest_status, a.has_binary, a.storage_key)
    head = ctx.c.storage.head_object(ctx.c.settings.bucket_proc, state[2]) if state[2] else None
    from app.services.ingest import IngestService

    with ctx.c.sessionmaker.begin() as db:
        swept = IngestService(ctx.c, db).sweep_expired()
    ga = c.get(f"/v1/assets/{created['asset_id']}", headers=hm)
    OUT["E_expired_reupload"] = (r1.status_code, r2.status_code, r2.json(), state[:2], head, swept, ga.status_code)

    # ---- F. listUploadParts storage failure -> undocumented 503
    data2 = b"\x00\x00\x00\x18ftypmp42" + b"y" * 300
    created2 = c.post("/v1/uploads", json=_manifest(data2), headers=hm).json()

    def boom(*a, **k):
        raise RuntimeError("storage down")

    monkeypatch.setattr(ctx.c.storage, "list_parts", boom)
    r = c.get(f"/v1/uploads/{created2['upload_id']}/parts", headers=hm)
    OUT["F_parts_storage_down"] = (r.status_code, r.json().get("code"))
    monkeypatch.undo()

    # ---- G. PERIOD report OBSERVATION_STAT mixes observations of another project
    prj2, dev2 = fid("prj", 299), fid("dev", 399)
    with ctx.c.sessionmaker.begin() as db:
        db.add(Project(project_id=prj2, organization_id=ORG_A, name="A 다른 사업", status="ACTIVE", created_at=SEED_AT))
        db.flush()
        db.add(Device(device_id=dev2, organization_id=ORG_A, project_id=prj2, name="A2 센서", kind="SOURCE",
                      device_type="SENSOR", collector_device_id=None, status="ACTIVE",
                      collection_mode="C3_APPROVED_FULL", local_copy_policy="RETAINED", local_retention_days=None,
                      expected_interval_s=None, source_timezone=None, last_heartbeat_at=None, created_at=SEED_AT))
    obs = []
    for dev, val in ((DEV_A_SENSOR, 10.0), (dev2, 30.0)):
        rr_ = c.post("/v1/observations", headers=hm, json={
            "schema_version": "1.0.0", "source_system_id": dev,
            "observations": [{"metric": "sea_water_temperature", "observed_at": "2026-10-15T00:00:00Z",
                              "value": val, "unit": "°C", "quality_flag": "GOOD"}]})
        obs.append((rr_.status_code, rr_.json().get("results") if rr_.status_code == 200 else rr_.json()))
    ptpl = {"code": "SWEEP_PERIOD", "name": "기간", "scope": "PERIOD",
            "sections": [{"kind": "METRICS", "title": "지표", "metrics": ["EXAMPLE_MEAN_WATER_TEMP"]}]}
    c.post("/v1/report-templates", json=ptpl, headers=op)
    r = c.post("/v1/reports", headers=hm, json={"template_code": "SWEEP_PERIOD", "project_id": PRJ_A,
                                               "from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:00:00Z"})
    m0 = r.json()["metrics"][0] if r.status_code == 201 else r.json()
    OUT["G_period_obs"] = (obs, r.status_code, m0.get("value"), m0.get("input_count"),
                           m0.get("sources", {}).get("observation_ranges"))

    # ---- H. cross-org evidence ref: audit?
    with ctx.c.sessionmaker() as db:
        other_dev = db.execute(select(Device.device_id).where(Device.organization_id != ORG_A)).scalars().first()
    before = ctx.audit_actions().count("CROSS_ORG_ACCESS_ATTEMPT")
    r = c.post(f"/v1/sessions/{SES_A}/records", headers=hm, json={"kind": "LOG", "title": "x", "evidence_refs": [
        {"kind": "OBSERVATION_RANGE", "source_system_id": other_dev, "metric": "sea_water_temperature",
         "from": "2026-10-01T00:00:00Z", "to": "2026-10-02T00:00:00Z"}]})
    after = ctx.audit_actions().count("CROSS_ORG_ACCESS_ATTEMPT")
    OUT["H_cross_org_evidence"] = (r.status_code, r.json().get("errors"), before, after)

    print("\nSWEEP_RESULTS=" + json.dumps(OUT, ensure_ascii=False, default=str, indent=1))
