"""13번 통합 시험 (A): 공유 후보 → 심사 흐름 → 등급 변경(상향 조건·운영자 승인·하향) → 우회 방지 → CORE 변경.

T08-a(CORE), T08-b(검수 전), T08-d(약정), T08-h(원본), T08-i(운영자 최종 승인), T08-j(ORG_ADMIN만) 통합 확인.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from app.db.models import Agreement, Asset, AssetLineage
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A, SES_A
from app.services import sharing
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
ADMIN, MEMBER, OP = 101, 102, 106
NOW = datetime.now(UTC)


def make_assets(ctx) -> dict[str, str]:
    with ctx.c.sessionmaker.begin() as db:
        orig = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        orig.data_role = "BYPRODUCT"
        core = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)  # CORE
        deid = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="DEID", scope="PRIVATE")
        deid.data_role, deid.privacy_status, deid.sensitivity_class = "BYPRODUCT", "PSEUDONYMIZED", "INTERNAL"
        orig.sensitivity_class = "INTERNAL"
        db.add_all([orig, core, deid])
        db.flush()
        db.add(AssetLineage(child_asset_id=deid.asset_id, parent_asset_id=orig.asset_id, relation="DEID",
                            created_at=NOW))
        return {"orig": orig.asset_id, "core": core.asset_id, "deid": deid.asset_id}


def review(ctx, asset_id: str, user: int, **body):
    return ctx.client.post(f"/v1/assets/{asset_id}/share-review", json=body, headers=ctx.as_user(user))


def share(ctx, asset_id: str, user: int, **body):
    return ctx.client.post(f"/v1/assets/{asset_id}/share-requests", json={"reason": "시험", **body},
                           headers=ctx.as_user(user))


def test_sharing_flow(ctx) -> None:
    ids = make_assets(ctx)
    # 요구사항 1: BYPRODUCT 원본만 후보, CORE·파생본은 사유와 함께 제외
    r = ctx.client.post("/v1/share-candidates", json={"asset_ids": [ids["orig"], ids["core"], ids["deid"]]},
                        headers=ctx.as_user(MEMBER))
    check(r, "POST", "/v1/share-candidates")
    assert r.json()["candidates"] == [ids["orig"]]
    assert {(x["asset_id"], x["reason"]) for x in r.json()["excluded"]} == {(ids["core"], "CORE"),
                                                                             (ids["deid"], "NOT_ORIGINAL")}
    r = ctx.client.post("/v1/share-candidates", json={"session_id": SES_A}, headers=ctx.as_user(MEMBER))
    assert any(x["reason"] == "STATUS_CANDIDATE" for x in r.json()["excluded"])  # 이미 후보

    # 요구사항 4: 심사 흐름은 표의 주체만
    assert review(ctx, ids["orig"], MEMBER, action="START_LEGAL_REVIEW").status_code == 403
    r = review(ctx, ids["orig"], OP, action="START_LEGAL_REVIEW")
    check(r, "POST", "/v1/assets/{asset_id}/share-review")
    assert r.json()["share_status"] == "LEGAL_REVIEW"
    assert review(ctx, ids["orig"], OP, action="PASS_LEGAL_REVIEW").status_code == 422  # legal_basis_ref 필수
    assert review(ctx, ids["orig"], OP, action="PASS_LEGAL_REVIEW", legal_basis_ref="LB-2026-001").json()[
        "share_status"] == "PROCESSING"
    with ctx.c.engine.connect() as conn:  # 비식별 작업 등록 (14번이 처리)
        assert conn.execute(text("SELECT count(*) FROM jobs WHERE job_type='deidentify'")).scalar() == 1
    assert review(ctx, ids["orig"], OP, action="APPROVE").status_code == 409  # PROCESSING에서 승인 불가
    with ctx.c.sessionmaker.begin() as db:
        sharing.mark_processed(ctx.c, db, db.get(Asset, ids["orig"]))  # SERVICE: 파생본 생성 완료
    r = review(ctx, ids["orig"], OP, action="APPROVE")
    assert r.status_code == 422  # 검수 합격 파생본 없음
    with ctx.c.sessionmaker.begin() as db:
        db.execute(update(Asset).where(Asset.asset_id == ids["deid"]).values(review_result="PASSED"))
    r = review(ctx, ids["orig"], OP, action="APPROVE")
    assert r.json()["share_status"] == "APPROVED"
    deid = next(d for d in r.json()["derived"] if d["asset_id"] == ids["deid"])
    assert (deid["share_status"], deid["review_result"]) == ("APPROVED", "PASSED")  # 요구사항 4.5
    r = ctx.client.get(f"/v1/assets/{ids['orig']}/share-review", headers=ctx.as_user(MEMBER))
    check(r, "GET", "/v1/assets/{asset_id}/share-review")
    assert [e["to_status"] for e in r.json()["events"]] == ["CANDIDATE", "LEGAL_REVIEW", "PROCESSING",
                                                            "QUALITY_REVIEW", "APPROVED"]

    # 요구사항 3: 등급 상향 조건
    assert share(ctx, ids["deid"], MEMBER, target_scope="SANDBOX_PARTNERS").status_code == 403  # T08-j
    r = share(ctx, ids["deid"], ADMIN, target_scope="SANDBOX_PARTNERS")
    assert r.status_code == 422 and [u["condition"] for u in r.json()["unmet_conditions"]] == ["제공기관 2단 약정 유효"]
    with ctx.c.sessionmaker.begin() as db:
        db.add(Agreement(agreement_id=new_id("agreement"), organization_id=ORG_A, tier="TIER2", reference="합성-2단",
                         valid_from=NOW - timedelta(days=1), valid_until=None, allowed_purposes=["AI_TRAINING"],
                         status="ACTIVE", created_by="t", created_at=NOW))
    r = share(ctx, ids["deid"], ADMIN, target_scope="SANDBOX_PARTNERS")
    check(r, "POST", "/v1/assets/{asset_id}/share-requests")
    assert (r.json()["state"], r.json()["from_scope"]) == ("APPLIED", "PRIVATE")
    r = share(ctx, ids["orig"], ADMIN, target_scope="SANDBOX_PARTNERS")
    assert "variant ≠ ORIGINAL" in [u["condition"] for u in r.json()["unmet_conditions"]]  # T08-h
    r = share(ctx, ids["core"], ADMIN, target_scope="SANDBOX_PARTNERS")
    assert "data_role=BYPRODUCT (원 자산 포함)" in [u["condition"] for u in r.json()["unmet_conditions"]]  # T08-a
    r = share(ctx, ids["deid"], ADMIN, target_scope="APPROVED_PARTNERS")
    assert [u["condition"] for u in r.json()["unmet_conditions"]] == ["데이터셋 버전에 포함"]

    # L4: 가명처리본은 불가 → 운영자가 근거와 함께 개인정보 비해당 지정 → 신청은 운영자 승인 대기 → 승인
    assert "privacy_status ∈ {NOT_APPLICABLE, ANONYMIZED_VERIFIED}" in [
        u["condition"] for u in share(ctx, ids["deid"], ADMIN, target_scope="PUBLIC").json()["unmet_conditions"]]
    assert ctx.client.patch(f"/v1/assets/{ids['deid']}/classification", headers=ctx.as_user(ADMIN), json={
        "privacy_status": "NOT_APPLICABLE", "basis_ref": "점검-1", "reason": "사람 없음"}).status_code == 403
    r = ctx.client.patch(f"/v1/assets/{ids['deid']}/classification", headers=ctx.as_user(OP), json={
        "privacy_status": "NOT_APPLICABLE", "basis_ref": "점검-2026-11-04-01", "reason": "해상 원경, 사람 없음"})
    check(r, "PATCH", "/v1/assets/{asset_id}/classification")
    r = share(ctx, ids["deid"], ADMIN, target_scope="PUBLIC")
    assert r.json()["state"] == "IN_REVIEW"
    req_id = r.json()["share_request_id"]
    with ctx.c.sessionmaker() as db:
        assert db.get(Asset, ids["deid"]).sharing_scope == "SANDBOX_PARTNERS"  # 아직 안 바뀜
    assert ctx.client.post(f"/v1/share-requests/{req_id}/decisions", json={"decision": "APPROVE", "reason": "확인"},
                           headers=ctx.as_user(ADMIN)).status_code == 403
    r = ctx.client.post(f"/v1/share-requests/{req_id}/decisions", json={"decision": "APPROVE", "reason": "확인"},
                        headers=ctx.as_user(OP))
    check(r, "POST", "/v1/share-requests/{share_request_id}/decisions")
    assert r.json()["state"] == "APPLIED"
    assert ctx.client.post(f"/v1/share-requests/{req_id}/decisions", json={"decision": "APPROVE", "reason": "다시"},
                           headers=ctx.as_user(OP)).status_code == 409

    # 하향: 언제든 (권한 처리 선택 필수)
    assert share(ctx, ids["deid"], ADMIN, target_scope="PROGRAM_REVIEW").status_code == 422
    r = share(ctx, ids["deid"], ADMIN, target_scope="PROGRAM_REVIEW", existing_grant_handling="REVOKE_NOW")
    assert r.json()["state"] == "APPLIED"
    r = ctx.client.get(f"/v1/assets/{ids['deid']}/share-requests", headers=ctx.as_user(MEMBER))
    check(r, "GET", "/v1/assets/{asset_id}/share-requests")
    assert [x["target_scope"] for x in r.json()["items"]] == ["SANDBOX_PARTNERS", "PUBLIC", "PROGRAM_REVIEW"]
    actions = ctx.audit_actions()
    assert {"SHARE_SCOPE_REQUESTED", "SHARE_SCOPE_DECIDED", "ASSET_CLASSIFIED", "SHARE_REVIEW_APPROVE"} <= set(actions)

    # 요구사항 3.3: Sharing_Rules를 거치지 않은 상향은 저장되지 않는다
    with pytest.raises(sharing.SharingBypass), ctx.c.sessionmaker.begin() as db:
        db.get(Asset, ids["deid"]).sharing_scope = "PUBLIC"
    with pytest.raises(sharing.SharingBypass), ctx.c.sessionmaker.begin() as db:
        bad = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="DEID", scope="SANDBOX_PARTNERS")
        db.add(bad)

    # 요구사항 2: data_role CORE로 → 파생본까지 L1 이하, 원본 공유 철회
    share(ctx, ids["deid"], ADMIN, target_scope="SANDBOX_PARTNERS")
    assert ctx.client.patch(f"/v1/assets/{ids['orig']}", json={"data_role": "CORE"},
                            headers=ctx.as_user(ADMIN)).status_code == 400  # 사유 필수
    r = ctx.client.patch(f"/v1/assets/{ids['orig']}", json={"data_role": "CORE", "reason": "핵심 영상으로 재분류"},
                         headers=ctx.as_user(ADMIN))
    assert r.status_code == 200, r.text
    with ctx.c.sessionmaker() as db:
        assert db.get(Asset, ids["deid"]).sharing_scope == "PROGRAM_REVIEW"
        assert db.get(Asset, ids["orig"]).share_status == "NONE"


def test_reject_notifies_and_withdraw(ctx) -> None:
    ids = make_assets(ctx)
    ctx.client.post("/v1/share-candidates", json={"asset_ids": [ids["orig"]]}, headers=ctx.as_user(MEMBER))
    assert review(ctx, ids["orig"], OP, action="REJECT").status_code == 422  # 사유 필수
    r = review(ctx, ids["orig"], OP, action="REJECT", reason="법적 근거 불충분")
    assert r.json()["share_status"] == "REJECTED"
    n = next(x for x in ctx.c.notifier.sent if x.kind == "share.review")
    assert n.recipients == ("admin-a@example.invalid",) and "법적 근거 불충분" in n.body
    assert review(ctx, ids["orig"], MEMBER, action="RESUBMIT").status_code == 403  # ORG_ADMIN만
    assert review(ctx, ids["orig"], ADMIN, action="RESUBMIT").json()["share_status"] == "CANDIDATE"
    assert review(ctx, ids["orig"], ADMIN, action="WITHDRAW", existing_grant_handling="REVOKE_NOW").json()[
        "share_status"] == "NONE"
    assert review(ctx, ids["deid"], ADMIN, action="WITHDRAW").status_code == 422  # 원본 단위
    with ctx.c.sessionmaker() as db:
        assert db.execute(select(Asset.share_status).where(Asset.asset_id == ids["orig"])).scalar() == "NONE"
