"""T08 공유 조건 강제·T15 데이터셋 배포 (docs/ACCEPTANCE.md) — 두 제공기관(A·B), 이용기관, 운영자.

승인 자산 → 데이터셋 버전(DATASET 존 사본·매니페스트) → 게시 → 이용 신청(약정 목적·기관별 승인·한 곳 거절은 끝)
→ 운영자 최종 승인 → Grant → 단기 다운로드 URL·받는 쪽 매니페스트 → 이용 이력 → 하향(즉시 철회)·파기 확인 → DCAT.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update

from app.adapters.system_clock import FixedClock
from app.db.models import AccessRequest, Agreement, Asset, AssetLineage, Grant
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, PRJ_B
from app.services import datasets
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]
ADMIN_A, ADMIN_B, OP, CONSUMER = 101, 103, 106, 109
NOW = datetime.now(UTC)
CARD = {
    "title": "연안 환경 영상 (합성)", "rights_holders": ["제공기관 A (시험)", "제공기관 B (시험)"], "review_owner": "운영기관",
    "source_summary": {"organization_count": 2, "session_count": 2, "device_types": ["CAMERA"],
                       "period_start": "2026-11-01", "period_end": "2026-11-03", "sea_areas_generalized": ["남해 연안"]},
    "contents": {"file_count": 2, "total_bytes": 20, "formats": ["video/mp4"]},
    "conditions": {"missing_data_note": "야간 없음", "bias_note": "맑은 날 위주", "limitations": "합성 시험 데이터"},
    "privacy": {"risk_note": "사람 없음 확인", "processing_methods": ["얼굴·번호판 블러"], "review_method": "전수 검수"},
    "lineage": {"model_versions": ["blur@1"]},
    "usage": {"allowed_purposes": ["AI_TRAINING", "JOINT_RESEARCH"], "redistribution_allowed": False,
              "commercial_use_allowed": False, "revocation_terms": "철회 시 파기"},
    "delivery": {"methods": ["DOWNLOAD_URL"]},
}


def approved_pair(ctx, org: str, prj: str, dev: str, purposes: list[str]) -> dict[str, str]:
    """심사를 마친 원본과 비식별본 (14번 검수 결과까지 반영된 상태를 합성)."""
    data = hashlib.sha256(org.encode()).digest()[:10]
    with ctx.c.sessionmaker.begin() as db:
        orig = _asset(org, prj, dev, session=None)
        orig.data_role, orig.share_status, orig.legal_basis_ref = "BYPRODUCT", "APPROVED", "LB-1"
        deid = _asset(org, prj, dev, variant="DEID", scope="PRIVATE", session=None)
        deid.data_role, deid.share_status, deid.review_result = "BYPRODUCT", "APPROVED", "PASSED"
        deid.privacy_status, deid.sensitivity_class, deid.legal_basis_ref = "ANONYMIZED_VERIFIED", "INTERNAL", "LB-1"
        orig.sensitivity_class = "INTERNAL"
        deid.storage_zone, deid.storage_key = "PROCESSED", f"{org}/deid/{deid.asset_id}.mp4"
        deid.byte_size, deid.checksum_sha256 = len(data), hashlib.sha256(data).hexdigest()
        db.add_all([orig, deid])
        db.flush()
        db.add(AssetLineage(child_asset_id=deid.asset_id, parent_asset_id=orig.asset_id, relation="DEID",
                            created_at=NOW))
        db.add(Agreement(agreement_id=new_id("agreement"), organization_id=org, tier="TIER2", reference="합성 2단",
                         valid_from=NOW - timedelta(days=1), valid_until=None, allowed_purposes=purposes,
                         status="ACTIVE", created_by="t", created_at=NOW))
        ids = {"orig": orig.asset_id, "deid": deid.asset_id}
    ctx.c.storage.put_object(ctx.c.settings.bucket_proc, f"{org}/deid/{ids['deid']}.mp4", data, "video/mp4")
    return ids


def share(ctx, user: int, asset_id: str, target: str, **kw):
    return ctx.client.post(f"/v1/assets/{asset_id}/share-requests", headers=ctx.as_user(user),
                           json={"target_scope": target, "reason": "공동활용", **kw})


def to_l3(ctx, user: int, asset_id: str) -> None:
    r = share(ctx, user, asset_id, "APPROVED_PARTNERS")
    assert r.json()["state"] == "IN_REVIEW", r.text
    r = ctx.client.post(f"/v1/share-requests/{r.json()['share_request_id']}/decisions", headers=ctx.as_user(OP),
                        json={"decision": "APPROVE", "reason": "이용자 약정 양식 확인"})
    assert r.json()["state"] == "APPLIED", r.text


def access(ctx, version_id: str, purpose: str):
    return ctx.client.post("/v1/access-requests", headers=ctx.as_user(CONSUMER), json={
        "dataset_version_id": version_id, "purpose": purpose, "purpose_detail": "연안 객체 탐지 모델 학습용",
        "period": {"start_date": NOW.date().isoformat(), "end_date": (NOW + timedelta(days=30)).date().isoformat()},
        "security_measures": "사내 격리 서버 보관, 접근 2인 제한, 종료 후 파기", "agreement_accepted": True})


def decide(ctx, user: int, req_id: str, decision: str):
    return ctx.client.post(f"/v1/access-requests/{req_id}/decisions", headers=ctx.as_user(user),
                           json={"decision": decision, "reason": "검토 결과"})


@pytest.mark.covers("T08-e", "T08-f", "T15-c", "T15-d", "T15-e", "T15-g", "T06-c")
def test_t08_t15_dataset_distribution(ictx) -> None:
    ctx = ictx
    a = approved_pair(ctx, ORG_A, PRJ_A, DEV_A_CAMERA, ["AI_TRAINING", "JOINT_RESEARCH"])
    b = approved_pair(ctx, ORG_B, PRJ_B, DEV_B_CAMERA, ["AI_TRAINING"])
    assert share(ctx, ADMIN_A, a["deid"], "SANDBOX_PARTNERS").json()["state"] == "APPLIED"
    assert share(ctx, ADMIN_B, b["deid"], "SANDBOX_PARTNERS").json()["state"] == "APPLIED"

    # 데이터셋 버전: 원본·조건 미충족 자산은 넣을 수 없고, 데이터 카드 필수 항목이 비면 거부
    r = ctx.client.post("/v1/datasets", json={"title": "연안 환경 (합성)"}, headers=ctx.as_user(OP))
    check(r, "POST", "/v1/datasets")
    ds = r.json()["dataset_id"]
    assert ctx.client.post("/v1/datasets", json={"title": "x"}, headers=ctx.as_user(ADMIN_A)).status_code == 403
    path = f"/v1/datasets/{ds}/versions"
    r = ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "1.0.0", "item_asset_ids": [a["orig"]],
                                                             "data_card": CARD})
    assert r.status_code == 422 and "바이너리가 있는 공유용 파생본" in r.text
    bad_card = {k: v for k, v in CARD.items() if k != "privacy"}
    r = ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "1.0.0", "item_asset_ids": [a["deid"]],
                                                             "data_card": bad_card})
    assert r.status_code == 422
    r = ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "1.0.0",
                                                             "item_asset_ids": [a["deid"], b["deid"]], "data_card": CARD})
    check(r, "POST", "/v1/datasets/{dataset_id}/versions")
    v1 = r.json()
    assert v1["scope"] == "SANDBOX_PARTNERS" and v1["manifest"]["item_count"] == 2
    item = next(i for i in v1["manifest"]["items"] if i["asset_id"] == a["deid"])
    assert item["source_asset_id"] == a["orig"] and item["path"].startswith("deid/") and "cam01" not in item["path"]
    copied = ctx.c.storage.get_object(ctx.c.settings.bucket_datasets, f"{ds}/{v1['dataset_version_id']}/{item['path']}")
    assert hashlib.sha256(copied).hexdigest() == item["checksum_sha256"]
    assert ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "1.0.0", "item_asset_ids": [a["deid"]],
                                                                "data_card": CARD}).status_code == 409  # 버전 불변

    # 카탈로그: 게시 전·L3 미만은 이용기관에 보이지 않는다 (요구사항 7)
    assert ctx.client.get("/v1/datasets", headers=ctx.as_user(CONSUMER)).json()["items"] == []
    to_l3(ctx, ADMIN_A, a["deid"])
    to_l3(ctx, ADMIN_B, b["deid"])
    r = ctx.client.post(f"/v1/datasets/{ds}/publish", headers=ctx.as_user(OP))
    check(r, "POST", "/v1/datasets/{dataset_id}/publish")
    r = ctx.client.get("/v1/datasets", headers=ctx.as_user(CONSUMER))
    check(r, "GET", "/v1/datasets")
    assert [x["dataset_id"] for x in r.json()["items"]] == [ds]
    assert r.json()["items"][0]["versions"][0]["scope"] == "APPROVED_PARTNERS"
    assert a["deid"] not in r.text and b["orig"] not in r.text  # 카탈로그는 데이터 카드·통계만 (자산 비노출)
    vid = v1["dataset_version_id"]

    # 이용 신청: 데이터 카드·모든 제공기관 약정의 허용 목적 (T08-f, 기관 이름 비공개)
    assert access(ctx, vid, "COMMERCIAL_SERVICE").status_code == 422
    r = access(ctx, vid, "JOINT_RESEARCH")
    assert r.status_code == 422 and "허용하지 않는 기관 1곳" in r.text and "제공기관 B" not in r.text
    r = access(ctx, vid, "AI_TRAINING")
    check(r, "POST", "/v1/access-requests")
    req = r.json()
    assert (req["status"], req["pending_provider_approvals"]) == ("REQUESTED", 2)
    # 운영자는 제공기관 승인 전에 승인할 수 없고, 한 곳이라도 거절하면 끝 (T08-e)
    assert decide(ctx, OP, req["access_request_id"], "APPROVE").status_code == 409
    r = decide(ctx, ADMIN_A, req["access_request_id"], "APPROVE")
    check(r, "POST", "/v1/access-requests/{access_request_id}/decisions")
    assert (r.json()["status"], r.json()["pending_provider_approvals"]) == ("REQUESTED", 1)
    assert decide(ctx, ADMIN_B, req["access_request_id"], "DENY").json()["status"] == "DENIED"
    assert decide(ctx, OP, req["access_request_id"], "APPROVE").status_code == 409
    assert decide(ctx, 102, req["access_request_id"], "APPROVE").status_code == 403  # ORG_MEMBER

    # 다시 신청 → 두 기관 승인 → 운영자 최종 승인 → Grant
    rid = access(ctx, vid, "AI_TRAINING").json()["access_request_id"]
    decide(ctx, ADMIN_A, rid, "APPROVE")
    assert decide(ctx, ADMIN_B, rid, "APPROVE").json()["status"] == "PROVIDER_APPROVED"
    r = decide(ctx, OP, rid, "APPROVE")
    assert r.json()["status"] == "GRANTED"
    grant = r.json()["grant_id"]
    r = ctx.client.get("/v1/access-requests", headers=ctx.as_user(CONSUMER))
    check(r, "GET", "/v1/access-requests")

    # 배포: 파일별 단기 URL + 받는 쪽 매니페스트, 감사·이용 이력 (요구사항 9)
    r = ctx.client.post("/v1/distributions", json={"grant_id": grant}, headers=ctx.as_user(CONSUMER))
    check(r, "POST", "/v1/distributions")
    files = {f["path"]: f for f in r.json()["files"]}
    assert "RECIPIENT_MANIFEST.json" in files and r.json()["recipient"]["grant_id"] == grant
    got = httpx.get(files[item["path"]]["url"], timeout=30).content
    assert hashlib.sha256(got).hexdigest() == item["checksum_sha256"]
    assert ctx.client.post("/v1/distributions", json={"grant_id": grant},
                           headers=ctx.as_user(ADMIN_A)).status_code == 403
    r = ctx.client.get("/v1/usage-history", headers=ctx.as_user(ADMIN_A))
    check(r, "GET", "/v1/usage-history")
    kinds = [x["kind"] for x in r.json()["items"]]
    assert {"REQUESTED", "DECIDED", "GRANTED", "DOWNLOAD_ISSUED"} <= set(kinds)
    assert all(x["consumer_organization"] == "이용기관 (시험)" for x in r.json()["items"])
    assert "DISTRIBUTION_ISSUED" in ctx.audit_actions()

    # T15-e: v1 Grant로 v2에 접근할 수 없다 — 이용 권한은 버전 하나에 묶이고 배포는 그 버전 파일만 준다
    r = ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "2.0.0", "data_card": CARD,
                                                             "item_asset_ids": [a["deid"], b["deid"]]})
    v2 = r.json()["dataset_version_id"]
    assert r.status_code == 201 and v2 != vid
    r = ctx.client.post("/v1/distributions", json={"grant_id": grant}, headers=ctx.as_user(CONSUMER))
    assert r.json()["dataset_version_id"] == vid and r.json()["recipient"]["manifest_sha256"] == v1["manifest_sha256"]
    assert all(v2 not in f["url"] for f in r.json()["files"])
    r = ctx.client.post("/v1/distributions", json={"grant_id": grant, "dataset_version_id": v2},
                        headers=ctx.as_user(CONSUMER))
    assert r.status_code in (400, 422)  # 버전을 바꿔 달라는 필드 자체가 없다
    assert {g["dataset_version_id"] for g in ctx.client.get("/v1/grants", headers=ctx.as_user(CONSUMER))
            .json()["items"]} == {vid}

    # T15-c: 유효한 이용 권한이 있는 데이터셋의 포함 자산은 삭제 거부 + 철회 안내
    r = ctx.client.post(f"/v1/assets/{a['deid']}/deletion", json={"reason": "시험", "confirm_affected": False},
                        headers=ctx.as_user(ADMIN_A, mfa=True))
    assert r.status_code == 409
    ds_hit = next(x for x in r.json()["affected"] if x["cited_by"] == "DATASET")
    assert ds_hit["grant_id"] == grant and "철회" in ds_hit["action"]

    # 하향 + 즉시 철회 (요구사항 10.3): 새 URL 발급 중단, 회수 불가 안내, 파기 확인
    r = share(ctx, ADMIN_A, a["deid"], "PROGRAM_REVIEW", existing_grant_handling="REVOKE_NOW")
    assert r.json()["state"] == "APPLIED"
    r = ctx.client.get("/v1/grants", headers=ctx.as_user(CONSUMER))
    check(r, "GET", "/v1/grants")
    g = r.json()["items"][0]
    assert g["status"] == "REVOKED" and "회수되지 않습니다" in g["notice"]
    note = next(n for n in ctx.c.notifier.sent if n.kind == "access.decided" and "철회" in n.subject)
    assert "회수되지 않습니다" in note.body
    assert ctx.client.post("/v1/distributions", json={"grant_id": grant},
                           headers=ctx.as_user(CONSUMER)).status_code == 410
    r = ctx.client.post(f"/v1/grants/{grant}/destruction-confirmation", headers=ctx.as_user(CONSUMER))
    check(r, "POST", "/v1/grants/{grant_id}/destruction-confirmation")
    assert ctx.client.post(f"/v1/grants/{grant}/destruction-confirmation",
                           headers=ctx.as_user(CONSUMER)).status_code == 409
    # 하향된 자산은 새 버전에 못 들어가고, 기존 버전 등급도 내려가 카탈로그에서 빠진다
    assert ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "1.1.0", "item_asset_ids": [a["deid"]],
                                                                "data_card": CARD}).status_code == 422
    assert ctx.client.get("/v1/datasets", headers=ctx.as_user(CONSUMER)).json()["items"] == []

    # DCAT: 모든 항목이 L4인 게시 데이터셋만 (요구사항 12)
    assert ctx.client.get("/v1/catalog/dcat").json()["dcat:dataset"] == []
    r = share(ctx, ADMIN_B, b["deid"], "PUBLIC")
    ctx.client.post(f"/v1/share-requests/{r.json()['share_request_id']}/decisions", headers=ctx.as_user(OP),
                    json={"decision": "APPROVE", "reason": "공개 적합"})
    ds2 = ctx.client.post("/v1/datasets", json={"title": "B 공개 (합성)"}, headers=ctx.as_user(OP)).json()["dataset_id"]
    r = ctx.client.post(f"/v1/datasets/{ds2}/versions", headers=ctx.as_user(OP),
                        json={"version": "1.0.0", "item_asset_ids": [b["deid"]], "data_card": CARD})
    assert r.json()["scope"] == "PUBLIC"
    ctx.client.post(f"/v1/datasets/{ds2}/publish", headers=ctx.as_user(OP))
    r = ctx.client.get("/v1/catalog/dcat")
    check(r, "GET", "/v1/catalog/dcat")
    assert r.headers["content-type"].startswith("application/ld+json")
    assert [x["dct:identifier"] for x in r.json()["dcat:dataset"]] == [ds2]


@pytest.mark.covers("T15-d")
def test_t15_deadline_and_expiry(ictx) -> None:
    """응답 기한 초과 → 거부, 권한 기간 만료 → EXPIRED (요구사항 8.3, 10.1)."""
    ctx = ictx
    b = approved_pair(ctx, ORG_B, PRJ_B, DEV_B_CAMERA, ["AI_TRAINING"])
    share(ctx, ADMIN_B, b["deid"], "SANDBOX_PARTNERS")
    ds = ctx.client.post("/v1/datasets", json={"title": "B (합성)"}, headers=ctx.as_user(OP)).json()["dataset_id"]
    vid = ctx.client.post(f"/v1/datasets/{ds}/versions", headers=ctx.as_user(OP), json={
        "version": "1.0.0", "item_asset_ids": [b["deid"]], "data_card": CARD}).json()["dataset_version_id"]
    to_l3(ctx, ADMIN_B, b["deid"])
    ctx.client.post(f"/v1/datasets/{ds}/publish", headers=ctx.as_user(OP))
    stale = access(ctx, vid, "AI_TRAINING").json()["access_request_id"]
    ok = access(ctx, vid, "AI_TRAINING").json()["access_request_id"]
    decide(ctx, ADMIN_B, ok, "APPROVE")
    grant = decide(ctx, OP, ok, "APPROVE").json()["grant_id"]
    later = FixedClock(datetime.now(UTC) + timedelta(days=15))
    import dataclasses

    c2 = dataclasses.replace(ctx.c, clock=later)
    with c2.sessionmaker.begin() as db:
        assert datasets.expire(c2, db) == {"denied": 1, "expired": 0}
        assert db.get(AccessRequest, stale).status == "DENIED"
        db.execute(update(Grant).where(Grant.grant_id == grant).values(expires_at=later.now() - timedelta(days=1)))
    with c2.sessionmaker.begin() as db:
        assert datasets.expire(c2, db)["expired"] == 1
        assert db.get(Grant, grant).status == "EXPIRED"
    assert ctx.client.post("/v1/distributions", json={"grant_id": grant},
                           headers=ctx.as_user(CONSUMER)).status_code == 410
    with ctx.c.sessionmaker() as db:
        assert db.get(Asset, b["deid"]).sharing_scope == "APPROVED_PARTNERS"
