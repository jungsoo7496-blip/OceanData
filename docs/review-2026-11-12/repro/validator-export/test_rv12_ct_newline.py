"""Scratch: downstream effect of trailing-newline values accepted by the Python-re `$` semantics."""
from __future__ import annotations
import hashlib
import pytest
from app.seed import DEV_A_CAMERA, SES_A
from tests.integration.test_ingest import manifest, mp4_bytes

pytestmark = pytest.mark.integration


def test_newline_values(ictx):
    h = ictx.as_user(102)
    data = mp4_bytes(1024, seed=7)
    ok = manifest(data)
    r = ictx.client.post("/v1/uploads", json=ok, headers=h)
    print("baseline valid upload ->", r.status_code)
    data2 = mp4_bytes(1024, seed=8)
    sha2 = hashlib.sha256(data2).hexdigest()
    bad_sha = manifest(data2, sha=sha2 + "\n")
    r = ictx.client.post("/v1/uploads", json=bad_sha, headers=h)
    print("checksum+\\n ->", r.status_code, (r.json().get("code") if r.headers.get("content-type", "").endswith("json") else r.text[:100]))
    data3 = mp4_bytes(1024, seed=9)
    bad_mime = manifest(data3, mime="video/mp4\n")
    r = ictx.client.post("/v1/uploads", json=bad_mime, headers=h)
    print("mime 'video/mp4\\n' ->", r.status_code, (r.json().get("code") if r.headers.get("content-type", "").endswith("json") else r.text[:100]))
    # same via contract validate (no side effects)
    r = ictx.client.post("/v1/contracts/validate", json={"contract": "upload-manifest", "document": bad_sha}, headers=h)
    print("validate(checksum+\\n) ->", r.status_code, r.json())
