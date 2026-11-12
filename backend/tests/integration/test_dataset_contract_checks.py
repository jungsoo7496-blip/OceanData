"""계약 점검 S01·S03·S04·S05: 데이터셋 버전 만들기·목록·계보가 계약대로 답하는가 (13번 요구사항 5·6·7, 작업 32)."""

from __future__ import annotations

import pytest

from app.seed import DEV_A_CAMERA, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, PRJ_B
from app.services import datasets
from tests.acceptance.test_t08_t15_sharing import CARD, approved_pair, share
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
ADMIN_A, ADMIN_B, OP, CONSUMER = 101, 103, 106, 109
VERSIONS = "/v1/datasets/{dataset_id}/versions"
LINEAGE = "/v1/datasets/{dataset_id}/versions/{dataset_version_id}/lineage"


def _dataset(ctx, title: str = "합성") -> str:  # type: ignore[no-untyped-def]
    r = ctx.client.post("/v1/datasets", json={"title": title}, headers=ctx.as_user(OP))
    assert r.status_code == 201, r.text
    return str(r.json()["dataset_id"])


def _shared_pair(ctx) -> tuple[dict[str, str], dict[str, str]]:  # type: ignore[no-untyped-def]
    """두 기관의 비식별본, 둘 다 L2 (데이터셋에 넣을 수 있다)."""
    a = approved_pair(ctx, ORG_A, PRJ_A, DEV_A_CAMERA, ["AI_TRAINING"])
    b = approved_pair(ctx, ORG_B, PRJ_B, DEV_B_CAMERA, ["AI_TRAINING"])
    assert share(ctx, ADMIN_A, a["deid"], "SANDBOX_PARTNERS").json()["state"] == "APPLIED"
    assert share(ctx, ADMIN_B, b["deid"], "SANDBOX_PARTNERS").json()["state"] == "APPLIED"
    return a, b


def _post_queued(ctx, path: str, body: dict):  # type: ignore[no-untyped-def]
    """항목 2개부터 작업 큐로 가게 한도를 낮춰 보낸다."""
    ctx.c.settings.__dict__["dataset_sync_max_items"] = 1
    try:
        return ctx.client.post(path, headers=ctx.as_user(OP), json=body)
    finally:
        ctx.c.settings.__dict__["dataset_sync_max_items"] = 200


# ---- S04: 다 만들지 못한 버전의 계보는 계약에 적힌 409


def test_lineage_of_unfinished_version_is_documented_conflict(ictx) -> None:
    ctx = ictx
    a, b = _shared_pair(ctx)
    ds = _dataset(ctx)
    path = f"/v1/datasets/{ds}/versions"
    r = _post_queued(ctx, path, {"version": "1.0.0", "data_card": CARD, "item_asset_ids": [a["deid"], b["deid"]]})
    assert r.status_code == 202, r.text
    vid = r.json()["dataset_version_id"]

    lr = ctx.client.get(f"{path}/{vid}/lineage", headers=ctx.as_user(OP))
    assert lr.status_code == 409 and lr.json()["code"] == "CONFLICT", lr.text
    check(lr, "GET", LINEAGE)

    # 그사이 조건이 바뀌어 FAILED가 된 버전도 409
    assert share(ctx, ADMIN_A, a["deid"], "PROGRAM_REVIEW", existing_grant_handling="REVOKE_NOW").json()[
        "state"] == "APPLIED"
    job = ctx.c.queue.claim(["dataset_build"], "t", ("CPU",))
    assert job is not None and datasets.finish_build(ctx.c, job.payload["dataset_version_id"]) == "FAILED"
    ctx.c.queue.complete(job.job_id)
    lr = ctx.client.get(f"{path}/{vid}/lineage", headers=ctx.as_user(OP))
    assert lr.status_code == 409 and lr.json()["code"] == "CONFLICT", lr.text
    check(lr, "GET", LINEAGE)
