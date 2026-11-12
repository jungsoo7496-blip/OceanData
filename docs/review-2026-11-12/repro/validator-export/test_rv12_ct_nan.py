from __future__ import annotations
import json
import pytest
from app.seed import DEV_A_SENSOR, SES_A
pytestmark = pytest.mark.integration

def test_nan(ctx):
    h = {**ctx.as_user(102), "content-type": "application/json"}
    raw = ('{"schema_version":"1.0.0","source_system_id":"%s","session_id":"%s","observations":[{"metric":"sea_water_temperature",'
           '"observed_at":"2026-11-01T02:00:00Z","value":NaN,"unit":"degC","quality_flag":"GOOD"}]}' % (DEV_A_SENSOR, SES_A))
    r = ctx.client.post("/v1/observations", content=raw, headers=h)
    print("POST NaN ->", r.status_code, r.text[:200])
    r = ctx.client.get("/v1/observations", params={"source_system_id": DEV_A_SENSOR, "from": "2026-11-01T00:00:00Z", "to": "2026-11-02T00:00:00Z"}, headers=h)
    print("GET ->", r.status_code, r.headers.get("content-type"), r.text[:400])
    try:
        json.loads(r.text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError("non-standard JSON literal " + c)))
        print("strict JSON parse: OK")
    except ValueError as e:
        print("strict JSON parse FAILS:", e)
    r = ctx.client.get("/v1/observations", params={"source_system_id": DEV_A_SENSOR, "metric": "sea_water_temperature", "from": "2026-11-01T00:00:00Z", "to": "2026-11-02T00:00:00Z", "interval": "PT60M"}, headers=h)
    print("GET interval ->", r.status_code, r.text[:300])
