#!/usr/bin/env python3
"""Intra-org (A1 vs A2) and cross-org (B vs A) device isolation matrix using REAL enrolled device credentials.
Every request that names someone else's resource must be 404 (existence hidden)."""
import json
import sys

import httpx

E = "/tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/e2e12"
BASE = "http://127.0.0.1:18416"
SES_A = "ses_00000000000000000000000401"
A1_COL, A1_CAM, A1_SEN = (f"dev_0000000000000000000000030{i}" for i in (1, 2, 3))
A2_COL, A2_CAM = "dev_00000000000000000000000321", "dev_00000000000000000000000322"
B_COL, B_CAM = "dev_00000000000000000000000311", "dev_00000000000000000000000312"
A1_ASSET, A1_UPLOAD, A2_ASSET, A2_UPLOAD = sys.argv[1:5]

cl = httpx.Client(base_url=BASE, timeout=30)


def token(dirname):
    c = json.load(open(f"{E}/{dirname}/credentials.json"))
    r = cl.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": c["client_id"],
                                        "client_secret": c["client_secret"]})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


T = {"A1": token("agentdir"), "A2": token("agentdir2"), "B": token("agentdirB")}
bad = 0
OBS = {"schema_version": "1.0.0", "observations": [
    {"metric": "sea_water_temperature", "observed_at": "2026-11-12T01:00:00Z", "value": 1.0, "unit": "degC",
     "quality_flag": "GOOD"}]}
EVT = {"schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL", "started_at": "2026-11-12T01:00:00Z",
       "timestamp_quality": {"source": "MANUAL", "verified": False}, "source": "DEVICE"}
MANIFEST = {"schema_version": "1.0.0", "watch_rule_id": "x", "data_type": "VIDEO", "variant": "CLIP",
            "data_role": "CORE", "session_label": "A-2026-11-01",
            "file": {"original_filename": "x.mp4", "original_relative_path": "A-2026-11-01/x/x.mp4",
                     "byte_size": 10, "checksum_sha256": "a" * 64, "mime_type": "video/mp4"},
            "client": {"kind": "AGENT", "version": "0.1.0-dev", "local_collection_id": "lc-x"},
            "timestamp_quality": {"source": "UNKNOWN", "verified": False}}


def run(who, label, method, path, expected, **kw):
    global bad
    r = cl.request(method, path, headers=T[who], **kw)
    try:
        code = r.json().get("code", "")
    except Exception:  # noqa: BLE001
        code = ""
    ok = r.status_code == expected
    bad += 0 if ok else 1
    print(f"{'OK      ' if ok else 'MISMATCH'} {who:2s} {label:62s} {method:6s} -> {r.status_code} {code}")


def others(who, col, cam, asset, upload, label):
    """`who` touches the resources of `label`."""
    run(who, f"{label} asset receipt", "GET", f"/v1/assets/{asset}/receipt", 404)
    run(who, f"{label} upload status", "GET", f"/v1/uploads/{upload}", 404)
    run(who, f"{label} upload parts", "GET", f"/v1/uploads/{upload}/parts", 404)
    run(who, f"{label} upload complete", "POST", f"/v1/uploads/{upload}/complete", 404,
        json={"parts": [{"part_number": 1, "etag": "x"}]})
    run(who, f"{label} upload abort (DELETE)", "DELETE", f"/v1/uploads/{upload}", 404)
    run(who, f"{label} collector policy", "GET", f"/v1/devices/{col}/policy", 404)
    run(who, f"{label} collector health", "GET", f"/v1/agents/{col}/health", 404)
    run(who, f"{label} source mapping", "GET", f"/v1/devices/{cam}/observation-mapping", 404)
    run(who, f"{label} POST /v1/uploads (source of other)", "POST", "/v1/uploads", 404,
        json={**MANIFEST, "source_system_id": cam})
    run(who, f"{label} POST /v1/observations (source of other)", "POST", "/v1/observations", 404,
        json={**OBS, "source_system_id": cam})
    run(who, f"{label} POST /v1/events (source of other)", "POST", "/v1/events", 404,
        json={**EVT, "source_system_id": cam})
    run(who, f"{label} POST /v1/events (own source, linked asset of other)", "POST", "/v1/events", 404,
        json={**EVT, "source_system_id": {"A1": A1_CAM, "A2": A2_CAM, "B": B_CAM}[who], "linked_asset_ids": [asset]})


print("== A1 -> A2 (same org, different collector)")
others("A1", A2_COL, A2_CAM, A2_ASSET, A2_UPLOAD, "A2's")
print("== A2 -> A1")
others("A2", A1_COL, A1_CAM, A1_ASSET, A1_UPLOAD, "A1's")
print("== B -> A1 (other org)")
others("B", A1_COL, A1_CAM, A1_ASSET, A1_UPLOAD, "A1's")
print("== B -> A2 (other org)")
others("B", A2_COL, A2_CAM, A2_ASSET, A2_UPLOAD, "A2's")
print("== sanity: each token still reaches its OWN resources")
run("A1", "A1 own receipt", "GET", f"/v1/assets/{A1_ASSET}/receipt", 200)
run("A1", "A1 own upload status", "GET", f"/v1/uploads/{A1_UPLOAD}", 200)
run("A2", "A2 own receipt", "GET", f"/v1/assets/{A2_ASSET}/receipt", 200)
run("A2", "A2 own upload status", "GET", f"/v1/uploads/{A2_UPLOAD}", 200)
run("A2", "A2 own policy", "GET", f"/v1/devices/{A2_COL}/policy", 200)
run("A2", "A2 own health", "GET", f"/v1/agents/{A2_COL}/health", 200)
run("B", "B /v1/me", "GET", "/v1/me", 200)
run("B", "B own policy (default pv-0)", "GET", f"/v1/devices/{B_COL}/policy", 200)
run("B", "B own health", "GET", f"/v1/agents/{B_COL}/health", 200)
run("A1", "A1 /v1/me", "GET", "/v1/me", 200)
print(json.dumps({"mismatches": bad}))
