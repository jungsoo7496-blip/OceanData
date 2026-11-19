"""자산 수정(PATCH /v1/assets/{asset_id})의 감사와 계약 응답 (계약 점검 S23~S26, ADR-0076).

- S23: data_role 변경 감사(DATA_ROLE_CHANGED)는 변경과 같은 트랜잭션이다. 같은 요청의 회차 지정이 404·409·422로 실패해
  변경이 되돌아가면 감사도 남지 않는다 (전에는 따로 커밋돼 일어나지 않은 변경이 SUCCESS로 남았다).
- S24: ORG_ADMIN이 아닌 사용자의 data_role 변경 403은 PERMISSION_DENIED 감사를 남긴다.
- S25: CORE로 바꿔 파생본의 공유 등급을 내리고 이용 권한을 끊으면, 내려간 자산마다 SHARE_SCOPE_DOWNGRADED,
  끊긴 권한마다 GRANT_REVOKED를 남긴다.
- S26: 자산과 다른 실증사업의 회차를 지정하면 422 RULE_VIOLATION — 계약에 적힌 응답이다.
시험 데이터는 합성 값이다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.db.models import (
    AccessRequest,
    Asset,
    AssetLineage,
    AuditLog,
    Dataset,
    DatasetVersion,
    DatasetVersionItem,
    Grant,
    Project,
    TrialSession,
)
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, ORG_A, ORG_CONSUMER, PRJ_A, fid
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
ADMIN, MEMBER = 101, 102
NOW = datetime.now(UTC)
PATH = "/v1/assets/{asset_id}"


def _sessions(ctx) -> dict[str, str]:
    """같은 실증사업의 다른 회차, 다른 실증사업(같은 기관)의 회차."""
    other_prj = fid("prj", 296)
    out = {"same_project": fid("ses", 496), "other_project": fid("ses", 497)}
    with ctx.c.sessionmaker.begin() as db:
        db.add(Project(project_id=other_prj, organization_id=ORG_A, name="합성 두 번째 사업", status="ACTIVE",
                       created_at=NOW))
        db.flush()
        for label, (sid, prj) in {"S23-a": (out["same_project"], PRJ_A),
                                  "S23-b": (out["other_project"], other_prj)}.items():
            db.add(TrialSession(session_id=sid, organization_id=ORG_A, project_id=prj, label=label, title=label,
                                starts_at=NOW, ends_at=None, site=None, note=None, created_at=NOW))
    return out


def _new_asset(ctx) -> str:
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)  # CORE, READY_PRIVATE, 회차 SES_A
        db.add(a)
        return a.asset_id


def _audits(ctx, response) -> list[AuditLog]:
    with ctx.c.sessionmaker() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.request_id == response.headers["x-request-id"])
                               .order_by(AuditLog.seq)))


def _role(ctx, asset_id: str) -> str:
    with ctx.c.engine.connect() as conn:
        return str(conn.execute(text("SELECT data_role FROM assets WHERE asset_id = :a"), {"a": asset_id}).scalar())


@pytest.mark.parametrize(("session_key", "status", "code"), [
    ("other_project", 422, "RULE_VIOLATION"),  # 다른 실증사업의 회차
    ("missing", 404, "NOT_FOUND"),  # 없는 회차
    ("same_project", 409, "CONFLICT"),  # 등록된 자산의 회차 바꾸기
])
def test_failed_session_change_leaves_no_data_role_audit(ctx, session_key: str, status: int, code: str) -> None:
    sessions = {**_sessions(ctx), "missing": fid("ses", 495)}
    aid = _new_asset(ctx)
    r = ctx.client.patch(f"/v1/assets/{aid}", json={"data_role": "BYPRODUCT", "reason": "합성 재분류",
                                                     "session_id": sessions[session_key]},
                         headers=ctx.as_user(ADMIN))
    assert (r.status_code, r.json()["code"]) == (status, code), r.text
    assert _role(ctx, aid) == "CORE"  # 변경은 되돌아갔다
    assert [x.action for x in _audits(ctx, r) if x.action == "DATA_ROLE_CHANGED"] == [], \
        "되돌아간 data_role 변경이 감사 로그에 SUCCESS로 남았다"
    check(r, "PATCH", PATH)


def test_successful_data_role_change_writes_one_audit(ctx) -> None:
    aid = _new_asset(ctx)
    r = ctx.client.patch(f"/v1/assets/{aid}", json={"data_role": "BYPRODUCT", "reason": "합성 재분류"},
                         headers=ctx.as_user(ADMIN))
    assert r.status_code == 200, r.text
    assert _role(ctx, aid) == "BYPRODUCT"
    rows = [(x.action, x.result, x.target_type, x.target_id, x.reason, x.details) for x in _audits(ctx, r)]
    assert rows == [("DATA_ROLE_CHANGED", "SUCCESS", "Asset", aid, "합성 재분류", {"from": "CORE", "to": "BYPRODUCT"})]


def test_member_data_role_change_denial_is_audited(ctx) -> None:
    aid = _new_asset(ctx)
    r = ctx.client.patch(f"/v1/assets/{aid}", json={"data_role": "BYPRODUCT", "reason": "합성 재분류"},
                         headers=ctx.as_user(MEMBER))
    assert (r.status_code, r.json()["code"]) == (403, "FORBIDDEN"), r.text
    check(r, "PATCH", PATH)
    assert _role(ctx, aid) == "CORE"
    rows = [(x.action, x.result, x.actor_id, x.organization_id, x.target_type, x.target_id, x.details)
            for x in _audits(ctx, r)]
    assert rows == [("PERMISSION_DENIED", "DENIED", fid("usr", MEMBER), ORG_A, "Asset", aid,
                     {"action": "asset:data_role", "from": "CORE", "to": "BYPRODUCT"})]
    # 지금 값과 같은 data_role은 변경이 아니다 — 거부도 감사도 없다
    same = ctx.client.patch(f"/v1/assets/{aid}", json={"data_role": "CORE"}, headers=ctx.as_user(MEMBER))
    assert same.status_code == 200, same.text
    assert [x.action for x in _audits(ctx, same)] == []


def _byproduct_with_shared_deid(ctx) -> dict[str, str]:
    """BYPRODUCT 원본, 그 비식별 파생본(SANDBOX_PARTNERS), 파생본이 든 데이터셋 버전의 이용 권한(GRANTED)."""
    with ctx.c.sessionmaker.begin() as db:
        orig = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        deid = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="DEID")
        orig.data_role = deid.data_role = "BYPRODUCT"
        db.add_all([orig, deid])
        db.flush()
        db.add(AssetLineage(child_asset_id=deid.asset_id, parent_asset_id=orig.asset_id, relation="DEID",
                            created_at=NOW))
        ds = Dataset(dataset_id=new_id("dataset"), title="합성 데이터셋", created_by="u", created_at=NOW)
        db.add(ds)
        db.flush()
        v = DatasetVersion(dataset_version_id=new_id("dataset_version"), dataset_id=ds.dataset_id, version="1",
                           provider_organization_ids=[ORG_A], item_count=1, total_bytes=10, data_card={},
                           manifest={}, manifest_key="k", manifest_sha256="0" * 64, created_by="u", created_at=NOW)
        db.add(v)
        db.flush()
        db.add(DatasetVersionItem(dataset_version_id=v.dataset_version_id, asset_id=deid.asset_id,
                                  organization_id=ORG_A, path="a.mp4", object_key="k/a", byte_size=10,
                                  checksum_sha256="0" * 64))
        ar = AccessRequest(access_request_id=new_id("access_request"), dataset_version_id=v.dataset_version_id,
                           organization_id=ORG_CONSUMER, requested_by="u", purpose="RESEARCH", purpose_detail="x",
                           period_start=NOW, period_end=NOW + timedelta(days=30), security_measures="x",
                           status="GRANTED", deadline_at=NOW + timedelta(days=3), created_at=NOW, updated_at=NOW)
        db.add(ar)
        db.flush()
        g = Grant(grant_id=new_id("grant"), access_request_id=ar.access_request_id,
                  dataset_version_id=v.dataset_version_id, organization_id=ORG_CONSUMER, status="GRANTED",
                  starts_at=NOW - timedelta(hours=1), expires_at=NOW + timedelta(days=30), created_at=NOW)
        db.add(g)
        ids = {"orig": orig.asset_id, "deid": deid.asset_id, "grant": g.grant_id}
    with ctx.c.engine.begin() as conn:  # 공유 흐름으로 도달한 등급 (ORM은 Sharing_Rules 우회를 막는다)
        conn.execute(text("UPDATE assets SET sharing_scope = 'SANDBOX_PARTNERS' WHERE asset_id = :d"),
                     {"d": ids["deid"]})
    return ids


def _scope_and_grant(ctx, ids: dict[str, str]) -> tuple[str, str]:
    with ctx.c.sessionmaker() as db:
        return db.get(Asset, ids["deid"]).sharing_scope, db.get(Grant, ids["grant"]).status


def test_core_change_audits_downgraded_descendant_and_revoked_grant(ctx) -> None:
    ids = _byproduct_with_shared_deid(ctx)
    r = ctx.client.patch(f"/v1/assets/{ids['orig']}", json={"data_role": "CORE", "reason": "핵심 영상으로 재분류"},
                         headers=ctx.as_user(ADMIN, mfa=True))
    assert r.status_code == 200, r.text
    assert _scope_and_grant(ctx, ids) == ("PROGRAM_REVIEW", "REVOKED")
    rows = [(x.action, x.result, x.organization_id, x.target_type, x.target_id, x.reason, x.details)
            for x in _audits(ctx, r)]
    assert rows == [
        ("DATA_ROLE_CHANGED", "SUCCESS", ORG_A, "Asset", ids["orig"], "핵심 영상으로 재분류",
         {"from": "BYPRODUCT", "to": "CORE"}),
        ("SHARE_SCOPE_DOWNGRADED", "SUCCESS", ORG_A, "Asset", ids["deid"], "핵심 영상으로 재분류",
         {"from": "SANDBOX_PARTNERS", "to": "PROGRAM_REVIEW", "existing_grant_handling": "REVOKE_NOW",
          "cause": "DATA_ROLE_CHANGED", "origin_asset_id": ids["orig"], "revoked_grant_ids": [ids["grant"]]}),
        ("GRANT_REVOKED", "SUCCESS", ORG_CONSUMER, "Grant", ids["grant"], "핵심 영상으로 재분류",
         {"cause": "SHARE_SCOPE_DOWNGRADED", "asset_id": ids["deid"]}),
    ]


def test_failed_core_change_leaves_scope_grant_and_audits_unchanged(ctx) -> None:
    ids = _byproduct_with_shared_deid(ctx)
    other = _sessions(ctx)["other_project"]
    r = ctx.client.patch(f"/v1/assets/{ids['orig']}", json={"data_role": "CORE", "reason": "핵심 영상으로 재분류",
                                                            "session_id": other},
                         headers=ctx.as_user(ADMIN, mfa=True))
    assert r.status_code == 422, r.text
    assert _scope_and_grant(ctx, ids) == ("SANDBOX_PARTNERS", "GRANTED")
    assert [x.action for x in _audits(ctx, r)] == []


def test_other_project_session_is_documented_422(ctx) -> None:
    other = _sessions(ctx)["other_project"]
    with ctx.c.sessionmaker.begin() as db:
        a = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, session=None)
        a.ingest_status = "RECEIVED"
        db.add(a)
        aid = a.asset_id
    r = ctx.client.patch(f"/v1/assets/{aid}", json={"session_id": other}, headers=ctx.as_user(ADMIN))
    assert r.status_code == 422, r.text
    check(r, "PATCH", PATH)
    body = r.json()
    assert body["code"] == "RULE_VIOLATION"
    assert body["unmet_conditions"][0]["condition"] == "session.project_id=asset.project_id"
    with ctx.c.sessionmaker() as db:
        assert db.get(Asset, aid).session_id is None
