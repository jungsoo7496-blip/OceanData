"""Scratch review probes (read-only review; not part of the repo).

Run from backend/: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD TEST_DATABASE_URL=... \
  .venv/bin/pytest -p tests.conftest -p no:cacheprovider -q -s <this file> -k <name>
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select, text

from app.db.models import AssetLineage, OrgQuota, Upload
from app.seed import ORG_A
from tests.integration.test_ingest import MB, drain, manifest, mp4_bytes, put_parts


# 1) parent_ref lineage row -> abort 500, sweeper wedged
def test_rv_lineage_blocks_abort_and_sweep(ictx) -> None:
    h = ictx.as_user(102)
    parent = mp4_bytes(5 * MB, seed=101)
    rp = ictx.client.post("/v1/uploads", json=manifest(parent), headers=h)
    assert rp.status_code == 201, rp.text
    child = mp4_bytes(5 * MB, seed=102)
    mc = manifest(child, variant="PROXY", parent_ref={"checksum_sha256": hashlib.sha256(parent).hexdigest()})
    rc = ictx.client.post("/v1/uploads", json=mc, headers=h)
    assert rc.status_code == 201, rc.text
    with ictx.c.sessionmaker() as db:
        n = db.execute(select(AssetLineage).where(AssetLineage.child_asset_id == rc.json()["asset_id"])).all()
    print("lineage rows for child:", len(n))
    r = ictx.client.delete(f"/v1/uploads/{rc.json()['upload_id']}", headers=h)
    print("DELETE child upload ->", r.status_code, r.text[:200])
    with ictx.c.sessionmaker() as db:
        print("child upload status after DELETE:", db.get(Upload, rc.json()["upload_id"]).status)
    # expiry path: child expires first, parent second
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE uploads SET expires_at = now() - interval '2 minute' WHERE upload_id = :u"),
                     {"u": rc.json()["upload_id"]})
        conn.execute(text("UPDATE uploads SET expires_at = now() - interval '1 minute' WHERE upload_id = :u"),
                     {"u": rp.json()["upload_id"]})
    from app.workers.main import sweep

    for i in range(2):
        try:
            print("sweep ->", sweep(ictx.c))
        except Exception as exc:  # noqa: BLE001
            print(f"sweep #{i} raised:", type(exc).__name__, str(exc).splitlines()[0][:160])
    with ictx.c.sessionmaker() as db:
        print("parent upload status:", db.get(Upload, rp.json()["upload_id"]).status,
              "reserved:", db.get(OrgQuota, ORG_A).reserved_bytes)
    # the parent's own expiry is also stuck behind the child: GET parts on parent -> 410 works individually
    r = ictx.client.get(f"/v1/uploads/{rp.json()['upload_id']}/parts", headers=h)
    print("GET parent parts ->", r.status_code)
    r = ictx.client.get(f"/v1/uploads/{rc.json()['upload_id']}/parts", headers=h)
    print("GET child parts ->", r.status_code)


# 3b) CHECKSUM_MISMATCH (transfer corruption) -> the correct file can never be re-sent
def test_rv_already_registered_after_checksum_quarantine(ictx) -> None:
    h = ictx.as_user(102)
    good = mp4_bytes(5 * MB, seed=401)
    corrupted = bytearray(good)
    corrupted[1000] ^= 0xFF
    m = manifest(good)  # agent declares the true local checksum
    r = ictx.client.post("/v1/uploads", json=m, headers=h)
    cr = r.json()
    parts = put_parts(cr, bytes(corrupted))  # bytes damaged in transit
    ictx.client.post(f"/v1/uploads/{cr['upload_id']}/complete", json={"parts": parts}, headers=h)
    drain(ictx)
    st = ictx.client.get(f"/v1/uploads/{cr['upload_id']}", headers=h).json()
    print("status:", st["ingest_status"], st.get("quarantine_reason"))
    retry = ictx.client.post("/v1/uploads", json=m, headers=h)
    print("re-POST of the correct file ->", retry.status_code, retry.json())
