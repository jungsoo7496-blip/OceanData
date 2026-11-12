"""RV12 scratch: randomized lock-order stress for clip-store / deletion API / retention run / upload churn."""
from __future__ import annotations

import hashlib
import random
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.db.models import Asset, OrgQuota
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A
from app.services import pipeline, retention
from app.workers import main as worker
from tests.integration.test_catalog import _asset
from tests.integration.test_ingest import MB, manifest, mp4_bytes

pytestmark = pytest.mark.integration


def test_lock_order_stress(ictx):
    c = ictx.c
    errors: list[str] = []
    stop = threading.Event()
    counters = {"store": 0, "delete": 0, "retention": 0, "upload": 0, "sweep": 0}
    h_member, h_admin = ictx.as_user(102), ictx.as_user(101, mfa=True)

    def new_parent(expired=False):
        with c.sessionmaker.begin() as db:
            a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
            a.checksum_sha256 = hashlib.sha256(new_id("asset").encode()).hexdigest()
            a.storage_zone, a.storage_key, a.byte_size = "RAW", f"x/{a.asset_id}", 10
            if expired:
                a.retention_policy_id, a.retention_until = "raw_default", c.clock.now() - timedelta(days=1)
            db.add(a)
            from app.services.ingest import Quota
            Quota(c, db)._row(ORG_A).used_bytes += 10   # fixtures behave like registered assets (counted)
            return a.asset_id

    parents: list[str] = []
    plock = threading.Lock()

    def refill():
        with plock:
            while len(parents) < 6:
                parents.append(new_parent())

    def classify(exc: BaseException) -> str:
        s = str(exc)
        return "DEADLOCK" if "40P01" in s or "deadlock" in s.lower() else f"{type(exc).__name__}: {s[:100]}"

    def store_worker():
        while not stop.is_set():
            refill()
            with plock:
                pid = random.choice(parents)
            try:
                with c.sessionmaker.begin() as db:
                    p = db.get(Asset, pid, with_for_update=True)
                    if p is None or p.deleted_at is not None:
                        continue
                    sha = hashlib.sha256(new_id("asset").encode()).hexdigest()
                    now = c.clock.now()
                    child = pipeline.new_derived(c, db, p, sha=sha, size=7, variant="CLIP", mime="video/mp4",
                                                 relation="CLIP", tool="t", job_id=None, now=now)
                    pipeline.record_derived(c, db, p, child, relation="CLIP", tool="t", now=now, enforce_quota=True)
                counters["store"] += 1
            except Exception as exc:  # noqa: BLE001
                errors.append("store " + classify(exc))

    def deleter():
        while not stop.is_set():
            refill()
            with plock:
                pid = parents.pop(random.randrange(len(parents)))
            r = ictx.client.post(f"/v1/assets/{pid}/deletion", json={"reason": "stress delete"}, headers=h_admin)
            counters["delete"] += 1
            if r.status_code >= 500:
                errors.append(f"delete HTTP {r.status_code} {r.text[:80]}")

    def retention_runner():
        while not stop.is_set():
            ids = [new_parent(expired=True) for _ in range(3)]
            try:
                with c.sessionmaker.begin() as db:
                    retention.execute_expired(c, db, c.clock.now())
                counters["retention"] += 1
            except Exception as exc:  # noqa: BLE001
                errors.append("retention " + classify(exc))

    def uploader():
        i = 0
        while not stop.is_set():
            i += 1
            d = mp4_bytes(64, seed=5000 + i + random.randrange(10**6))
            r = ictx.client.post("/v1/uploads", json=manifest(d), headers=h_member)
            if r.status_code >= 500:
                errors.append(f"upload HTTP {r.status_code}")
            elif r.status_code == 201:
                ictx.client.delete(f"/v1/uploads/{r.json()['upload_id']}", headers=h_member)
            counters["upload"] += 1

    def sweeper():
        while not stop.is_set():
            with c.engine.begin() as conn:
                conn.execute(text("UPDATE uploads SET expires_at = now() - interval '1 minute' WHERE status='UPLOADING'"))
            try:
                worker.sweep(c)
                counters["sweep"] += 1
            except Exception as exc:  # noqa: BLE001
                errors.append("sweep " + classify(exc))
            time.sleep(0.05)

    ts = [threading.Thread(target=f) for f in (store_worker, store_worker, deleter, retention_runner, uploader, sweeper)]
    [t.start() for t in ts]
    time.sleep(25)
    stop.set()
    [t.join(60) for t in ts]
    from collections import Counter
    print("OBS stress counters", counters)
    print("OBS stress errors", dict(Counter(errors)))
    with c.sessionmaker() as db:
        q = db.get(OrgQuota, ORG_A)
        print("OBS quota", q.used_bytes, q.quarantine_bytes, q.reserved_bytes)
        n = db.execute(text("SELECT coalesce(sum(byte_size),0) FROM assets WHERE organization_id=:o AND deleted_at IS NULL AND has_binary"), {"o": ORG_A}).scalar()
        r = db.execute(text("SELECT coalesce(sum(reserved_bytes),0) FROM uploads WHERE organization_id=:o AND status='UPLOADING'"), {"o": ORG_A}).scalar()
        print("OBS live asset bytes", n, "(fixtures start at 10 B each, not counted in used_bytes)", "live upload reservations", r)
