"""Second batch of scratch probes (read-only review, not committed)."""

from __future__ import annotations

import hashlib
import json

from app.seed import DEV_A_CAMERA, SES_A

OUT: dict = {}


def _manifest(data: bytes, **extra) -> dict:
    m = {
        "schema_version": "1.0.0", "source_system_id": DEV_A_CAMERA, "data_type": "VIDEO", "variant": "CLIP",
        "file": {"original_filename": "cam01_0001.mp4", "byte_size": len(data), "mime_type": "video/mp4",
                 "checksum_sha256": hashlib.sha256(data).hexdigest()},
        "timestamp_quality": {"source": "DEVICE", "verified": True, "est_offset_ms": 20},
        "capture_start_at": "2026-11-01T01:00:00Z", "session_id": SES_A,
        "client": {"kind": "AGENT", "version": "0.1.0"},
    }
    m.update(extra)
    return m


def test_probe_more(ictx) -> None:
    ctx = ictx
    c = ctx.client
    hm, ha, viewer = ctx.as_user(102), ctx.as_user(101), ctx.as_user(105)

    # ---- I. previous (approved) version's period_to changes after POST /versions
    rep = c.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A}, headers=hm).json()
    rid = rep["report_id"]
    v1_before = c.get(f"/v1/reports/{rid}", params={"version": 1}, headers=hm).json()["period_to"]
    ap = c.post(f"/v1/reports/{rid}/approve", headers=ha)
    snap_key = None
    with ctx.c.sessionmaker() as db:
        from app.db.models import ReportVersion

        snap_key = db.get(ReportVersion, (rid, 1)).snapshot_key
    snap = json.loads(ctx.c.storage.get_object(ctx.c.settings.bucket_evidence, snap_key))
    ps = c.patch(f"/v1/sessions/{SES_A}", json={"ends_at": "2026-11-03T00:00:00Z"}, headers=hm)
    nv = c.post(f"/v1/reports/{rid}/versions", headers=hm)
    v1_after = c.get(f"/v1/reports/{rid}", params={"version": 1}, headers=hm).json()
    OUT["I_period_to"] = {"approve": ap.status_code, "session_patch": ps.status_code, "new_version": nv.status_code,
                          "v1_before": v1_before, "snapshot_v1": snap.get("period_to"),
                          "v1_after": v1_after["period_to"], "v1_status": v1_after["status"],
                          "v2": nv.json().get("period_to")}
    # restore session end for following probes
    c.patch(f"/v1/sessions/{SES_A}", json={"ends_at": None}, headers=hm)

    # ---- J. trace for PROGRAM_VIEWER: hidden asset IDs still listed in events[].asset_ids
    data = b"\x00\x00\x00\x18ftypmp42" + b"j" * 100
    up = c.post("/v1/uploads", json=_manifest(data), headers=hm).json()
    ev = c.post("/v1/events", headers=hm, json={
        "schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL", "started_at": "2026-11-01T02:00:00Z",
        "timestamp_quality": {"source": "MANUAL", "verified": False, "est_offset_ms": None}, "source": "MANUAL",
        "linked_asset_ids": [up["asset_id"]]})
    rep2 = c.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A}, headers=hm).json()
    rid2 = rep2["report_id"]
    a2 = c.post(f"/v1/reports/{rid2}/approve", headers=ha).status_code
    s2 = c.post(f"/v1/reports/{rid2}/submit", headers=ha).status_code
    t = c.get(f"/v1/reports/{rid2}/metrics/EXAMPLE_VESSEL_EVENTS/trace", headers=viewer)
    tj = t.json()
    OUT["J_trace_viewer"] = {"event": ev.status_code, "approve": a2, "submit": s2, "trace": t.status_code,
                             "asset": up["asset_id"], "events": tj.get("events"), "assets": tj.get("assets"),
                             "hidden_count": tj.get("hidden_count"),
                             "metric_sources": (tj.get("metric") or {}).get("sources")}

    # ---- K. abort of a derived upload whose asset has a lineage row
    d1 = b"\x00\x00\x00\x18ftypmp42" + b"p" * 100
    d2 = b"\xff\xd8\xff" + b"c" * 100
    u1 = c.post("/v1/uploads", json=_manifest(d1), headers=hm).json()
    m2 = _manifest(d2, data_type="IMAGE", variant="SAMPLE_FRAME",
                   parent_ref={"checksum_sha256": hashlib.sha256(d1).hexdigest()})
    m2["file"]["mime_type"] = "image/jpeg"
    m2["file"]["original_filename"] = "frame.jpg"
    r2 = c.post("/v1/uploads", json=m2, headers=hm)
    u2 = r2.json()
    with ctx.c.sessionmaker() as db:
        from sqlalchemy import select

        from app.db.models import AssetLineage

        lin = db.execute(select(AssetLineage.child_asset_id, AssetLineage.parent_asset_id)).all()
    d_child = c.delete(f"/v1/uploads/{u2.get('upload_id')}", headers=hm)
    d_parent = c.delete(f"/v1/uploads/{u1.get('upload_id')}", headers=hm)
    OUT["K_abort_lineage"] = {"create2": r2.status_code, "lineage": [tuple(x) for x in lin],
                              "delete_child": (d_child.status_code, d_child.text[:200]),
                              "delete_parent": (d_parent.status_code, d_parent.text[:200])}

    print("\nSWEEP_RESULTS2=" + json.dumps(OUT, ensure_ascii=False, default=str, indent=1))
