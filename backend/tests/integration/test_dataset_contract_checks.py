"""계약 점검 S01·S03·S04·S05: 데이터셋 버전 만들기·목록·계보가 계약대로 답하는가 (13번 요구사항 5·6·7, 작업 32)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.db.models import DatasetVersion
from app.seed import DEV_A_CAMERA, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, PRJ_B, fid
from app.services import datasets
from tests.acceptance.test_t08_t15_sharing import CARD, approved_pair, share, to_l3
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


def _pages(ctx, user: int, limit: int) -> list[list[str]]:  # type: ignore[no-untyped-def]
    """next_cursor를 따라 끝까지 읽은 쪽별 dataset_id."""
    pages: list[list[str]] = []
    cursor = None
    while True:
        params: dict[str, str | int] = {"limit": limit, **({"cursor": cursor} if cursor else {})}
        r = ctx.client.get("/v1/datasets", params=params, headers=ctx.as_user(user))
        check(r, "GET", "/v1/datasets")
        assert r.status_code == 200, r.text
        pages.append([x["dataset_id"] for x in r.json()["items"]])
        cursor = r.json()["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 100


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


# ---- S01: 큰 버전도 데이터 카드를 요청 때 검사한다


def test_queued_version_checks_data_card_before_building(ictx) -> None:
    ctx = ictx
    a, b = _shared_pair(ctx)
    ds = _dataset(ctx)
    path = f"/v1/datasets/{ds}/versions"
    bad = {k: v for k, v in CARD.items() if k != "privacy"}
    body = {"version": "1.0.0", "data_card": bad, "item_asset_ids": [a["deid"], b["deid"]]}
    sync = ctx.client.post(path, headers=ctx.as_user(OP), json=body)  # 요청 안에서 만드는 경로: 이미 422
    assert sync.status_code == 422, sync.text

    r = _post_queued(ctx, path, body)
    check(r, "POST", VERSIONS)
    assert r.status_code == 422 and r.json()["code"] == "RULE_VIOLATION", r.text
    # 두 경로가 같은 오류 본문 (포인터는 /data_card부터)
    assert (r.json()["detail"], r.json()["unmet_conditions"]) == (sync.json()["detail"], sync.json()["unmet_conditions"])
    assert {"condition": "/data_card", "actual": "'privacy' is a required property"} in r.json()["unmet_conditions"]
    # BUILDING 버전도 작업도 남지 않는다
    assert ctx.c.queue.claim(["dataset_build"], "t", ("CPU",)) is None
    with ctx.c.sessionmaker() as db:
        assert db.execute(select(func.count()).select_from(DatasetVersion)
                          .where(DatasetVersion.dataset_id == ds)).scalar_one() == 0
    g = ctx.client.get(f"/v1/datasets/{ds}", headers=ctx.as_user(OP))
    check(g, "GET", "/v1/datasets/{dataset_id}")
    assert g.json()["versions"] == [] and "data_card" not in g.json()
    lst = ctx.client.get("/v1/datasets", headers=ctx.as_user(OP))
    check(lst, "GET", "/v1/datasets")

    # 안쪽 항목이 비어도 같은 포인터 규칙 (/data_card/privacy/…)
    worse = {**CARD, "privacy": {**CARD["privacy"], "risk_note": ""}}
    sync = ctx.client.post(path, headers=ctx.as_user(OP), json={**body, "data_card": worse})
    r = _post_queued(ctx, path, {**body, "data_card": worse})
    assert r.status_code == sync.status_code == 422, r.text
    assert r.json()["unmet_conditions"] == sync.json()["unmet_conditions"]
    assert [u["condition"] for u in r.json()["unmet_conditions"]] == ["/data_card/privacy/risk_note"]

    # 카드가 맞으면 지금처럼 202 BUILDING
    r = _post_queued(ctx, path, {**body, "data_card": CARD})
    assert r.status_code == 202 and r.json()["status"] == "BUILDING", r.text


# ---- S05: item_asset_ids는 계약 형식(asset_id·중복 없음)


def test_version_item_ids_follow_contract_format(ictx) -> None:
    ctx = ictx
    a, b = _shared_pair(ctx)
    ds = _dataset(ctx)
    path = f"/v1/datasets/{ds}/versions"
    key = {**ctx.as_user(OP), "Idempotency-Key": "dsa-s05-item-ids-0001"}

    r = ctx.client.post(path, headers=key, json={"version": "1.0.0", "data_card": CARD,
                                                 "item_asset_ids": [a["deid"], b["deid"], a["deid"]]})
    check(r, "POST", VERSIONS)
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION_ERROR", r.text
    assert [(e["pointer"], e["code"]) for e in r.json()["errors"]] == [("/item_asset_ids/2", "unique")]

    r = ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "1.0.0", "data_card": CARD,
                                                             "item_asset_ids": [a["deid"], "not-an-asset-id"]})
    check(r, "POST", VERSIONS)
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION_ERROR", r.text
    assert [e["pointer"] for e in r.json()["errors"]] == ["/item_asset_ids/1"]

    # 큰 버전(작업 큐) 경로도 같은 검사
    r = _post_queued(ctx, path, {"version": "1.0.0", "data_card": CARD,
                                 "item_asset_ids": [a["deid"], b["deid"], b["deid"]]})
    assert r.status_code == 400 and r.json()["errors"][0]["pointer"] == "/item_asset_ids/2", r.text
    assert ctx.c.queue.claim(["dataset_build"], "t", ("CPU",)) is None

    # 형식은 맞는데 없는 자산은 지금처럼 422 RULE_VIOLATION (공유 조건 미충족)
    r = ctx.client.post(path, headers=ctx.as_user(OP), json={"version": "1.0.0", "data_card": CARD,
                                                             "item_asset_ids": [fid("ast", 999)]})
    check(r, "POST", VERSIONS)
    assert r.status_code == 422 and r.json()["code"] == "RULE_VIOLATION", r.text

    # 거부된 본문은 버전도 멱등 키도 남기지 않는다: 같은 키로 올바른 본문을 보내면 새로 만든다
    g = ctx.client.get(f"/v1/datasets/{ds}", headers=ctx.as_user(OP))
    assert g.json()["versions"] == []
    r = ctx.client.post(path, headers=key, json={"version": "1.0.0", "data_card": CARD,
                                                 "item_asset_ids": [a["deid"], b["deid"]]})
    assert r.status_code == 201 and r.json()["manifest"]["item_count"] == 2, r.text


# ---- S03: 목록은 limit·cursor로 나눈다


def test_dataset_list_pages_by_limit_and_cursor(ctx) -> None:
    ids = sorted(_dataset(ctx, f"쪽 {i}") for i in range(51))
    r = ctx.client.get("/v1/datasets", headers=ctx.as_user(OP))  # 기본 50
    check(r, "GET", "/v1/datasets")
    assert len(r.json()["items"]) == 50 and r.json()["next_cursor"], r.text

    pages = _pages(ctx, OP, 20)
    assert [len(p) for p in pages] == [20, 20, 11]
    assert [x for p in pages for x in p] == ids  # 겹치지도 빠지지도 않는다
    assert ctx.client.get("/v1/datasets?limit=0", headers=ctx.as_user(OP)).status_code == 400
    assert ctx.client.get("/v1/datasets?limit=501", headers=ctx.as_user(OP)).status_code == 400


def test_consumer_dataset_pages_skip_hidden_datasets(ictx) -> None:
    """이용기관에게는 게시·L3 이상·READY 버전이 있는 데이터셋만 센다. 앞에 안 보이는 데이터셋이 있어도 쪽이 짧지 않고,
    뒤에 보이는 것이 없으면 next_cursor는 null."""
    ctx = ictx
    a, b = _shared_pair(ctx)
    ids = sorted(_dataset(ctx, f"카탈로그 {i}") for i in range(6))
    visible = [ids[2], ids[4]]
    for ds in ids:  # 보일 것에는 a(뒤에 L3로 올림), 안 보일 것에는 b(L2에 둠)만 담는다
        item = a["deid"] if ds in visible else b["deid"]
        r = ctx.client.post(f"/v1/datasets/{ds}/versions", headers=ctx.as_user(OP),
                            json={"version": "1.0.0", "data_card": CARD, "item_asset_ids": [item]})
        assert r.status_code == 201, r.text
    to_l3(ctx, ADMIN_A, a["deid"])
    for ds in ids:
        assert ctx.client.post(f"/v1/datasets/{ds}/publish", headers=ctx.as_user(OP)).status_code == 200

    assert _pages(ctx, CONSUMER, 1) == [[ids[2]], [ids[4]]]
    assert _pages(ctx, CONSUMER, 2) == [visible]
    assert [x for p in _pages(ctx, OP, 4) for x in p] == ids  # 운영자는 모두
