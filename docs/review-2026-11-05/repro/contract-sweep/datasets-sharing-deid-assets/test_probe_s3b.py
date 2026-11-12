"""Read-only contract sweep probe -- async build accepts invalid data_card; GET dataset then breaks schema."""

from __future__ import annotations

from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A
from tests.acceptance.test_t08_t15_sharing import CARD, approved_pair, share
from tests.contract.openapi_contract import assert_matches_contract as check

ADMIN_A, OP = 101, 106


def _contract(r, method, path):
    try:
        check(r, method, path)
        return "OK"
    except AssertionError as exc:
        return "CONTRACT-FAIL: " + str(exc)[:400]


def test_s3b(ictx) -> None:
    ctx = ictx
    a = approved_pair(ctx, ORG_A, PRJ_A, DEV_A_CAMERA, ["AI_TRAINING"])
    share(ctx, ADMIN_A, a["deid"], "SANDBOX_PARTNERS")
    ds = ctx.client.post("/v1/datasets", json={"title": "probe"}, headers=ctx.as_user(OP)).json()["dataset_id"]
    bad_card = {k: v for k, v in CARD.items() if k != "privacy"}
    ctx.c.settings.__dict__["dataset_sync_max_items"] = 0
    try:
        r = ctx.client.post(f"/v1/datasets/{ds}/versions", headers=ctx.as_user(OP),
                            json={"version": "1.0.0", "data_card": bad_card, "item_asset_ids": [a["deid"]]})
        print("\nP9b async bad card:", r.status_code)
    finally:
        ctx.c.settings.__dict__["dataset_sync_max_items"] = 200
    g = ctx.client.get(f"/v1/datasets/{ds}", headers=ctx.as_user(OP))
    print("P9b GET dataset:", g.status_code, _contract(g, "GET", "/v1/datasets/{dataset_id}"))
