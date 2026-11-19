"""통합 검토 UPLOADS-1 (ADR-0084): 503으로 끝난 완료를 저장소가 늦게 마친 경우의 정리.

UPLOADS-1: 완료 요청의 저장소 완료 호출이 시간 초과로 503이 되면(ADR-0082) 요청 트랜잭션이 되돌려져 업로드 행 잠금이 풀린다.
저장소는 그 완료를 그 뒤에도 마칠 수 있다. 정리(취소·만료 정리기·자산 삭제)의 완성 객체 확인(HEAD)과 멀티파트 중단 사이에 그
완료가 끝나면, 중단은 NoSuchUpload(성공으로 본다)라 정리는 객체가 없다고 보고 세션을 끝냈다. 완성 객체는 어느 자산에도 속하지
않고 쿼터에도 세지 않은 채 남았고, 삭제 경로는 그 바이트를 빼고 삭제 증명을 냈다.

실제 S3 API(moto 또는 TEST_S3_ENDPOINT), 합성 값.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from botocore.exceptions import ReadTimeoutError
from sqlalchemy import func, select

from app.db.models import Asset, DeletionCertificate, Upload
from app.ports.storage import CompletedPart
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_upload_cleanup_storage_errors import (
    CANCEL,
    COMPLETE,
    SIZE,
    _expire,
    _multipart_open,
    _start,
    _state,
    _sweep,
)

pytestmark = pytest.mark.integration
DELETION = "/v1/assets/{asset_id}/deletion"


def _complete_times_out(ictx: Ctx, up: Upload, parts: list[dict]) -> None:
    """완료 요청의 저장소 완료 호출이 시간 초과다(저장소는 아직 마치지 않았다) — 503, 세션 유지, 행 잠금이 풀린다."""
    def timeout(**_kw: Any) -> None:
        raise ReadTimeoutError(endpoint_url="http://127.0.0.1:1")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ictx.c.storage.client, "complete_multipart_upload", timeout)
        r = ictx.client.post(COMPLETE.format(upload_id=up.upload_id), json={"parts": parts},
                             headers=ictx.as_user(102))
    assert r.status_code == 503 and r.json()["code"] == "STORAGE_UNAVAILABLE", r.text
    assert ictx.c.storage.head_object(up.bucket, up.object_key) is None


def _completion_lands_after_first_check(ictx: Ctx, patch: pytest.MonkeyPatch, up: Upload,
                                        parts: list[dict]) -> list[str]:
    """정리의 첫 완성 객체 확인이 '없음'을 본 직후 저장소가 앞서 시간 초과된 완료를 마친다(확인과 중단 사이)."""
    real_head = ictx.c.storage.head_object
    heads: list[str] = []

    def head_then_storage_completes(bucket: str, key: str) -> Any:
        info = real_head(bucket, key)
        heads.append(key)
        if len(heads) == 1:
            assert info is None
            ictx.c.storage.complete_multipart_upload(bucket, key, up.s3_upload_id,
                                                     [CompletedPart(p["part_number"], p["etag"]) for p in parts])
        return info

    patch.setattr(ictx.c.storage, "head_object", head_then_storage_completes)
    return heads


# ---------------------------------------------------------------- UPLOADS-1
@pytest.mark.parametrize("cleanup", ["cancel", "sweep"])
def test_cleanup_removes_the_object_of_a_timed_out_complete_that_storage_finished_late(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch, cleanup: str,
) -> None:
    created, up, parts = _start(ictx, 6560)
    _complete_times_out(ictx, up, parts)
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")
    if cleanup == "sweep":
        _expire(ictx, up.upload_id)
    with monkeypatch.context() as patch:
        heads = _completion_lands_after_first_check(ictx, patch, up, parts)
        if cleanup == "sweep":
            assert _sweep(ictx) == 1
        else:
            r = ictx.client.delete(CANCEL.format(upload_id=up.upload_id), headers=ictx.as_user(102))
            assert r.status_code == 204, r.text

    assert ictx.c.storage.head_object(up.bucket, up.object_key) is None, "어느 자산도 가리키지 않는 객체가 남았다"
    assert _state(ictx, created) == ("EXPIRED" if cleanup == "sweep" else "ABORTED", 0, 0, None)
    assert heads == [up.object_key] * 2  # 처음 확인에서 없었으면 멀티파트 중단 뒤 한 번 더 확인한다


def test_asset_deletion_refuses_while_a_timed_out_complete_finishes_late(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """삭제 경로(preserve_asset)는 늦게 생긴 완성 객체를 지우지 않고 409다(ADR-0046). 그 객체를 뺀 삭제 증명을 내지 않는다."""
    created, up, parts = _start(ictx, 6561)
    _complete_times_out(ictx, up, parts)
    path = DELETION.format(asset_id=created["asset_id"])
    with monkeypatch.context() as patch:
        _completion_lands_after_first_check(ictx, patch, up, parts)
        r = ictx.client.post(path, json={"reason": "업로드 중 삭제 합성 시험"}, headers=ictx.as_user(101, mfa=True))
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT", r.text  # 전: 200, objects가 빈 삭제 증명
    check(r, "POST", DELETION)
    stored = ictx.c.storage.head_object(up.bucket, up.object_key)
    assert stored is not None and stored.size == SIZE
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")
    with ictx.c.sessionmaker() as db:
        assert db.get(Asset, created["asset_id"]).deleted_at is None
        assert db.scalar(select(func.count()).select_from(DeletionCertificate)) == 0

    # 다시 보낸 완료는 저장소의 완성 객체로 마친다 (ADR-0053·0082). 그 뒤 삭제는 보통 경로다
    retry = ictx.client.post(COMPLETE.format(upload_id=up.upload_id), json={"parts": parts},
                             headers=ictx.as_user(102))
    assert retry.status_code == 202 and retry.json()["ingest_status"] == "RECEIVED", retry.text


def test_failed_recheck_after_abort_keeps_the_session(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """중단 뒤의 다시 확인이 실패하면 완성 객체가 없다고 보지 않는다 — 503, 세션·예약 유지, 다시 보내면 이어서 정리한다."""
    created, up, _ = _start(ictx, 6562)
    path = CANCEL.format(upload_id=up.upload_id)
    real_head = ictx.c.storage.head_object
    heads: list[str] = []

    def second_head_times_out(bucket: str, key: str) -> Any:
        heads.append(key)
        if len(heads) == 2:
            raise TimeoutError("합성 저장소 응답 없음")
        return real_head(bucket, key)

    with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger="app.services.ingest"):
        patch.setattr(ictx.c.storage, "head_object", second_head_times_out)
        r = ictx.client.delete(path, headers=ictx.as_user(102))
    assert r.status_code == 503 and r.json()["code"] == "STORAGE_UNAVAILABLE", r.text  # 전: 204
    assert r.headers["Retry-After"] == "30"
    check(r, "DELETE", CANCEL)
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")
    assert not _multipart_open(ictx, up)  # 중단은 됐다 — 다시 보낸 취소는 NoSuchUpload를 정리된 것으로 보고 끝낸다
    failed = [rec for rec in caplog.records if rec.getMessage() == "upload_cleanup_failed"]
    assert [(rec.step, rec.error_type) for rec in failed] == [  # type: ignore[attr-defined]
        ("head_object_after_abort", "TimeoutError")]

    again = ictx.client.delete(path, headers=ictx.as_user(102))
    assert again.status_code == 204, again.text
    assert _state(ictx, created) == ("ABORTED", 0, 0, None)

