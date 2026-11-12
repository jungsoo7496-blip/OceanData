"""RV12 scratch: upload lifecycle reproductions (throwaway)."""
from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select, text

from app.db.models import Asset, AssetLineage, DeletionCertificate, OrgQuota, Upload
from app.seed import ORG_A
from app.services.ingest import IngestService
from app.workers import main as worker
from tests.integration.test_ingest import MB, drain, manifest, mp4_bytes, put_parts, upload_all

pytestmark = pytest.mark.integration


def obs(*a):
    print("OBS", *a, flush=True)


def quota(ctx):
    with ctx.c.sessionmaker() as db:
        q = db.get(OrgQuota, ORG_A)
        return dict(used=q.used_bytes, quar=q.quarantine_bytes, res=q.reserved_bytes)


def expire(ctx, upload_id, minutes=1):
    with ctx.c.engine.begin() as conn:
        conn.execute(text("UPDATE uploads SET expires_at = now() - make_interval(mins => :m) WHERE upload_id = :u"),
                     {"m": minutes, "u": upload_id})


def test_stuck_validating_after_stream_error(ictx, monkeypatch):
    h = ictx.as_user(102)
    data = mp4_bytes(128, seed=901)
    created = upload_all(ictx, manifest(data), data, h)
    aid, uid = created["asset_id"], created["upload_id"]
    obs("after complete", quota(ictx))
    real = ictx.c.storage.iter_object

    def boom(*a, **k):
        raise TimeoutError("synthetic stream error")
        yield b""  # pragma: no cover

    monkeypatch.setattr(ictx.c.storage, "iter_object", boom)
    worker.run_once(ictx.c, "t")  # claims integrity_verify, fails
    monkeypatch.setattr(ictx.c.storage, "iter_object", real)
    with ictx.c.engine.begin() as conn:  # skip backoff
        conn.execute(text("UPDATE jobs SET run_after = now() WHERE status = 'QUEUED'"))
    while worker.run_once(ictx.c, "t"):
        pass
    with ictx.c.engine.connect() as conn:
        jobs = [tuple(r) for r in conn.execute(text("SELECT job_type, status, attempts, last_error_code FROM jobs"))]
    obs("jobs", jobs)
    with ictx.c.sessionmaker() as db:
        a = db.get(Asset, aid)
        up = db.get(Upload, uid)
        obs("asset", a.ingest_status, "upload", up.status, up.reserved_bytes)
    obs("quota", quota(ictx))
    # try to recover/delete
    r = ictx.client.post(f"/v1/assets/{aid}/deletion", json={"reason": "stuck validating test"},
                         headers=ictx.as_user(101, mfa=True))
    obs("delete ->", r.status_code, r.json().get("detail"))
    ictx.c.queue.reap_expired_leases()
    worker.maintenance(ictx.c)
    with ictx.c.sessionmaker() as db:
        obs("asset after maintenance", db.get(Asset, aid).ingest_status)
    obs("quota after maintenance", quota(ictx))
    # a manual re-enqueue of the verify job is also a no-op
    ictx.c.queue.enqueue("integrity_verify", {"asset_id": aid}, organization_id=ORG_A, dedupe_key="manual1")
    drain(ictx)
    with ictx.c.sessionmaker() as db:
        obs("asset after manual re-verify", db.get(Asset, aid).ingest_status)


def test_sweeper_swallows_abort_failure_and_never_retries(ictx, monkeypatch):
    h = ictx.as_user(102)
    data = mp4_bytes(5 * MB, seed=902)
    cr = ictx.client.post("/v1/uploads", json=manifest(data), headers=h).json()
    put_parts(cr, data)
    up = None
    with ictx.c.sessionmaker() as db:
        up = db.get(Upload, cr["upload_id"])
        bucket, key, s3id = up.bucket, up.object_key, up.s3_upload_id
    expire(ictx, cr["upload_id"])
    real = ictx.c.storage.abort_multipart_upload

    def boom(*a, **k):
        raise TimeoutError("synthetic s3 outage")

    monkeypatch.setattr(ictx.c.storage, "abort_multipart_upload", boom)
    n = worker.sweep(ictx.c)
    obs("sweep#1 released", n)
    monkeypatch.setattr(ictx.c.storage, "abort_multipart_upload", real)
    n2 = worker.sweep(ictx.c)
    obs("sweep#2 released", n2)
    with ictx.c.sessionmaker() as db:
        up2 = db.get(Upload, cr["upload_id"])
        obs("upload status", up2.status, "reserved", up2.reserved_bytes, "asset row", db.get(Asset, cr["asset_id"]))
    obs("quota", quota(ictx))
    try:
        parts = ictx.c.storage.list_parts(bucket, key, s3id)
        obs("S3 multipart STILL OPEN with parts:", [(p.part_number, p.size) for p in parts])
    except ClientError as e:
        obs("S3 multipart gone", e.response["Error"]["Code"])
    # same for the user abort path
    cr2 = ictx.client.post("/v1/uploads", json=manifest(mp4_bytes(5 * MB, seed=903)), headers=h).json()
    with ictx.c.sessionmaker() as db:
        u2 = db.get(Upload, cr2["upload_id"])
        b2, k2, s2 = u2.bucket, u2.object_key, u2.s3_upload_id
    monkeypatch.setattr(ictx.c.storage, "abort_multipart_upload", boom)
    r = ictx.client.delete(f"/v1/uploads/{cr2['upload_id']}", headers=h)
    monkeypatch.setattr(ictx.c.storage, "abort_multipart_upload", real)
    obs("user abort during outage ->", r.status_code)
    try:
        ictx.c.storage.list_parts(b2, k2, s2)
        obs("user-abort multipart STILL OPEN")
    except ClientError as e:
        obs("user-abort multipart gone", e.response["Error"]["Code"])
    obs("quota", quota(ictx))


# ------------------------------------------------------------------ R4: lock-order inversion (integrity vs quota)
def _quarantined_unknown_session(ictx, h, seed):
    from app.seed import DEV_A_SENSOR
    data = (b"time,temp\n2026-11-01T00:00:00Z,%d\n" % seed)
    m = manifest(data, source=DEV_A_SENSOR, data_type="SENSOR", variant="ORIGINAL", mime="text/csv", session=None,
                 timestamp_quality={"source": "UNKNOWN", "verified": False})
    m.pop("capture_start_at")
    created = upload_all(ictx, m, data, h)
    drain(ictx)
    return created["asset_id"]


def test_deadlock_revalidate_vs_derived_record(ictx, monkeypatch):
    from app.seed import SES_A
    from app.services import ingest as ingest_mod, pipeline
    from app.services.ingest import IntegrityVerifier
    from tests.integration.test_lifecycle import upload_csv

    h = ictx.as_user(102)
    parent_id = upload_csv(ictx, b"t,v\n7,7\n")  # registered parent for the derived record
    q_aid = _quarantined_unknown_session(ictx, h, 11)
    r = ictx.client.patch(f"/v1/assets/{q_aid}", json={"session_id": SES_A}, headers=h)
    assert r.status_code == 200 and r.json()["ingest_status"] == "VALIDATING", r.text

    a_has_quota, b_has_integrity = threading.Event(), threading.Event()
    real_ai = ingest_mod.append_integrity
    real_pipe_ai = pipeline.append_integrity

    def hooked_ingest_ai(db, asset, now):
        a_has_quota.set()                       # thread A already holds the org quota row (revalidate branch)
        assert b_has_integrity.wait(15)
        return real_ai(db, asset, now)

    def hooked_pipe_ai(db, asset, now):
        assert a_has_quota.wait(15)
        e = real_pipe_ai(db, asset, now)        # B takes the integrity advisory lock ...
        b_has_integrity.set()
        return e

    monkeypatch.setattr(ingest_mod, "append_integrity", hooked_ingest_ai)
    monkeypatch.setattr(pipeline, "append_integrity", hooked_pipe_ai)
    res = {}

    def thread_a():
        try:
            res["A"] = IntegrityVerifier(ictx.c).verify(q_aid)
        except Exception as exc:  # noqa: BLE001
            res["A"] = f"EXC {type(exc).__name__}: {str(exc)[:160]}"

    def thread_b():
        try:
            with ictx.c.sessionmaker.begin() as db:
                parent = db.get(Asset, parent_id)
                now = ictx.c.clock.now()
                child = pipeline.new_derived(ictx.c, db, parent, sha="ab" * 32, size=10, variant="PROXY",
                                             mime="video/mp4", relation="PROXY", tool="t", job_id=None, now=now)
                pipeline.record_derived(ictx.c, db, parent, child, relation="PROXY", tool="t", now=now)
            res["B"] = "OK"
        except Exception as exc:  # noqa: BLE001
            res["B"] = f"EXC {type(exc).__name__}: {str(exc)[:160]}"

    t0 = time.monotonic()
    ta, tb = threading.Thread(target=thread_a), threading.Thread(target=thread_b)
    ta.start(); tb.start(); ta.join(60); tb.join(60)
    obs("deadlock result", res, "elapsed %.1fs" % (time.monotonic() - t0))
    with ictx.c.sessionmaker() as db:
        a = db.get(Asset, q_aid)
        obs("revalidated asset status/zone/key-has-session", a.ingest_status, a.storage_zone, a.storage_key and SES_A in a.storage_key)
        if a.storage_key:
            zone_bucket = ictx.c.settings.buckets[a.storage_zone]
            obs("object present at recorded location?", ictx.c.storage.head_object(zone_bucket, a.storage_key) is not None)
        from app.services.ingest import object_key, zone_for
        a.session_id = SES_A
        new_key = object_key(a)
        obs("object stranded at NEW raw key (unreferenced by DB)?",
            ictx.c.storage.head_object(ictx.c.settings.buckets[zone_for(a.variant)], new_key) is not None)
    try:
        monkeypatch.setattr(ingest_mod, "append_integrity", real_ai)
        monkeypatch.setattr(pipeline, "append_integrity", real_pipe_ai)
        obs("retry verify ->", IntegrityVerifier(ictx.c).verify(q_aid))
    except Exception as exc:  # noqa: BLE001
        obs("retry verify raises", type(exc).__name__, str(exc)[:120])


# ------------------------------------------------------------------ R6: lineage of a REGISTERED child lost when parent upload expires
def test_registered_child_loses_lineage_when_parent_upload_expires(ictx):
    h = ictx.as_user(102)
    parent = mp4_bytes(5 * MB, seed=1101)
    rp = ictx.client.post("/v1/uploads", json=manifest(parent), headers=h).json()
    child = mp4_bytes(5 * MB, seed=1102)
    mc = manifest(child, variant="PROXY", parent_ref={"checksum_sha256": hashlib.sha256(parent).hexdigest()})
    rc = ictx.client.post("/v1/uploads", json=mc, headers=h).json()
    parts = put_parts(rc, child)
    r = ictx.client.post(f"/v1/uploads/{rc['upload_id']}/complete", json={"parts": parts}, headers=h)
    assert r.status_code == 202
    drain(ictx)
    with ictx.c.sessionmaker() as db:
        c_asset = db.get(Asset, rc["asset_id"])
        obs("child status", c_asset.ingest_status, "provenance.parent", c_asset.provenance.get("parent_asset_ids"))
        obs("lineage rows before", db.execute(select(AssetLineage.parent_asset_id).where(
            AssetLineage.child_asset_id == rc["asset_id"])).all())
    r = ictx.client.get(f"/v1/assets/{rc['asset_id']}/lineage", headers=h)
    obs("lineage API ancestors before", [x["asset_id"] for x in r.json()["ancestors"]])
    expire(ictx, rp["upload_id"])
    obs("sweep released", worker.sweep(ictx.c))
    with ictx.c.sessionmaker() as db:
        obs("lineage rows after", db.execute(select(AssetLineage.parent_asset_id).where(
            AssetLineage.child_asset_id == rc["asset_id"])).all())
        c_asset = db.get(Asset, rc["asset_id"])
        obs("child still REGISTERED/READY?", c_asset.ingest_status, "provenance.parent (dangling)", c_asset.provenance.get("parent_asset_ids"),
            "parent row exists?", db.get(Asset, rp["asset_id"]) is not None)
    r = ictx.client.get(f"/v1/assets/{rc['asset_id']}/lineage", headers=h)
    obs("lineage API ancestors after", [x["asset_id"] for x in r.json()["ancestors"]])


# ------------------------------------------------------------------ R5: concurrent same-checksum create
def test_concurrent_same_checksum_create_upload(ictx, monkeypatch):
    h = ictx.as_user(102)
    data = mp4_bytes(256, seed=1201)
    m = manifest(data)
    in_hook, go = threading.Event(), threading.Event()
    real = ictx.c.storage.create_multipart_upload
    first = {"done": False}
    lock = threading.Lock()

    def hooked(*a, **k):
        with lock:
            me_first = not first["done"]
            first["done"] = True
        if me_first:
            in_hook.set()
            assert go.wait(20)
        return real(*a, **k)

    monkeypatch.setattr(ictx.c.storage, "create_multipart_upload", hooked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(ictx.client.post, "/v1/uploads", json=m, headers=h)
        assert in_hook.wait(10)
        f2 = pool.submit(ictx.client.post, "/v1/uploads", json=m, headers=h)
        time.sleep(1.5)
        go.set()
        r1, r2 = f1.result(30), f2.result(30)
    obs("concurrent same-checksum statuses", r1.status_code, r2.status_code, r2.text[:120])
    obs("quota", quota(ictx))


# ------------------------------------------------------------------ R12: sweeper holds org quota lock across S3 calls
def test_sweeper_blocks_new_uploads_while_calling_s3(ictx, monkeypatch):
    h = ictx.as_user(102)
    ids = []
    for i in range(6):
        d = mp4_bytes(64, seed=1300 + i)
        ids.append(ictx.client.post("/v1/uploads", json=manifest(d), headers=h).json()["upload_id"])
    for u in ids:
        expire(ictx, u)
    real = ictx.c.storage.abort_multipart_upload
    started = threading.Event()

    def slow(*a, **k):
        started.set()
        time.sleep(0.5)
        return real(*a, **k)

    monkeypatch.setattr(ictx.c.storage, "abort_multipart_upload", slow)
    t = threading.Thread(target=lambda: worker.sweep(ictx.c))
    t.start()
    assert started.wait(10)
    time.sleep(0.7)  # sweeper has released >=1 session -> holds quota row lock now
    t0 = time.monotonic()
    r = ictx.client.post("/v1/uploads", json=manifest(mp4_bytes(64, seed=1399)), headers=h)
    dt = time.monotonic() - t0
    t.join(30)
    obs("create_upload during sweep of 6 expired sessions (0.5s S3 each): status", r.status_code, "latency %.2fs" % dt)


# ------------------------------------------------------------------ R2b: RECEIVED stuck after retries exhausted (scanner required but unavailable)
def test_received_stuck_after_verify_retries_exhausted(ictx):
    from app.db.models import Organization
    with ictx.c.sessionmaker.begin() as db:
        db.get(Organization, ORG_A).malware_scan_required = True
    h = ictx.as_user(102)
    data = mp4_bytes(128, seed=951)
    created = upload_all(ictx, manifest(data), data, h)
    aid = created["asset_id"]
    for _ in range(8):
        with ictx.c.engine.begin() as conn:
            conn.execute(text("UPDATE jobs SET run_after = now() WHERE status = 'QUEUED'"))
        worker.run_once(ictx.c, "t")
    with ictx.c.engine.connect() as conn:
        obs("jobs", [tuple(r) for r in conn.execute(text("SELECT job_type, status, attempts, last_error_code FROM jobs"))])
    with ictx.c.sessionmaker() as db:
        obs("asset", db.get(Asset, aid).ingest_status)
    obs("quota (reserved leaked)", quota(ictx))
    # operator is unable to recover: reprocess rejects RECEIVED, deletion rejects RECEIVED
    obs("delete ->", ictx.client.post(f"/v1/assets/{aid}/deletion", json={"reason": "stuck received"},
                                      headers=ictx.as_user(101, mfa=True)).status_code)
    obs("reprocess ->", ictx.client.post(f"/v1/assets/{aid}/reprocess", json={"steps": ["META"], "reason": "retry it"},
                                         headers=ictx.as_user(106, mfa=True)).status_code)
    # fix the config and re-enqueue: verify works again?
    with ictx.c.sessionmaker.begin() as db:
        db.get(Organization, ORG_A).malware_scan_required = False
    ictx.c.queue.enqueue("integrity_verify", {"asset_id": aid}, organization_id=ORG_A, dedupe_key="manual-fix")
    drain(ictx)
    with ictx.c.sessionmaker() as db:
        obs("after config fix + manual re-enqueue", db.get(Asset, aid).ingest_status)
    obs("quota", quota(ictx))


# ------------------------------------------------------------------ R3b: crash between S3 complete and DB commit -> completed object orphaned by sweeper
def test_completed_object_orphaned_by_sweeper(ictx):
    from app.ports.storage import CompletedPart
    h = ictx.as_user(102)
    data = mp4_bytes(5 * MB, seed=961)
    cr = ictx.client.post("/v1/uploads", json=manifest(data), headers=h).json()
    parts = put_parts(cr, data)
    with ictx.c.sessionmaker() as db:
        up = db.get(Upload, cr["upload_id"])
        bucket, key, s3id = up.bucket, up.object_key, up.s3_upload_id
    # simulate: S3 CompleteMultipartUpload succeeded, process died before the DB commit
    ictx.c.storage.complete_multipart_upload(bucket, key, s3id, [CompletedPart(p["part_number"], p["etag"]) for p in parts])
    r = ictx.client.post(f"/v1/uploads/{cr['upload_id']}/complete", json={"parts": parts}, headers=h)
    obs("client retries complete ->", r.status_code, r.json().get("code"))
    expire(ictx, cr["upload_id"])
    obs("sweep", worker.sweep(ictx.c))
    with ictx.c.sessionmaker() as db:
        obs("upload", db.get(Upload, cr["upload_id"]).status, "asset row", db.get(Asset, cr["asset_id"]))
    obs("quota", quota(ictx))
    obs("completed object left in S3 (orphan, unreferenced, uncounted):", ictx.c.storage.head_object(bucket, key))


# ------------------------------------------------------------------ R7: quarantine move failure -> counters drift on delete
def test_quarantine_move_failure_then_delete_leaks_quarantine_bytes(ictx, monkeypatch):
    h = ictx.as_user(102)
    data = mp4_bytes(256, seed=971)
    m = manifest(data, sha="0" * 64)  # declared checksum is wrong -> CHECKSUM_MISMATCH
    cr = ictx.client.post("/v1/uploads", json=m, headers=h).json()
    parts = put_parts(cr, data)
    assert ictx.client.post(f"/v1/uploads/{cr['upload_id']}/complete", json={"parts": parts}, headers=h).status_code == 202
    real_move = ictx.c.storage.move_object

    def boom(*a, **k):
        raise TimeoutError("synthetic s3 failure during quarantine move")

    monkeypatch.setattr(ictx.c.storage, "move_object", boom)
    drain(ictx)
    monkeypatch.setattr(ictx.c.storage, "move_object", real_move)
    with ictx.c.sessionmaker() as db:
        a = db.get(Asset, cr["asset_id"])
        obs("asset", a.ingest_status, a.quarantine_reason, "zone", a.storage_zone)
    obs("quota after quarantine", quota(ictx))
    r = ictx.client.post(f"/v1/assets/{cr['asset_id']}/deletion", json={"reason": "discard quarantined"},
                         headers=ictx.as_user(101, mfa=True))
    obs("delete ->", r.status_code)
    obs("quota after delete (expected all zero)", quota(ictx))


# ------------------------------------------------------------------ R4b: how often does the inversion bite under background derived-asset traffic?
def test_deadlock_frequency_revalidate_under_background_traffic(ictx):
    from app.seed import SES_A
    from app.services import pipeline
    from app.services.ingest import IntegrityVerifier
    from tests.integration.test_lifecycle import upload_csv

    h = ictx.as_user(102)
    parent_id = upload_csv(ictx, b"t,v\n8,8\n")
    ids = []
    for i in range(12):
        aid = _quarantined_unknown_session(ictx, h, 100 + i)
        r = ictx.client.patch(f"/v1/assets/{aid}", json={"session_id": SES_A}, headers=h)
        assert r.status_code == 200
        ids.append(aid)
    # drop the queued revalidation jobs so we can run verify() ourselves
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status='DONE' WHERE job_type='integrity_verify' AND status='QUEUED'"))
    stop = threading.Event()
    bg = {"n": 0, "err": 0}

    def background():
        i = 0
        while not stop.is_set():
            i += 1
            try:
                with ictx.c.sessionmaker.begin() as db:
                    parent = db.get(Asset, parent_id)
                    now = ictx.c.clock.now()
                    child = pipeline.new_derived(ictx.c, db, parent, sha=hashlib.sha256(f"{i}-{time.time()}".encode()).hexdigest(),
                                                 size=10, variant="PROXY", mime="video/mp4", relation="PROXY", tool="t",
                                                 job_id=None, now=now)
                    pipeline.record_derived(ictx.c, db, parent, child, relation="PROXY", tool="t", now=now)
                    time.sleep(0.01)  # keep the tx open a little, like a real proxy/clip record would
                bg["n"] += 1
            except Exception:  # noqa: BLE001
                bg["err"] += 1

    ths = [threading.Thread(target=background) for _ in range(3)]
    [t.start() for t in ths]
    res = {"ok": 0, "deadlock": 0, "other": 0}
    broken = 0
    for aid in ids:
        try:
            IntegrityVerifier(ictx.c).verify(aid)
            res["ok"] += 1
        except Exception as exc:  # noqa: BLE001
            if "40P01" in str(exc):
                res["deadlock"] += 1
            else:
                res["other"] += 1
                obs("other exc", type(exc).__name__, str(exc)[:100])
    stop.set()
    [t.join(30) for t in ths]
    for aid in ids:
        with ictx.c.sessionmaker() as db:
            a = db.get(Asset, aid)
            if a.ingest_status == "VALIDATING" and a.storage_zone == "QUARANTINE":
                if ictx.c.storage.head_object(ictx.c.settings.buckets["QUARANTINE"], a.storage_key) is None:
                    broken += 1
    obs("revalidations:", res, "background derived txs ok/err", bg["n"], bg["err"], "assets left pointing at a missing object:", broken)


# ------------------------------------------------------------------ R9: C0 asset reverted after aborted upload keeps stale storage_key -> phantom object in deletion certificate
def test_c0_asset_after_aborted_upload_deletion_certificate(ictx):
    from app.db.models import Device
    from app.seed import DEV_A_CAMERA
    h = ictx.as_user(102)
    data = mp4_bytes(128, seed=991)
    payload = manifest(data, variant="ORIGINAL")
    with ictx.c.sessionmaker.begin() as db:
        db.get(Device, DEV_A_CAMERA).collection_mode = "C0_METADATA_ONLY"
    reg = ictx.client.post("/v1/uploads", json=payload, headers=h)
    assert reg.status_code == 201 and reg.json()["status"] == "METADATA_ONLY_REGISTERED", reg.text
    with ictx.c.sessionmaker.begin() as db:
        db.get(Device, DEV_A_CAMERA).collection_mode = "C3_APPROVED_FULL"
    cr = ictx.client.post("/v1/uploads", json=payload, headers=h).json()
    aid = cr["asset_id"]
    assert aid == reg.json()["asset_id"]
    r = ictx.client.delete(f"/v1/uploads/{cr['upload_id']}", headers=h)
    obs("abort ->", r.status_code)
    with ictx.c.sessionmaker() as db:
        a = db.get(Asset, aid)
        obs("asset after abort: status", a.ingest_status, "has_binary", a.has_binary, "storage_key", a.storage_key, "zone", a.storage_zone)
    d = ictx.client.post(f"/v1/assets/{aid}/deletion", json={"reason": "delete c0 record"}, headers=ictx.as_user(101, mfa=True))
    obs("delete ->", d.status_code, "certificate objects:", d.json().get("objects"))


class _SimulatedKill(BaseException):
    """not an Exception subclass: run_once() cannot catch it, like SIGKILL/OOM of the worker."""


def test_stuck_validating_after_worker_kill_and_lease_reap(ictx, monkeypatch):
    h = ictx.as_user(102)
    data = mp4_bytes(128, seed=981)
    created = upload_all(ictx, manifest(data), data, h)
    aid = created["asset_id"]
    real = ictx.c.storage.iter_object

    def kill(*a, **k):
        raise _SimulatedKill()
        yield b""  # pragma: no cover

    monkeypatch.setattr(ictx.c.storage, "iter_object", kill)
    with pytest.raises(_SimulatedKill):
        worker.run_once(ictx.c, "t")          # worker dies while hashing; job stays RUNNING
    monkeypatch.setattr(ictx.c.storage, "iter_object", real)
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE status='RUNNING'"))
    obs("reaped leases", ictx.c.queue.reap_expired_leases())
    while worker.run_once(ictx.c, "t2"):      # another worker picks up the re-queued job
        pass
    with ictx.c.engine.connect() as conn:
        obs("jobs", [tuple(r) for r in conn.execute(text("SELECT job_type, status, attempts, last_error_code FROM jobs"))])
    with ictx.c.sessionmaker() as db:
        obs("asset", db.get(Asset, aid).ingest_status, "upload", db.get(Upload, created["upload_id"]).status)
    obs("quota", quota(ictx))
