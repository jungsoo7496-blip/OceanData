"""rv12 misc: lock_timeout leak, aborted requests leak, S3 orphan / stuck retry after commit failure."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import app.api.observations as obs_api
import app.services.idempotency as idem
from app.db.models import Asset, Upload
from app.seed import ORG_A, SES_A, fid
from app.services.idempotency import lock_scope
from rv12_live import LiveServer, raw_request
from tests.integration.test_ingest import MB, manifest, mp4_bytes, put_parts

pytestmark = pytest.mark.integration
MEMBER_A = 102
EVENT = {"schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL", "started_at": "2026-11-02T00:00:00Z",
         "timestamp_quality": {"source": "DEVICE", "verified": True}, "source": "MANUAL"}


def test_lock_timeout_does_not_leak(ctx, monkeypatch):
    seen = []
    real_view = obs_api.event_view

    def view(db, e):
        seen.append(db.execute(text("SHOW lock_timeout")).scalar())
        return real_view(db, e)

    monkeypatch.setattr(obs_api, "event_view", view)
    h = {**ctx.as_user(MEMBER_A), "Idempotency-Key": "lt-key-00001"}
    assert ctx.client.post("/v1/events", json=EVENT, headers=h).status_code == 201
    print("LT inside fn after _serialize:", seen)
    # timeout path
    monkeypatch.setattr(idem, "LOCK_WAIT_MS", 200)
    scope = lock_scope(ORG_A, fid("usr", MEMBER_A), "POST /v1/events", "lt-key-00002")
    with ctx.c.engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(hashtextextended(:s, 0))"), {"s": scope})
        r = ctx.client.post("/v1/events", json=EVENT, headers={**ctx.as_user(MEMBER_A), "Idempotency-Key": "lt-key-00002"})
        print("LT timeout path status:", r.status_code, r.headers.get("retry-after"))
        holder.execute(text("SELECT pg_advisory_unlock(hashtextextended(:s, 0))"), {"s": scope})
        holder.commit()
    vals = set()
    for _ in range(12):
        with ctx.c.engine.connect() as c2:
            vals.add(c2.execute(text("SHOW lock_timeout")).scalar())
    print("LT values on pooled connections after timeout:", vals)
    assert vals == {"0"}


def test_aborted_requests_leave_nothing_behind(ctx):
    from sqlalchemy import create_engine
    diag = create_engine(ctx.c.engine.url, pool_size=2, max_overflow=0)
    srv = LiveServer(ctx.client.app).start()
    try:
        hdr = ctx.as_user(MEMBER_A)
        key = "abort-key-0001"
        scope = lock_scope(ORG_A, fid("usr", MEMBER_A), "POST /v1/events", key)
        body = json.dumps(EVENT).encode()
        with ctx.c.engine.connect() as holder:
            holder.execute(text("SELECT pg_advisory_lock(hashtextextended(:s, 0))"), {"s": scope})
            def fire(i):
                kind = i % 5
                if kind == 0:   # waits on the held advisory lock, then client vanishes
                    return raw_request(srv.port, "POST", "/v1/events", {**hdr, "Content-Type": "application/json", "Idempotency-Key": key}, body, abort_after=0.05)
                if kind == 1:   # plain read, client vanishes
                    return raw_request(srv.port, "GET", "/v1/me", hdr, abort_after=0.0)
                if kind == 2:   # bad token
                    return raw_request(srv.port, "GET", "/v1/me", {"Authorization": "Bearer nope"}, abort_after=0.0)
                if kind == 3:   # validation failure after dependencies
                    return raw_request(srv.port, "POST", "/v1/events", {**hdr, "Content-Type": "application/json"}, b'{"x":1}', abort_after=0.0)
                return raw_request(srv.port, "POST", "/v1/events", {**hdr, "Content-Type": "application/json"}, body, abort_after=0.0)
            with ThreadPoolExecutor(max_workers=40) as pool:
                list(pool.map(fire, range(150)))
            time.sleep(1.0)
            with diag.connect() as c2:
                waiting = c2.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")).scalar()
            print("ABORT: waiters on advisory lock while holder held:", waiting, " pool:", ctx.c.engine.pool.status())
            with diag.connect() as c2:
                rows = c2.execute(text("SELECT state, wait_event_type, wait_event, left(query, 90) q, count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() GROUP BY 1,2,3,4 ORDER BY 5 DESC")).all()
            for r in rows:
                print("ABORT-SNAP", tuple(r))
            import sys, traceback
            frames = sys._current_frames()
            import collections
            where = collections.Counter()
            for th in threading.enumerate():
                if th.name.startswith("AnyIO") or "worker" in th.name.lower() or th.name.startswith("Thread"):
                    fr = frames.get(th.ident)
                    if fr is None: continue
                    st = traceback.extract_stack(fr)
                    app_frames = [f"{f.filename.split('/backend/')[-1]}:{f.lineno}:{f.name}" for f in st if "/backend/app/" in f.filename]
                    where[" > ".join(app_frames[-3:]) or ("(" + st[-1].name + ")")] += 1
            for k, v in where.most_common(8):
                print("ABORT-THREADS", v, k)
            holder.execute(text("SELECT pg_advisory_unlock(hashtextextended(:s, 0))"), {"s": scope})
            holder.commit()
        # drain
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with diag.connect() as c2:
                busy = c2.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND state IN ('active','idle in transaction')")).scalar()
            if ctx.c.engine.pool.checkedout() == 0 and busy == 0:
                break
            time.sleep(0.2)
        with diag.connect() as c2:
            states = c2.execute(text("SELECT state, count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() GROUP BY 1")).all()
            adv = c2.execute(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted")).scalar()
            n_events = c2.execute(text("SELECT count(*) FROM events")).scalar()
        print("ABORT after drain: pool:", ctx.c.engine.pool.status(), " pg states:", states, " advisory locks held:", adv, " events:", n_events)
        assert ctx.c.engine.pool.checkedout() == 0 and adv == 0
    finally:
        srv.stop()


def _fail_next_commit(monkeypatch):
    real = Session.commit
    st = {"on": False, "hits": 0}

    def commit(self):
        if st["on"]:
            st["on"] = False
            st["hits"] += 1
            raise OperationalError("COMMIT", {}, Exception("synthetic commit failure"))
        return real(self)

    monkeypatch.setattr(Session, "commit", commit)
    return st


def test_commit_failure_after_s3_side_effects(ictx, monkeypatch):
    st = _fail_next_commit(monkeypatch)
    h = ictx.as_user(MEMBER_A)
    data = mp4_bytes(12 * MB)
    m = manifest(data)
    storage = ictx.c.storage
    bucket = ictx.c.settings.buckets["RAW"] if "RAW" in ictx.c.settings.buckets else list(ictx.c.settings.buckets.values())[0]
    client = storage.client if hasattr(storage, "client") else storage._client

    def mp_uploads():
        out = 0
        for b in ictx.c.settings.buckets.values():
            try:
                out += len(client.list_multipart_uploads(Bucket=b).get("Uploads", []))
            except Exception:
                pass
        return out

    # 1) create: S3 multipart is created inside the txn; commit fails -> client sees an error; S3 keeps an orphan
    st["on"] = True
    r = ictx.client.post("/v1/uploads", json=m, headers={**h, "Idempotency-Key": "s3-create-0001"})
    print("S3-ORPHAN create with commit failure:", r.status_code, "| multipart uploads in S3:", mp_uploads())
    with ictx.c.sessionmaker() as db:
        print("   DB uploads:", db.execute(text("SELECT count(*) FROM uploads")).scalar(),
              " reserved:", db.execute(text("SELECT reserved_bytes FROM org_quotas WHERE organization_id=:o"), {"o": ORG_A}).scalar())
    r = ictx.client.post("/v1/uploads", json=m, headers={**h, "Idempotency-Key": "s3-create-0001"})
    print("   retry create:", r.status_code, "| multipart uploads in S3 now:", mp_uploads())
    created = r.json()
    uid = created["upload_id"]
    parts = put_parts(created, data)
    # 2) complete: S3 complete happens inside the txn; commit fails -> client error; S3 object exists; DB says UPLOADING
    st["on"] = True
    ck = {**h, "Idempotency-Key": f"complete-{uid}"}
    r1 = ictx.client.post(f"/v1/uploads/{uid}/complete", json={"parts": parts}, headers=ck)
    print("S3-STUCK complete with commit failure:", r1.status_code, r1.text[:160])
    with ictx.c.sessionmaker() as db:
        up = db.get(Upload, uid)
        a = db.get(Asset, created["asset_id"])
        print("   DB upload.status:", up.status, " asset.ingest_status:", a.ingest_status)
        bucket, key = up.bucket, up.object_key
    try:
        client.head_object(Bucket=bucket, Key=key)
        print("   S3 object exists after failed complete: True")
    except Exception as e:
        print("   S3 object exists after failed complete: False", type(e).__name__)
    for i in range(3):
        r2 = ictx.client.post(f"/v1/uploads/{uid}/complete", json={"parts": parts}, headers=ck)
        print(f"   retry #{i+1} complete:", r2.status_code, r2.json().get("code"), r2.headers.get("retry-after"), (r2.json().get("detail") or "")[:80])
    with ictx.c.sessionmaker() as db:
        print("   final DB upload.status:", db.get(Upload, uid).status)
