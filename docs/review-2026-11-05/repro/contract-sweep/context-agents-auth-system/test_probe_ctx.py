"""Read-only probe for contract-vs-server mismatches (context/agents/auth/system). Prints observations."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text

from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_B_COLLECTOR, ORG_A, PRJ_A, SES_A

OUT = Path(__file__).with_name("probe_out.jsonl")
CONTRACTS = Path("/home/user/oceandata/contracts")


def note(name, **kw):
    with OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"probe": name, **kw}, ensure_ascii=False, default=str) + "\n")


def audits(ctx, action=None):
    with ctx.c.engine.connect() as conn:
        rows = conn.execute(text("SELECT action, organization_id, target_id, result FROM audit_log ORDER BY seq")).all()
    return [tuple(r) for r in rows if action is None or r[0] == action]


def device_token(ctx, device_id=DEV_A_COLLECTOR):
    admin = ctx.as_user(101, mfa=True)
    r = ctx.client.post(f"/v1/devices/{device_id}/enrollment-codes", headers=admin)
    assert r.status_code == 201, r.text
    e = ctx.client.post("/v1/agents/enroll", json={"enrollment_code": r.json()["code"], "agent_version": "0.3.0",
                                                   "os": "linux"})
    assert e.status_code == 201, e.text
    t = ctx.client.post("/v1/auth/token", data={"grant_type": "client_credentials", "client_id": e.json()["client_id"],
                                                "client_secret": e.json()["client_secret"]})
    assert t.status_code == 200, t.text
    return {"Authorization": f"Bearer {t.json()['access_token']}"}


def test_probe_agents_auth(ctx):
    c = ctx.client
    admin = ctx.as_user(101, mfa=True)
    # enroll with unknown code
    r = c.post("/v1/agents/enroll", json={"enrollment_code": "unknown-code-123", "agent_version": "0.3.0", "os": "linux"})
    note("enroll_unknown_code", status=r.status_code, code=r.json().get("code"))
    # enrollment code for SOURCE device
    r = c.post(f"/v1/devices/{DEV_A_CAMERA}/enrollment-codes", headers=admin)
    note("enrollment_code_source_device", status=r.status_code, code=r.json().get("code"))
    # enrollment code without MFA
    r = c.post(f"/v1/devices/{DEV_A_COLLECTOR}/enrollment-codes", headers=ctx.as_user(101))
    note("enrollment_code_no_mfa", status=r.status_code, code=r.json().get("code"))

    dev = device_token(ctx)
    hb = json.loads((CONTRACTS / "examples/agent-heartbeat.vessel-agent.json").read_text())
    hb["sources"] = []
    hb_other = {**hb, "collector_device_id": DEV_B_COLLECTOR}
    before = len(audits(ctx, "CROSS_ORG_ACCESS_ATTEMPT"))
    r = c.post("/v1/agents/heartbeat", json=hb_other, headers=dev)
    note("heartbeat_other_org_collector", status=r.status_code, code=r.json().get("code"),
         cross_org_audits_added=len(audits(ctx, "CROSS_ORG_ACCESS_ATTEMPT")) - before)
    hb_major = {**hb, "collector_device_id": DEV_A_COLLECTOR, "schema_version": "2.0.0"}
    r = c.post("/v1/agents/heartbeat", json=hb_major, headers=dev)
    note("heartbeat_major_version", status=r.status_code, code=r.json().get("code"))

    before = len(audits(ctx, "CROSS_ORG_ACCESS_ATTEMPT"))
    r = c.get(f"/v1/agents/{DEV_B_COLLECTOR}/health", headers=dev)
    r2 = c.get(f"/v1/devices/{DEV_B_COLLECTOR}/policy", headers=dev)
    note("device_principal_other_org_health_policy", health=r.status_code, policy=r2.status_code,
         cross_org_audits_added=len(audits(ctx, "CROSS_ORG_ACCESS_ATTEMPT")) - before)
    # compare: user principal of org A on org B device
    before = len(audits(ctx, "CROSS_ORG_ACCESS_ATTEMPT"))
    r = c.get(f"/v1/agents/{DEV_B_COLLECTOR}/health", headers=ctx.as_user(102))
    note("user_other_org_health", status=r.status_code,
         cross_org_audits_added=len(audits(ctx, "CROSS_ORG_ACCESS_ATTEMPT")) - before)


def test_probe_context(ctx):
    c = ctx.client
    admin = ctx.as_user(101, mfa=True)
    op = ctx.as_user(106, mfa=True)
    # create device: collector_device_id that is a SOURCE / nonexistent
    base = {"name": "probe", "kind": "SOURCE", "device_type": "CAMERA", "local_copy_policy": "RETAINED"}
    r = c.post("/v1/devices", json={**base, "collector_device_id": DEV_A_CAMERA}, headers=admin)
    note("create_device_collector_is_source", status=r.status_code, code=r.json().get("code"))
    r = c.post("/v1/devices", json={**base, "collector_device_id": "dev_" + "Z" * 26}, headers=admin)
    note("create_device_collector_missing", status=r.status_code, code=r.json().get("code"))
    r = c.post("/v1/devices", json={**base, "project_id": "prj_" + "Z" * 26}, headers=admin)
    note("create_device_project_missing", status=r.status_code, code=r.json().get("code"))
    # new collector -> default policy with no sources
    r = c.post("/v1/devices", json={"name": "probe collector", "kind": "COLLECTOR", "device_type": "AGENT_PC",
                                     "local_copy_policy": "RETAINED", "project_id": PRJ_A}, headers=admin)
    assert r.status_code == 201, r.text
    new_collector = r.json()["device_id"]
    r = c.get(f"/v1/devices/{new_collector}/policy", headers=admin)
    from app.services.contracts import schema_problems

    probs = schema_problems("device-policy", r.json()) if r.status_code == 200 else None
    note("policy_new_collector_no_sources", status=r.status_code, sources=r.json().get("sources"),
         schema_problems=[(p.pointer, p.code, p.message) for p in (probs or [])])

    # agreement end with effective_at before valid_from
    r = c.post("/v1/agreements", json={"organization_id": ORG_A, "tier": "TIER2", "reference": "AGR-PROBE-1",
                                        "valid_from": "2026-12-01T00:00:00Z",
                                        "allowed_purposes": ["JOINT_RESEARCH"]}, headers=op)
    note("agreement_create", status=r.status_code)
    agr = r.json()["agreement_id"]
    r = c.post(f"/v1/agreements/{agr}/end", json={"reason": "probe end", "effective_at": "2026-11-15T00:00:00Z"},
               headers=op)
    note("agreement_end_before_valid_from", status=r.status_code, code=r.json().get("code"))

    # break-glass revoke on REQUESTED grant
    r = c.post("/v1/break-glass-grants", json={"target_type": "SESSION", "target_id": SES_A,
                                                "reason": "INC-0001 probe reason text", "duration_s": 600}, headers=op)
    assert r.status_code == 201, r.text
    gid = r.json()["grant_id"]
    r = c.post(f"/v1/break-glass-grants/{gid}/revoke", json={"reason": "probe"}, headers=op)
    note("break_glass_revoke_requested", status=r.status_code, grant_status=r.json().get("status"))
