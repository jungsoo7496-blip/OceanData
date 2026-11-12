"""04번 업로드 정리·크기·중복 판정 회귀 시험 (실제 S3 API, moto 또는 TEST_S3_ENDPOINT).

검토에서 재현된 문제: 계보 행 때문에 취소·만료 정리가 실패해 정리 작업 전체가 멈춤, 신고 크기 미검사로
쿼터 우회, 받지 않은 파일의 ALREADY_REGISTERED, 업로드 중 삭제의 거짓 삭제 증명서, 완료·취소 경합.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import httpx
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import event, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from app.adapters.system_clock import FixedClock
from app.db.models import Asset, AssetLineage, Device, OrgQuota, Upload
from app.ports.storage import CompletedPart
from app.seed import DEV_A_CAMERA, ORG_A
from app.services.ingest import IngestService
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_ingest import MB, drain, manifest, mp4_bytes, put_parts

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


@pytest.mark.parametrize("metadata_only", [False, True], ids=["new-asset", "keep-c0-asset"])
def test_reregister_replaces_expired_upload(ictx: Ctx, metadata_only: bool) -> None:
    headers = ictx.as_user(102)
    data = mp4_bytes(128)
    payload = manifest(data, variant="ORIGINAL" if metadata_only else "CLIP")
    if metadata_only:
        with ictx.c.sessionmaker.begin() as db:
            db.get(Device, DEV_A_CAMERA).collection_mode = "C0_METADATA_ONLY"
        registered = ictx.client.post("/v1/uploads", json=payload, headers=headers)
        assert registered.status_code == 201, registered.text
        assert registered.json()["status"] == "METADATA_ONLY_REGISTERED"
        with ictx.c.sessionmaker.begin() as db:
            db.get(Device, DEV_A_CAMERA).collection_mode = "C3_APPROVED_FULL"

    created = ictx.client.post("/v1/uploads", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    old = created.json()
    if metadata_only:
        assert old["asset_id"] == registered.json()["asset_id"]
    put_parts(old, data)
    original = _upload(ictx, old["upload_id"])
    with ictx.c.sessionmaker.begin() as db:
        db.get(OrgQuota, ORG_A).quota_bytes = len(data)  # 예약을 하나만 수용한다.
    assert _quota(ictx).reserved_bytes == len(data)
    _expire(ictx, old["upload_id"], 1)

    replacement = ictx.client.post("/v1/uploads", json=payload, headers=headers)
    assert replacement.status_code == 201, replacement.text
    check(replacement, "POST", "/v1/uploads")
    new = replacement.json()
    assert new["status"] == "UPLOAD_CREATED" and new["upload_id"] != old["upload_id"]
    assert (new["asset_id"] == old["asset_id"]) is metadata_only
    with ictx.c.sessionmaker() as db:
        old_session = db.get(Upload, old["upload_id"])
        assert (old_session.status, old_session.reserved_bytes) == ("EXPIRED", 0)
        asset_ids = db.scalars(select(Asset.asset_id).where(Asset.checksum_sha256 == hashlib.sha256(data).hexdigest())).all()
        assert asset_ids == [new["asset_id"]]
    assert _quota(ictx).reserved_bytes == len(data)
    with pytest.raises(ClientError) as missing:
        ictx.c.storage.list_parts(original.bucket, original.object_key, original.s3_upload_id)
    assert missing.value.response["Error"]["Code"] == "NoSuchUpload"
    gone = ictx.client.get(f"/v1/uploads/{old['upload_id']}/parts", headers=headers)
    assert gone.status_code == 410 and gone.json()["code"] == "GONE"
    check(gone, "GET", "/v1/uploads/{upload_id}/parts")

    completed = ictx.client.post(f"/v1/uploads/{new['upload_id']}/complete", headers=headers,
                                 json={"parts": put_parts(new, data)})
    assert completed.status_code == 202, completed.text
    drain(ictx)
    receipt = ictx.client.get(f"/v1/assets/{new['asset_id']}/receipt", headers=headers)
    assert receipt.status_code == 200, receipt.text
    check(receipt, "GET", "/v1/assets/{asset_id}/receipt")
    assert receipt.json()["byte_size"] == len(data)
    assert receipt.json()["checksum_sha256"] == hashlib.sha256(data).hexdigest()
    assert _quota(ictx).reserved_bytes == 0


def test_reregister_expired_upload_retries_when_old_session_locked(ictx: Ctx) -> None:
    headers = ictx.as_user(102)
    data = mp4_bytes(128)
    payload = manifest(data)
    created = ictx.client.post("/v1/uploads", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    upload_id = created.json()["upload_id"]
    _expire(ictx, upload_id, 1)

    def limit_statement_time(conn: Connection) -> None:
        conn.exec_driver_sql("SET LOCAL statement_timeout = '3s'")

    with ictx.c.sessionmaker.begin() as holder:
        old = holder.get(Upload, upload_id, with_for_update=True)
        assert old is not None
        event.listen(ictx.c.engine, "begin", limit_statement_time)
        try:
            retry = ictx.client.post("/v1/uploads", json=payload, headers=headers)
        finally:
            event.remove(ictx.c.engine, "begin", limit_statement_time)
    assert retry.status_code == 409, retry.text
    assert retry.json()["code"] == "CONFLICT"
    check(retry, "POST", "/v1/uploads")
    assert _upload(ictx, upload_id).status == "UPLOADING"
    assert _quota(ictx).reserved_bytes == len(data)
    assert ictx.c.storage.list_parts(old.bucket, old.object_key, old.s3_upload_id) == []

    replacement = ictx.client.post("/v1/uploads", json=payload, headers=headers)
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["upload_id"] != upload_id
    assert _quota(ictx).reserved_bytes == len(data)


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
        released = real(self, up, final)
        if up.upload_id == bad["upload_id"]:
            raise RuntimeError("합성 실패")
        return released

    monkeypatch.setattr(IngestService, "_release", failing)
    assert sweep(ictx.c) == 1
    assert _upload(ictx, bad["upload_id"]).status == "UPLOADING"  # 실패한 세션은 되돌려져 다음 주기에 다시 시도
    assert _upload(ictx, good["upload_id"]).status == "EXPIRED"
    assert _quota(ictx).reserved_bytes == 5 * MB
    monkeypatch.setattr(IngestService, "_release", real)
    assert sweep(ictx.c) == 1
    assert _upload(ictx, bad["upload_id"]).status == "EXPIRED"
    assert _quota(ictx).reserved_bytes == 0


def test_sweep_does_not_hold_quota_lock_during_storage_calls(ictx, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """만료 정리기는 저장소를 부르는 동안 기관 쿼터 행을 잠그고 있지 않는다 (검토 2026-11-12 C8, ADR-0059).

    한 트랜잭션에서 여러 세션을 정리하면 첫 세션의 예약 해제가 잡은 쿼터 행 잠금을 다음 세션들의 저장소 호출 내내
    쥐고 있어, 그동안 같은 기관의 새 업로드 예약이 기다린다. 세션마다 짧은 트랜잭션으로 확정해야 한다."""
    from app.workers.main import sweep

    h = ictx.as_user(102)
    ids = []
    for i in range(3):
        r = ictx.client.post("/v1/uploads", json=manifest(mp4_bytes(64, seed=1300 + i)), headers=h)
        assert r.status_code == 201, r.text
        ids.append(r.json()["upload_id"])
    for n, upload_id in enumerate(ids):
        _expire(ictx, upload_id, 3 - n)  # 오래된 것부터 정리된다
    real = ictx.c.storage.abort_multipart_upload
    quota_row_free: list[bool] = []

    def probing_abort(bucket: str, key: str, upload_id: str) -> None:
        with ictx.c.engine.connect() as conn:  # 다른 연결에서 쿼터 행을 NOWAIT로 잠가 본다
            try:
                conn.execute(text(f"SELECT 1 FROM {OrgQuota.__tablename__} WHERE organization_id = :o "
                                  "FOR UPDATE NOWAIT"), {"o": ORG_A})
                quota_row_free.append(True)
            except DBAPIError:
                quota_row_free.append(False)
            finally:
                conn.rollback()
        real(bucket, key, upload_id)

    monkeypatch.setattr(ictx.c.storage, "abort_multipart_upload", probing_abort)
    assert sweep(ictx.c) == 3
    assert quota_row_free == [True, True, True], "앞 세션의 예약 해제가 잡은 쿼터 행 잠금을 저장소 호출 중에 쥐고 있다"
    assert [_upload(ictx, u).status for u in ids] == ["EXPIRED"] * 3
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


@pytest.mark.parametrize("contender", ["cancel", "sweep", "lazy-expiry"])
def test_completion_serializes_with_upload_cleanup(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contender: str,
) -> None:
    from app.workers.main import sweep

    clock = FixedClock(ictx.c.clock.now())
    monkeypatch.setattr(ictx.c, "clock", clock)
    monkeypatch.setattr(ictx.c.settings, "work_dir", str(tmp_path))
    headers = ictx.as_user(102)
    data = mp4_bytes(128, seed=701)
    response = ictx.client.post("/v1/uploads", json=manifest(data), headers=headers)
    assert response.status_code == 201, response.text
    created = response.json()
    path = f"/v1/uploads/{created['upload_id']}"
    original = _upload(ictx, created["upload_id"])
    parts = put_parts(created, data)
    if contender == "sweep":
        other = ictx.client.post("/v1/uploads", json=manifest(mp4_bytes(128, seed=702)), headers=headers)
        assert other.status_code == 201, other.text
        other_upload_id = other.json()["upload_id"]

    storage_completed, finish_db = Event(), Event()
    complete_storage = ictx.c.storage.complete_multipart_upload

    def pause_after_storage_complete(bucket: str, key: str, upload_id: str, uploaded: list[CompletedPart]) -> None:
        complete_storage(bucket, key, upload_id, uploaded)
        storage_completed.set()
        assert finish_db.wait(15), "DB 완료 재개를 기다리다 시간이 지났습니다"

    monkeypatch.setattr(ictx.c.storage, "complete_multipart_upload", pause_after_storage_complete)
    with ThreadPoolExecutor(max_workers=2) as pool:
        completing = pool.submit(ictx.client.post, f"{path}/complete", headers=headers, json={"parts": parts})
        try:
            assert storage_completed.wait(5), "스토리지 완료 단계에 도달하지 못했습니다"
            if contender != "cancel":
                clock.advance(seconds=ictx.c.settings.upload_session_ttl_s + 1)
                assert original.expires_at < clock.now()
            if contender == "sweep":
                # 잠긴 세션을 기다리지 않고 만료된 다른 세션만 정리해야 한다.
                swept = pool.submit(sweep, ictx.c).result(timeout=5)
            else:
                method, cleanup_path = ("DELETE", path) if contender == "cancel" else ("GET", f"{path}/parts")
                cleaning = pool.submit(ictx.client.request, method, cleanup_path, headers=headers)
                blocked = False
                deadline = monotonic() + 5
                with ictx.c.engine.connect() as conn:
                    while not cleaning.done():
                        blocked = bool(conn.scalar(text("""
                            SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = current_database()
                                AND pid <> pg_backend_pid() AND cardinality(pg_blocking_pids(pid)) > 0)
                        """)))
                        conn.rollback()  # 다음 조회는 최신 잠금 상태를 본다.
                        if blocked:
                            break
                        assert monotonic() < deadline, "정리 요청이 DB 잠금에 도달하지 못했습니다"
                        sleep(0.02)
        finally:
            finish_db.set()
        completed = completing.result(timeout=10)
        if contender != "sweep":
            cleanup = cleaning.result(timeout=10)

    stored = ictx.c.storage.head_object(original.bucket, original.object_key)
    assert stored is not None and stored.size == len(data)
    with ictx.c.sessionmaker() as db:
        upload = db.get(Upload, original.upload_id)
        asset = db.get(Asset, created["asset_id"])
        quota = db.get(OrgQuota, ORG_A)
        state = (upload.status, asset.ingest_status if asset else None, upload.reserved_bytes, quota.reserved_bytes)
        cleanup_result = swept if contender == "sweep" else cleanup.status_code
        assert state == ("RECEIVED", "RECEIVED", len(data), len(data)), (state, completed.status_code, cleanup_result)
        if contender == "sweep":
            assert swept == 1 and db.get(Upload, other_upload_id).status == "EXPIRED"
    assert completed.status_code == 202, completed.text
    check(completed, "POST", "/v1/uploads/{upload_id}/complete")
    if contender != "sweep":
        assert blocked and cleanup.status_code == 409, cleanup.text
        assert cleanup.json()["code"] == "CONFLICT"
        template = "/v1/uploads/{upload_id}" if contender == "cancel" else "/v1/uploads/{upload_id}/parts"
        check(cleanup, method, template)

    drain(ictx)
    receipt = ictx.client.get(f"/v1/assets/{created['asset_id']}/receipt", headers=headers)
    assert receipt.status_code == 200 and receipt.json()["checksum_sha256"] == hashlib.sha256(data).hexdigest()
    quota = _quota(ictx)
    assert (quota.reserved_bytes, quota.used_bytes, quota.quarantine_bytes) == (0, len(data), 0)
