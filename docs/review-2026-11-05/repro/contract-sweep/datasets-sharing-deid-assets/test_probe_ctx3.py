"""Read-only contract sweep probe -- MISSED decision re-check leaves L2 asset without review_result (ctx)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.db.models import Agreement, Asset, AssetLineage, DeidReview
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A, SES_A, fid
from app.services import sharing
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset
from tests.integration.test_sharing import review, share

NOW = datetime.now(UTC)
ADMIN, MEMBER, OP, REVIEWER = 101, 102, 106, 108


def _contract(r, method, path):
    try:
        check(r, method, path)
        return "OK"
    except AssertionError as exc:
        return "CONTRACT-FAIL: " + str(exc)[:400]


def _pair(db):
    o = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, session=SES_A)
    d = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="DEID", session=SES_A)
    for x in (o, d):
        x.location = None
        x.checksum_sha256 = hashlib.sha256(x.asset_id.encode()).hexdigest()
        x.data_role, x.sensitivity_class = "BYPRODUCT", "INTERNAL"
    d.privacy_status = "PSEUDONYMIZED"
    db.add_all([o, d])
    db.flush()
    db.add(AssetLineage(child_asset_id=d.asset_id, parent_asset_id=o.asset_id, relation="DEID", created_at=NOW))
    rv = DeidReview(review_id=new_id("review"), organization_id=ORG_A, session_id=SES_A,
                    original_asset_id=o.asset_id, deid_asset_id=d.asset_id, reviewer_id=fid("usr", REVIEWER),
                    risk_level="LOW", samples=[0.5], recheck=False, status="ASSIGNED", missed=[],
                    manual_regions=[], created_at=NOW)
    db.add(rv)
    return o.asset_id, d.asset_id, rv.review_id


def test_p13_missed_recheck_breaks_l2_invariant(ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        o1, d1, r1 = _pair(db)
        o2, d2, r2 = _pair(db)
        db.add(Agreement(agreement_id=new_id("agreement"), organization_id=ORG_A, tier="TIER2", reference="t",
                         valid_from=NOW - timedelta(days=1), valid_until=None, allowed_purposes=["AI_TRAINING"],
                         status="ACTIVE", created_by="t", created_at=NOW))
    # O1 through the share flow; D1 passes deid review; D1 -> L2
    ctx.client.post("/v1/share-candidates", json={"asset_ids": [o1]}, headers=ctx.as_user(MEMBER))
    review(ctx, o1, OP, action="START_LEGAL_REVIEW")
    review(ctx, o1, OP, action="PASS_LEGAL_REVIEW", legal_basis_ref="LB-1")
    with ctx.c.sessionmaker.begin() as db:
        sharing.mark_processed(ctx.c, db, db.get(Asset, o1))
    r = ctx.client.post(f"/v1/deid-reviews/{r1}/decision", json={"result": "PASS"}, headers=ctx.as_user(REVIEWER))
    print("\nP13 R1 PASS:", r.status_code)
    print("P13 approve O1:", review(ctx, o1, OP, action="APPROVE").json().get("share_status"))
    r = share(ctx, d1, ADMIN, target_scope="SANDBOX_PARTNERS")
    print("P13 D1 -> L2:", r.status_code, r.json().get("state"))
    g = ctx.client.get(f"/v1/assets/{d1}", headers=ctx.as_user(ADMIN))
    print("P13 D1 before:", g.json()["sharing_scope"], g.json().get("review_result"), _contract(g, "GET", "/v1/assets/{asset_id}"))
    # another review in the same session is decided MISSED -> same-session recheck
    r = ctx.client.post(f"/v1/deid-reviews/{r2}/decision", json={"result": "MISSED", "missed": [{"cls": "FACE", "t": 1}]},
                        headers=ctx.as_user(REVIEWER))
    print("P13 R2 MISSED:", r.status_code, "recheck:", r.json().get("recheck_review_ids"))
    g = ctx.client.get(f"/v1/assets/{d1}", headers=ctx.as_user(ADMIN))
    print("P13 D1 after:", g.json()["sharing_scope"], g.json().get("review_result"))
    print("P13 contract:", _contract(g, "GET", "/v1/assets/{asset_id}"))
