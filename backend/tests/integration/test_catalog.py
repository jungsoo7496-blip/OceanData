"""03번 통합 시험: 스키마 게시·번들, 코드표, 사전 검증, 장비 정책, 계보, 자산 범위."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from jsonschema import Draft202012Validator

from app.db.models import Asset, AssetLineage
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, DEV_A_COLLECTOR, DEV_B_CAMERA, DEV_B_COLLECTOR, ORG_A, ORG_B, PRJ_A, PRJ_B, SES_A
from app.services.contracts import contracts_dir, schema_problems
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration
T0 = datetime(2026, 11, 1, tzinfo=UTC)


def test_schema_index_and_bundle_match_repository_files(ctx) -> None:
    h = ctx.as_user(102)
    idx = ctx.client.get("/v1/schemas", headers=h)
    check(idx, "GET", "/v1/schemas")
    names = {i["name"] for i in idx.json()["items"]}
    assert {"asset-metadata", "upload-manifest", "observation-batch", "common"} <= names
    r = ctx.client.get("/v1/schemas/upload-manifest/1.0.0", headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/schema+json")
    bundled = r.json()
    assert "common.schema.json" not in r.text
    # 번들로 검증한 결과가 저장소 파일(참조 포함)로 검증한 결과와 같아야 한다 (요구사항 2.2)
    root = contracts_dir() / "examples"
    for path in sorted(root.glob("**/upload-manifest.*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert bool(list(Draft202012Validator(bundled).iter_errors(doc))) == bool(
            schema_problems("upload-manifest", doc)), path.name
    assert ctx.client.get("/v1/schemas/upload-manifest/9.0.0", headers=h).status_code == 404
    assert ctx.client.get("/v1/schemas/nope/1.0.0", headers=h).status_code == 404


def test_code_tables(ctx) -> None:
    h = ctx.as_user(102)
    for table in ("units", "metrics", "event-categories", "purposes", "quarantine-reasons"):
        r = ctx.client.get(f"/v1/codes/{table}", headers=h)
        check(r, "GET", "/v1/codes/{table}")
        assert r.json()["items"], table
    reasons = {i["code"] for i in ctx.client.get("/v1/codes/quarantine-reasons", headers=h).json()["items"]}
    assert {"CHECKSUM_MISMATCH", "UNKNOWN_SESSION", "MALWARE_SUSPECTED"} <= reasons
    assert ctx.client.get("/v1/codes/nope", headers=h).status_code in (400, 404)


def test_validate_endpoint_uses_same_rules(ctx) -> None:
    h = ctx.as_user(102)
    root = contracts_dir() / "examples"
    good = json.loads((root / "upload-manifest.vessel-video.json").read_text(encoding="utf-8"))
    r = ctx.client.post("/v1/contracts/validate", headers=h, json={"contract": "upload-manifest", "document": good})
    check(r, "POST", "/v1/contracts/validate")
    assert r.json() == {"valid": True, "problems": []}
    bad = json.loads((root / "invalid" / "upload-manifest.swapped-lonlat.json").read_text(encoding="utf-8"))
    r = ctx.client.post("/v1/contracts/validate", headers=h, json={"contract": "upload-manifest", "document": bad})
    assert r.json()["valid"] is False
    assert [p["code"] for p in r.json()["problems"]] == ["lonlat_swapped"]
    future = {**good, "schema_version": "1.9.0"}
    r = ctx.client.post("/v1/contracts/validate", headers=h, json={"contract": "upload-manifest", "document": future})
    assert r.json()["problems"][0]["code"] == "UNSUPPORTED_SCHEMA_VERSION"
    obs = json.loads((root / "invalid" / "observation-batch.no-timezone.json").read_text(encoding="utf-8"))
    r = ctx.client.post("/v1/contracts/validate", headers=h, json={"contract": "observation-batch", "document": obs})
    assert "timezone_required" in [p["code"] for p in r.json()["problems"]]
    assert ctx.client.post("/v1/contracts/validate", headers=h,
                           json={"contract": "asset-metadata", "document": {}}).status_code == 400


def test_device_policy_default_and_scope(ctx) -> None:
    r = ctx.client.get(f"/v1/devices/{DEV_A_COLLECTOR}/policy", headers=ctx.as_user(102))
    check(r, "GET", "/v1/devices/{device_id}/policy")
    body = r.json()
    assert body["collector_device_id"] == DEV_A_COLLECTOR
    assert {s["device_id"] for s in body["sources"]} == {DEV_A_CAMERA, "dev_" + "0" * 23 + "303"}
    assert ctx.client.get(f"/v1/devices/{DEV_B_COLLECTOR}/policy", headers=ctx.as_user(102)).status_code == 404
    assert ctx.client.get(f"/v1/devices/{DEV_A_CAMERA}/policy", headers=ctx.as_user(102)).status_code == 404


def _asset(org: str, prj: str, dev: str, *, variant: str = "ORIGINAL", scope: str = "PRIVATE", session=SES_A) -> Asset:
    return Asset(asset_id=new_id("asset"), schema_version="1.0.0", organization_id=org, project_id=prj,
                 session_id=session, source_system_id=dev, data_type="VIDEO", variant=variant, data_role="CORE",
                 ingested_at=T0, timestamp_quality={"source": "UNKNOWN", "verified": False}, mime_type="video/mp4",
                 byte_size=10, checksum_sha256=new_id("asset")[4:].lower().ljust(64, "0"), has_binary=True,
                 collection_mode="C3_APPROVED_FULL", privacy_status="REVIEW_REQUIRED", sensitivity_class="RESTRICTED",
                 sharing_scope=scope, retention_policy_id="default", retention_until=T0, quality_status="PENDING",
                 ingest_status="READY_PRIVATE", share_status="NONE", backup_class="NONE",
                 provenance={"ingest_channel": "AGENT"}, location={"type": "Point", "coordinates": [126.5, 34.7]},
                 created_at=T0, updated_at=T0)


def test_lineage_and_asset_scope(ctx) -> None:
    with ctx.c.sessionmaker.begin() as db:
        orig = _asset(ORG_A, PRJ_A, DEV_A_CAMERA)
        proxy = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="PROXY", scope="PROGRAM_REVIEW")
        thumb = _asset(ORG_A, PRJ_A, DEV_A_CAMERA, variant="THUMBNAIL")
        other = _asset(ORG_B, PRJ_B, DEV_B_CAMERA, session=None)
        db.add_all([orig, proxy, thumb, other])
        db.flush()
        db.add_all([AssetLineage(child_asset_id=proxy.asset_id, parent_asset_id=orig.asset_id, relation="PROXY",
                                 tool_version="ffmpeg 6.1", created_at=T0),
                     AssetLineage(child_asset_id=thumb.asset_id, parent_asset_id=proxy.asset_id, relation="THUMBNAIL",
                                  tool_version="ffmpeg 6.1", created_at=T0)])
        ids = (orig.asset_id, proxy.asset_id, thumb.asset_id, other.asset_id)
    o, p_, t, b = ids
    r = ctx.client.get(f"/v1/assets/{o}/lineage", headers=ctx.as_user(102))
    check(r, "GET", "/v1/assets/{asset_id}/lineage")
    assert r.json()["ancestors"] == [] and {e["asset_id"] for e in r.json()["descendants"]} == {p_, t}
    up = ctx.client.get(f"/v1/assets/{t}/lineage", headers=ctx.as_user(102)).json()["ancestors"]
    assert {e["asset_id"] for e in up} == {o, p_}
    # 다른 기관 자산 계보는 404 (T01)
    assert ctx.client.get(f"/v1/assets/{b}/lineage", headers=ctx.as_user(102)).status_code == 404
    # 관리기관 열람자는 배정 사업의 L1 이상 자산만: PRIVATE 원본·썸네일은 숨긴다
    v = ctx.client.get(f"/v1/assets/{p_}/lineage", headers=ctx.as_user(105))
    assert v.status_code == 200 and v.json()["ancestors"] == [] and v.json()["descendants"] == []
    assert ctx.client.get(f"/v1/assets/{o}/lineage", headers=ctx.as_user(105)).status_code == 404
    # 위치는 PostGIS geometry로 저장되고 GeoJSON으로 돌아온다
    with ctx.c.sessionmaker() as db:
        assert db.get(Asset, o).location == {"type": "Point", "coordinates": [126.5, 34.7]}
