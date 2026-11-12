"""Read-only contract sweep probes (datasets) -- ictx fixture (moto S3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db.models import Organization, User
from app.seed import DEV_A_CAMERA, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, PRJ_B, fid, sub
from app.services import datasets
from tests.acceptance.test_t08_t15_sharing import CARD, access, approved_pair, decide, share, to_l3
from tests.contract.openapi_contract import assert_matches_contract as check

NOW = datetime.now(UTC)
ADMIN_A, ADMIN_B, OP, CONSUMER, CONSUMER2 = 101, 103, 106, 109, 110


def _contract(r, method, path):
    try:
        check(r, method, path)
        return "OK"
    except AssertionError as exc:
        return "CONTRACT-FAIL: " + str(exc)[:300]


def _body(version_id, **over):
    b = {"dataset_version_id": version_id, "purpose": "AI_TRAINING", "purpose_detail": "coastal detection training",
         "period": {"start_date": NOW.date().isoformat(), "end_date": (NOW + timedelta(days=30)).date().isoformat()},
         "security_measures": "isolated server, 2-person access, destroy after", "agreement_accepted": True,
         "agreement_version": "v1"}
    b.update(over)
    return b


def test_s3_probes(ictx) -> None:
    ctx = ictx
    org_c2 = fid("org", 6)
    with ctx.c.sessionmaker.begin() as db:
        db.add(Organization(organization_id=org_c2, name="consumer 2 (synthetic)", org_type="CONSUMER",
                            status="ACTIVE", created_at=NOW))
        db.flush()
        db.add(User(user_id=fid("usr", 110), organization_id=org_c2, idp_subject=sub(CONSUMER2), username="consumer2",
                    contact_email=None, roles=["CONSUMER"], status="ACTIVE", created_at=NOW, updated_at=NOW))
    a = approved_pair(ctx, ORG_A, PRJ_A, DEV_A_CAMERA, ["AI_TRAINING", "JOINT_RESEARCH"])
    assert share(ctx, ADMIN_A, a["deid"], "SANDBOX_PARTNERS").json()["state"] == "APPLIED"
    ds = ctx.client.post("/v1/datasets", json={"title": "probe"}, headers=ctx.as_user(OP)).json()["dataset_id"]
    path = f"/v1/datasets/{ds}/versions"
    r = ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "1.0.0", "item_asset_ids": [a["deid"]],
                                                             "data_card": CARD})
    assert r.status_code == 201, r.text
    vid = r.json()["dataset_version_id"]
    to_l3(ctx, ADMIN_A, a["deid"])
    assert ctx.client.post(f"/v1/datasets/{ds}/publish", headers=ctx.as_user(OP)).status_code == 200

    # P7: impossible calendar date (schema: format=date)
    for bad in ("2026-02-30", "2026-13-01"):
        r = ctx.client.post("/v1/access-requests", headers=ctx.as_user(CONSUMER),
                            json=_body(vid, period={"start_date": bad, "end_date": "2026-12-31"}))
        print(f"\nP7 start_date={bad}:", r.status_code, r.json().get("code"))
    # P7b: agreement_version omitted (required by access-request.schema.json)
    body = _body(vid)
    del body["agreement_version"]
    r = ctx.client.post("/v1/access-requests", headers=ctx.as_user(CONSUMER), json=body)
    print("P7b no agreement_version:", r.status_code, r.json().get("status"))

    # P8: operator approval after version scope fell below L3 -> undocumented 422
    rid = access(ctx, vid, "AI_TRAINING").json()["access_request_id"]
    r = decide(ctx, ADMIN_A, rid, "APPROVE")
    print("P8 provider approve:", r.status_code, r.json().get("status"))
    r = share(ctx, ADMIN_A, a["deid"], "SANDBOX_PARTNERS", existing_grant_handling="KEEP_UNTIL_EXPIRY")
    print("P8 downgrade L3->L2:", r.status_code, r.json().get("state"))
    r = decide(ctx, OP, rid, "APPROVE")
    print("P8 operator approve:", r.status_code, r.json().get("code"),
          _contract(r, "POST", "/v1/access-requests/{access_request_id}/decisions"))
    to_l3(ctx, ADMIN_A, a["deid"])

    # P10: grant cross-org (other consumer org) -> 404 w/o CROSS_ORG audit
    rid = access(ctx, vid, "AI_TRAINING").json()["access_request_id"]
    decide(ctx, ADMIN_A, rid, "APPROVE")
    r = decide(ctx, OP, rid, "APPROVE")
    grant = r.json()["grant_id"]
    before = ctx.audit_actions()
    r1 = ctx.client.post("/v1/distributions", json={"grant_id": grant}, headers=ctx.as_user(CONSUMER2))
    r2 = ctx.client.post(f"/v1/grants/{grant}/destruction-confirmation", headers=ctx.as_user(CONSUMER2))
    r3 = ctx.client.post(f"/v1/grants/{grant}/revoke", json={"reason": "probe"}, headers=ctx.as_user(ADMIN_B))
    print("P10 other-consumer distribution:", r1.status_code, "destruction:", r2.status_code,
          "unrelated ORG_ADMIN revoke:", r3.status_code, "new audit rows:", ctx.audit_actions()[len(before):])

    # P11: lineage on BUILDING version -> 409 (not in contract)
    # P9: async build path does not check data_card before 202
    b = approved_pair(ctx, ORG_B, PRJ_B, DEV_B_CAMERA, ["AI_TRAINING"])
    share(ctx, ADMIN_B, b["deid"], "SANDBOX_PARTNERS")
    a2 = a
    bad_card = {k: v for k, v in CARD.items() if k != "privacy"}
    r = ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "3.0.0", "data_card": bad_card,
                                                             "item_asset_ids": [a2["deid"]]})
    print("P9 sync path bad card:", r.status_code, r.json().get("code"))
    ctx.c.settings.__dict__["dataset_sync_max_items"] = 1
    try:
        r = ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "2.0.0", "data_card": bad_card,
                                                                 "item_asset_ids": [a2["deid"], b["deid"]]})
        print("P9 async path bad card:", r.status_code, r.json())
        v2 = r.json().get("dataset_version_id")
        lr = ctx.client.get(f"{path}/{v2}/lineage", headers=ctx.as_user(OP))
        print("P11 lineage BUILDING:", lr.status_code, lr.json().get("code"),
              _contract(lr, "GET", "/v1/datasets/{dataset_id}/versions/{dataset_version_id}/lineage"))
        out = []
        while (job := ctx.c.queue.claim(["dataset_build"], "t", ("CPU",))) is not None:
            out.append(datasets.finish_build(ctx.c, job.payload["dataset_version_id"]))
            ctx.c.queue.complete(job.job_id)
        v = next(x for x in ctx.client.get(f"/v1/datasets/{ds}", headers=ctx.as_user(OP)).json()["versions"]
                 if x["dataset_version_id"] == v2)
        print("P9 build result:", out, v["status"], (v["build_error"] or "")[:160])
    finally:
        ctx.c.settings.__dict__["dataset_sync_max_items"] = 200
