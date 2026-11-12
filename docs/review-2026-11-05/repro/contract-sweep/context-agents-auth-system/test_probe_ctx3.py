"""Read-only probe 3: audit rows written for refused break-glass self-approval and operator device registration."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text

from app.seed import SES_A

OUT = Path(__file__).with_name("probe_out3.jsonl")


def note(name, **kw):
    with OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"probe": name, **kw}, ensure_ascii=False, default=str) + "\n")


def rows_for(ctx, request_id):
    with ctx.c.engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT action, result, target_id FROM audit_log WHERE request_id = :r ORDER BY seq"), {"r": request_id})]


def test_probe_audit_on_refusals(ctx):
    c = ctx.client
    op = ctx.as_user(106, mfa=True)
    r = c.post("/v1/break-glass-grants", json={"target_type": "SESSION", "target_id": SES_A,
                                                "reason": "INC-0002 probe reason text", "duration_s": 600}, headers=op)
    gid = r.json()["grant_id"]
    rid = "01JZZZZZZZZZZZZZZZZZZZZZZ1"
    r = c.post(f"/v1/break-glass-grants/{gid}/decision", json={"decision": "APPROVE"},
               headers={**op, "X-Request-Id": rid})
    note("break_glass_self_approve", status=r.status_code, code=r.json().get("code"), audit_rows=rows_for(ctx, rid))
    rid2 = "01JZZZZZZZZZZZZZZZZZZZZZZ2"
    r = c.post("/v1/devices", json={"name": "op-dev", "kind": "SOURCE", "device_type": "CAMERA",
                                     "local_copy_policy": "RETAINED"}, headers={**op, "X-Request-Id": rid2})
    note("operator_create_device", status=r.status_code, code=r.json().get("code"), audit_rows=rows_for(ctx, rid2))
    # compare: a table-level permission denial (member creating a device) is audited
    rid3 = "01JZZZZZZZZZZZZZZZZZZZZZZ3"
    r = c.post("/v1/devices", json={"name": "m-dev", "kind": "SOURCE", "device_type": "CAMERA",
                                     "local_copy_policy": "RETAINED"},
               headers={**ctx.as_user(102), "X-Request-Id": rid3})
    note("member_create_device", status=r.status_code, code=r.json().get("code"), audit_rows=rows_for(ctx, rid3))
