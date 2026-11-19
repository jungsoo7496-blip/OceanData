"""이용 권한·이용 신청·배포의 다른 기관 요청 — 404와 교차 기관 시도 감사 (02번 요구사항 2.2, 계약 점검 S02·S06).

역할별 조회(이용기관은 자기 기관 것, 제공기관은 자기 자산이 든 버전의 것)에서 빠진 행이 다른 기관 것이면
없는 행과 같은 404를 돌려주고 `CROSS_ORG_ACCESS_ATTEMPT`를 남긴다. 없는 ID에는 감사를 남기지 않는다.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import AccessDecision, AccessRequest, AuditLog, Grant, Organization, User
from app.seed import DEV_A_CAMERA, ORG_A, ORG_B, ORG_CONSUMER, PRJ_A, fid, sub
from tests.acceptance.test_t08_t15_sharing import CARD, access, approved_pair, decide, share, to_l3
from tests.conftest import Ctx

pytestmark = pytest.mark.integration
ADMIN_A, ADMIN_B, OP, CONSUMER = 101, 103, 106, 109
CONSUMER2, CONSUMER_ADMIN = 110, 111  # 시험 안에서 만드는 합성 사용자
ORG_CONSUMER2 = fid("org", 6)


def published_version(ctx: Ctx) -> tuple[dict[str, str], str]:
    """A 기관 자산 하나로 만든 게시된 L3 데이터셋 버전 (합성)."""
    a = approved_pair(ctx, ORG_A, PRJ_A, DEV_A_CAMERA, ["AI_TRAINING", "JOINT_RESEARCH"])
    assert share(ctx, ADMIN_A, a["deid"], "SANDBOX_PARTNERS").json()["state"] == "APPLIED"
    ds = ctx.client.post("/v1/datasets", json={"title": "합성 데이터셋"}, headers=ctx.as_user(OP)).json()["dataset_id"]
    r = ctx.client.post(f"/v1/datasets/{ds}/versions", headers=ctx.as_user(OP),
                        json={"version": "1.0.0", "item_asset_ids": [a["deid"]], "data_card": CARD})
    assert r.status_code == 201, r.text
    to_l3(ctx, ADMIN_A, a["deid"])
    assert ctx.client.post(f"/v1/datasets/{ds}/publish", headers=ctx.as_user(OP)).status_code == 200
    return a, r.json()["dataset_version_id"]


def _add_users(ctx: Ctx) -> None:
    """두 번째 이용기관의 CONSUMER와, 이용기관 안의 ORG_ADMIN(제공 자산 없음)."""
    now = ctx.c.clock.now()
    with ctx.c.sessionmaker.begin() as db:
        db.add(Organization(organization_id=ORG_CONSUMER2, name="이용기관 2 (합성)", org_type="CONSUMER",
                            status="ACTIVE", created_at=now))
        db.flush()
        users = ((CONSUMER2, ORG_CONSUMER2, ["CONSUMER"]), (CONSUMER_ADMIN, ORG_CONSUMER, ["ORG_ADMIN"]))
        for n, org, roles in users:
            db.add(User(user_id=fid("usr", n), organization_id=org, idp_subject=sub(n), username=f"synthetic-{n}",
                        contact_email=None, roles=roles, status="ACTIVE", created_at=now, updated_at=now))


def _cross_org_rows(ctx: Ctx) -> list[tuple[Any, ...]]:
    with ctx.c.sessionmaker() as db:
        return [tuple(r) for r in db.execute(select(
            AuditLog.actor_id, AuditLog.actor_organization_id, AuditLog.organization_id, AuditLog.target_type,
            AuditLog.target_id, AuditLog.result, AuditLog.request_id, AuditLog.details,
        ).where(AuditLog.action == "CROSS_ORG_ACCESS_ATTEMPT").order_by(AuditLog.seq)).all()]


def _problem(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in ("request_id", "instance")}


def test_cross_org_grant_and_access_request_are_404_and_audited(ictx: Ctx) -> None:
    ctx = ictx
    _add_users(ctx)
    _, vid = published_version(ctx)
    pending = access(ctx, vid, "AI_TRAINING").json()["access_request_id"]
    rid = access(ctx, vid, "AI_TRAINING").json()["access_request_id"]
    assert decide(ctx, ADMIN_A, rid, "APPROVE").json()["status"] == "PROVIDER_APPROVED"
    gid = decide(ctx, OP, rid, "APPROVE").json()["grant_id"]
    missing_grant, missing_request = fid("grt", 999999), fid("acr", 999999)
    cases = [  # (요청자, 기관, 경로, 본문, 대상 종류, 다른 기관 대상, 없는 대상)
        (CONSUMER2, ORG_CONSUMER2, "/v1/distributions", lambda i: {"grant_id": i}, "Grant", gid, missing_grant),
        (CONSUMER2, ORG_CONSUMER2, "/v1/grants/{}/destruction-confirmation", lambda _: None, "Grant", gid,
         missing_grant),
        (ADMIN_B, ORG_B, "/v1/grants/{}/revoke", lambda _: {"reason": "합성 시험"}, "Grant", gid, missing_grant),
        (ADMIN_B, ORG_B, "/v1/access-requests/{}/decisions", lambda _: {"decision": "DENY", "reason": "합성 시험"},
         "AccessRequest", pending, missing_request),
    ]
    for user, org, path, body, target_type, ident, missing in cases:
        headers = ctx.as_user(user, mfa=True)
        before = _cross_org_rows(ctx)
        absent = ctx.client.post(path.format(missing), json=body(missing), headers=headers)
        assert absent.status_code == 404 and absent.json()["code"] == "NOT_FOUND", (path, absent.text)
        assert _cross_org_rows(ctx) == before, f"{path}: 없는 ID에는 교차 기관 감사를 남기지 않는다"

        hidden = ctx.client.post(path.format(ident), json=body(ident), headers=headers)
        assert hidden.status_code == 404, (path, hidden.text)
        assert _problem(hidden.json()) == _problem(absent.json()), f"{path}: 없는 것과 같은 404여야 한다"
        assert _cross_org_rows(ctx)[len(before):] == [
            (fid("usr", user), org, ORG_CONSUMER, target_type, ident, "DENIED", hidden.json()["request_id"],
             {"path": path.format(ident)}),
        ], path

    # 같은 기관 사용자가 역할 범위 밖이라 못 보는 것은 교차 기관 시도가 아니다 (tenant.get_scoped와 같은 규칙)
    before = _cross_org_rows(ctx)
    same_org = ctx.client.post(f"/v1/grants/{gid}/revoke", json={"reason": "합성 시험"},
                               headers=ctx.as_user(CONSUMER_ADMIN, mfa=True))
    assert same_org.status_code == 404 and same_org.json()["code"] == "NOT_FOUND", same_org.text
    assert _cross_org_rows(ctx) == before

    # 다른 기관 요청은 아무것도 바꾸지 않았고, 주인은 그대로 쓴다
    with ctx.c.sessionmaker() as db:
        assert db.get(AccessRequest, pending).status == "REQUESTED"
        assert db.scalar(select(AccessDecision.access_request_id)
                         .where(AccessDecision.access_request_id == pending)) is None
        g = db.get(Grant, gid)
        assert (g.status, g.destruction_confirmed_at) == ("GRANTED", None)
    own = ctx.client.post("/v1/distributions", json={"grant_id": gid}, headers=ctx.as_user(CONSUMER))
    assert own.status_code == 201, own.text
    revoked = ctx.client.post(f"/v1/grants/{gid}/revoke", json={"reason": "합성 시험"},
                              headers=ctx.as_user(ADMIN_A, mfa=True))
    assert revoked.status_code == 200 and revoked.json()["status"] == "REVOKED", revoked.text
    confirmed = ctx.client.post(f"/v1/grants/{gid}/destruction-confirmation", headers=ctx.as_user(CONSUMER))
    assert confirmed.status_code == 200, confirmed.text
    assert _cross_org_rows(ctx) == before
