"""Read-only contract sweep probes (datasets/sharing/deid/assets) -- ctx fixture (no S3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.db.models import Agreement, Asset, DeidReview, Project, TrialSession, User
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, DEV_B_CAMERA, ORG_A, ORG_B, ORG_OPERATOR, PRJ_A, PRJ_B, SES_B, fid, sub
from app.services import sharing
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset
from tests.integration.test_sharing import make_assets, review, share

NOW = datetime.now(UTC)
ADMIN, MEMBER, ADMIN_B, OP, REVIEWER = 101, 102, 103, 106, 108


def _contract(r, method, path):
    try:
        check(r, method, path)
        return "OK"
    except AssertionError as exc:
        return "CONTRACT-FAIL: " + str(exc)[:300]


def test_p1_classification_breaks_l2_invariant(ctx) -> None:
    ids = make_assets(ctx)
    with ctx.c.sessionmaker.begin() as db:  # test helper leaves location_precision unset; avoid unrelated noise
        db.execute(update(Asset).values(location_precision="GENERALIZED"))
    ctx.client.post("/v1/share-candidates", json={"asset_ids": [ids["orig"]]}, headers=ctx.as_user(MEMBER))
    assert review(ctx, ids["orig"], OP, action="START_LEGAL_REVIEW").status_code == 200
    assert review(ctx, ids["orig"], OP, action="PASS_LEGAL_REVIEW", legal_basis_ref="LB-1").status_code == 200
    with ctx.c.sessionmaker.begin() as db:
        sharing.mark_processed(ctx.c, db, db.get(Asset, ids["orig"]))
        db.execute(update(Asset).where(Asset.asset_id == ids["deid"]).values(review_result="PASSED"))
    assert review(ctx, ids["orig"], OP, action="APPROVE").json()["share_status"] == "APPROVED"
    with ctx.c.sessionmaker.begin() as db:
        db.add(Agreement(agreement_id=new_id("agreement"), organization_id=ORG_A, tier="TIER2", reference="t",
                         valid_from=NOW - timedelta(days=1), valid_until=None, allowed_purposes=["AI_TRAINING"],
                         status="ACTIVE", created_by="t", created_at=NOW))
    r = share(ctx, ids["deid"], ADMIN, target_scope="SANDBOX_PARTNERS")
    assert r.json()["state"] == "APPLIED", r.text
    g = ctx.client.get(f"/v1/assets/{ids['deid']}", headers=ctx.as_user(ADMIN))
    print("\nP1 before classify:", g.json()["sharing_scope"], g.json()["sensitivity_class"],
          _contract(g, "GET", "/v1/assets/{asset_id}"))
    r = ctx.client.patch(f"/v1/assets/{ids['deid']}/classification", headers=ctx.as_user(OP),
                         json={"sensitivity_class": "SECURITY", "basis_ref": "inspect-1", "reason": "secure area"})
    print("P1 classify:", r.status_code, r.json())
    g = ctx.client.get(f"/v1/assets/{ids['deid']}", headers=ctx.as_user(ADMIN))
    print("P1 after classify GET:", g.status_code, g.json()["sharing_scope"], g.json()["sensitivity_class"])
    print("P1 contract:", _contract(g, "GET", "/v1/assets/{asset_id}"))
    lst = ctx.client.get("/v1/assets?variant=DEID", headers=ctx.as_user(ADMIN))
    print("P1 list contract:", _contract(lst, "GET", "/v1/assets"))


def test_p2_deletion_certificate_cross_org_audit(ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        b = _asset(ORG_B, PRJ_B, DEV_B_CAMERA, session=SES_B)
        b.location = None
        db.add(b)
        bid = b.asset_id
    r = ctx.client.post(f"/v1/assets/{bid}/deletion", json={"reason": "cleanup"},
                        headers=ctx.as_user(ADMIN_B, mfa=True))
    print("\nP2 delete:", r.status_code, r.text[:200])
    cert = r.json()["certificate_id"]
    before = ctx.audit_actions()
    g = ctx.client.get(f"/v1/deletion-certificates/{cert}", headers=ctx.as_user(ADMIN))
    after = ctx.audit_actions()
    print("P2 cross-org GET:", g.status_code, g.json().get("code"), "new audit rows:", after[len(before):])
    # compare: same request shape on an asset (load_scoped) records the audit
    before = ctx.audit_actions()
    g2 = ctx.client.get(f"/v1/assets/{bid}", headers=ctx.as_user(ADMIN))
    print("P2 compare deleted-asset GET:", g2.status_code, "new audit rows:", ctx.audit_actions()[len(before):])


def test_p3_list_datasets_ignores_limit(ctx) -> None:
    for t in ("ds-1", "ds-2", "ds-3"):
        assert ctx.client.post("/v1/datasets", json={"title": t}, headers=ctx.as_user(OP)).status_code == 201
    r = ctx.client.get("/v1/datasets?limit=1", headers=ctx.as_user(OP))
    print("\nP3 limit=1:", r.status_code, "items:", len(r.json()["items"]), "next_cursor:", r.json()["next_cursor"])
    r = ctx.client.get("/v1/assets?limit=1", headers=ctx.as_user(ADMIN))
    print("P3 compare /v1/assets limit=1:", r.status_code, len(r.json()["items"]))


def test_p4_patch_asset_session_other_project_422(ctx) -> None:
    prj2, ses2 = fid("prj", 299), fid("ses", 499)
    with ctx.c.sessionmaker.begin() as db:
        db.add(Project(project_id=prj2, organization_id=ORG_A, name="A second project", status="ACTIVE",
                       created_at=NOW))
        db.flush()
        db.add(TrialSession(session_id=ses2, organization_id=ORG_A, project_id=prj2, label="A2", title="A2",
                            starts_at=NOW, ends_at=None, site=None, note=None, created_at=NOW))
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, session=None)
        a.location = None
        a.ingest_status, a.quarantine_reason = "QUARANTINED", "UNKNOWN_SESSION"
        db.add(a)
        aid = a.asset_id
    r = ctx.client.patch(f"/v1/assets/{aid}", json={"session_id": ses2}, headers=ctx.as_user(MEMBER))
    print("\nP4 PATCH:", r.status_code, r.json().get("code"), _contract(r, "PATCH", "/v1/assets/{asset_id}"))


def test_p5_share_request_same_scope_409(ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        db.add(a)
        aid = a.asset_id
    r = share(ctx, aid, ADMIN, target_scope="PRIVATE", existing_grant_handling="REVOKE_NOW")
    print("\nP5 same scope:", r.status_code, r.json().get("code"),
          _contract(r, "POST", "/v1/assets/{asset_id}/share-requests"))


def test_p6_deid_review_unassigned_cross_org_audit(ctx) -> None:
    other = fid("usr", 120)
    with ctx.c.sessionmaker.begin() as db:
        db.add(User(user_id=other, organization_id=ORG_OPERATOR, idp_subject=sub(120), username="reviewer2",
                    contact_email=None, roles=["REVIEWER"], status="ACTIVE", created_at=NOW, updated_at=NOW))
        orig = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        deid = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="DEID")
        db.add_all([orig, deid])
        db.flush()
        rv = DeidReview(review_id=new_id("review"), organization_id=ORG_A, session_id=None,
                        original_asset_id=orig.asset_id, deid_asset_id=deid.asset_id, reviewer_id=other,
                        risk_level="LOW", samples=[0.5], recheck=False, status="ASSIGNED", missed=[],
                        manual_regions=[], created_at=NOW)
        db.add(rv)
        rid = rv.review_id
    before = ctx.audit_actions()
    r = ctx.client.get(f"/v1/deid-reviews/{rid}", headers=ctx.as_user(REVIEWER))
    r2 = ctx.client.post(f"/v1/deid-reviews/{rid}/decision", json={"result": "PASS"}, headers=ctx.as_user(REVIEWER))
    print("\nP6 unassigned reviewer GET:", r.status_code, "POST decision:", r2.status_code,
          "new audit rows:", ctx.audit_actions()[len(before):])
