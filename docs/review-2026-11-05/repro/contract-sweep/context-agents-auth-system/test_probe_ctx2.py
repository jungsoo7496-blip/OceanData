"""Read-only probe 2: run the project's own contract checker on the mismatching responses + idempotency race."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_B_COLLECTOR, ORG_A, PRJ_A, SES_A
from tests.contract.openapi_contract import assert_matches_contract as check

OUT = Path(__file__).with_name("probe_out2.jsonl")
CONTRACTS = Path("/home/user/oceandata/contracts")


def note(name, **kw):
    with OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"probe": name, **kw}, ensure_ascii=False, default=str) + "\n")


def checked(name, resp, method, path):
    try:
        check(resp, method, path)
        note(name, status=resp.status_code, contract="ok")
    except AssertionError as exc:
        note(name, status=resp.status_code, contract=str(exc).splitlines()[0][:300])


def test_probe_contract_checker(ctx):
    c = ctx.client
    admin = ctx.as_user(101, mfa=True)
    op = ctx.as_user(106, mfa=True)
    checked("enroll_unknown", c.post("/v1/agents/enroll", json={"enrollment_code": "unknown-code-123",
                                                                "agent_version": "0.3.0", "os": "linux"}),
            "POST", "/v1/agents/enroll")
    checked("enrollment_code_source", c.post(f"/v1/devices/{DEV_A_CAMERA}/enrollment-codes", headers=admin),
            "POST", "/v1/devices/{device_id}/enrollment-codes")
    base = {"name": "probe", "kind": "SOURCE", "device_type": "CAMERA", "local_copy_policy": "RETAINED"}
    checked("create_device_bad_collector", c.post("/v1/devices", json={**base, "collector_device_id": DEV_A_CAMERA},
                                                  headers=admin), "POST", "/v1/devices")
    checked("create_device_missing_project", c.post("/v1/devices", json={**base, "project_id": "prj_" + "Z" * 26},
                                                    headers=admin), "POST", "/v1/devices")
    r = c.post("/v1/devices", json={"name": "probe collector", "kind": "COLLECTOR", "device_type": "AGENT_PC",
                                     "local_copy_policy": "RETAINED", "project_id": PRJ_A}, headers=admin)
    checked("policy_new_collector", c.get(f"/v1/devices/{r.json()['device_id']}/policy", headers=admin),
            "GET", "/v1/devices/{device_id}/policy")
    # agreement not yet in effect, ended "now" (default effective_at)
    r = c.post("/v1/agreements", json={"organization_id": ORG_A, "tier": "TIER1", "reference": "AGR-PROBE-2",
                                        "valid_from": "2027-01-01T00:00:00Z"}, headers=op)
    checked("agreement_end_default_now_future_agreement",
            c.post(f"/v1/agreements/{r.json()['agreement_id']}/end", json={"reason": "probe end"}, headers=op),
            "POST", "/v1/agreements/{agreement_id}/end")
    # heartbeat for another collector
    code = c.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes", headers=admin).json()["code"]
    e = c.post("/v1/agents/enroll", json={"enrollment_code": code, "agent_version": "0.3.0", "os": "linux"}).json()
    t = c.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": e["client_id"],
                                       "client_secret": e["client_secret"]}).json()
    dev = {"Authorization": f"Bearer {t['access_token']}"}
    hb = json.loads((CONTRACTS / "examples/agent-heartbeat.vessel-agent.json").read_text())
    hb["sources"] = []
    checked("heartbeat_other_collector", c.post("/v1/agents/heartbeat", json={**hb, "collector_device_id":
                                                                               DEV_B_COLLECTOR}, headers=dev),
            "POST", "/v1/agents/heartbeat")
    checked("heartbeat_major", c.post("/v1/agents/heartbeat", json={**hb, "collector_device_id": DEV_A_COLLECTOR,
                                                                      "schema_version": "2.0.0"}, headers=dev),
            "POST", "/v1/agents/heartbeat")


def test_probe_idempotent_retry_race(ctx, monkeypatch):
    """Same Idempotency-Key + same body sent twice while the first is still in flight."""
    import app.api.context as ctxmod

    real_view = ctxmod.session_view
    first = threading.Event()

    def slow_view(*a, **kw):
        if not first.is_set():
            first.set()
            time.sleep(1.5)  # keep request 1's transaction open
        return real_view(*a, **kw)

    monkeypatch.setattr(ctxmod, "session_view", slow_view)
    h = {**ctx.as_user(102), "Idempotency-Key": "retry-key-0001"}
    body = {"project_id": PRJ_A, "label": "IDEM-RACE-1", "title": "race", "starts_at": "2026-11-03T00:00:00Z"}
    results = {}

    def send(i):
        results[i] = ctx.client.post("/v1/sessions", json=body, headers=h)

    t1 = threading.Thread(target=send, args=(1,))
    t1.start()
    first.wait(5)
    time.sleep(0.2)
    t2 = threading.Thread(target=send, args=(2,))
    t2.start()
    t1.join(30)
    t2.join(30)
    note("idempotent_retry_race_sessions",
         first=(results[1].status_code, results[1].json().get("session_id") or results[1].json().get("code")),
         second=(results[2].status_code, results[2].json().get("session_id") or results[2].json().get("code"),
                 results[2].headers.get("idempotent-replayed")))
    # a sequential retry afterwards replays
    r3 = ctx.client.post("/v1/sessions", json=body, headers=h)
    note("idempotent_sequential_retry", status=r3.status_code, replayed=r3.headers.get("idempotent-replayed"))
