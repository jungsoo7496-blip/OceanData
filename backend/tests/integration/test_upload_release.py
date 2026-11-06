"""04번 업로드 정리·크기·중복 판정 회귀 시험 (실제 S3 API, moto 또는 TEST_S3_ENDPOINT).

검토에서 재현된 문제: 계보 행 때문에 취소·만료 정리가 실패해 정리 작업 전체가 멈춤, 신고 크기 미검사로
쿼터 우회, 받지 않은 파일의 ALREADY_REGISTERED, 업로드 중 삭제의 거짓 삭제 증명서, 완료·취소 경합.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select, text

from app.db.models import Asset, AssetLineage, OrgQuota, Upload
from app.seed import ORG_A
from app.services.ingest import IngestService
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


def test_part_urls_sign_planned_content_length(ictx) -> None:  # type: ignore[no-untyped-def]
    """사전 서명 파트 URL은 계획한 파트 크기를 Content-Length로 서명한다 (마지막 파트는 나머지)."""
    from urllib.parse import parse_qs, urlparse

    h = ictx.as_user(102)
    data = mp4_bytes(12 * MB, seed=201)
    cr = ictx.client.post("/v1/uploads", json=manifest(data), headers=h).json()
    assert cr["part_count"] == 3
    for part in cr["parts"]:
        q = parse_qs(urlparse(part["url"]).query)
        assert "content-length" in q["X-Amz-SignedHeaders"][0].split(";")
    with ictx.c.sessionmaker() as db:
        up = db.get(Upload, cr["upload_id"])
        assert [IngestService._part_len(up, n) for n in (1, 2, 3)] == [5 * MB, 5 * MB, 2 * MB]


def _upload_oversized_legacy_part(ictx, headers: dict) -> tuple[dict, Upload, str]:  # type: ignore[no-untyped-def]
    cr = ictx.client.post("/v1/uploads", json=manifest(b"Z"), headers=headers).json()
    assert cr["part_count"] == 1
    assert _quota(ictx).reserved_bytes == 1
    # 배포 전에 발급한 URL에는 크기 서명이 없었다. 실제 저장소에서도 완료 검사의 동작을 검증한다.
    up = _upload(ictx, cr["upload_id"])
    legacy_url = ictx.c.storage.presign_upload_part(up.bucket, up.object_key, up.s3_upload_id, 1, 60)
    r = httpx.put(legacy_url, content=mp4_bytes(6 * MB, seed=299), timeout=60)
    assert r.is_success
    return cr, up, r.headers["etag"]


def test_complete_rejects_parts_larger_than_declared(ictx) -> None:  # type: ignore[no-untyped-def]
    """1바이트로 신고하고 더 큰 파트를 올려 완료하면 409, 세션 취소·예약 해제, 객체를 만들지 않는다."""
    from tests.contract.openapi_contract import assert_matches_contract as check

    h = ictx.as_user(102)
    cr, _, etag = _upload_oversized_legacy_part(ictx, h)
    r2 = ictx.client.post(f"/v1/uploads/{cr['upload_id']}/complete",
                          json={"parts": [{"part_number": 1, "etag": etag}]}, headers=h)
    assert r2.status_code == 409, r2.text
    body = r2.json()
    assert body["code"] == "CONFLICT"
    assert body["size_mismatch"] == [{"part_number": 1, "expected_size": 1, "actual_size": 6 * MB}]
    check(r2, "post", "/v1/uploads/{upload_id}/complete")
    up = _upload(ictx, cr["upload_id"])
    assert up.status == "ABORTED"
    assert _quota(ictx).reserved_bytes == 0
    assert ictx.c.storage.head_object(up.bucket, up.object_key) is None
    with ictx.c.sessionmaker() as db:
        assert db.get(Asset, cr["asset_id"]) is None  # 받은 것이 없는 자산 기록은 지운다
    again = ictx.client.post(f"/v1/uploads/{cr['upload_id']}/complete",
                             json={"parts": [{"part_number": 1, "etag": etag}]}, headers=h)
    assert again.status_code == 410


@pytest.mark.parametrize("storage_failure", ["size_missing", "abort_failed"])
def test_complete_preserves_reservation_when_size_cannot_be_verified_or_cleaned(
    ictx, monkeypatch: pytest.MonkeyPatch, storage_failure: str,  # type: ignore[no-untyped-def]
) -> None:
    from tests.contract.openapi_contract import assert_matches_contract as check

    h = ictx.as_user(102)
    cr, up, etag = _upload_oversized_legacy_part(ictx, h)
    with monkeypatch.context() as patch:
        if storage_failure == "size_missing":
            original = ictx.c.storage.list_parts

            def sizes_missing(*args):
                return [replace(part, size=None) for part in original(*args)]

            patch.setattr(ictx.c.storage, "list_parts", sizes_missing)
        else:
            def abort_failed(*_args):
                raise TimeoutError("합성 스토리지 중단")

            patch.setattr(ictx.c.storage, "abort_multipart_upload", abort_failed)
        r = ictx.client.post(f"/v1/uploads/{cr['upload_id']}/complete",
                             json={"parts": [{"part_number": 1, "etag": etag}]}, headers=h)
    assert r.status_code == 503 and r.json()["code"] == "STORAGE_UNAVAILABLE"
    assert r.headers["Retry-After"] == "30"
    check(r, "POST", "/v1/uploads/{upload_id}/complete")
    assert _upload(ictx, up.upload_id).status == "UPLOADING"
    assert _quota(ictx).reserved_bytes == 1
    assert ictx.c.storage.head_object(up.bucket, up.object_key) is None
    assert len(ictx.c.storage.list_parts(up.bucket, up.object_key, up.s3_upload_id)) == 1


def test_complete_uses_etag_from_validated_part_snapshot(ictx, monkeypatch) -> None:
    """크기를 확인한 뒤 파트가 바뀌면 다른 ETag로는 완료하지 않는다."""
    h = ictx.as_user(102)
    cr = ictx.client.post("/v1/uploads", json=manifest(b"Z"), headers=h).json()
    up = _upload(ictx, cr["upload_id"])
    assert httpx.put(cr["parts"][0]["url"], content=b"Z").is_success
    snapshot = ictx.c.storage.list_parts(up.bucket, up.object_key, up.s3_upload_id)
    assert len(snapshot) == 1 and snapshot[0].size == 1
    unsigned_url = ictx.c.storage.presign_upload_part(up.bucket, up.object_key, up.s3_upload_id, 1, 60)
    other = httpx.put(unsigned_url, content=b"ZZ")
    assert other.is_success and other.headers["etag"] != snapshot[0].etag
    monkeypatch.setattr(ictx.c.storage, "list_parts", lambda *_args: snapshot)
    r = ictx.client.post(f"/v1/uploads/{up.upload_id}/complete", headers=h,
                         json={"parts": [{"part_number": 1, "etag": other.headers["etag"]}]})
    assert r.status_code == 409
    assert "size_mismatch" not in r.json()
    assert _upload(ictx, up.upload_id).status == "UPLOADING"
    assert _quota(ictx).reserved_bytes == 1
    assert ictx.c.storage.head_object(up.bucket, up.object_key) is None


def test_stale_abort_does_not_release_another_uploads_reservation(ictx) -> None:
    h = ictx.as_user(102)
    _, up, etag = _upload_oversized_legacy_part(ictx, h)
    other = ictx.client.post("/v1/uploads", json=manifest(b"X"), headers=h)
    assert other.status_code == 201 and _quota(ictx).reserved_bytes == 2
    with ictx.c.sessionmaker.begin() as stale:
        stale_upload = stale.get(Upload, up.upload_id)
        assert stale_upload is not None and stale_upload.reserved_bytes == 1
        response = ictx.client.post(f"/v1/uploads/{up.upload_id}/complete", headers=h,
                                    json={"parts": [{"part_number": 1, "etag": etag}]})
        assert response.status_code == 409
        assert _quota(ictx).reserved_bytes == 1
        IngestService(ictx.c, stale)._release(stale_upload, "ABORTED")
    assert _quota(ictx).reserved_bytes == 1
