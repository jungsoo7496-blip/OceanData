"""08번 요구사항 1.3·1.4: 설정에 따라 서버 측 암호화 요청, 지원하지 않으면 기동 경고와 /readyz 상세 표시."""

from __future__ import annotations

import dataclasses

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import storage_check
from tests.contract.openapi_contract import assert_matches_contract as check

pytestmark = pytest.mark.integration


def _with_sse(ctx):  # type: ignore[no-untyped-def]
    return dataclasses.replace(ctx.c, settings=ctx.c.settings.model_copy(update={"s3_sse": "AES256"}))


def test_sse_requested_and_supported(ictx) -> None:
    c = _with_sse(ictx)
    for bucket in c.settings.buckets.values():
        assert c.storage.encryption_status(bucket) == "disabled"  # 아직 요청 전
        assert c.storage.ensure_encryption(bucket) == "enabled"
    assert storage_check.encryption_warnings(c, refresh=True) == []
    with TestClient(create_app(c)) as client:
        r = client.get("/readyz")
        check(r, "GET", "/readyz")
        assert r.status_code == 200 and "warnings" not in r.json()


def test_sse_unsupported_warns_at_startup_and_in_readyz(ictx, monkeypatch) -> None:
    c = _with_sse(ictx)

    def not_implemented(**_kw):  # type: ignore[no-untyped-def]
        raise ClientError({"Error": {"Code": "NotImplemented", "Message": "x"}}, "GetBucketEncryption")

    monkeypatch.setattr(c.storage.client, "get_bucket_encryption", not_implemented)
    monkeypatch.setattr(c.storage.client, "put_bucket_encryption", not_implemented)
    assert c.storage.ensure_encryption(c.settings.bucket_raw) == "unsupported"
    warned: list[str] = []
    monkeypatch.setattr(storage_check.log, "warning", lambda msg, **_kw: warned.append(msg))
    app = create_app(c)  # 기동 경고
    assert warned == ["storage_encryption_unsupported"]
    with TestClient(app) as client:
        r = client.get("/readyz")
        check(r, "GET", "/readyz")
        body = r.json()
        assert r.status_code == 200 and body["status"] == "ready"  # 준비 상태는 바꾸지 않는다
        assert "storage_encryption_unsupported:RAW" in body["warnings"] and len(body["warnings"]) == 5
        assert "http" not in r.text  # 주소 노출 없음
    # 설정이 none이면 확인하지 않는다
    assert storage_check.encryption_warnings(ictx.c, refresh=True) == []
