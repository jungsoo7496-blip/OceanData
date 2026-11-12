"""Scratch: (1) prod-like validator without rfc3339_validator, (2) NaN/Infinity values, (3) item without any time field."""
from __future__ import annotations
import json
import pytest
from app.seed import DEV_A_SENSOR, SES_A, DEV_A_COLLECTOR
from app.services import contracts

pytestmark = pytest.mark.integration
T = "2026-11-01T01:00:00Z"
OBS = {"metric": "sea_water_temperature", "observed_at": T, "value": 18.2, "unit": "degC", "quality_flag": "GOOD"}
EVENT = {"schema_version": "1.0.0", "session_id": SES_A, "event_code": "FLOATING_OBJECT", "started_at": T,
         "timestamp_quality": {"source": "MANUAL", "verified": False, "est_offset_ms": None}, "source": "MANUAL"}


def show(label, r):
    ct = r.headers.get("content-type", "")
    body = r.json() if "json" in ct else r.text[:100]
    code = body.get("code") if isinstance(body, dict) else body
    print(f"{r.status_code}  {label} -> {code}")
    return body


def test_prod_like_formats(ctx, monkeypatch):
    h = ctx.as_user(102)
    r = ctx.client.post("/v1/events", json={**EVENT, "started_at": "2026-13-02T00:00:00Z"}, headers=h)
    show("[dev venv: rfc3339_validator present] event started_at=2026-13-02T00:00:00Z", r)
    from jsonschema import FormatChecker
    def prodlike():
        fc = FormatChecker()
        for k in ("date-time", "time"):
            fc.checkers.pop(k, None)
        return fc
    monkeypatch.setattr(contracts, "FormatChecker", prodlike)
    r = ctx.client.post("/v1/events", json={**EVENT, "started_at": "2026-13-02T00:00:00Z"}, headers=h)
    show("[prod-like: no rfc3339_validator] event started_at=2026-13-02T00:00:00Z", r)
    r = ctx.client.post("/v1/events", json={**EVENT, "started_at": "2026-02-30T00:00:00Z"}, headers=h)
    show("[prod-like] event started_at=2026-02-30T00:00:00Z", r)
    r = ctx.client.post("/v1/events", json={**EVENT, "ended_at": "2026-11-31T25:00:00Z"}, headers=h)
    show("[prod-like] event ended_at=2026-11-31T25:00:00Z", r)
    hb = json.load(open("/home/user/oceandata/contracts/examples/agent-heartbeat.vessel-agent.json"))
    hb = {**hb, "collector_device_id": DEV_A_COLLECTOR, "last_success_at": "2026-13-02T00:00:00Z"}
    dev_token = None


def test_nan_inf_and_missing_time(ctx):
    h = ctx.as_user(102)
    base = {"schema_version": "1.0.0", "source_system_id": DEV_A_SENSOR, "session_id": SES_A}
    # item without any time field
    noT = {k: v for k, v in OBS.items() if k != "observed_at"}
    r = ctx.client.post("/v1/observations", json={**base, "observations": [OBS, noT]}, headers=h)
    b = show("batch of [valid, item without observed_at/source_timestamp]", r)
    if r.status_code == 400: print("     errors:", [(e["pointer"], e["code"]) for e in b["errors"]])
    # NaN / Infinity values: send raw text (json.dumps would emit the same non-standard literals)
    for lit in ("NaN", "Infinity", "-Infinity"):
        raw = ('{"schema_version":"1.0.0","source_system_id":"%s","session_id":"%s","observations":[{"metric":"sea_water_temperature",'
               '"observed_at":"2026-11-01T02:00:00Z","value":%s,"unit":"degC","quality_flag":"GOOD"}]}' % (DEV_A_SENSOR, SES_A, lit))
        r = ctx.client.post("/v1/observations", content=raw, headers={**h, "content-type": "application/json"})
        b = show(f"observation value={lit}", r)
        if r.status_code == 200: print("     results:", b.get("results"))
    r = ctx.client.get("/v1/observations", params={"source_system_id": DEV_A_SENSOR, "from": "2026-11-01T00:00:00Z", "to": "2026-11-02T00:00:00Z"}, headers=h)
    show("GET /v1/observations after storing NaN/Infinity", r)
    r = ctx.client.post("/v1/contracts/validate", content=json.dumps({"contract": "observation-batch", "document": {**base, "observations": [{**OBS, "value": float("nan")}]}}), headers={**h, "content-type": "application/json"})
    b = show("validate(value=NaN)", r)
    print("     ", b)
