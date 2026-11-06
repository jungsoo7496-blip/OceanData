"""Scratch review probes (read-only review; not part of the repo).

Run from backend/: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD TEST_DATABASE_URL=... \
  .venv/bin/pytest -p tests.conftest -p no:cacheprovider -q -s <this file> -k <name>
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select, text

from app.db.models import Asset, AssetLineage, OrgQuota, Upload
from app.seed import ORG_A, SES_A
from tests.integration.test_ingest import MB, drain, manifest, mp4_bytes, put_parts


def _quota(c) -> OrgQuota:
    with c.sessionmaker() as db:
        return db.get(OrgQuota, ORG_A)


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


# 3a) expired-but-unswept session -> ALREADY_REGISTERED for a file whose bytes never arrived
def test_rv_already_registered_for_expired_session(ictx) -> None:
    h = ictx.as_user(102)
    data = mp4_bytes(5 * MB, seed=301)
    m = manifest(data)
    first = ictx.client.post("/v1/uploads", json=m, headers=h).json()
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE uploads SET expires_at = now() - interval '1 minute' WHERE upload_id = :u"),
                     {"u": first["upload_id"]})
    again = ictx.client.post("/v1/uploads", json=m, headers=h)
    print("re-POST after expiry ->", again.status_code, again.json())
    rc = ictx.client.get(f"/v1/assets/{first['asset_id']}/receipt", headers=h)
    print("receipt ->", rc.status_code)
    from app.workers.main import sweep

    print("sweep ->", sweep(ictx.c))
    print("asset after sweep ->", ictx.client.get(f"/v1/assets/{first['asset_id']}", headers=h).status_code)


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


# 7) complete vs abort race: S3 object completed, DB rows gone -> orphan object outside quota
def test_rv_complete_abort_race_orphans_object(ictx) -> None:
    from app.services.ingest import IngestService

    h = ictx.as_user(102)
    data = mp4_bytes(6 * MB, seed=701)
    cr = ictx.client.post("/v1/uploads", json=manifest(data), headers=h).json()
    parts = put_parts(cr, data)
    storage = ictx.c.storage
    real_complete = storage.complete_multipart_upload
    token = h["Authorization"].split()[1]

    def racing_complete(bucket, key, upload_id, ps):  # type: ignore[no-untyped-def]
        real_complete(bucket, key, upload_id, ps)
        # a concurrent DELETE /v1/uploads/{id} lands here (between S3 complete and the DB flush)
        with ictx.c.sessionmaker.begin() as db2:
            p = ictx.c.authenticator.authenticate(db2, token)
            svc = IngestService(ictx.c, db2)
            svc.abort(svc.load_upload(p, cr["upload_id"]))

    storage.complete_multipart_upload = racing_complete
    try:
        r = ictx.client.post(f"/v1/uploads/{cr['upload_id']}/complete", json={"parts": parts}, headers=h)
    finally:
        storage.complete_multipart_upload = real_complete
    print("complete ->", r.status_code, r.text[:120])
    with ictx.c.sessionmaker() as db:
        up = db.get(Upload, cr["upload_id"])
        a = db.get(Asset, cr["asset_id"])
        print("upload status:", up.status, "asset row:", a)
        info = ictx.c.storage.head_object(up.bucket, up.object_key)
        print("object in", up.bucket, "size:", info.size if info else None)
        q = db.get(OrgQuota, ORG_A)
        print("quota used/reserved/quarantine:", q.used_bytes, q.reserved_bytes, q.quarantine_bytes)
    retry = ictx.client.post(f"/v1/uploads/{cr['upload_id']}/complete", json={"parts": parts}, headers=h)
    print("client retry complete ->", retry.status_code, retry.json().get("code"))


# 8) deleting an asset/session while its upload is in progress: certificate issued, upload still completes
def test_rv_delete_during_upload(ictx) -> None:
    h = ictx.as_user(102)
    admin = ictx.as_user(101, mfa=True)
    data = mp4_bytes(6 * MB, seed=801)
    cr = ictx.client.post("/v1/uploads", json=manifest(data), headers=h).json()
    q0 = _quota(ictx.c)
    d = ictx.client.post(f"/v1/sessions/{SES_A}/deletion", json={"reason": "개인정보 포함 회차 삭제"}, headers=admin)
    print("session deletion ->", d.status_code, [o["object_key"][-20:] for o in d.json().get("objects", [])])
    q1 = _quota(ictx.c)
    print("quota used before/after deletion:", q0.used_bytes, q1.used_bytes, "reserved:", q1.reserved_bytes)
    parts = put_parts(cr, data)
    r = ictx.client.post(f"/v1/uploads/{cr['upload_id']}/complete", json={"parts": parts}, headers=h)
    print("complete after deletion ->", r.status_code, r.json().get("ingest_status"))
    with ictx.c.sessionmaker() as db:
        up = db.get(Upload, cr["upload_id"])
        info = ictx.c.storage.head_object(up.bucket, up.object_key)
        a = db.get(Asset, cr["asset_id"])
        print("object exists after 'deletion':", info.size if info else None, "asset deleted_at:", a.deleted_at,
              "storage_key:", a.storage_key)
