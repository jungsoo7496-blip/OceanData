"""검토 2026-11-12 C4 (ADR-0065): 업로드 취소·만료 정리가 저장소 오류를 삼키지 않고, 완료된 채 남은 객체를 지운다.

전에는 정리(만료 정리기·요청 중 만료·사용자 취소)가 `abort_multipart_upload` 실패를 기록만 하고 세션을 끝냈다 —
미완성 멀티파트가 저장소에 남고 다시 정리하지 않았다. 또 저장소에서 멀티파트를 완료한 뒤 DB 확정 전에 프로세스가 죽으면
세션은 UPLOADING인데 객체는 완성돼 있어, 만료·취소가 자산 기록만 지우고 객체를 남겼다(어느 자산도 가리키지 않고 쿼터에도
세지 않는 객체). 실제 S3 API(moto 또는 TEST_S3_ENDPOINT), 합성 값.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select, text

from app.db.models import Asset, Device, OrgQuota, Upload
from app.ports.storage import CompletedPart
from app.seed import DEV_A_CAMERA, ORG_A
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_ingest import MB, drain, manifest, mp4_bytes, put_parts
from tests.integration.test_upload_release import _upload_oversized_legacy_part

pytestmark = pytest.mark.integration
SIZE = 128
CANCEL = "/v1/uploads/{upload_id}"
PARTS = "/v1/uploads/{upload_id}/parts"
COMPLETE = "/v1/uploads/{upload_id}/complete"


def _start(ictx: Ctx, seed: int, payload: dict | None = None) -> tuple[dict, Upload, list[dict]]:
    data = mp4_bytes(SIZE, seed=seed)
    r = ictx.client.post("/v1/uploads", json=payload or manifest(data), headers=ictx.as_user(102))
    assert r.status_code == 201, r.text
    created = r.json()
    parts = put_parts(created, data)
    return created, _upload(ictx, created["upload_id"]), parts


def _upload(ictx: Ctx, upload_id: str) -> Upload:
    with ictx.c.sessionmaker() as db:
        up = db.get(Upload, upload_id)
        assert up is not None
        return up


def _expire(ictx: Ctx, upload_id: str) -> None:
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE uploads SET expires_at = now() - interval '1 minute' WHERE upload_id = :u"),
                     {"u": upload_id})


def _state(ictx: Ctx, created: dict) -> tuple[str, int, int, str | None]:
    """(세션 상태, 세션 예약, 기관 예약, 자산 수집 상태 — 자산 기록이 없으면 None)."""
    with ictx.c.sessionmaker() as db:
        up = db.get(Upload, created["upload_id"])
        quota = db.get(OrgQuota, ORG_A)
        asset = db.get(Asset, created["asset_id"])
        return up.status, up.reserved_bytes, quota.reserved_bytes, asset.ingest_status if asset else None


def _crash_after_storage_complete(ictx: Ctx, up: Upload, parts: list[dict]) -> None:
    """저장소에서 멀티파트를 완료하고 DB 확정 전에 프로세스가 죽은 상태 — DB는 UPLOADING, 저장소에는 완성 객체."""
    ictx.c.storage.complete_multipart_upload(up.bucket, up.object_key, up.s3_upload_id,
                                             [CompletedPart(p["part_number"], p["etag"]) for p in parts])
    assert ictx.c.storage.head_object(up.bucket, up.object_key) is not None


def _multipart_open(ictx: Ctx, up: Upload) -> bool:
    try:
        ictx.c.storage.list_parts(up.bucket, up.object_key, up.s3_upload_id)
    except ClientError as exc:
        assert exc.response["Error"]["Code"] == "NoSuchUpload"
        return False
    return True


def _break_abort(ictx: Ctx, patch: pytest.MonkeyPatch, failure: str) -> None:
    """저장소 장애를 흉내 낸다. timeout은 어댑터 밖의 예외, s3-error는 저장소가 돌려준 NoSuchUpload가 아닌 오류."""
    if failure == "timeout":
        def timeout(*_args: Any) -> None:
            raise TimeoutError("합성 저장소 응답 없음")

        patch.setattr(ictx.c.storage, "abort_multipart_upload", timeout)
    else:
        def slow_down(**_kw: Any) -> None:
            raise ClientError({"Error": {"Code": "SlowDown", "Message": "합성"}}, "AbortMultipartUpload")

        patch.setattr(ictx.c.storage.client, "abort_multipart_upload", slow_down)


def _sweep(ictx: Ctx) -> int:
    from app.workers.main import sweep

    return sweep(ictx.c)


def _timeout(*_args: Any) -> None:
    raise TimeoutError("합성 저장소 응답 없음")


# ---------------------------------------------------------------- 1. 멀티파트 정리 실패는 세션을 끝내지 않는다
@pytest.mark.parametrize("failure", ["timeout", "s3-error"])
def test_sweep_keeps_the_session_when_abort_fails_and_retries_next_cycle(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str,
) -> None:
    created, up, _ = _start(ictx, 6501)
    _expire(ictx, up.upload_id)
    with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger="app.services.ingest"):
        _break_abort(ictx, patch, failure)
        swept = _sweep(ictx)
    assert swept == 0, "정리하지 못한 세션을 정리했다고 셌다"
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")  # 예약·자산 기록을 그대로 둔다
    assert _multipart_open(ictx, up)
    failed = [r for r in caplog.records if r.getMessage() == "expired_upload_release_failed"]
    assert len(failed) == 1 and failed[0].levelno == logging.ERROR
    assert failed[0].upload_id == up.upload_id  # type: ignore[attr-defined]
    assert failed[0].error_type == ("TimeoutError" if failure == "timeout" else "ClientError")  # type: ignore[attr-defined]
    for record in caplog.records:  # 정리 로그에 저장소 주소·멀티파트 ID를 남기지 않는다
        if record.name != "app.services.ingest":
            continue
        logged = str(record.__dict__)
        assert "http" not in logged and up.s3_upload_id not in logged

    assert _sweep(ictx) == 1  # 다음 주기가 다시 시도한다
    assert _state(ictx, created) == ("EXPIRED", 0, 0, None)
    assert not _multipart_open(ictx, up)


def test_sweep_treats_a_missing_multipart_as_already_cleaned(ictx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """저장소가 NoSuchUpload를 돌려주면(이미 정리됐다) 세션을 끝낸다."""
    created, up, _ = _start(ictx, 6502)
    _expire(ictx, up.upload_id)
    real = ictx.c.storage.client.abort_multipart_upload

    def gone(**kw: Any) -> None:
        real(**kw)
        raise ClientError({"Error": {"Code": "NoSuchUpload", "Message": "합성"}}, "AbortMultipartUpload")

    monkeypatch.setattr(ictx.c.storage.client, "abort_multipart_upload", gone)
    assert _sweep(ictx) == 1
    assert _state(ictx, created) == ("EXPIRED", 0, 0, None)


@pytest.mark.parametrize("failure", ["timeout", "s3-error"])
def test_cancel_answers_503_while_abort_fails(ictx: Ctx, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    created, up, _ = _start(ictx, 6503)
    path = CANCEL.format(upload_id=up.upload_id)
    with monkeypatch.context() as patch:
        _break_abort(ictx, patch, failure)
        r = ictx.client.delete(path, headers=ictx.as_user(102))
    assert r.status_code == 503, r.text  # 전: 204 — 멀티파트는 남았는데 취소됐다고 답했다
    assert r.json()["code"] == "STORAGE_UNAVAILABLE" and r.headers["Retry-After"] == "30"
    check(r, "DELETE", CANCEL)
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")
    assert _multipart_open(ictx, up)

    again = ictx.client.delete(path, headers=ictx.as_user(102))
    assert again.status_code == 204, again.text
    assert _state(ictx, created) == ("ABORTED", 0, 0, None)
    assert not _multipart_open(ictx, up)


@pytest.mark.parametrize(("method", "template"), [("GET", PARTS), ("POST", COMPLETE)], ids=["parts", "complete"])
def test_lazy_expiry_answers_503_while_abort_fails(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch, method: str, template: str,
) -> None:
    created, up, parts = _start(ictx, 6504)
    _expire(ictx, up.upload_id)
    path = template.format(upload_id=up.upload_id)
    body = {"parts": parts} if method == "POST" else None
    with monkeypatch.context() as patch:
        _break_abort(ictx, patch, "timeout")
        r = ictx.client.request(method, path, json=body, headers=ictx.as_user(102))
    assert r.status_code == 503, r.text  # 전: 410 — 정리하지 못했는데 정리했다고 확정했다
    assert r.json()["code"] == "STORAGE_UNAVAILABLE" and r.headers["Retry-After"] == "30"
    check(r, method, template)
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")
    assert _multipart_open(ictx, up)

    gone = ictx.client.request(method, path, json=body, headers=ictx.as_user(102))
    assert gone.status_code == 410 and gone.json()["code"] == "GONE", gone.text
    check(gone, method, template)
    assert _state(ictx, created) == ("EXPIRED", 0, 0, None)
    assert not _multipart_open(ictx, up)


@pytest.mark.parametrize("cleanup", ["sweep", "cancel"])
def test_object_check_failure_keeps_the_session_and_the_multipart(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, cleanup: str,
) -> None:
    """완성 객체 확인(head_object)이 실패해도 정리했다고 보지 않는다. 확인은 멀티파트 중단보다 먼저라 저장소도 그대로다."""
    created, up, _ = _start(ictx, 6505)
    path = CANCEL.format(upload_id=up.upload_id)
    if cleanup == "sweep":
        _expire(ictx, up.upload_id)
    with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger="app.services.ingest"):
        patch.setattr(ictx.c.storage, "head_object", _timeout)
        if cleanup == "sweep":
            assert _sweep(ictx) == 0
        else:
            r = ictx.client.delete(path, headers=ictx.as_user(102))
            assert r.status_code == 503 and r.json()["code"] == "STORAGE_UNAVAILABLE", r.text
            assert r.headers["Retry-After"] == "30"
            check(r, "DELETE", CANCEL)
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")
    assert _multipart_open(ictx, up), "확인하지 못했는데 멀티파트를 중단했다"
    failed = [rec for rec in caplog.records if rec.getMessage() == "upload_cleanup_failed"]
    assert [(rec.step, rec.error_type) for rec in failed] == [("head_object", "TimeoutError")]  # type: ignore[attr-defined]

    if cleanup == "sweep":
        assert _sweep(ictx) == 1
    else:
        assert ictx.client.delete(path, headers=ictx.as_user(102)).status_code == 204
    assert _state(ictx, created) == ("EXPIRED" if cleanup == "sweep" else "ABORTED", 0, 0, None)
    assert not _multipart_open(ictx, up)


# ---------------------------------------------------------------- 2. 완료된 채 남은 객체
@pytest.mark.parametrize("cleanup", ["sweep", "cancel", "parts", "complete", "reregister"])
def test_cleanup_removes_the_object_left_by_a_crashed_complete(ictx: Ctx, cleanup: str) -> None:
    data = mp4_bytes(SIZE, seed=6510)
    created, up, parts = _start(ictx, 6510)
    _crash_after_storage_complete(ictx, up, parts)
    h = ictx.as_user(102)
    if cleanup != "cancel":
        _expire(ictx, up.upload_id)
    if cleanup == "sweep":
        assert _sweep(ictx) == 1
    elif cleanup == "cancel":
        r = ictx.client.delete(CANCEL.format(upload_id=up.upload_id), headers=h)
        assert r.status_code == 204, r.text
    elif cleanup == "parts":
        r = ictx.client.get(PARTS.format(upload_id=up.upload_id), headers=h)
        assert r.status_code == 410, r.text
    elif cleanup == "complete":
        r = ictx.client.post(COMPLETE.format(upload_id=up.upload_id), json={"parts": parts}, headers=h)
        assert r.status_code == 410, r.text
    else:
        r = ictx.client.post("/v1/uploads", json=manifest(data), headers=h)
        assert r.status_code == 201 and r.json()["upload_id"] != up.upload_id, r.text
        check(r, "POST", "/v1/uploads")
        assert r.json()["asset_id"] != created["asset_id"]

    assert ictx.c.storage.head_object(up.bucket, up.object_key) is None, "어느 자산도 가리키지 않는 객체가 남았다"
    final = "ABORTED" if cleanup == "cancel" else "EXPIRED"
    reserved = SIZE if cleanup == "reregister" else 0  # 다시 신고한 새 세션의 예약만 남는다
    assert _state(ictx, created) == (final, 0, reserved, None)
    with ictx.c.sessionmaker() as db:
        assert db.get(OrgQuota, ORG_A).used_bytes == 0


@pytest.mark.parametrize("cleanup", ["sweep", "cancel"])
def test_completed_object_that_cannot_be_deleted_keeps_the_session(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch, cleanup: str,
) -> None:
    created, up, parts = _start(ictx, 6520)
    _crash_after_storage_complete(ictx, up, parts)
    path = CANCEL.format(upload_id=up.upload_id)
    if cleanup == "sweep":
        _expire(ictx, up.upload_id)

    def unavailable(*_args: Any) -> None:
        raise TimeoutError("합성 객체 삭제 실패")

    with monkeypatch.context() as patch:
        patch.setattr(ictx.c.storage, "delete_object", unavailable)
        if cleanup == "sweep":
            assert _sweep(ictx) == 0
        else:
            r = ictx.client.delete(path, headers=ictx.as_user(102))
            assert r.status_code == 503 and r.headers["Retry-After"] == "30", r.text
            check(r, "DELETE", CANCEL)
    assert ictx.c.storage.head_object(up.bucket, up.object_key) is not None
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")  # 다시 시도할 수 있게 그대로 둔다

    if cleanup == "sweep":
        assert _sweep(ictx) == 1
    else:
        assert ictx.client.delete(path, headers=ictx.as_user(102)).status_code == 204
    assert ictx.c.storage.head_object(up.bucket, up.object_key) is None
    assert _state(ictx, created)[0] == ("EXPIRED" if cleanup == "sweep" else "ABORTED")


def test_delete_that_raised_after_removing_the_object_counts_as_removed(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """삭제 호출이 예외를 냈어도 객체가 실제로 없으면 지운 것이다 (retention._purge_object와 같은 기준, ADR-0055)."""
    created, up, parts = _start(ictx, 6521)
    _crash_after_storage_complete(ictx, up, parts)
    real = ictx.c.storage.delete_object

    def deleted_then_timeout(bucket: str, key: str) -> None:
        real(bucket, key)
        raise TimeoutError("합성: 지웠는데 응답을 받지 못했다")

    monkeypatch.setattr(ictx.c.storage, "delete_object", deleted_then_timeout)
    r = ictx.client.delete(CANCEL.format(upload_id=up.upload_id), headers=ictx.as_user(102))
    assert r.status_code == 204, r.text
    assert ictx.c.storage.head_object(up.bucket, up.object_key) is None
    assert _state(ictx, created) == ("ABORTED", 0, 0, None)


def test_delete_failure_that_cannot_be_checked_keeps_the_session(ictx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """삭제가 실패하고 남았는지 확인하는 head_object도 실패하면 지웠다고 보지 않는다 — 503, 세션·예약 유지."""
    created, up, parts = _start(ictx, 6522)
    _crash_after_storage_complete(ictx, up, parts)
    path = CANCEL.format(upload_id=up.upload_id)
    real_head = ictx.c.storage.head_object
    heads: list[str] = []

    def head_then_timeout(bucket: str, key: str) -> Any:
        heads.append(key)
        if len(heads) > 1:  # 첫 확인(완성 객체 있음)은 통과하고, 삭제 실패 뒤의 확인이 실패한다
            raise TimeoutError("합성 저장소 응답 없음")
        return real_head(bucket, key)

    with monkeypatch.context() as patch:
        patch.setattr(ictx.c.storage, "delete_object", _timeout)
        patch.setattr(ictx.c.storage, "head_object", head_then_timeout)
        r = ictx.client.delete(path, headers=ictx.as_user(102))
    assert r.status_code == 503 and r.headers["Retry-After"] == "30", r.text
    check(r, "DELETE", CANCEL)
    assert len(heads) == 2
    assert ictx.c.storage.head_object(up.bucket, up.object_key) is not None
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")

    assert ictx.client.delete(path, headers=ictx.as_user(102)).status_code == 204
    assert ictx.c.storage.head_object(up.bucket, up.object_key) is None
    assert _state(ictx, created) == ("ABORTED", 0, 0, None)


def test_cancel_of_a_c0_upload_removes_the_completed_object_and_keeps_the_metadata_record(ictx: Ctx) -> None:
    """바이너리 없는 C0 자산에서 연 세션도 같다. 자산은 바이너리 없는 기록으로 돌아가므로 객체를 남기면 고아가 된다."""
    h = ictx.as_user(102)
    data = mp4_bytes(SIZE, seed=6530)
    payload = manifest(data, variant="ORIGINAL")
    with ictx.c.sessionmaker.begin() as db:
        db.get(Device, DEV_A_CAMERA).collection_mode = "C0_METADATA_ONLY"
    registered = ictx.client.post("/v1/uploads", json=payload, headers=h)
    assert registered.status_code == 201 and registered.json()["status"] == "METADATA_ONLY_REGISTERED", registered.text
    with ictx.c.sessionmaker.begin() as db:
        db.get(Device, DEV_A_CAMERA).collection_mode = "C3_APPROVED_FULL"
    created, up, parts = _start(ictx, 6530, payload)
    assert created["asset_id"] == registered.json()["asset_id"]
    _crash_after_storage_complete(ictx, up, parts)

    r = ictx.client.delete(CANCEL.format(upload_id=up.upload_id), headers=h)
    assert r.status_code == 204, r.text
    assert ictx.c.storage.head_object(up.bucket, up.object_key) is None
    assert _state(ictx, created) == ("ABORTED", 0, 0, "READY_PRIVATE")
    with ictx.c.sessionmaker() as db:
        asset = db.get(Asset, created["asset_id"])
        assert (asset.has_binary, asset.storage_zone, asset.storage_key) == (False, None, None)


def test_object_that_a_live_asset_points_to_is_not_deleted(ictx: Ctx) -> None:
    """정리는 살아 있는 자산이 가리키는 객체를 지우지 않는다 (그 객체는 그 자산의 것이고 쿼터에도 세어져 있다)."""
    h = ictx.as_user(102)
    other = mp4_bytes(SIZE, seed=6541)
    r = ictx.client.post("/v1/uploads", json=manifest(other), headers=h)
    assert r.status_code == 201, r.text
    registered = r.json()
    done = ictx.client.post(COMPLETE.format(upload_id=registered["upload_id"]),
                            json={"parts": put_parts(registered, other)}, headers=h)
    assert done.status_code == 202, done.text
    drain(ictx)
    created, up, parts = _start(ictx, 6540)
    _crash_after_storage_complete(ictx, up, parts)
    with ictx.c.sessionmaker.begin() as db:  # 합성: 등록된 다른 자산이 같은 키를 가리킨다
        live = db.get(Asset, registered["asset_id"])
        assert live.ingest_status == "REGISTERED" and live.storage_zone == "PROCESSED"
        live.storage_key = up.object_key
    _expire(ictx, up.upload_id)

    assert _sweep(ictx) == 1
    assert ictx.c.storage.head_object(up.bucket, up.object_key) is not None
    assert _state(ictx, created) == ("EXPIRED", 0, 0, None)
    with ictx.c.sessionmaker() as db:
        assert db.scalar(select(Asset.storage_key).where(Asset.asset_id == registered["asset_id"])) == up.object_key
        assert db.get(Asset, registered["asset_id"]).checksum_sha256 == hashlib.sha256(other).hexdigest()


# ---------------------------------------------------------------- 3. 완료 요청의 크기 불일치 취소
def test_size_mismatch_cancel_finishes_on_retry_after_a_failed_object_check(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """크기 불일치 취소에서 완성 객체 확인이 실패하면 503이고 멀티파트를 그대로 둔다 — 완료를 다시 보내면 취소를 마친다.

    확인을 멀티파트 중단 뒤에 하면, 확인 실패의 503 뒤에 다시 보낸 완료는 이미 중단된 멀티파트의 파트 목록을 조회하다
    (moto는 NoSuchUpload) 503만 받았다. 세션은 유효 기간(7일)이 끝날 때까지 예약을 쥔 채 남았다(검토 지적)."""
    h = ictx.as_user(102)
    cr, up, etag = _upload_oversized_legacy_part(ictx, h)
    path = COMPLETE.format(upload_id=up.upload_id)
    body = {"parts": [{"part_number": 1, "etag": etag}]}
    real_list, real_head = ictx.c.storage.list_parts, ictx.c.storage.head_object
    listed: list[str] = []
    failed: list[str] = []

    def listing(*args: Any) -> list[CompletedPart]:
        listed.append(args[1])
        return real_list(*args)

    def head_fails_once_after_listing(bucket: str, key: str) -> Any:
        if listed and not failed:  # 파트 크기를 확인한 뒤의 첫 객체 확인(취소 정리 안)이 한 번 실패한다
            failed.append(key)
            raise TimeoutError("합성 저장소 응답 없음")
        return real_head(bucket, key)

    with monkeypatch.context() as patch:
        patch.setattr(ictx.c.storage, "list_parts", listing)
        patch.setattr(ictx.c.storage, "head_object", head_fails_once_after_listing)
        first = ictx.client.post(path, json=body, headers=h)
    assert first.status_code == 503 and first.json()["code"] == "STORAGE_UNAVAILABLE", first.text
    assert first.headers["Retry-After"] == "30" and failed == [up.object_key]
    check(first, "POST", COMPLETE)
    assert _state(ictx, cr) == ("UPLOADING", 1, 1, "UPLOADING")
    assert _multipart_open(ictx, up), "확인하지 못했는데 멀티파트를 중단했다 — 완료 재시도가 이 취소에 다시 닿지 못한다"

    retry = ictx.client.post(path, json=body, headers=h)
    assert retry.status_code == 409 and retry.json()["code"] == "CONFLICT", retry.text
    assert retry.json()["size_mismatch"] == [{"part_number": 1, "expected_size": 1, "actual_size": 6 * MB}]
    check(retry, "POST", COMPLETE)
    assert _state(ictx, cr) == ("ABORTED", 0, 0, None)
    assert not _multipart_open(ictx, up)
    assert ictx.client.post(path, json=body, headers=h).status_code == 410
