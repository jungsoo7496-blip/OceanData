"""지시서 5번 검토 K3 (ADR-0053): 완료 요청이 저장소에서 멀티파트를 끝낸 뒤 DB 커밋이 실패해도 같은 요청의 재시도가 끝낸다.

커밋을 응답 앞으로 옮기면서 커밋 실패가 클라이언트에 오류로 보이게 됐다. `complete`는 저장소(CompleteMultipartUpload)가 먼저 일어나므로
커밋이 실패하면 객체는 완성됐는데 DB는 UPLOADING이다. 이때 재시도가 완료된 멀티파트의 파트 목록을 조회하지 못해 세션이 만료될 때까지
503(Retry-After)만 받았다. 합성 값.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.models import Asset, Upload
from tests.integration.test_ingest import drain, manifest, mp4_bytes, put_parts

pytestmark = pytest.mark.integration


def test_complete_retry_after_a_failed_commit_finishes_from_the_stored_object(ictx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    h = ictx.as_user(102)
    data = mp4_bytes(128, seed=5301)
    created = ictx.client.post("/v1/uploads", json=manifest(data), headers=h).json()
    parts = put_parts(created, data)
    body = {"parts": parts}
    path = f"/v1/uploads/{created['upload_id']}/complete"

    real_commit, armed = Session.commit, {"on": True}

    def commit(self: Session) -> None:
        if armed["on"]:
            armed["on"] = False
            raise OperationalError("COMMIT", {}, Exception("합성 커밋 실패"))
        real_commit(self)

    monkeypatch.setattr(Session, "commit", commit)
    failed = ictx.client.post(path, json=body, headers=h)
    assert failed.status_code == 500, failed.text  # 클라이언트는 실패를 안다 (전: 응답 뒤라 202였다)
    with ictx.c.sessionmaker() as db:  # 저장소에서는 끝났지만 DB는 되돌아갔다
        assert db.get(Upload, created["upload_id"]).status == "UPLOADING"
        assert db.get(Asset, created["asset_id"]).ingest_status == "UPLOADING"
    bucket, key = _place(ictx, created)
    assert ictx.c.storage.head_object(bucket, key) is not None  # 저장소에서는 멀티파트가 끝났다

    retry = ictx.client.post(path, json=body, headers=h)

    assert retry.status_code == 202, retry.text  # 전: 완료된 멀티파트를 조회하지 못해 503 STORAGE_UNAVAILABLE이 반복됐다
    assert retry.json()["ingest_status"] == "RECEIVED"
    drain(ictx)
    with ictx.c.sessionmaker() as db:
        assert db.get(Asset, created["asset_id"]).ingest_status == "REGISTERED"
        assert db.get(Upload, created["upload_id"]).status == "RECEIVED"
    again = ictx.client.post(path, json=body, headers=h)
    assert again.status_code == 202  # 세 번째 재시도도 안전하다


def _place(ctx, created: dict) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    with ctx.c.sessionmaker() as db:
        up = db.get(Upload, created["upload_id"])
        return up.bucket, up.object_key
