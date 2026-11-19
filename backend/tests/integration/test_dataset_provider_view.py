"""제공기관은 데이터셋에서 자기 기관 자산이 든 버전만 본다 (통합 검토 SHARING-2, ADR-0088).

두 기관이 한 데이터셋에 들어 있으면 제공기관 관리자에게 다른 기관만 든 버전의 크기·매니페스트 해시·등급, 그 버전의 데이터 카드,
그 버전에서 뽑은 샘플 썸네일 URL까지 보였다 (domain.md 2장: 메타데이터·바이너리는 자기 기관만). 계보 조회는 이미 자기 버전만 준다.
합성 행만 쓴다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import Dataset, DatasetVersion, DatasetVersionItem
from app.domain.ids import new_id
from app.seed import DEV_A_CAMERA, DEV_B_CAMERA, ORG_A, ORG_B, PRJ_A, PRJ_B
from tests.acceptance.test_t08_t15_sharing import CARD
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_catalog import _asset

pytestmark = pytest.mark.integration
ADMIN_A, ADMIN_B, OP = 101, 103, 106
T0 = datetime(2026, 11, 20, 1, 0, tzinfo=UTC)


def _version(db, ds: str, version: str, org: str, title: str,  # type: ignore[no-untyped-def]
             at: datetime, samples: list[str] | None) -> str:
    a = _asset(org, PRJ_A if org == ORG_A else PRJ_B, DEV_A_CAMERA if org == ORG_A else DEV_B_CAMERA,
               variant="DEID", scope="PROGRAM_REVIEW", session=None)
    db.add(a)
    vid = new_id("dataset_version")
    db.add(DatasetVersion(dataset_version_id=vid, dataset_id=ds, version=version, provider_organization_ids=[org],
                          item_count=1, total_bytes=10, data_card={**CARD, "title": title, "rights_holders": [title]},
                          manifest={}, manifest_key=f"{ds}/{vid}/MANIFEST.json", manifest_sha256="0" * 64,
                          created_by="t", created_at=at, status="READY",
                          sample_keys=[f"{ds}/{vid}/samples/{i}.jpg" for i in samples] if samples else None))
    db.flush()
    db.add(DatasetVersionItem(dataset_version_id=vid, asset_id=a.asset_id, organization_id=org,
                              path=f"deid/{version}.mp4", object_key=f"{ds}/{vid}/deid/{version}.mp4", byte_size=10,
                              checksum_sha256="0" * 64))
    return vid


def _two_org_dataset(ctx) -> tuple[str, str, str]:  # type: ignore[no-untyped-def]
    """v1.0.0은 B 기관 자산만, 뒤에 만든 v2.0.0은 A 기관 자산만 (READY, 샘플 있음). 게시 전."""
    ds = new_id("dataset")
    with ctx.c.sessionmaker.begin() as db:
        db.add(Dataset(dataset_id=ds, title="두 기관 합성", created_by="t", created_at=T0))
        db.flush()
        v1 = _version(db, ds, "1.0.0", ORG_B, "B 카드", T0, None)
        v2 = _version(db, ds, "2.0.0", ORG_A, "A 전용 카드", T0 + timedelta(hours=1), ["1"])
    return ds, v1, v2


def _seen(body: dict) -> tuple[list[str], str | None, int]:
    return ([v["dataset_version_id"] for v in body["versions"]], (body.get("data_card") or {}).get("title"),
            len(body.get("samples") or []))


def test_provider_sees_only_versions_with_own_assets(ictx) -> None:
    ctx = ictx
    ds, v1, v2 = _two_org_dataset(ctx)

    r = ctx.client.get(f"/v1/datasets/{ds}", headers=ctx.as_user(ADMIN_B))
    check(r, "GET", "/v1/datasets/{dataset_id}")
    assert r.status_code == 200, r.text
    assert _seen(r.json()) == ([v1], "B 카드", 0)  # A 버전·A 카드·A 샘플 URL은 없다

    r = ctx.client.get("/v1/datasets", headers=ctx.as_user(ADMIN_B))
    check(r, "GET", "/v1/datasets")
    assert [_seen(x) for x in r.json()["items"] if x["dataset_id"] == ds] == [([v1], "B 카드", 0)]

    r = ctx.client.get(f"/v1/datasets/{ds}", headers=ctx.as_user(ADMIN_A))  # A는 자기 버전과 그 샘플
    assert _seen(r.json()) == ([v2], "A 전용 카드", 1)

    r = ctx.client.get(f"/v1/datasets/{ds}", headers=ctx.as_user(OP))  # 운영자는 그대로 전부
    assert _seen(r.json()) == ([v1, v2], "A 전용 카드", 1)

    # 계보와 같은 규칙: 다른 기관 버전의 계보는 404
    assert ctx.client.get(f"/v1/datasets/{ds}/versions/{v2}/lineage",
                          headers=ctx.as_user(ADMIN_B)).status_code == 404
