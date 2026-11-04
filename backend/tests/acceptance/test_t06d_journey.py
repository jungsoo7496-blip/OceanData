"""T06-d 가상 3사 전체 여정 (17번 요구사항 2): 선박 영상(A)·해수욕장 CCTV(B)·해양 센서(C) + 이용기관.

수집 → 회차 기록·보고서 → 공유 후보·심사·비식별·검수 → 데이터셋 → 이용 신청·배포가 끊기지 않고, 각 단계가 계보로
이어지는지 본다. 모든 데이터는 합성(ffmpeg 시험 영상·합성 관측값)이다.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Agreement, Asset, AssetLineage, Device, Organization, Project, TrialSession, User
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_B, SES_A
from app.services import deid, pipeline, sharing
from app.services.ingest import IntegrityVerifier
from tests.acceptance.test_t08_t15_sharing import CARD, access, decide, share, to_l3
from tests.conftest import user_token
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset
from tests.integration.test_ingest import manifest, upload_all
from tests.integration.test_pipeline import synth_video

pytestmark = [pytest.mark.integration, pytest.mark.acceptance,
              pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg가 필요합니다")]
ADMIN_A, MEMBER_A, ADMIN_B, OP, REVIEWER, CONSUMER = 101, 102, 103, 106, 108, 109
NOW = datetime.now(UTC).replace(microsecond=0)


def org_c(ctx) -> dict[str, str]:
    """세 번째 제공기관(해양 센서) — 합성."""
    ids = {"org": new_id("organization"), "prj": new_id("project"), "dev": new_id("device"),
           "ses": new_id("session"), "user": new_id("user"), "sub": "journey-sensor-admin"}
    with ctx.c.sessionmaker.begin() as db:
        db.add(Organization(organization_id=ids["org"], name="제공기관 C (합성 센서)", org_type="PROVIDER",
                            status="ACTIVE", created_at=NOW))
        db.flush()
        db.add(User(user_id=ids["user"], organization_id=ids["org"], idp_subject=ids["sub"], username="admin-c",
                    roles=["ORG_ADMIN"], status="ACTIVE", contact_email="admin-c@example.invalid", created_at=NOW,
                    updated_at=NOW))
        db.add(Project(project_id=ids["prj"], organization_id=ids["org"], name="C 해양 센서 실증", status="ACTIVE",
                       created_at=NOW))
        db.flush()
        db.add(Device(device_id=ids["dev"], organization_id=ids["org"], project_id=ids["prj"], name="C 수질 부이",
                      kind="SOURCE", device_type="SENSOR", status="ACTIVE", collection_mode="C3_APPROVED_FULL",
                      local_copy_policy="RETAINED", created_at=NOW))
        db.add(TrialSession(session_id=ids["ses"], organization_id=ids["org"], project_id=ids["prj"],
                            label="C-2026-11-04", title="C 연안 관측", starts_at=NOW - timedelta(hours=3),
                            ends_at=NOW, created_at=NOW))
    return ids


@pytest.mark.covers("T06-d", "T06-b")
def test_t06d_three_providers_full_journey(ictx, tmp_path: Path) -> None:
    ctx = ictx
    ha, hm = ctx.as_user(ADMIN_A), ctx.as_user(MEMBER_A)

    # 1) 수집: A는 업로드 API(실제 S3 멀티파트·무결성 검증), C는 관측값 API
    video = synth_video(tmp_path, seconds=3)
    with ctx.c.sessionmaker.begin() as db:  # 선박 영상 원천 장비를 C3로 (원본 반출 허용 장비)
        db.get(Device, DEV_A_CAMERA).collection_mode = "C3_APPROVED_FULL"
    up = upload_all(ctx, manifest(video, variant="ORIGINAL", data_type="VIDEO", mime="video/mp4",
                                  file={"original_filename": "vessel_0001.mp4", "byte_size": len(video),
                                        "mime_type": "video/mp4",
                                        "checksum_sha256": hashlib.sha256(video).hexdigest()}), video, hm)
    while (job := ctx.c.queue.claim(["integrity_verify"], "t")) is not None:
        IntegrityVerifier(ctx.c).verify(job.payload["asset_id"])
        ctx.c.queue.complete(job.job_id)
    a_orig = up["asset_id"]
    assert pipeline.media_probe(ctx.c, a_orig) == "OK"
    c = org_c(ctx)
    hc = {"Authorization": f"Bearer {user_token(c['sub'])}"}
    obs = [{"metric": "sea_water_temperature", "value": 17 + i / 10, "unit": "degC", "quality_flag": "GOOD",
            "observed_at": (NOW - timedelta(hours=2, minutes=-i * 10)).strftime("%Y-%m-%dT%H:%M:%SZ")}
           for i in range(6)]
    r = ctx.client.post("/v1/observations", headers=hc, json={"schema_version": "1.0.0", "source_system_id": c["dev"],
                                                              "session_id": c["ses"], "observations": obs})
    assert r.json()["accepted"] == 6

    # 2) 증빙·보고: A 회차 기록(원본 인용)·승인·보고서, C 보고서(관측 통계)
    rec = ctx.client.post(f"/v1/sessions/{SES_A}/records", headers=hm, json={
        "kind": "TEST", "title": "항해 시험", "evidence_refs": [{"kind": "ASSET", "asset_id": a_orig}]}).json()
    ctx.client.post(f"/v1/records/{rec['record_id']}/approve", headers=ha)
    rep_a = ctx.client.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": SES_A},
                            headers=hm).json()
    assert rep_a["record_ids"] == [rec["record_id"]]
    rep_c = ctx.client.post("/v1/reports", json={"template_code": "EXAMPLE_SESSION_LOG", "session_id": c["ses"]},
                            headers=hc).json()
    temp = next(m for m in rep_c["metrics"] if m["metric_code"] == "EXAMPLE_MEAN_WATER_TEMP")
    assert temp["status"] == "OK" and temp["sources"]["observation_ranges"][0]["source_system_id"] == c["dev"]

    # 3) 공유: A 원본 후보 → 심사 → 비식별(모의 탐지) → 검수 통과 → 최종 승인 → L2
    with ctx.c.sessionmaker.begin() as db:
        a = db.get(Asset, a_orig)
        a.data_role, a.sensitivity_class = "BYPRODUCT", "INTERNAL"
        a.ext = {**(a.ext or {}), "synthetic_regions": [{"cls": "FACE", "t0": 0.5, "t1": 1.5, "x": 30, "y": 30,
                                                         "w": 40, "h": 40}]}
        for org, purposes in ((ORG_A, ["AI_TRAINING"]), (ORG_B, ["AI_TRAINING"])):
            db.add(Agreement(agreement_id=new_id("agreement"), organization_id=org, tier="TIER2", reference="합성",
                             valid_from=NOW - timedelta(days=1), valid_until=None, allowed_purposes=purposes,
                             status="ACTIVE", created_by="t", created_at=NOW))
    assert ctx.client.post("/v1/share-candidates", json={"asset_ids": [a_orig]}, headers=hm).json()["candidates"]
    for action, extra in (("START_LEGAL_REVIEW", {}), ("PASS_LEGAL_REVIEW", {"legal_basis_ref": "LB-J-1"})):
        assert ctx.client.post(f"/v1/assets/{a_orig}/share-review", json={"action": action, **extra},
                               headers=ctx.as_user(OP)).status_code == 200
    job = ctx.c.queue.claim(["deidentify"], "t", ("CPU",))
    assert deid.deidentify(ctx.c, a_orig, job.job_id, job.payload) == "OK"
    rv = next(x for x in ctx.client.get("/v1/deid-reviews", headers=ctx.as_user(REVIEWER)).json()["items"]
              if x["original_asset_id"] == a_orig)
    ctx.client.post(f"/v1/deid-reviews/{rv['review_id']}/decision", json={"result": "PASS"},
                    headers=ctx.as_user(REVIEWER))
    assert ctx.client.post(f"/v1/assets/{a_orig}/share-review", json={"action": "APPROVE"},
                           headers=ctx.as_user(OP)).json()["share_status"] == "APPROVED"
    a_deid = rv["deid_asset_id"]
    assert share(ctx, ADMIN_A, a_deid, "SANDBOX_PARTNERS").json()["state"] == "APPLIED"

    # B CCTV: 심사를 마친 비식별본 (합성)
    data_b = hashlib.sha256(b"cctv").digest() * 4
    with ctx.c.sessionmaker.begin() as db:
        b_orig = _asset(ORG_B, PRJ_B, DEV_B_CAMERA, session=None)
        b_orig.data_role, b_orig.share_status, b_orig.sensitivity_class = "BYPRODUCT", "APPROVED", "INTERNAL"
        b_deid = _asset(ORG_B, PRJ_B, DEV_B_CAMERA, variant="DEID", scope="PRIVATE", session=None)
        b_deid.data_role, b_deid.share_status, b_deid.review_result = "BYPRODUCT", "APPROVED", "PASSED"
        b_deid.privacy_status, b_deid.sensitivity_class, b_deid.legal_basis_ref = "PSEUDONYMIZED", "INTERNAL", "LB-B"
        b_deid.storage_zone, b_deid.storage_key = "PROCESSED", f"{ORG_B}/deid/journey.mp4"
        b_deid.byte_size, b_deid.checksum_sha256 = len(data_b), hashlib.sha256(data_b).hexdigest()
        db.add_all([b_orig, b_deid])
        db.flush()
        db.add(AssetLineage(child_asset_id=b_deid.asset_id, parent_asset_id=b_orig.asset_id, relation="DEID",
                            created_at=NOW))
        ids_b = (b_orig.asset_id, b_deid.asset_id)
    ctx.c.storage.put_object(ctx.c.settings.bucket_proc, f"{ORG_B}/deid/journey.mp4", data_b, "video/mp4")
    assert share(ctx, ADMIN_B, ids_b[1], "SANDBOX_PARTNERS").json()["state"] == "APPLIED"

    # 4) 데이터셋 → L3 → 게시 → 이용 신청 → 두 제공기관·운영자 승인 → 배포
    ds = ctx.client.post("/v1/datasets", json={"title": "연안 여정 (합성)"}, headers=ctx.as_user(OP)).json()["dataset_id"]
    v = ctx.client.post(f"/v1/datasets/{ds}/versions", headers=ctx.as_user(OP),
                        json={"version": "1.0.0", "item_asset_ids": [a_deid, ids_b[1]], "data_card": CARD}).json()
    sources = {i["asset_id"]: i["source_asset_id"] for i in v["manifest"]["items"]}
    assert sources == {a_deid: a_orig, ids_b[1]: ids_b[0]}  # 데이터셋 → 원본 계보
    to_l3(ctx, ADMIN_A, a_deid)
    to_l3(ctx, ADMIN_B, ids_b[1])
    ctx.client.post(f"/v1/datasets/{ds}/publish", headers=ctx.as_user(OP))
    req = access(ctx, v["dataset_version_id"], "AI_TRAINING").json()["access_request_id"]
    decide(ctx, ADMIN_A, req, "APPROVE")
    decide(ctx, ADMIN_B, req, "APPROVE")
    grant = decide(ctx, OP, req, "APPROVE").json()["grant_id"]
    dist = ctx.client.post("/v1/distributions", json={"grant_id": grant}, headers=ctx.as_user(CONSUMER)).json()
    f = next(x for x in dist["files"] if x["path"].startswith("deid/") and a_deid in x["path"])
    got = httpx.get(f["url"], timeout=30).content
    assert hashlib.sha256(got).hexdigest() == f["checksum_sha256"]

    # 5) 계보: 배포 파일 → 데이터셋 항목(비식별본) → 원본(업로드한 영상), 보고서 기록 → 같은 원본
    with ctx.c.sessionmaker() as db:
        parent = db.execute(select(AssetLineage.parent_asset_id).where(
            AssetLineage.child_asset_id == a_deid)).scalar_one()
        assert parent == a_orig and db.get(Asset, a_orig).checksum_sha256 == hashlib.sha256(video).hexdigest()
    assert sharing.ancestors  # 공유 판단도 같은 계보를 쓴다

    # T06-b: 데이터셋 → 원본·모델 버전·검수자를 한 번에 (운영자 전체, 제공기관은 자기 항목만, 이용기관 불가)
    lpath = f"/v1/datasets/{ds}/versions/{v['dataset_version_id']}/lineage"
    r = ctx.client.get(lpath, headers=ctx.as_user(OP))
    check(r, "GET", "/v1/datasets/{dataset_id}/versions/{dataset_version_id}/lineage")
    lin = {i["asset_id"]: i for i in r.json()["items"]}
    assert r.json()["hidden_item_count"] == 0 and r.json()["manifest_sha256"] == v["manifest_sha256"]
    item = lin[a_deid]
    assert item["source_asset_id"] == a_orig and item["chain"][-1]["asset_id"] == a_orig
    assert item["processing"]["model"]["name"] and item["processing"]["model"]["version"]
    assert "sha256" in item["processing"]["model"] and item["processing"]["processed_at"]  # 모의 탐지기는 가중치 파일 없음 → null
    assert [(x["reviewer_id"], x["status"]) for x in item["reviews"]] == [(rv["reviewer_id"], "PASSED")]
    assert lin[ids_b[1]]["source_asset_id"] == ids_b[0]
    r = ctx.client.get(lpath, headers=ha)
    assert [i["asset_id"] for i in r.json()["items"]] == [a_deid] and r.json()["hidden_item_count"] == 1
    assert ctx.client.get(lpath, headers=ctx.as_user(CONSUMER)).status_code == 403
    hist = ctx.client.get("/v1/usage-history", headers=ha).json()["items"]
    assert {"REQUESTED", "GRANTED", "DOWNLOAD_ISSUED"} <= {h["kind"] for h in hist}
