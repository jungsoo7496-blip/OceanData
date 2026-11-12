"""Scratch: end-to-end confirmation of 500s / validator behavior via the real FastAPI app (DB per test)."""
from __future__ import annotations
import json
import pytest
from app.seed import DEV_A_SENSOR, SES_A, DEV_A_COLLECTOR

pytestmark = pytest.mark.integration
OBS = {"metric": "sea_water_temperature", "observed_at": "2026-11-02T00:00:00Z", "value": 18.2, "unit": "degC", "quality_flag": "GOOD"}
MANIFEST = json.load(open("/home/user/oceandata/contracts/examples/upload-manifest.vessel-video.json"))


def post(ctx, path, body, who=102):
    r = ctx.client.post(path, json=body, headers=ctx.as_user(who))
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith(("application/json", "application/problem+json")) else r.text)


def test_crashes_end_to_end(ctx):
    base = {"schema_version": "1.0.0", "source_system_id": DEV_A_SENSOR, "session_id": SES_A}
    cases = {
        "obs: observations=5": ("/v1/observations", {**base, "observations": 5}),
        "obs: observations=true": ("/v1/observations", {**base, "observations": True}),
        "obs: observed_at=0001-01-01T00:00:00+23:59": ("/v1/observations", {**base, "observations": [{**OBS, "observed_at": "0001-01-01T00:00:00+23:59"}]}),
        "obs: observed_at=9999-12-31T23:59:59-23:59": ("/v1/observations", {**base, "observations": [{**OBS, "observed_at": "9999-12-31T23:59:59-23:59"}]}),
        "validate obs observations=5": ("/v1/contracts/validate", {"contract": "observation-batch", "document": {**base, "observations": 5}}),
        "validate manifest LineString coordinates=5": ("/v1/contracts/validate", {"contract": "upload-manifest", "document": {**MANIFEST, "location": {"type": "LineString", "coordinates": 5}}}),
        "validate manifest Polygon coordinates=[1]": ("/v1/contracts/validate", {"contract": "upload-manifest", "document": {**MANIFEST, "location": {"type": "Polygon", "coordinates": [1]}}}),
        "validate manifest Point big int": ("/v1/contracts/validate", {"contract": "upload-manifest", "document": {**MANIFEST, "location": {"type": "Point", "coordinates": [10**400, 1]}}}),
        "validate obs observed_at overflow": ("/v1/contracts/validate", {"contract": "observation-batch", "document": {**base, "observations": [{**OBS, "observed_at": "0001-01-01T00:00:00+23:59"}]}}),
    }
    for name, (path, body) in cases.items():
        st, js = post(ctx, path, body)
        print(f"{st}  {name}  -> {(js.get('code') if isinstance(js, dict) else js)}")
