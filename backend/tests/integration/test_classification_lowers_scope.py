"""분류를 더 엄격하게 바꾸면 공유 등급을 조건에 맞게 내린다 (계약 점검 S11, ADR-0074).

L2로 공유 중인 비식별본이나 그 원본을 운영자가 SECURITY로 지정하면, 파생본은 원 자산의 가장 엄격한 민감도를
물려받으므로(domain.md 3장) L2 이상 조건(sensitivity_class ≠ SECURITY)을 잃는다. 등급을 L1로 내리고, 그 자산이 든
데이터셋 버전의 이용 권한을 즉시 철회해야 한다. 바뀌기 전부터 충족하지 못하던 조건(2단 약정 만료)만으로는 내리지 않는다.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.db.models import (
    AccessRequest,
    Agreement,
    Asset,
    AuditLog,
    Dataset,
    DatasetVersion,
    DatasetVersionItem,
    Grant,
)
from app.domain.ids import new_id
from app.seed import ORG_A, ORG_CONSUMER
from app.services import sharing
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_sharing import make_assets, review, share

pytestmark = pytest.mark.integration
ADMIN, MEMBER, OP = 101, 102, 106
NOW = datetime.now(UTC)


def _shared_deid(ctx: Ctx) -> dict[str, str]:
    """정상 흐름(후보 → 법률 검토 → 승인 → 2단 약정 → L2 신청)으로 비식별본을 SANDBOX_PARTNERS에 올린다."""
    ids = make_assets(ctx)
    with ctx.c.sessionmaker.begin() as db:  # 도우미 자산의 계약 밖 값(위치 정밀도·체크섬)을 맞춘다
        for aid in ids.values():
            db.execute(update(Asset).where(Asset.asset_id == aid).values(
                location_precision="GENERALIZED", checksum_sha256=hashlib.sha256(aid.encode()).hexdigest()))
    ctx.client.post("/v1/share-candidates", json={"asset_ids": [ids["orig"]]}, headers=ctx.as_user(MEMBER))
    assert review(ctx, ids["orig"], OP, action="START_LEGAL_REVIEW").status_code == 200
    assert review(ctx, ids["orig"], OP, action="PASS_LEGAL_REVIEW", legal_basis_ref="LB-1").status_code == 200
    with ctx.c.sessionmaker.begin() as db:
        sharing.mark_processed(ctx.c, db, db.get(Asset, ids["orig"]))
        db.execute(update(Asset).where(Asset.asset_id == ids["deid"]).values(review_result="PASSED"))
    assert review(ctx, ids["orig"], OP, action="APPROVE").json()["share_status"] == "APPROVED"
    with ctx.c.sessionmaker.begin() as db:
        db.add(Agreement(agreement_id=new_id("agreement"), organization_id=ORG_A, tier="TIER2", reference="합성-2단",
                         valid_from=NOW - timedelta(days=1), valid_until=None, allowed_purposes=["AI_TRAINING"],
                         status="ACTIVE", created_by="t", created_at=NOW))
    r = share(ctx, ids["deid"], ADMIN, target_scope="SANDBOX_PARTNERS")
    assert r.json()["state"] == "APPLIED", r.text
    return ids


def _grant_on(ctx: Ctx, asset_id: str) -> str:
    """그 자산이 든 데이터셋 버전과 이용기관의 GRANTED 권한 (합성 행)."""
    did, vid, rid, gid = new_id("dataset"), new_id("dataset_version"), new_id("access_request"), new_id("grant")
    with ctx.c.sessionmaker.begin() as db:
        db.add(Dataset(dataset_id=did, title="합성 데이터셋", created_by="t", created_at=NOW))
        db.flush()
        db.add(DatasetVersion(dataset_version_id=vid, dataset_id=did, version="1.0.0", provider_organization_ids=[ORG_A],
                              item_count=1, total_bytes=10, data_card={}, manifest={}, manifest_key=f"{did}/m.json",
                              manifest_sha256="0" * 64, created_by="t", created_at=NOW))
        db.flush()
        db.add(DatasetVersionItem(dataset_version_id=vid, asset_id=asset_id, organization_id=ORG_A, path="deid/a.mp4",
                                  object_key=f"{did}/{vid}/deid/a.mp4", byte_size=10, checksum_sha256="0" * 64))
        db.add(AccessRequest(access_request_id=rid, dataset_version_id=vid, organization_id=ORG_CONSUMER,
                             requested_by="t", purpose="AI_TRAINING", purpose_detail="합성 시험", period_start=NOW,
                             period_end=NOW + timedelta(days=30), security_measures="합성", status="GRANTED",
                             deadline_at=NOW + timedelta(days=7), created_at=NOW, updated_at=NOW))
        db.flush()
        db.add(Grant(grant_id=gid, access_request_id=rid, dataset_version_id=vid, organization_id=ORG_CONSUMER,
                     status="GRANTED", starts_at=NOW - timedelta(hours=1), expires_at=NOW + timedelta(days=30),
                     created_at=NOW))
    return gid


def _classify(ctx: Ctx, asset_id: str, **body: str):
    r = ctx.client.patch(f"/v1/assets/{asset_id}/classification", headers=ctx.as_user(OP),
                         json={"basis_ref": "점검-2026-11-12-01", "reason": "민감 해역 확인", **body})
    assert r.status_code == 200, r.text
    check(r, "PATCH", "/v1/assets/{asset_id}/classification")
    return r


def _scope(ctx: Ctx, asset_id: str) -> str:
    with ctx.c.sessionmaker() as db:
        return db.execute(select(Asset.sharing_scope).where(Asset.asset_id == asset_id)).scalar_one()


def _grant_status(ctx: Ctx, grant_id: str) -> str:
    with ctx.c.sessionmaker() as db:
        return db.execute(select(Grant.status).where(Grant.grant_id == grant_id)).scalar_one()


def _classified_details(ctx: Ctx, response) -> dict:
    with ctx.c.sessionmaker() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.request_id == response.headers["x-request-id"],
                                                 AuditLog.action == "ASSET_CLASSIFIED")).all()
    assert len(rows) == 1
    return rows[0].details


def test_security_on_shared_deid_lowers_it_and_revokes_grants(ctx: Ctx) -> None:
    ids = _shared_deid(ctx)
    gid = _grant_on(ctx, ids["deid"])

    # RESTRICTED는 L2 조건 안이다 — 등급·권한을 건드리지 않는다
    r = _classify(ctx, ids["deid"], sensitivity_class="RESTRICTED")
    assert _scope(ctx, ids["deid"]) == "SANDBOX_PARTNERS"
    assert _grant_status(ctx, gid) == "GRANTED"
    assert _classified_details(ctx, r).get("lowered", []) == []

    r = _classify(ctx, ids["deid"], sensitivity_class="SECURITY")
    g = ctx.client.get(f"/v1/assets/{ids['deid']}", headers=ctx.as_user(ADMIN))
    assert (g.json()["sharing_scope"], g.json()["sensitivity_class"]) == ("PROGRAM_REVIEW", "SECURITY")
    check(g, "GET", "/v1/assets/{asset_id}")  # asset-metadata 'L2 이상 등급의 불변식'
    assert _grant_status(ctx, gid) == "REVOKED"
    details = _classified_details(ctx, r)
    assert details["lowered"] == [{"asset_id": ids["deid"], "from": "SANDBOX_PARTNERS", "to": "PROGRAM_REVIEW"}]
    assert details["existing_grant_handling"] == "REVOKE_NOW"
    # 다시 올리려 해도 규칙이 막는다
    r = share(ctx, ids["deid"], ADMIN, target_scope="SANDBOX_PARTNERS")
    assert r.status_code == 422 and "sensitivity_class ≠ SECURITY" in [u["condition"] for u in
                                                                        r.json()["unmet_conditions"]]


def test_security_on_original_lowers_shared_derivative(ctx: Ctx) -> None:
    ids = _shared_deid(ctx)
    r = _classify(ctx, ids["orig"], sensitivity_class="SECURITY")
    assert _scope(ctx, ids["deid"]) == "PROGRAM_REVIEW"  # 파생본은 원본의 SECURITY를 물려받는다
    assert _scope(ctx, ids["orig"]) == "PRIVATE"
    assert _classified_details(ctx, r)["lowered"] == [
        {"asset_id": ids["deid"], "from": "SANDBOX_PARTNERS", "to": "PROGRAM_REVIEW"}]
    check(ctx.client.get(f"/v1/assets/{ids['deid']}", headers=ctx.as_user(ADMIN)), "GET", "/v1/assets/{asset_id}")


def test_condition_unmet_before_the_change_does_not_lower(ctx: Ctx) -> None:
    ids = _shared_deid(ctx)
    gid = _grant_on(ctx, ids["deid"])
    with ctx.c.sessionmaker.begin() as db:  # 2단 약정 만료: 이용 권한을 줄 때 보는 조건이고, 분류와는 상관없다
        db.execute(update(Agreement).where(Agreement.organization_id == ORG_A)
                   .values(valid_until=NOW - timedelta(minutes=1)))
    r = _classify(ctx, ids["deid"], privacy_status="NOT_APPLICABLE")
    assert _scope(ctx, ids["deid"]) == "SANDBOX_PARTNERS"
    assert _grant_status(ctx, gid) == "GRANTED"
    assert _classified_details(ctx, r).get("lowered", []) == []

    r = _classify(ctx, ids["deid"], sensitivity_class="SECURITY")  # 이번 변경이 새로 깨는 조건은 본다
    assert _scope(ctx, ids["deid"]) == "PROGRAM_REVIEW"
    assert _grant_status(ctx, gid) == "REVOKED"
