"""RV12 scratch: deletion / S3 ordering reproductions (throwaway)."""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.db.models import Asset, DeletionCertificate, OrgQuota
from app.seed import ORG_A
from tests.integration.test_lifecycle import asset, upload_csv

pytestmark = pytest.mark.integration


def _q(ctx):
    with ctx.c.sessionmaker() as db:
        q = db.get(OrgQuota, ORG_A)
        return (q.used_bytes, q.quarantine_bytes, q.reserved_bytes)


def test_delete_with_nul_reason_destroys_object_but_rolls_back_db(ictx):
    aid = upload_csv(ictx, b"t,v\n1,1\n")
    a = asset(ictx, aid)
    bucket = ictx.c.settings.buckets[a.storage_zone]
    key = a.storage_key
    assert ictx.c.storage.head_object(bucket, key) is not None
    before = _q(ictx)
    r = ictx.client.post(f"/v1/assets/{aid}/deletion", json={"reason": "ab\u0000cd"},
                         headers=ictx.as_user(101, mfa=True))
    print("OBS DELETE status", r.status_code, r.text[:200])
    a2 = asset(ictx, aid)
    print("OBS asset deleted_at:", a2.deleted_at, "storage_key:", a2.storage_key, "ingest:", a2.ingest_status)
    print("OBS S3 object still there?:", ictx.c.storage.head_object(bucket, key))
    with ictx.c.sessionmaker() as db:
        print("OBS certs:", db.scalars(select(DeletionCertificate)).all())
    print("OBS quota before/after", before, _q(ictx))
    # download URL of the 'live' asset now points to a missing object
    d = ictx.client.post(f"/v1/assets/{aid}/download-url", headers=ictx.as_user(102))
    print("OBS download-url", d.status_code)


def test_delete_swallows_s3_delete_failure_and_certifies(ictx, monkeypatch):
    aid = upload_csv(ictx, b"t,v\n2,2\n")
    a = asset(ictx, aid)
    bucket = ictx.c.settings.buckets[a.storage_zone]
    key = a.storage_key

    def boom(*_a, **_k):
        raise TimeoutError("synthetic s3 outage")

    monkeypatch.setattr(ictx.c.storage, "delete_object", boom)
    r = ictx.client.post(f"/v1/assets/{aid}/deletion", json={"reason": "outage test"},
                         headers=ictx.as_user(101, mfa=True))
    print("OBS DELETE status", r.status_code, r.json().get("objects"))
    monkeypatch.undo()
    a2 = asset(ictx, aid)
    print("OBS asset deleted_at:", a2.deleted_at, "storage_key:", a2.storage_key)
    print("OBS S3 object still there?:", ictx.c.storage.head_object(bucket, key))
    print("OBS quota", _q(ictx))


def test_legal_hold_does_not_lock_row_so_racing_delete_wins(ictx, monkeypatch):
    """Operator sets hold; an ORG_ADMIN deletion that starts after the hold handler's SELECT but before its commit
    is not blocked: the hold 'succeeds' on an already-deleted asset."""
    import threading

    aid = upload_csv(ictx, b"t,v\n3,3\n")
    real = ictx.c.audit.record
    out = {}

    def record(action, **kw):
        if action == "LEGAL_HOLD_SET":
            def do_delete():
                out["del"] = ictx.client.post(f"/v1/assets/{aid}/deletion", json={"reason": "race delete"},
                                              headers=ictx.as_user(101, mfa=True))
            t = threading.Thread(target=do_delete)
            t.start()
            t.join(30)
            out["delete_finished_while_hold_in_flight"] = not t.is_alive()
        return real(action, **kw)

    monkeypatch.setattr(ictx.c.audit, "record", record)
    r = ictx.client.post(f"/v1/assets/{aid}/legal-hold", json={"hold": True, "reason": "litigation hold"},
                         headers=ictx.as_user(106, mfa=True))
    monkeypatch.setattr(ictx.c.audit, "record", real)
    a = asset(ictx, aid)
    print("OBS legal-hold response", r.status_code, r.json())
    print("OBS racing delete", out["del"].status_code, "finished while hold in flight:", out["delete_finished_while_hold_in_flight"])
    print("OBS final asset: legal_hold", a.legal_hold, "deleted_at", a.deleted_at, "storage_key", a.storage_key)
