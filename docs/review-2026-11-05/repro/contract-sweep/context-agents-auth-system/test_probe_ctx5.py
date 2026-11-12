"""Read-only probe 5: credential revoke on a SOURCE device."""

from __future__ import annotations

import json
from pathlib import Path

from app.seed import DEV_A_CAMERA

OUT = Path(__file__).with_name("probe_out5.jsonl")


def note(name, **kw):
    with OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"probe": name, **kw}, ensure_ascii=False, default=str) + "\n")


def test_probe_revoke_source(ctx):
    c = ctx.client
    admin = ctx.as_user(101, mfa=True)
    before = c.get(f"/v1/devices/{DEV_A_CAMERA}", headers=admin).json()["status"]
    r = c.post(f"/v1/devices/{DEV_A_CAMERA}/credentials/revoke", headers=admin)
    after = c.get(f"/v1/devices/{DEV_A_CAMERA}", headers=admin).json()["status"]
    e = c.post(f"/v1/devices/{DEV_A_CAMERA}/enrollment-codes", headers=admin)
    note("revoke_source_device", before=before, revoke=(r.status_code, r.json()), after=after,
         enrollment_code=(e.status_code, e.json().get("code")))
