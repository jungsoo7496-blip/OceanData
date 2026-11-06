"""자산 조회의 원래 파일 경로와 원천 자산 ID는 열람 범위에 맞춰 반환한다."""

from __future__ import annotations

import hashlib

import pytest

from app.db.models import AssetLineage
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A
from tests.conftest import Ctx, in_future
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration

ASSET_LIST = "/v1/assets"
ASSET_DETAIL = "/v1/assets/{asset_id}"
SAFE_PROVENANCE = {"ingest_channel": "AGENT", "client_version": "0.1.0"}
ORIGINAL_PATHS = {
    "original_filename": "synthetic-camera.mp4",
    "original_relative_path": "synthetic-voyage/synthetic-camera.mp4",
}


@pytest.fixture()
def shared_assets(ctx: Ctx) -> tuple[str, str, str]:
    private = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
    visible = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, scope="PROGRAM_REVIEW")
    derived = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="PROXY", scope="PROGRAM_REVIEW")
    parents = [private.asset_id, visible.asset_id]
    derived.provenance = {**SAFE_PROVENANCE, **ORIGINAL_PATHS, "parent_asset_ids": parents}
    with ctx.c.sessionmaker.begin() as db:
        for i, a in enumerate((private, visible, derived)):
            a.checksum_sha256 = hashlib.sha256(f"synthetic-camera-{i}".encode()).hexdigest()
            a.location_precision = "EXACT_RESTRICTED"
            db.add(a)
        db.flush()
        db.add_all([AssetLineage(child_asset_id=derived.asset_id, parent_asset_id=parent, relation="PROXY",
                                 created_at=derived.created_at) for parent in parents])
    return derived.asset_id, private.asset_id, visible.asset_id


@pytest.mark.parametrize("endpoint", [ASSET_LIST, ASSET_DETAIL], ids=["list", "detail"])
def test_asset_provenance_omits_paths_and_private_parent_for_program_viewer(
    ctx: Ctx, shared_assets: tuple[str, str, str], endpoint: str,
) -> None:
    shared_asset_id, private_parent, visible_parent = shared_assets

    def provenance_for(user: int) -> dict:
        response = ctx.client.get(endpoint.format(asset_id=shared_asset_id), headers=ctx.as_user(user))
        assert response.status_code == 200, response.text
        check(response, "GET", endpoint)
        body = response.json()
        asset = next(a for a in body["items"] if a["asset_id"] == shared_asset_id) if endpoint == ASSET_LIST else body
        assert asset["asset_id"] == shared_asset_id
        return asset["provenance"]

    viewer = provenance_for(105)  # 배정 사업의 L1 자산 메타데이터에 접근할 수 있다.
    for user in (101, 102, 106):  # 소유 기관 관리자·구성원과 운영자에게는 원래 값을 보존한다.
        assert provenance_for(user) == {**SAFE_PROVENANCE, **ORIGINAL_PATHS,
                                       "parent_asset_ids": [private_parent, visible_parent]}
    hidden = ctx.client.get(ASSET_DETAIL.format(asset_id=private_parent), headers=ctx.as_user(105))
    assert hidden.status_code == 404
    assert viewer == {**SAFE_PROVENANCE, "parent_asset_ids": [visible_parent]}


def test_asset_provenance_omits_paths_for_same_org_api_key(ctx: Ctx, shared_assets: tuple[str, str, str]) -> None:
    shared_asset_id, private_parent, visible_parent = shared_assets
    issued = ctx.client.post("/v1/api-keys", headers=ctx.as_user(101, mfa=True), json={
        "name": "synthetic-provenance-reader", "scopes": ["asset:read"], "expires_at": in_future(days=1),
    })
    assert issued.status_code == 201
    credentials = issued.json()
    token = ctx.client.post("/v1/auth/token", data={
        "grant_type": "client_credentials", "client_id": credentials["client_id"],
        "client_secret": credentials["client_secret"], "scope": "asset:read",
    })
    assert token.status_code == 200
    headers = {"Authorization": "Bearer " + token.json()["access_token"]}
    response = ctx.client.get(ASSET_DETAIL.format(asset_id=shared_asset_id), headers=headers)
    assert response.status_code == 200, response.text
    check(response, "GET", ASSET_DETAIL)
    assert response.json()["asset_id"] == shared_asset_id
    assert response.json()["provenance"] == {**SAFE_PROVENANCE, "parent_asset_ids": [private_parent, visible_parent]}


@pytest.mark.parametrize("user", [108, 109], ids=["reviewer", "consumer"])
def test_asset_provenance_paths_do_not_grant_asset_access(
    ctx: Ctx, shared_assets: tuple[str, str, str], user: int,
) -> None:
    shared_asset_id, _, _ = shared_assets
    for endpoint in (ASSET_LIST, ASSET_DETAIL):
        response = ctx.client.get(endpoint.format(asset_id=shared_asset_id), headers=ctx.as_user(user))
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "FORBIDDEN"
        check(response, "GET", endpoint)
