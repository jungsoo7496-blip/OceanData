from __future__ import annotations
import hashlib, json
import pytest
from tests.integration.test_ingest import manifest, mp4_bytes
pytestmark = pytest.mark.integration

def test_amplification_and_upload_500(ctx):
    h = ctx.as_user(102)
    m = manifest(mp4_bytes(1024, 3))
    r = ctx.client.post("/v1/uploads", json={**m, "location": {"type": "LineString", "coordinates": 5}}, headers=h)
    print("POST /v1/uploads LineString coordinates=5 ->", r.status_code, r.json().get("code"))
    for n in (2000, 20000):
        body = json.dumps({**m, "location": {"type": "LineString", "coordinates": "x" * n}})
        r = ctx.client.post("/v1/uploads", content=body, headers={**h, "content-type": "application/json"})
        errs = r.json().get("errors", [])
        print(f"POST /v1/uploads coordinates=<{n} chars>: request={len(body)}B -> {r.status_code} errors={len(errs)} response={len(r.content)}B amplification={len(r.content)/len(body):.0f}x")
    r = ctx.client.post("/v1/contracts/validate", json={"contract": "upload-manifest", "document": {**m, "location": {"type": "LineString", "coordinates": "x" * 20000}}}, headers=h)
    print("POST /v1/contracts/validate same ->", r.status_code, "problems=", len(r.json()["problems"]), "response=", len(r.content), "B")
