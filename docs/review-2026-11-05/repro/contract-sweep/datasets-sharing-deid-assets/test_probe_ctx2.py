"""Read-only contract sweep probes -- direct 403s and audit (ctx fixture, no S3)."""

from __future__ import annotations

from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A
from tests.integration.test_catalog import _asset

ADMIN, MEMBER, VIEWER, OP = 101, 102, 105, 106


def test_p12_direct_forbidden_not_audited(ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        a.location = None
        a.data_role = "BYPRODUCT"
        a.storage_zone, a.storage_key = "RAW", f"{ORG_A}/raw/{a.asset_id}"
        db.add(a)
        aid = a.asset_id
    cases = [
        ("operator download-url without break-glass", "POST", f"/v1/assets/{aid}/download-url", None, OP),
        ("viewer download-url (require() path, for comparison)", "POST", f"/v1/assets/{aid}/download-url", None,
         VIEWER),
        ("member PATCH data_role", "PATCH", f"/v1/assets/{aid}", {"data_role": "CORE", "reason": "x1"}, MEMBER),
        ("member share-review RESUBMIT-like (CANDIDATE->LEGAL_REVIEW)", "POST", f"/v1/assets/{aid}/share-review",
         {"action": "START_LEGAL_REVIEW"}, MEMBER),
    ]
    ctx.client.post("/v1/share-candidates", json={"asset_ids": [aid]}, headers=ctx.as_user(MEMBER))
    for label, method, path, body, user in cases:
        before = ctx.audit_actions()
        r = ctx.client.request(method, path, json=body, headers=ctx.as_user(user))
        print(f"\nP12 {label}: {r.status_code} {r.json().get('code')} new audit rows: {ctx.audit_actions()[len(before):]}")
