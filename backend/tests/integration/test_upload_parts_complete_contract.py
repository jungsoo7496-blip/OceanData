"""계약 점검 S45·S46·S47 (2026-11-12 업로드 묶음): 파트 목록·완료의 저장소 장애 응답과 파트 URL 수 한도.

S45: 재개 조회(`GET .../parts`)는 저장소에서 파트 목록을 읽지 못하면 503 STORAGE_UNAVAILABLE이다. 계약에 없던 응답이다
(계약은 검토 C4, ADR-0065에서 먼저 고쳤다 — 여기서는 그 문서화된 동작을 고정한다).
S46: 완료 요청에서 저장소의 멀티파트 완료 호출이 장애(연결 실패·시간 초과·저장소 내부 오류)로 실패하면, 방금 저장소 목록과
맞춰 본 파트 목록이 틀렸다는 409 CONFLICT가 아니라 503 STORAGE_UNAVAILABLE + Retry-After이고 세션·예약은 그대로다(ADR-0082).
저장소가 파트 목록을 거부한 경우(InvalidPart 등)만 409다.
S47: 파트 URL 목록(`parts`·`pending_parts`)은 응답마다 서버 한도(기본 1000개)까지만 담는다. 계약이 이를 설명한다.
실제 S3 API(moto 또는 TEST_S3_ENDPOINT), 합성 값.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
import yaml
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from sqlalchemy import text

from app.config import Settings
from app.db.models import Asset, OrgQuota, Upload
from app.seed import ORG_A
from app.services.contracts import contracts_dir
from tests.conftest import Ctx
from tests.contract.openapi_contract import assert_matches_contract as check
from tests.integration.test_ingest import MB, manifest, mp4_bytes, put_parts

pytestmark = pytest.mark.integration
SIZE = 128
PARTS = "/v1/uploads/{upload_id}/parts"
COMPLETE = "/v1/uploads/{upload_id}/complete"
MISMATCH = "파트 목록이 저장소와 맞지 않습니다 (GET .../parts로 확인)"


def _start(ictx: Ctx, seed: int) -> tuple[dict, Upload, list[dict]]:
    data = mp4_bytes(SIZE, seed=seed)
    r = ictx.client.post("/v1/uploads", json=manifest(data), headers=ictx.as_user(102))
    assert r.status_code == 201, r.text
    created = r.json()
    parts = put_parts(created, data)
    with ictx.c.sessionmaker() as db:
        up = db.get(Upload, created["upload_id"])
        assert up is not None
    return created, up, parts


def _state(ictx: Ctx, created: dict) -> tuple[str, int, int, str | None]:
    """(세션 상태, 세션 예약, 기관 예약, 자산 수집 상태)."""
    with ictx.c.sessionmaker() as db:
        up = db.get(Upload, created["upload_id"])
        quota = db.get(OrgQuota, ORG_A)
        asset = db.get(Asset, created["asset_id"])
        return up.status, up.reserved_bytes, quota.reserved_bytes, asset.ingest_status if asset else None


def _idempotency_records(ictx: Ctx) -> int:
    with ictx.c.engine.connect() as conn:
        return int(conn.scalar(text("SELECT count(*) FROM idempotency_keys")) or 0)


# ---------------------------------------------------------------- S45 파트 목록을 읽지 못하면 문서화된 503
def test_list_parts_answers_the_documented_503_while_storage_cannot_list(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, up, _ = _start(ictx, 8401)
    path = PARTS.format(upload_id=up.upload_id)

    def down(*_args: Any) -> None:
        raise ConnectionError("합성 저장소 연결 실패")

    with monkeypatch.context() as patch:
        patch.setattr(ictx.c.storage, "list_parts", down)
        r = ictx.client.get(path, headers=ictx.as_user(102))
    assert r.status_code == 503, r.text
    assert r.json()["code"] == "STORAGE_UNAVAILABLE" and r.headers["Retry-After"] == "30"
    check(r, "GET", PARTS)  # 전(계약 C4 반영 전): 계약에 503 응답이 없었다
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")  # 세션·예약은 그대로

    again = ictx.client.get(path, headers=ictx.as_user(102))
    assert again.status_code == 200, again.text
    check(again, "GET", PARTS)
    assert [p["part_number"] for p in again.json()["completed_parts"]] == [1]


# ---------------------------------------------------------------- S46 완료 호출의 저장소 장애는 503, 세션 유지
def _outage(kind: str) -> Exception:
    if kind == "endpoint":
        return EndpointConnectionError(endpoint_url="http://127.0.0.1:1")
    if kind == "read-timeout":
        return ReadTimeoutError(endpoint_url="http://127.0.0.1:1")
    return ClientError({"Error": {"Code": "InternalError", "Message": "합성"},
                        "ResponseMetadata": {"HTTPStatusCode": 500}}, "CompleteMultipartUpload")


@pytest.mark.parametrize(("kind", "error_type"), [
    ("connection", "ConnectionError"), ("endpoint", "EndpointConnectionError"),
    ("read-timeout", "ReadTimeoutError"), ("internal-error", "ClientError")])
def test_storage_outage_during_complete_is_503_and_keeps_the_session(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, kind: str, error_type: str,
) -> None:
    created, up, parts = _start(ictx, 8410 + len(kind))
    path = COMPLETE.format(upload_id=up.upload_id)
    headers = {**ictx.as_user(102), "Idempotency-Key": f"complete-{up.upload_id}"}
    with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger="app.services.ingest"):
        if kind == "connection":  # 어댑터 밖의 예외 (포트 구현이 무엇이든)
            def refused(*_args: Any) -> None:
                raise ConnectionError("합성 저장소 연결 실패")

            patch.setattr(ictx.c.storage, "complete_multipart_upload", refused)
        else:  # 실제 S3 어댑터 안에서 botocore가 내는 예외
            def failing(**_kw: Any) -> None:
                raise _outage(kind)

            patch.setattr(ictx.c.storage.client, "complete_multipart_upload", failing)
        r = ictx.client.post(path, json={"parts": parts}, headers=headers)
    assert r.status_code == 503, r.text  # 전: 409 CONFLICT '파트 목록이 저장소와 맞지 않습니다', Retry-After 없음
    assert r.json()["code"] == "STORAGE_UNAVAILABLE" and r.headers["Retry-After"] == "30"
    check(r, "POST", COMPLETE)
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")
    assert ictx.c.storage.head_object(up.bucket, up.object_key) is None
    assert len(ictx.c.storage.list_parts(up.bucket, up.object_key, up.s3_upload_id)) == 1  # 멀티파트도 그대로
    assert _idempotency_records(ictx) == 0  # 오류 응답은 멱등 기록으로 남지 않는다 (같은 키로 다시 보낼 수 있다)
    failed = [rec for rec in caplog.records if rec.getMessage() == "complete_multipart_failed"]
    assert len(failed) == 1 and failed[0].error_type == error_type  # type: ignore[attr-defined]
    assert "http" not in str(failed[0].__dict__)  # 저장소 주소는 로그에 남기지 않는다

    retry = ictx.client.post(path, json={"parts": parts}, headers=headers)
    assert retry.status_code == 202, retry.text
    assert "Idempotent-Replayed" not in retry.headers
    check(retry, "POST", COMPLETE)
    assert retry.json()["ingest_status"] == "RECEIVED"
    assert _state(ictx, created) == ("RECEIVED", SIZE, SIZE, "RECEIVED")


def test_complete_that_storage_finished_before_the_timeout_finishes_on_retry(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """저장소는 완료했는데 응답을 받지 못한 경우: 503 뒤 다시 보낸 완료가 저장소의 완성 객체로 마친다 (ADR-0053 경로)."""
    created, up, parts = _start(ictx, 8415)
    path = COMPLETE.format(upload_id=up.upload_id)
    real = ictx.c.storage.client.complete_multipart_upload

    def lost_response(**kw: Any) -> None:
        real(**kw)
        raise ReadTimeoutError(endpoint_url="http://127.0.0.1:1")

    with monkeypatch.context() as patch:
        patch.setattr(ictx.c.storage.client, "complete_multipart_upload", lost_response)
        r = ictx.client.post(path, json={"parts": parts}, headers=ictx.as_user(102))
    assert r.status_code == 503 and r.headers["Retry-After"] == "30", r.text  # 전: 409 CONFLICT
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")
    stored = ictx.c.storage.head_object(up.bucket, up.object_key)
    assert stored is not None and stored.size == SIZE

    retry = ictx.client.post(path, json={"parts": parts}, headers=ictx.as_user(102))
    assert retry.status_code == 202 and retry.json()["ingest_status"] == "RECEIVED", retry.text
    assert _state(ictx, created) == ("RECEIVED", SIZE, SIZE, "RECEIVED")


@pytest.mark.parametrize("code", ["InvalidPart", "InvalidPartOrder", "EntityTooSmall", "NoSuchUpload"])
def test_storage_rejecting_the_part_list_stays_409(
    ictx: Ctx, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, code: str,
) -> None:
    """저장소가 완료 요청의 파트 목록을 거부하면(장애가 아니다) 지금처럼 409 CONFLICT이고 세션은 그대로다."""
    created, up, parts = _start(ictx, 8420 + len(code))
    path = COMPLETE.format(upload_id=up.upload_id)

    def rejected(**_kw: Any) -> None:
        raise ClientError({"Error": {"Code": code, "Message": "합성"},
                           "ResponseMetadata": {"HTTPStatusCode": 404 if code == "NoSuchUpload" else 400}},
                          "CompleteMultipartUpload")

    with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger="app.services.ingest"):
        patch.setattr(ictx.c.storage.client, "complete_multipart_upload", rejected)
        r = ictx.client.post(path, json={"parts": parts}, headers=ictx.as_user(102))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "CONFLICT" and r.json()["detail"] == MISMATCH
    assert "Retry-After" not in r.headers
    check(r, "POST", COMPLETE)
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")
    rejections = [rec for rec in caplog.records if rec.getMessage() == "complete_multipart_rejected"]
    assert len(rejections) == 1 and rejections[0].error_code == code  # type: ignore[attr-defined]

    retry = ictx.client.post(path, json={"parts": parts}, headers=ictx.as_user(102))
    assert retry.status_code == 202, retry.text


def test_wrong_etag_is_still_409_before_storage_is_asked(ictx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """저장소 목록과 다른 ETag는 완료 호출 전에 409다 (완료 호출은 부르지 않는다)."""
    created, up, parts = _start(ictx, 8430)
    calls: list[str] = []
    real = ictx.c.storage.complete_multipart_upload

    def counted(*args: Any) -> None:
        calls.append(args[2])
        real(*args)

    monkeypatch.setattr(ictx.c.storage, "complete_multipart_upload", counted)
    wrong = [{"part_number": 1, "etag": '"' + "0" * 32 + '"'}]
    r = ictx.client.post(COMPLETE.format(upload_id=up.upload_id), json={"parts": wrong}, headers=ictx.as_user(102))
    assert r.status_code == 409 and r.json()["detail"] == MISMATCH, r.text
    assert calls == []
    assert _state(ictx, created) == ("UPLOADING", SIZE, SIZE, "UPLOADING")
    ok = ictx.client.post(COMPLETE.format(upload_id=up.upload_id), json={"parts": parts}, headers=ictx.as_user(102))
    assert ok.status_code == 202, ok.text


# ---------------------------------------------------------------- S47 파트 URL 수 한도는 계약에 적힌 대로
def test_part_url_lists_are_capped_as_the_contract_documents(ictx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = yaml.safe_load((contracts_dir() / "openapi.yaml").read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]
    default_cap = Settings.model_fields["max_part_urls_per_response"].default
    documented = {
        "createUpload": spec["paths"]["/v1/uploads"]["post"]["description"],
        "UploadCreated.parts": schemas["UploadCreated"]["properties"]["parts"].get("description", ""),
        "PartList.pending_parts": schemas["PartList"]["properties"]["pending_parts"].get("description", ""),
    }
    for where, words in documented.items():  # 전: 한도도, 남은 URL을 다시 받는 방법도 적혀 있지 않았다
        assert f"{default_cap}개" in words and "GET /v1/uploads/{upload_id}/parts" in words, where
    completed = schemas["PartList"]["properties"]["completed_parts"].get("description", "")
    assert "잘리지 않" in completed  # 완료된 파트는 한도 없이 모두 돌려준다

    monkeypatch.setattr(ictx.c.settings, "max_part_urls_per_response", 2)
    h = ictx.as_user(102)
    data = mp4_bytes(3 * 5 * MB + 100, seed=8440)  # 5 MiB 파트 4개
    r = ictx.client.post("/v1/uploads", json=manifest(data), headers=h)
    assert r.status_code == 201, r.text
    check(r, "POST", "/v1/uploads")
    created = r.json()
    assert created["part_count"] == 4
    assert [p["part_number"] for p in created["parts"]] == [1, 2]  # 낮은 번호부터 한도까지
    first = put_parts(created, data, only={1})

    path = PARTS.format(upload_id=created["upload_id"])
    listed = ictx.client.get(path, headers=h)
    check(listed, "GET", PARTS)
    assert [p["part_number"] for p in listed.json()["completed_parts"]] == [1]
    assert [p["part_number"] for p in listed.json()["pending_parts"]] == [2, 3]  # 남은 파트 중 낮은 번호부터
    middle = put_parts(listed.json(), data)

    listed = ictx.client.get(path, headers=h)
    assert [p["part_number"] for p in listed.json()["completed_parts"]] == [1, 2, 3]
    assert [p["part_number"] for p in listed.json()["pending_parts"]] == [4]
    last = put_parts(listed.json(), data)

    done = ictx.client.post(COMPLETE.format(upload_id=created["upload_id"]), json={"parts": first + middle + last},
                            headers=h)
    assert done.status_code == 202, done.text
