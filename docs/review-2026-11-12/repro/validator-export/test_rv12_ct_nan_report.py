from __future__ import annotations
import pytest
from app.seed import DEV_A_SENSOR, SES_A
pytestmark = pytest.mark.integration

def test_nan_poisons_reports(ctx):
    h = {**ctx.as_user(102), "content-type": "application/json"}
    good = ('{"schema_version":"1.0.0","source_system_id":"%s","session_id":"%s","observations":[{"metric":"sea_water_temperature",'
            '"observed_at":"2026-11-01T01:00:00Z","value":18.2,"unit":"degC","quality_flag":"GOOD"}]}' % (DEV_A_SENSOR, SES_A))
    r = ctx.client.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A}, headers=ctx.as_user(102))
    print("baseline create report (no observations) ->", r.status_code)
    r = ctx.client.post("/v1/observations", content=good, headers=h); print("post good ->", r.status_code, r.json().get("accepted"))
    r = ctx.client.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A}, headers=ctx.as_user(102))
    print("create report with 1 good obs ->", r.status_code)
    bad = good.replace("2026-11-01T01:00:00Z", "2026-11-01T01:05:00Z").replace("18.2", "NaN")
    r = ctx.client.post("/v1/observations", content=bad, headers=h); print("post NaN ->", r.status_code, r.json().get("accepted"))
    r = ctx.client.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A}, headers=ctx.as_user(102))
    print("create report after NaN obs ->", r.status_code, (r.json().get("code") if r.headers.get("content-type", "").endswith("json") else r.text[:80]))
