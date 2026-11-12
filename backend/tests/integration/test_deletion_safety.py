"""삭제 안전 (지시서 12번 독립 검토): 저장소 객체는 DB가 받아들인 뒤에만 지우고, 지우지 못한 객체는 삭제 증명에 넣지 않는다 (08번 요구사항 4.1·5.1).

예전에는 객체를 먼저 지우고 그 뒤에 증명 행을 넣었다. 증명 행이 DB 제약(예: 사유의 NUL)에 걸려 되돌려지면 객체는 이미 사라진 채
자산이 살아 있었고, 저장소 오류를 삼킨 채 '지웠다'는 증명을 발급해 객체가 남았다 (ADR-0055).
"""

from __future__ import annotations

import hashlib
import threading
import time

import httpx
import pytest
from sqlalchemy import select, text

from app.db.models import Asset, DeletionCertificate, Device, OrgQuota, Upload
from app.seed import DEV_A_CAMERA, DEV_B_CAMERA, ORG_A, ORG_B, SES_A, SES_B
from app.services import retention
from app.workers import main as worker
from tests.integration.test_ingest import manifest, mp4_bytes
from tests.integration.test_lifecycle import asset, upload_csv

pytestmark = pytest.mark.integration


def _quota(ctx, org: str = ORG_A) -> tuple[int, int, int]:
    with ctx.c.sessionmaker() as db:
        q = db.get(OrgQuota, org)
        return q.used_bytes, q.quarantine_bytes, q.reserved_bytes


def _certs(ctx) -> list[DeletionCertificate]:
    with ctx.c.sessionmaker() as db:
        return list(db.scalars(select(DeletionCertificate)))


def _place(ctx, aid: str) -> tuple[str, str]:
    a = asset(ctx, aid)
    return ctx.c.settings.buckets[a.storage_zone], a.storage_key


def _break_deletes(ctx, monkeypatch, *, only_key: str | None = None):
    """저장소 객체 삭제를 실패시킨다(only_key만, 없으면 전부). 원래 함수를 돌려준다."""
    real = ctx.c.storage.delete_object

    def flaky(bucket: str, key: str) -> None:
        if only_key is None or key == only_key:
            raise TimeoutError("synthetic storage outage")
        real(bucket, key)

    monkeypatch.setattr(ctx.c.storage, "delete_object", flaky)
    return real


def test_db_rejection_leaves_the_object_alone(ictx) -> None:
    """증명 행을 쓰지 못해 삭제가 거부되면 객체는 그대로 있다 (사유의 NUL은 PostgreSQL 텍스트에 저장할 수 없다)."""
    aid = upload_csv(ictx, b"t,v\n1,1\n")
    bucket, key = _place(ictx, aid)
    before = _quota(ictx)

    r = ictx.client.post(f"/v1/assets/{aid}/deletion", json={"reason": "ab\u0000cd"},
                         headers=ictx.as_user(101, mfa=True))

    assert r.status_code == 400, r.text
    assert ictx.c.storage.head_object(bucket, key) is not None, "DB가 거부한 삭제가 저장소 객체를 지웠다"
    a = asset(ictx, aid)
    assert a.deleted_at is None and a.storage_key == key
    assert _certs(ictx) == [] and _quota(ictx) == before


def test_object_that_cannot_be_deleted_is_not_certified(ictx, monkeypatch) -> None:
    """저장소가 지우지 못하면 삭제를 확정하지 않는다: 503, 자산·쿼터·증명 그대로. 다시 요청하면 끝난다."""
    aid = upload_csv(ictx, b"t,v\n2,2\n")
    bucket, key = _place(ictx, aid)
    before = _quota(ictx)
    real = _break_deletes(ictx, monkeypatch)

    r = ictx.client.post(f"/v1/assets/{aid}/deletion", json={"reason": "저장소 장애 중 삭제"},
                         headers=ictx.as_user(101, mfa=True))

    assert r.status_code == 503 and r.json()["code"] == "STORAGE_UNAVAILABLE", r.text
    assert r.headers.get("Retry-After")
    a = asset(ictx, aid)
    assert a.deleted_at is None and a.storage_key == key, "못 지운 객체의 자산이 삭제로 기록됐다"
    assert _certs(ictx) == [] and _quota(ictx) == before
    assert ictx.c.storage.head_object(bucket, key) is not None

    monkeypatch.setattr(ictx.c.storage, "delete_object", real)
    again = ictx.client.post(f"/v1/assets/{aid}/deletion", json={"reason": "저장소 복구 뒤 다시"},
                             headers=ictx.as_user(101, mfa=True))
    assert again.status_code == 200, again.text
    assert [o["asset_id"] for o in again.json()["objects"]] == [aid]
    assert ictx.c.storage.head_object(bucket, key) is None
    assert asset(ictx, aid).deleted_at is not None and len(_certs(ictx)) == 1


def test_partial_storage_failure_is_rolled_back_and_the_retry_finishes(ictx, monkeypatch) -> None:
    """회차 삭제 중 둘째 객체가 실패해도 첫째 자산은 '삭제됨'으로 남지 않는다. 다시 요청하면 이미 없는 객체까지 포함해 끝난다."""
    first = upload_csv(ictx, b"t,v\n3,3\n")
    second = upload_csv(ictx, b"t,v\n4,4\n")
    (b1, k1), (b2, k2) = _place(ictx, first), _place(ictx, second)
    real = _break_deletes(ictx, monkeypatch, only_key=k2)

    r = ictx.client.post(f"/v1/sessions/{SES_A}/deletion", json={"reason": "회차 정리"},
                         headers=ictx.as_user(101, mfa=True))

    assert r.status_code == 503, r.text
    assert asset(ictx, first).deleted_at is None and asset(ictx, second).deleted_at is None
    assert _certs(ictx) == []
    assert ictx.c.storage.head_object(b2, k2) is not None

    monkeypatch.setattr(ictx.c.storage, "delete_object", real)
    again = ictx.client.post(f"/v1/sessions/{SES_A}/deletion", json={"reason": "회차 정리 다시"},
                             headers=ictx.as_user(101, mfa=True))
    assert again.status_code == 200, again.text
    assert {o["asset_id"] for o in again.json()["objects"]} == {first, second}
    assert ictx.c.storage.head_object(b1, k1) is None and ictx.c.storage.head_object(b2, k2) is None
    assert asset(ictx, first).deleted_at is not None and asset(ictx, second).deleted_at is not None


def _org_b_sensor_upload(ctx, data: bytes) -> str:
    """기관 B에도 보존 만료 대상 자산을 만든다 (시드의 B 카메라를 C3 센서로 바꿔 CSV를 올린다)."""
    with ctx.c.sessionmaker.begin() as db:
        d = db.get(Device, DEV_B_CAMERA)
        d.device_type, d.collection_mode, d.source_timezone = "SENSOR", "C3_APPROVED_FULL", "Asia/Seoul"
    h = ctx.as_user(104)
    m = {"schema_version": "1.0.0", "source_system_id": DEV_B_CAMERA, "data_type": "SENSOR", "variant": "ORIGINAL",
         "session_id": SES_B, "timestamp_quality": {"source": "UNKNOWN", "verified": False},
         "file": {"original_filename": "b.csv", "byte_size": len(data), "mime_type": "text/csv",
                  "checksum_sha256": hashlib.sha256(data).hexdigest()}, "client": {"kind": "API", "version": "t"}}
    created = ctx.client.post("/v1/uploads", json=m, headers=h)
    assert created.status_code == 201, created.text
    created = created.json()
    r = httpx.put(created["parts"][0]["url"], content=data)
    done = ctx.client.post(f"/v1/uploads/{created['upload_id']}/complete",
                           json={"parts": [{"part_number": 1, "etag": r.headers["etag"]}]}, headers=h)
    assert done.status_code == 202, done.text
    while worker.run_once(ctx.c, "t"):
        pass
    return created["asset_id"]


def test_retention_failure_in_one_org_does_not_undo_the_others(ictx, monkeypatch) -> None:
    """기관마다 따로 확정한다: 뒤 기관의 저장소 삭제가 실패해도 앞 기관의 삭제(객체는 이미 지워졌다)는 DB에 남고,
    실패한 기관의 자산은 그대로 살아 있어 다음 실행이 이어서 지운다."""
    a_asset = upload_csv(ictx, b"t,v\n5,5\n")
    b_asset = _org_b_sensor_upload(ictx, b"t,v\n6,6\n")
    assert asset(ictx, a_asset).organization_id == ORG_A and asset(ictx, b_asset).organization_id == ORG_B
    with ictx.c.engine.begin() as conn:
        conn.execute(text("UPDATE assets SET retention_until = now() - interval '1 day' "
                          "WHERE asset_id IN (:a, :b)"), {"a": a_asset, "b": b_asset})
    (ba, ka), (bb, kb) = _place(ictx, a_asset), _place(ictx, b_asset)
    real = _break_deletes(ictx, monkeypatch, only_key=kb)

    with ictx.c.sessionmaker.begin() as db:
        certs = retention.execute_expired(ictx.c, db, ictx.c.clock.now())

    assert [c.organization_id for c in certs] == [ORG_A]
    assert asset(ictx, a_asset).deleted_at is not None, "앞 기관의 삭제가 뒤 기관의 실패로 되돌려졌다"
    assert ictx.c.storage.head_object(ba, ka) is None
    assert asset(ictx, b_asset).deleted_at is None and asset(ictx, b_asset).storage_key == kb
    assert ictx.c.storage.head_object(bb, kb) is not None
    assert [x.organization_id for x in _certs(ictx)] == [ORG_A]
    assert any(n.kind == "retention.delete_failed" for n in ictx.c.notifier.sent)

    monkeypatch.setattr(ictx.c.storage, "delete_object", real)  # 저장소가 돌아오면 다음 실행이 끝낸다
    with ictx.c.sessionmaker.begin() as db:
        later = retention.execute_expired(ictx.c, db, ictx.c.clock.now())
    assert [c.organization_id for c in later] == [ORG_B]
    assert ictx.c.storage.head_object(bb, kb) is None


def test_legal_hold_is_not_overtaken_by_a_racing_deletion(ictx, monkeypatch) -> None:
    """법적 보류가 자산 행을 잠근다: 보류 처리 중에 시작한 삭제는 보류가 확정된 뒤 거부된다.
    (전에는 보류가 행을 잠그지 않아 삭제가 먼저 끝나고, 이미 삭제·증명된 자산에 보류가 '성공'했다.)"""
    aid = upload_csv(ictx, b"t,v\n7,7\n")
    bucket, key = _place(ictx, aid)
    real_record = ictx.c.audit.record
    started, outcome = threading.Event(), {}

    def delete_in_background() -> None:
        outcome["delete"] = ictx.client.post(f"/v1/assets/{aid}/deletion", json={"reason": "경합 삭제 시도"},
                                             headers=ictx.as_user(101, mfa=True))

    def record(action, **kw):
        if action == "LEGAL_HOLD_SET" and not started.is_set():
            started.set()
            t = threading.Thread(target=delete_in_background)
            t.start()
            time.sleep(1.5)  # 보류 처리가 끝나기 전에 삭제 요청이 도착해 있게 한다
            outcome["thread"] = t
        return real_record(action, **kw)

    monkeypatch.setattr(ictx.c.audit, "record", record)
    hold = ictx.client.post(f"/v1/assets/{aid}/legal-hold", json={"hold": True, "reason": "소송 대응 보류"},
                            headers=ictx.as_user(106, mfa=True))
    outcome["thread"].join(30)
    monkeypatch.setattr(ictx.c.audit, "record", real_record)

    assert hold.status_code == 200 and hold.json()["legal_hold"] is True, hold.text
    deletion = outcome["delete"]
    assert deletion.status_code == 409, deletion.text
    a = asset(ictx, aid)
    assert a.legal_hold is True and a.deleted_at is None, "보류가 걸린 자산이 삭제됐다"
    assert ictx.c.storage.head_object(bucket, key) is not None and _certs(ictx) == []


def test_certificate_lists_no_phantom_object_for_a_reverted_c0_asset(ictx) -> None:
    """바이너리 없는 C0 자산에 업로드 세션을 열었다가 취소하면 객체는 만들어진 적이 없다. 이후 삭제 증명에 그 객체를 넣지 않는다."""
    h = ictx.as_user(102)
    data = mp4_bytes(128, seed=991)
    payload = manifest(data, variant="ORIGINAL")
    with ictx.c.sessionmaker.begin() as db:
        db.get(Device, DEV_A_CAMERA).collection_mode = "C0_METADATA_ONLY"
    registered = ictx.client.post("/v1/uploads", json=payload, headers=h)
    assert registered.status_code == 201 and registered.json()["status"] == "METADATA_ONLY_REGISTERED", registered.text
    with ictx.c.sessionmaker.begin() as db:
        db.get(Device, DEV_A_CAMERA).collection_mode = "C3_APPROVED_FULL"
    opened = ictx.client.post("/v1/uploads", json=payload, headers=h).json()
    aid = opened["asset_id"]
    assert aid == registered.json()["asset_id"]
    assert ictx.client.delete(f"/v1/uploads/{opened['upload_id']}", headers=h).status_code == 204

    with ictx.c.sessionmaker() as db:
        a = db.get(Asset, aid)
        assert (a.ingest_status, a.has_binary) == ("READY_PRIVATE", False)
        assert (a.storage_zone, a.storage_key) == (None, None), "객체가 없는 자산에 저장 위치가 남았다"
        assert db.scalar(select(Upload.status).where(Upload.upload_id == opened["upload_id"])) == "ABORTED"

    deleted = ictx.client.post(f"/v1/assets/{aid}/deletion", json={"reason": "C0 기록 삭제"},
                               headers=ictx.as_user(101, mfa=True))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["objects"] == [], "만들어진 적 없는 객체가 삭제 증명에 들어갔다"


def test_stale_storage_key_on_a_binaryless_asset_is_not_certified(ictx) -> None:
    """이미 저장된 행(수정 전에 취소된 C0 자산)에 저장 위치가 남아 있어도 바이너리가 없으면 증명에 넣지 않는다."""
    h = ictx.as_user(102)
    data = mp4_bytes(128, seed=992)
    payload = manifest(data, variant="ORIGINAL")
    with ictx.c.sessionmaker.begin() as db:
        db.get(Device, DEV_A_CAMERA).collection_mode = "C0_METADATA_ONLY"
    aid = ictx.client.post("/v1/uploads", json=payload, headers=h).json()["asset_id"]
    with ictx.c.engine.begin() as conn:  # 예전 취소가 남긴 상태를 직접 만든다
        conn.execute(text("UPDATE assets SET storage_zone = 'RAW', storage_key = 'stale/key' WHERE asset_id = :a"),
                     {"a": aid})

    deleted = ictx.client.post(f"/v1/assets/{aid}/deletion", json={"reason": "예전 취소 흔적"},
                               headers=ictx.as_user(101, mfa=True))

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["objects"] == []
