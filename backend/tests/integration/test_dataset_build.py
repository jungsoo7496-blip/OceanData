"""13번 작업 32: 큰 데이터셋 버전은 조건을 먼저 검사하고 작업 큐에서 사본·매니페스트를 만든다 (BUILDING → READY | FAILED)."""

from __future__ import annotations

import hashlib

import pytest

from app.seed import DEV_A_CAMERA, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, PRJ_B
from app.services import datasets
from tests.acceptance.test_t08_t15_sharing import CARD, access, approved_pair, share, to_l3
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
ADMIN_A, ADMIN_B, OP, CONSUMER = 101, 103, 106, 109


def _run_build_jobs(ctx) -> list[str]:  # type: ignore[no-untyped-def]
    out = []
    while (job := ctx.c.queue.claim(["dataset_build"], "t", ("CPU",))) is not None:
        out.append(datasets.finish_build(ctx.c, job.payload["dataset_version_id"]))
        ctx.c.queue.complete(job.job_id)
    return out


def test_large_dataset_version_builds_in_queue(ictx) -> None:
    ctx = ictx
    a = approved_pair(ctx, ORG_A, PRJ_A, DEV_A_CAMERA, ["AI_TRAINING"])
    b = approved_pair(ctx, ORG_B, PRJ_B, DEV_B_CAMERA, ["AI_TRAINING"])
    share(ctx, ADMIN_A, a["deid"], "SANDBOX_PARTNERS")
    share(ctx, ADMIN_B, b["deid"], "SANDBOX_PARTNERS")
    ds = ctx.client.post("/v1/datasets", json={"title": "큰 버전 (합성)"}, headers=ctx.as_user(OP)).json()["dataset_id"]
    path = f"/v1/datasets/{ds}/versions"
    ctx.c.settings.__dict__["dataset_sync_max_items"] = 1  # 항목 2개부터 작업 큐로
    try:
        # 조건은 요청 때 모두 검사한다: 원본이 섞이면 바로 422 (작업을 만들지 않는다)
        r = ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "1.0.0", "data_card": CARD,
                                                                 "item_asset_ids": [a["deid"], a["orig"]]})
        assert r.status_code == 422 and ctx.c.queue.claim(["dataset_build"], "t", ("CPU",)) is None

        r = ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "1.0.0", "data_card": CARD,
                                                                 "item_asset_ids": [a["deid"], b["deid"]]})
        check(r, "POST", "/v1/datasets/{dataset_id}/versions")
        assert r.status_code == 202 and r.json()["status"] == "BUILDING" and r.json()["item_count"] == 2
        vid = r.json()["dataset_version_id"]
        r = ctx.client.get(f"/v1/datasets/{ds}", headers=ctx.as_user(OP))
        check(r, "GET", "/v1/datasets/{dataset_id}")
        assert r.json()["versions"][0]["status"] == "BUILDING"
        assert ctx.client.post(f"/v1/datasets/{ds}/publish", headers=ctx.as_user(OP)).status_code == 422
        assert ctx.client.get(f"{path}/{vid}/lineage", headers=ctx.as_user(OP)).status_code == 409

        # 작업 큐 → READY: 사본·매니페스트·sha256
        assert _run_build_jobs(ctx) == ["READY"]
        v = ctx.client.get(f"/v1/datasets/{ds}", headers=ctx.as_user(OP)).json()["versions"][0]
        assert v["status"] == "READY" and v["item_count"] == 2 and len(v["manifest_sha256"]) == 64
        man = ctx.c.storage.get_object(ctx.c.settings.bucket_datasets, f"{ds}/{vid}/MANIFEST.json")
        assert hashlib.sha256(man).hexdigest() == v["manifest_sha256"]
        assert ctx.client.get(f"{path}/{vid}/lineage", headers=ctx.as_user(OP)).status_code == 200

        # 그사이 조건이 바뀌면 FAILED: 2.0.0을 시작한 뒤 A 비식별본을 L1으로 내린다
        r = ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "2.0.0", "data_card": CARD,
                                                                 "item_asset_ids": [a["deid"], b["deid"]]})
        v2 = r.json()["dataset_version_id"]
        assert share(ctx, ADMIN_A, a["deid"], "PROGRAM_REVIEW", existing_grant_handling="REVOKE_NOW").json()[
            "state"] == "APPLIED"
        assert _run_build_jobs(ctx) == ["FAILED"]
        failed = next(x for x in ctx.client.get(f"/v1/datasets/{ds}", headers=ctx.as_user(OP)).json()["versions"]
                      if x["dataset_version_id"] == v2)
        assert failed["status"] == "FAILED" and a["deid"] in failed["build_error"]
    finally:
        ctx.c.settings.__dict__["dataset_sync_max_items"] = 200

    # 이용기관에는 다 만든 버전만: 다시 L2·L3로 올리고 게시하면 1.0.0만 보이고 FAILED 버전은 신청할 수 없다
    share(ctx, ADMIN_A, a["deid"], "SANDBOX_PARTNERS")
    to_l3(ctx, ADMIN_A, a["deid"])
    to_l3(ctx, ADMIN_B, b["deid"])
    assert ctx.client.post(f"/v1/datasets/{ds}/publish", headers=ctx.as_user(OP)).status_code == 200
    seen = ctx.client.get("/v1/datasets", headers=ctx.as_user(CONSUMER)).json()["items"]
    assert [x["dataset_version_id"] for x in seen[0]["versions"]] == [vid]
    assert access(ctx, v2, "AI_TRAINING").status_code == 404


def test_catalog_sample_thumbnails(ictx, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """13번 요구사항 7.1: 카탈로그에 샘플 썸네일 (항목의 공유용 파생본에서 뽑은 JPEG, 단기 URL)."""
    import shutil

    import httpx

    from app.db.models import Asset
    from tests.integration.test_pipeline import synth_video

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg 필요")
    ctx = ictx
    a = approved_pair(ctx, ORG_A, PRJ_A, DEV_A_CAMERA, ["AI_TRAINING"])
    data = synth_video(tmp_path, seconds=2)  # 합성 영상 (실제 촬영물 아님)
    with ctx.c.sessionmaker.begin() as db:
        d = db.get(Asset, a["deid"])
        d.byte_size, d.checksum_sha256, d.mime_type = len(data), hashlib.sha256(data).hexdigest(), "video/mp4"
        key = d.storage_key
    ctx.c.storage.put_object(ctx.c.settings.bucket_proc, key, data, "video/mp4")
    share(ctx, ADMIN_A, a["deid"], "SANDBOX_PARTNERS")
    ds = ctx.client.post("/v1/datasets", json={"title": "샘플 (합성)"}, headers=ctx.as_user(OP)).json()["dataset_id"]
    r = ctx.client.post(f"/v1/datasets/{ds}/versions", headers=ctx.as_user(OP),
                        json={"version": "1.0.0", "data_card": CARD, "item_asset_ids": [a["deid"]]})
    assert r.status_code == 201, r.text
    to_l3(ctx, ADMIN_A, a["deid"])
    ctx.client.post(f"/v1/datasets/{ds}/publish", headers=ctx.as_user(OP))
    r = ctx.client.get("/v1/datasets", headers=ctx.as_user(CONSUMER))
    check(r, "GET", "/v1/datasets")
    samples = r.json()["items"][0]["samples"]
    assert len(samples) == 1
    jpg = httpx.get(samples[0]["url"], timeout=30).content
    assert jpg[:2] == b"\xff\xd8" and len(jpg) > 500  # JPEG
    r = ctx.client.get(f"/v1/datasets/{ds}", headers=ctx.as_user(CONSUMER))
    check(r, "GET", "/v1/datasets/{dataset_id}")
    assert len(r.json()["samples"]) == 1 and a["orig"] not in r.text  # 원본은 드러나지 않는다
