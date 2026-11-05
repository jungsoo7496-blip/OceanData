"""08번 요구사항 3.5: 데이터셋 버전 사본(DATASET 존)은 원 자산의 만료와 별개로 다룬다.

- 유효한 이용 권한(GRANTED)이 있는 버전에 든 자산은 보존 만료가 건너뛰고 dry-run에 경고한다 (철회 뒤 다음 실행에서 지운다).
- 원 자산(원본·비식별본)이 만료로 지워져도 데이터셋 버전의 사본·매니페스트·계보는 DATASET 정책(dataset_keep, KEEP)대로 남는다.
  새 배포는 13번 공유 규칙을 따른다: 항목 자산이 지워진 버전은 PRIVATE로 보아 이용기관에 보이지 않는다(새 신청 404).
  보존 만료로 지운 경우에도 새 배포를 허용할지는 확인 필요 (HANDOFF '사람이 결정할 것').
- 기관 사용량에는 DATASET 존 사본이 들어가지 않는다 (domain.md 9장).
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
from sqlalchemy import text

from app.db.models import Asset, OrgQuota
from app.seed import DEV_A_CAMERA, ORG_A, PRJ_A
from app.services import retention
from tests.acceptance.test_t08_t15_sharing import CARD, access, approved_pair, decide, share, to_l3

pytestmark = pytest.mark.integration
ADMIN_A, OP, CONSUMER = 101, 106, 109


def _grant(ctx, vid: str) -> str:  # type: ignore[no-untyped-def]
    r0 = access(ctx, vid, "AI_TRAINING")
    assert r0.status_code == 201, r0.text
    rid = r0.json()["access_request_id"]
    assert decide(ctx, ADMIN_A, rid, "APPROVE").json()["status"] == "PROVIDER_APPROVED"
    r = decide(ctx, OP, rid, "APPROVE")
    assert r.json()["status"] == "GRANTED", r.text
    return str(r.json()["grant_id"])


def _used(ctx) -> int:  # type: ignore[no-untyped-def]
    with ctx.c.sessionmaker() as db:
        q = db.get(OrgQuota, ORG_A)
        return int(q.used_bytes) if q is not None else 0


def _download(ctx, grant: str, path: str) -> bytes:  # type: ignore[no-untyped-def]
    r = ctx.client.post("/v1/distributions", json={"grant_id": grant}, headers=ctx.as_user(CONSUMER))
    assert r.status_code == 201, r.text
    files = {f["path"]: f["url"] for f in r.json()["files"]}
    return httpx.get(files[path], timeout=30).content


def test_dataset_copies_outlive_source_expiry(ictx) -> None:
    ctx = ictx
    a = approved_pair(ctx, ORG_A, PRJ_A, DEV_A_CAMERA, ["AI_TRAINING"])
    assert share(ctx, ADMIN_A, a["deid"], "SANDBOX_PARTNERS").json()["state"] == "APPLIED"
    ds = ctx.client.post("/v1/datasets", json={"title": "만료 시험 (합성)"}, headers=ctx.as_user(OP)).json()["dataset_id"]
    v = ctx.client.post(f"/v1/datasets/{ds}/versions", headers=ctx.as_user(OP),
                        json={"version": "1.0.0", "item_asset_ids": [a["deid"]], "data_card": CARD}).json()
    vid, item = v["dataset_version_id"], v["manifest"]["items"][0]
    to_l3(ctx, ADMIN_A, a["deid"])
    assert ctx.client.post(f"/v1/datasets/{ds}/publish", headers=ctx.as_user(OP)).status_code == 200
    grant = _grant(ctx, vid)
    used_before = _used(ctx)  # 데이터셋 사본은 기관 사용량에 넣지 않는다 (버전을 만들어도 늘지 않음)

    # 원본·비식별본 모두 보존 기간 만료
    with ctx.c.engine.begin() as conn:
        conn.execute(text("""UPDATE assets SET retention_until = now() - interval '1 day',
                             retention_policy_id = CASE variant WHEN 'ORIGINAL' THEN 'raw_default' ELSE 'deid_default' END
                             WHERE asset_id IN (:o, :d)"""), {"o": a["orig"], "d": a["deid"]})
    with ctx.c.sessionmaker() as db:
        dr = retention.dry_run(db, ctx.c.clock.now())
    assert dr["total_count"] == 2
    assert [(w["asset_id"], w["cited_by"], w["grant_id"]) for w in dr["warnings"]] == [(a["deid"], "DATASET", grant)]

    # 이용 권한이 있는 동안: 원본만 지우고 데이터셋에 든 비식별본은 건너뛴다. 사본은 그대로 받을 수 있다
    with ctx.c.sessionmaker.begin() as db:
        certs = retention.execute_expired(ctx.c, db, ctx.c.clock.now())
    assert len(certs) == 1 and certs[0].scope == "RETENTION"  # 합성 원본은 바이너리가 없어 증명의 객체 목록은 비어 있다
    with ctx.c.sessionmaker() as db:
        assert db.get(Asset, a["orig"]).deletion_certificate_id == certs[0].certificate_id
        assert db.get(Asset, a["deid"]).deleted_at is None
    assert hashlib.sha256(_download(ctx, grant, item["path"])).hexdigest() == item["checksum_sha256"]

    # 철회 뒤 다음 실행: 비식별본도 지워진다. 데이터셋 사본·매니페스트는 남고 새 권한으로 다시 받을 수 있다
    r = ctx.client.post(f"/v1/grants/{grant}/revoke", json={"reason": "시험 종료"}, headers=ctx.as_user(OP))
    assert r.status_code == 200, r.text
    with ctx.c.sessionmaker.begin() as db:
        certs = retention.execute_expired(ctx.c, db, ctx.c.clock.now())
    assert [o["asset_id"] for c in certs for o in c.objects] == [a["deid"]]
    with ctx.c.sessionmaker() as db:
        assert db.get(Asset, a["deid"]).deleted_at is not None
    assert _used(ctx) <= used_before  # 지운 원 자산만큼 줄 뿐, 남은 사본을 더하지 않는다
    copy = ctx.c.storage.get_object(ctx.c.settings.bucket_datasets, f"{ds}/{vid}/{item['path']}")
    assert hashlib.sha256(copy).hexdigest() == item["checksum_sha256"]
    lineage = ctx.client.get(f"/v1/datasets/{ds}/versions/{vid}/lineage", headers=ctx.as_user(OP))
    assert lineage.status_code == 200, lineage.text
    r = access(ctx, vid, "AI_TRAINING")  # 항목이 지워진 버전은 새 신청을 받지 않는다 (13번 version_scope 규칙)
    assert r.status_code == 404, r.text
    assert ctx.client.get("/v1/datasets", headers=ctx.as_user(CONSUMER)).json()["items"] == []
