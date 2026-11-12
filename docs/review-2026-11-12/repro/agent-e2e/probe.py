#!/usr/bin/env python3
"""e2e12 probe: uses the SAME device credentials the real agent saved (credentials.json) to get a token via
POST /v1/auth/token, then runs positive + negative checks against the local API (through the logging forwarder).
Prints one JSON line per check: {label, method, path, status, code, expected, verdict}."""
import json
import sys
import urllib.parse

import httpx

E = "/tmp/claude-0/-home-user-oceandata/804cf8c5-3e8a-5d79-bc09-75a711922000/scratchpad/e2e12"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18416"
SES_A, SES_B = "ses_00000000000000000000000401", "ses_00000000000000000000000411"
COL_A, CAM_A, SEN_A = (f"dev_0000000000000000000000030{i}" for i in (1, 2, 3))
COL_B, CAM_B = "dev_00000000000000000000000311", "dev_00000000000000000000000312"
PRJ_A = "prj_00000000000000000000000201"
CLIP_ASSET = sys.argv[2] if len(sys.argv) > 2 else "ast_00000000000000000000000999"

creds = json.load(open(f"{E}/agentdir/credentials.json"))
cl = httpx.Client(base_url=BASE, timeout=30)
r = cl.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": creds["client_id"],
                                    "client_secret": creds["client_secret"]})
print(json.dumps({"label": "token (client_credentials, agent creds)", "method": "POST", "path": "/v1/auth/token",
                  "status": r.status_code, "expected": 200}))
tok = r.json()["access_token"]
H = {"Authorization": f"Bearer {tok}"}
bad = 0


def chk(label, method, path, expected, **kw):
    global bad
    r = cl.request(method, path, headers={**H, **kw.pop("headers", {})}, **kw)
    code = ""
    try:
        code = r.json().get("code", "")
    except Exception:  # noqa: BLE001
        pass
    exp = expected if isinstance(expected, tuple) else (expected,)
    verdict = "OK" if r.status_code in exp else "MISMATCH"
    if verdict != "OK":
        bad += 1
    print(json.dumps({"label": label, "method": method, "path": path.split("?")[0], "status": r.status_code,
                      "code": code, "expected": list(exp), "verdict": verdict}, ensure_ascii=False))
    return r


print("== positive (agent-visible surface)")
chk("e) health of own collector", "GET", f"/v1/agents/{COL_A}/health", 200)
chk("c) own policy", "GET", f"/v1/devices/{COL_A}/policy", 200)
chk("d) mapping of mapped source (sensor)", "GET", f"/v1/devices/{SEN_A}/observation-mapping", 200)
chk("b) /v1/me", "GET", "/v1/me", 200)

print("== j) negatives, same device token (denial expected)")
chk("GET /v1/reports", "GET", "/v1/reports", 403)
chk("GET session records", "GET", f"/v1/sessions/{SES_A}/records", 403)
chk("POST session records", "POST", f"/v1/sessions/{SES_A}/records", 403,
    json={"kind": "NOTE", "title": "e2e12 device must not write records"})
q = urllib.parse.urlencode({"source_system_id": SEN_A, "from": "2026-11-01T00:00:00Z", "to": "2026-11-13T00:00:00Z"})
chk("GET observations of a MAPPED source (sensor)", "GET", f"/v1/observations?{q}", 403)
q = urllib.parse.urlencode({"source_system_id": CAM_B, "from": "2026-11-01T00:00:00Z", "to": "2026-11-13T00:00:00Z"})
chk("GET observations of a NOT mapped source (other org camera)", "GET", f"/v1/observations?{q}", 403)
q = urllib.parse.urlencode({"from": "2026-11-01T00:00:00Z", "to": "2026-11-13T00:00:00Z"})
chk("GET observations (no source filter)", "GET", f"/v1/observations?{q}", 403)
chk("GET /v1/usage", "GET", "/v1/usage", 403)
chk("GET /v1/incidents", "GET", "/v1/incidents", 403)
chk("GET /v1/ingest-status", "GET", "/v1/ingest-status", 403)
chk("GET /v1/audit-logs", "GET", "/v1/audit-logs", 403)

print("== j) other-org device (DEV_B_*): expect NOT FOUND (existence hidden)")
chk("policy of other-org collector", "GET", f"/v1/devices/{COL_B}/policy", 404)
chk("health of other-org collector", "GET", f"/v1/agents/{COL_B}/health", 404)
chk("mapping of other-org camera", "GET", f"/v1/devices/{CAM_B}/observation-mapping", 404)
HB = {"schema_version": "1.0.0", "agent_version": "0.1.0", "policy_version": "pv-x",
      "queue": {"items": 0, "bytes": 0, "observations": 0}, "sent_at": "2026-11-12T02:00:00Z",
      "disk_free_bytes": 1000000000, "plugins": [], "sources": []}
chk("heartbeat claiming other-org collector", "POST", "/v1/agents/heartbeat", 404,
    json={**HB, "collector_device_id": COL_B})
chk("heartbeat claiming own mapped SOURCE as collector", "POST", "/v1/agents/heartbeat", 404,
    json={**HB, "collector_device_id": CAM_A})
chk("observations for other-org camera", "POST", "/v1/observations", 404,
    json={"schema_version": "1.0.0", "source_system_id": CAM_B, "observations": [
        {"metric": "sea_water_temperature", "observed_at": "2026-11-12T01:00:00Z", "value": 1.0, "unit": "degC",
         "quality_flag": "GOOD"}]})
chk("receipt of unknown/other-org asset id", "GET", "/v1/assets/ast_00000000000000000000000998/receipt", 404)

print("== extra same-org scope checks (not on the agent path)")
chk("health of mapped SOURCE (not a collector)", "GET", f"/v1/agents/{CAM_A}/health", 404)
chk("policy of mapped SOURCE", "GET", f"/v1/devices/{CAM_A}/policy", 404)
chk("mapping of own COLLECTOR (not a mapped source id? it is own id)", "GET", f"/v1/devices/{COL_A}/observation-mapping",
    (200, 404))
chk("GET /v1/devices (device:read)", "GET", "/v1/devices", 403)
chk("GET /v1/sessions (session:read)", "GET", "/v1/sessions", 403)
chk("GET /v1/assets/{id} (asset:read)", "GET", f"/v1/assets/{CLIP_ASSET}", 403)
chk("GET /v1/events (event:read)", "GET", "/v1/events", 403)
chk("POST /v1/devices/{c}/enrollment-codes", "POST", f"/v1/devices/{COL_A}/enrollment-codes", 403)
chk("PUT mapping (mapping:write)", "PUT", f"/v1/devices/{SEN_A}/observation-mapping", 403,
    json={"format": "mqtt_json", "topic_filter": "x/+", "columns": [{"field": "a", "metric": "sea_water_temperature"}]})
chk("GET /v1/webhooks/subscriptions", "GET", "/v1/webhooks/subscriptions", 403)
chk("download-url (asset:download)", "POST", f"/v1/assets/{CLIP_ASSET}/download-url", (403, 404))
chk("GET /v1/projects (project:read)", "GET", "/v1/projects", 403)
chk("GET /v1/quality-reports", "GET", "/v1/quality-reports", 403)
print(json.dumps({"mismatches": bad}))
