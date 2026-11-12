"""rv12 (2)(7): error paths release the lock; mapped 409 conflicts are not logged."""
from __future__ import annotations

import logging
import time

import pytest
from sqlalchemy import text

import app.api.observations as obs_api
from app.db.models import IdempotencyRecord
from app.seed import ORG_A, SES_A, fid

pytestmark = pytest.mark.integration
MEMBER_A = 102
EVENT = {"schema_version": "1.0.0", "session_id": SES_A, "event_code": "VESSEL", "started_at": "2026-11-02T00:00:00Z",
         "timestamp_quality": {"source": "DEVICE", "verified": True}, "source": "MANUAL"}


class Grab(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.name, record.getMessage()[:120]))


def test_runtime_error_after_lock_releases_it(ctx, monkeypatch):
    real = obs_api.event_view
    calls = {"n": 0}

    def boom(db, e):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("synthetic bug")
        return real(db, e)

    monkeypatch.setattr(obs_api, "event_view", boom)
    h = {**ctx.as_user(MEMBER_A), "Idempotency-Key": "err-key-00001"}
    r1 = ctx.client.post("/v1/events", json=EVENT, headers=h)
    t0 = time.monotonic()
    r2 = ctx.client.post("/v1/events", json=EVENT, headers=h)
    print("ERR runtime: first", r1.status_code, "second", r2.status_code, f"in {time.monotonic() - t0:.2f}s",
          r2.headers.get("idempotent-replayed"))
    with ctx.c.engine.connect() as c:
        adv = c.execute(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted")).scalar()
    print("ERR advisory locks still held:", adv)
    assert (r1.status_code, r2.status_code) == (500, 201) and adv == 0


def test_mapped_conflict_is_not_logged(ctx, monkeypatch):
    grab = Grab()
    root = logging.getLogger()
    root.addHandler(grab)
    old = root.level
    root.setLevel(logging.DEBUG)
    try:
        with ctx.c.engine.begin() as conn:
            conn.execute(text("INSERT INTO idempotency_keys (organization_id, principal_id, method_path, key, body_hash, "
                              "status_code, response_body, created_at, expires_at) VALUES (:o, :p, 'POST /v1/events', "
                              "'dup-key-00001', 'x', 201, '{}', now(), now() + interval '1 day')"),
                         {"o": ORG_A, "p": fid("usr", MEMBER_A)})
        real_view = obs_api.event_view

        def view_then_conflict(db, e):
            out = real_view(db, e)
            db.add(IdempotencyRecord(organization_id=ORG_A, principal_id=fid("usr", MEMBER_A), method_path="POST /v1/events",
                                     key="dup-key-00001", body_hash="y", status_code=201, response_body={},
                                     created_at=e.created_at, expires_at=e.created_at))
            return out

        monkeypatch.setattr(obs_api, "event_view", view_then_conflict)
        r = ctx.client.post("/v1/events", json=EVENT, headers=ctx.as_user(MEMBER_A))
        print("LOG409 status:", r.status_code, r.json().get("code"), "| body:", r.text[:300])
        print("LOG409 records >= INFO from app loggers:", [x for x in grab.records if x[1].startswith(("app", "http"))])
        errs = [x for x in grab.records if x[0] in ("WARNING", "ERROR", "CRITICAL")]
        print("LOG409 WARNING+ records:", errs)
    finally:
        root.removeHandler(grab)
        root.setLevel(old)
