"""04번 업로드 정리·크기·중복 판정 회귀 시험 (실제 S3 API, moto 또는 TEST_S3_ENDPOINT).

검토에서 재현된 문제: 계보 행 때문에 취소·만료 정리가 실패해 정리 작업 전체가 멈춤, 신고 크기 미검사로
쿼터 우회, 받지 않은 파일의 ALREADY_REGISTERED, 업로드 중 삭제의 거짓 삭제 증명서, 완료·취소 경합.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select, text

from app.db.models import Asset, AssetLineage, OrgQuota, Upload
from app.seed import ORG_A
from tests.integration.test_ingest import MB, manifest, mp4_bytes

pytestmark = pytest.mark.integration


def _quota(ictx) -> OrgQuota:  # type: ignore[no-untyped-def]
    with ictx.c.sessionmaker() as db:
        q = db.get(OrgQuota, ORG_A)
        assert q is not None
        return q


def _upload(ictx, upload_id: str) -> Upload:  # type: ignore[no-untyped-def]
    with ictx.c.sessionmaker() as db:
        up = db.get(Upload, upload_id)
        assert up is not None
        return up


def _expire(ictx, upload_id: str, minutes: int) -> None:  # type: ignore[no-untyped-def]
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE uploads SET expires_at = now() - make_interval(mins => :m) WHERE upload_id = :u"),
                     {"m": minutes, "u": upload_id})


def _parent_and_child(ictx, h: dict) -> tuple[dict, dict]:  # type: ignore[no-untyped-def]
    parent = mp4_bytes(5 * MB, seed=101)
    rp = ictx.client.post("/v1/uploads", json=manifest(parent), headers=h)
    assert rp.status_code == 201, rp.text
    child = mp4_bytes(5 * MB, seed=102)
    mc = manifest(child, variant="PROXY", parent_ref={"checksum_sha256": hashlib.sha256(parent).hexdigest()})
    rc = ictx.client.post("/v1/uploads", json=mc, headers=h)
    assert rc.status_code == 201, rc.text
    with ictx.c.sessionmaker() as db:
        rows = db.execute(select(AssetLineage).where(AssetLineage.child_asset_id == rc.json()["asset_id"])).all()
    assert len(rows) == 1  # parent_ref로 계보 행이 생긴다
    return rp.json(), rc.json()


def test_abort_upload_with_parent_ref_lineage(ictx) -> None:  # type: ignore[no-untyped-def]
    """parent_ref로 계보 행이 생긴 업로드도 취소된다 (500이 아니라 204, 세션은 ABORTED, 예약 해제)."""
    h = ictx.as_user(102)
    parent, child = _parent_and_child(ictx, h)
    r = ictx.client.delete(f"/v1/uploads/{child['upload_id']}", headers=h)
    assert r.status_code == 204, r.text
    assert _upload(ictx, child["upload_id"]).status == "ABORTED"
    # 진행 중인 원본 업로드를 취소해도 (다른 업로드가 이 자산을 원본으로 지목했어도) 막히지 않는다
    child2 = mp4_bytes(5 * MB, seed=103)
    rc2 = ictx.client.post("/v1/uploads", json=manifest(child2, variant="PROXY", parent_ref={
        "checksum_sha256": hashlib.sha256(mp4_bytes(5 * MB, seed=101)).hexdigest()}), headers=h)
    assert rc2.status_code == 201, rc2.text
    r = ictx.client.delete(f"/v1/uploads/{parent['upload_id']}", headers=h)
    assert r.status_code == 204, r.text
    assert _upload(ictx, parent["upload_id"]).status == "ABORTED"
    with ictx.c.sessionmaker() as db:
        assert db.get(Asset, parent["asset_id"]) is None
        assert db.get(Asset, child["asset_id"]) is None
        assert db.execute(select(AssetLineage).where(
            AssetLineage.parent_asset_id == parent["asset_id"])).first() is None
    r = ictx.client.delete(f"/v1/uploads/{rc2.json()['upload_id']}", headers=h)
    assert r.status_code == 204, r.text
    assert _quota(ictx).reserved_bytes == 0


def test_sweep_releases_expired_uploads_with_lineage(ictx) -> None:  # type: ignore[no-untyped-def]
    """만료 정리기는 계보 행이 있는 세션(파생본이 먼저 만료)도 정리하고 예약을 모두 해제한다."""
    from app.workers.main import sweep

    h = ictx.as_user(102)
    parent, child = _parent_and_child(ictx, h)
    _expire(ictx, child["upload_id"], 2)
    _expire(ictx, parent["upload_id"], 1)
    assert sweep(ictx.c) == 2
    assert _upload(ictx, child["upload_id"]).status == "EXPIRED"
    assert _upload(ictx, parent["upload_id"]).status == "EXPIRED"
    assert _quota(ictx).reserved_bytes == 0
    assert sweep(ictx.c) == 0


def test_sweep_continues_past_failing_upload(ictx, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """한 세션 정리가 실패해도 같은 묶음의 다른 세션은 정리되고 정리 작업이 예외로 멈추지 않는다."""
    from app.services.ingest import IngestService
    from app.workers.main import sweep

    h = ictx.as_user(102)
    bad = ictx.client.post("/v1/uploads", json=manifest(mp4_bytes(5 * MB, seed=111)), headers=h).json()
    good = ictx.client.post("/v1/uploads", json=manifest(mp4_bytes(5 * MB, seed=112)), headers=h).json()
    _expire(ictx, bad["upload_id"], 2)  # 가장 오래된 세션이 실패한다
    _expire(ictx, good["upload_id"], 1)
    real = IngestService._release

    def failing(self, up, final):  # type: ignore[no-untyped-def]
        real(self, up, final)
        if up.upload_id == bad["upload_id"]:
            raise RuntimeError("합성 실패")

    monkeypatch.setattr(IngestService, "_release", failing)
    assert sweep(ictx.c) == 1
    assert _upload(ictx, bad["upload_id"]).status == "UPLOADING"  # 실패한 세션은 되돌려져 다음 주기에 다시 시도
    assert _upload(ictx, good["upload_id"]).status == "EXPIRED"
    assert _quota(ictx).reserved_bytes == 5 * MB
    monkeypatch.setattr(IngestService, "_release", real)
    assert sweep(ictx.c) == 1
    assert _upload(ictx, bad["upload_id"]).status == "EXPIRED"
    assert _quota(ictx).reserved_bytes == 0
