"""Read-only probe 4: concurrent same-key retry on POST /v1/devices."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from sqlalchemy import text

OUT = Path(__file__).with_name("probe_out4.jsonl")


def note(name, **kw):
    with OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"probe": name, **kw}, ensure_ascii=False, default=str) + "\n")


def test_probe_idempotent_retry_race_devices(ctx, monkeypatch):
    import app.api.context as ctxmod

    real_view = ctxmod.device_view
    first = threading.Event()

    def slow_view(*a, **kw):
        if not first.is_set():
            first.set()
            time.sleep(1.5)
        return real_view(*a, **kw)

    monkeypatch.setattr(ctxmod, "device_view", slow_view)
    h = {**ctx.as_user(101), "Idempotency-Key": "retry-key-dev-0001"}
    body = {"name": "race cam", "kind": "SOURCE", "device_type": "CAMERA", "local_copy_policy": "RETAINED"}
    results = {}

    def send(i):
        results[i] = ctx.client.post("/v1/devices", json=body, headers=h)

    t1 = threading.Thread(target=send, args=(1,))
    t1.start()
    first.wait(5)
    time.sleep(0.2)
    t2 = threading.Thread(target=send, args=(2,))
    t2.start()
    t1.join(30)
    t2.join(30)
    with ctx.c.engine.connect() as conn:
        n = [r[0] for r in conn.execute(text("SELECT device_id FROM devices WHERE name = 'race cam'"))]
        regs = conn.execute(text("SELECT count(*) FROM audit_log WHERE action = 'DEVICE_REGISTERED'")).scalar()
    note("idempotent_retry_race_devices",
         first=(results[1].status_code, results[1].json().get("device_id") or results[1].json().get("code")),
         second=(results[2].status_code, results[2].json().get("device_id") or results[2].json().get("code"),
                 results[2].headers.get("idempotent-replayed")),
         devices_named_race_cam=n, device_registered_audits=regs)
