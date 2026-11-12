"""rv12 (8)+(2): same-key waiters vs thread pool / DB pool; aborted requests leak nothing."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

import app.services.idempotency as idem
from app.seed import ORG_A, SES_A, fid
from app.services.idempotency import lock_scope
from rv12_live import LiveServer, raw_request

pytestmark = pytest.mark.integration
MEMBER_A = 102
EVENT = {"schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL", "started_at": "2026-11-02T00:00:00Z",
         "timestamp_quality": {"source": "DEVICE", "verified": True}, "source": "MANUAL"}


def _post_event(port, hdr, key):
    h = {**hdr, "Content-Type": "application/json", "Idempotency-Key": key}
    return raw_request(port, "POST", "/v1/events", h, json.dumps(EVENT).encode(), timeout=60)


def _pool_state(ctx):
    p = ctx.c.engine.pool
    return {"size": p.size(), "checkedout": p.checkedout(), "overflow": p.overflow()}


def _run_storm(ctx, srv, n, key, hold_s, serialize_off=False, monkeypatch=None):
    hdr = ctx.as_user(MEMBER_A)
    scope = lock_scope(ORG_A, fid("usr", MEMBER_A), "POST /v1/events", key)
    out = {}
    with ctx.c.engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(hashtextextended(:s, 0))"), {"s": scope})
        results = []
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=n + 4) as pool:
            futs = [pool.submit(_post_event, srv.port, hdr, key) for _ in range(n)]
            time.sleep(1.5)
            out["pool_at_1.5s"] = _pool_state(ctx)
            hz = raw_request(srv.port, "GET", "/healthz", {}, timeout=60)
            out["healthz_ms_during_storm"] = round((hz["t_end"] - hz["t_sent"]) * 1000)
            out["healthz_status"] = hz["status"]
            # release the holder after hold_s
            time.sleep(max(0.0, hold_s - 1.5 - (hz["t_end"] - hz["t_sent"])))
            holder.execute(text("SELECT pg_advisory_unlock(hashtextextended(:s, 0))"), {"s": scope})
            holder.commit()
            for f in futs:
                results.append(f.result(timeout=90))
        out["storm_total_s"] = round(time.monotonic() - t0, 1)
    codes = {}
    for r in results:
        codes[r["status"]] = codes.get(r["status"], 0) + 1
    out["status_counts"] = codes
    out["pool_after"] = _pool_state(ctx)
    return out


def test_waiters_starve_threadpool(ctx, monkeypatch):
    srv = LiveServer(ctx.client.app).start()
    try:
        # 1) baseline: no lock waits at all (old behaviour emulation: _serialize is a no-op)
        monkeypatch.setattr(idem, "_serialize", lambda db, pk: None)
        base = _run_storm(ctx, srv, 60, "storm-key-baseline", hold_s=3.0)
        print("STORM baseline (no serialize):", base)
        monkeypatch.undo()
        # 2) new behaviour: 60 same-key requests wait on a slow holder for ~6 s
        new = _run_storm(ctx, srv, 60, "storm-key-0001", hold_s=6.0)
        print("STORM new (advisory lock waiters):", new)
        # 3) health probe after storm is fast again
        hz = raw_request(srv.port, "GET", "/healthz", {}, timeout=30)
        print("healthz after:", round((hz["t_end"] - hz["t_sent"]) * 1000), "ms", hz["status"])
    finally:
        srv.stop()
